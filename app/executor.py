"""Analysis executor — the claim/execute supervisor behind the queue (T018).

T004 measured that, once `map_lookback` is vectorised (T019), the only meaningful CPU
left is sklearn training (~2s), which releases the GIL. So this is a **thread pool**,
not a process pool: it gives the needed parallelism without pickling DataFrames across
a process boundary and keeps the in-memory session store trivially shared.

Dispatch is pump-based. Enqueuing a job (or a job finishing) calls `pump()`, which
claims as many queued jobs as there is spare capacity for and runs each on the pool.
Each job runs in two stages (Principle II; data-model AnalysisJob "Staging"):

  stage one → deterministic summary + lift scores, cached, job marked ``partial``
  stage two → optional ML block, cached payload updated, job marked ``complete``

so a report is readable after stage one and the optional model never blocks it.
"""

from __future__ import annotations

import os
import statistics
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from app.compute import (
    build_report_document,
    build_result_payload,
    compute_ml_block,
    run_analysis,
)
from app.jobs import AnalysisJob, JobQueue, JobState
from app.sessions import SessionStore, SessionStoreAtCapacity


def _default_max_concurrent() -> int:
    env = os.getenv("ANALYSIS_MAX_CONCURRENT")
    if env:
        return max(1, int(env))
    return min(os.cpu_count() or 2, 4)  # T004 sizing


class AnalysisExecutor:
    def __init__(
        self,
        queue: JobQueue,
        store: SessionStore,
        *,
        max_concurrent: int | None = None,
        lease_seconds: float = 300.0,
        poll_after_seconds: int = 2,
        cold_start_prior_seconds: float = 5.0,
    ) -> None:
        self._queue = queue
        self._store = store
        self._max = max_concurrent or _default_max_concurrent()
        self._lease = lease_seconds
        self.poll_after_seconds = poll_after_seconds
        self._prior = cold_start_prior_seconds
        self._pool = ThreadPoolExecutor(
            max_workers=self._max, thread_name_prefix="analysis"
        )
        self._lock = threading.Lock()
        self._running = 0
        self._recent: deque[float] = deque(maxlen=20)

    @property
    def max_concurrent(self) -> int:
        return self._max

    # -- submission -------------------------------------------------------
    def submit(self, session_id: str, params_signature: str) -> AnalysisJob:
        """Enqueue (with duplicate suppression) and kick the pump.

        Called from request handlers via `run_in_threadpool`, because the SQLite
        backing's queue calls block the event loop (research R5a).
        """
        job = self._queue.enqueue(session_id, params_signature)
        self.pump()
        return job

    def pump(self) -> None:
        """Claim and start as many jobs as there is spare capacity for."""
        with self._lock:
            while self._running < self._max:
                job = self._queue.claim(self._lease)
                if job is None:
                    break
                self._running += 1
                self._pool.submit(
                    self._run_wrapper, job.job_id, job.session_id, job.params_signature
                )

    # -- estimation -------------------------------------------------------
    def estimate_wait_seconds(self, position: int) -> int:
        """Crude wait estimate (data-model AnalysisQueue). Refined in US3.

        `position / max_concurrent × median(recent durations)`, with a cold-start
        prior when no durations have been recorded yet.
        """
        base = statistics.median(self._recent) if self._recent else self._prior
        return int(round((position / self._max) * base))

    # -- execution --------------------------------------------------------
    def _run_wrapper(self, job_id: str, session_id: str, sig: str) -> None:
        start = time.perf_counter()
        try:
            self._run(job_id, session_id, sig)
        finally:
            elapsed = time.perf_counter() - start
            with self._lock:
                self._running -= 1
                self._recent.append(elapsed)
            # A slot freed — see if anything is waiting.
            self.pump()

    def _run(self, job_id: str, session_id: str, sig: str) -> None:
        session = self._store.get(session_id)
        if session is None or session.bac_df is None:
            self._queue.set_state(job_id, JobState.EXPIRED)
            return
        try:
            # Stage one: deterministic core. No model. Cache both the payload and the
            # report document so /results and /report are servable now (US2, FR-016/017).
            run_analysis(
                session,
                session.hours,
                session.min_obs,
                session.split_compounds,
                session.exclude_proteins,
            )
            payload = build_result_payload(session, include_ml=False)
            report = build_report_document(session, payload)
            session.results.set_payload(sig, payload)
            session.results.set_report(sig, report)
            if not self._persist(session_id, session):
                self._queue.set_state(job_id, JobState.EXPIRED)
                return
            self._queue.set_state(job_id, JobState.PARTIAL)

            # Stage two: optional ML, layered on top. A failure here is recorded in
            # the payload's `ml` field, not raised (Principle II). The report document
            # is model-free, so it does not change here.
            ml_block = compute_ml_block(session)
            payload = {**payload, "ml": ml_block}
            session.results.set_payload(sig, payload)
            if not self._persist(session_id, session):
                self._queue.set_state(job_id, JobState.EXPIRED)
                return
            self._queue.set_state(job_id, JobState.COMPLETE)
        except Exception:  # noqa: BLE001 — never leak internals to the client
            self._queue.set_state(
                job_id,
                JobState.FAILED,
                error="Analysis failed. Please try again.",
            )

    def _persist(self, session_id: str, session) -> bool:
        """Write the session back. Returns False if the session is gone."""
        try:
            self._store.set(session_id, session)
            return True
        except SessionStoreAtCapacity:
            # The session expired and was reclaimed mid-flight; drop the result.
            return False

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
