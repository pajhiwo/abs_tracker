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

# Recorded against a job dropped at its turn because the owner had gone (FR-013). The
# returning session sees this and can restart without re-uploading (FR-014).
_ABANDONED_MESSAGE = (
    "We paused your analysis while you were away. Restart it — no re-upload needed."
)


def _default_max_concurrent() -> int:
    env = os.getenv("ANALYSIS_MAX_CONCURRENT")
    if env:
        return max(1, int(env))
    return min(os.cpu_count() or 2, 4)  # T004 sizing


# Size-class thresholds on the complexity metric (readings × meals), used to
# condition the wait estimate so a median over mixed job sizes does not describe
# neither (data-model AnalysisQueue "Mixed job sizes"). Boundaries derived from the
# T004 fixtures: 1mo ≈ 72k pairs (small), 3mo ≈ 686k (medium), 12mo ≈ 10.6M (large).
_SMALL_MAX_PAIRS = 500_000
_MEDIUM_MAX_PAIRS = 5_000_000


def size_class(session) -> str | None:
    """Coarse size bucket from the parsed frames, or None if not analysable yet."""
    bac_df = getattr(session, "bac_df", None)
    meals_df = getattr(session, "meals_df", None)
    if bac_df is None or meals_df is None:
        return None
    pairs = len(bac_df) * len(meals_df)
    if pairs < _SMALL_MAX_PAIRS:
        return "small"
    if pairs < _MEDIUM_MAX_PAIRS:
        return "medium"
    return "large"


class AnalysisExecutor:
    def __init__(
        self,
        queue: JobQueue,
        store: SessionStore,
        *,
        max_concurrent: int | None = None,
        max_waiting: int | None = None,
        max_estimated_wait: float | None = None,
        abandon_grace_seconds: float | None = None,
        lease_seconds: float = 300.0,
        poll_after_seconds: int = 2,
        cold_start_prior_seconds: float = 5.0,
    ) -> None:
        self._queue = queue
        self._store = store
        self._max = max_concurrent or _default_max_concurrent()
        # FR-010 caps. Defaults are the spec's working values (50 waiting, 5 min); both
        # exist so whichever trips first refuses the arrival (data-model "At-capacity").
        self._max_waiting = (
            max_waiting
            if max_waiting is not None
            else int(os.getenv("ANALYSIS_MAX_WAITING", "50"))
        )
        self._max_estimated_wait = (
            max_estimated_wait
            if max_estimated_wait is not None
            else float(os.getenv("ANALYSIS_MAX_WAIT_SECONDS", "300"))
        )
        # R3 grace window, in minutes not seconds: mobile browsers throttle background
        # timers past 60s, so a short window would falsely abandon a waiting person.
        self._abandon_grace = (
            abandon_grace_seconds
            if abandon_grace_seconds is not None
            else float(os.getenv("ANALYSIS_ABANDON_GRACE", "300"))
        )
        self._lease = lease_seconds
        self.poll_after_seconds = poll_after_seconds
        # Cold-start prior (T004): a small job is ~2–4s post-vectorisation, so 5s is a
        # conservative seed until real durations accumulate (data-model "Cold start").
        self._prior = cold_start_prior_seconds
        self._pool = ThreadPoolExecutor(
            max_workers=self._max, thread_name_prefix="analysis"
        )
        self._lock = threading.Lock()
        self._running = 0
        self._recent: deque[float] = deque(maxlen=20)
        # Durations bucketed by size class, so the estimate is conditioned on the kind
        # of job rather than a median across a 3-day/year-scale mixture (T034).
        self._recent_by_class: dict[str, deque[float]] = {}

    @property
    def max_concurrent(self) -> int:
        return self._max

    @property
    def max_waiting(self) -> int:
        return self._max_waiting

    # -- admission --------------------------------------------------------
    def can_accept(self, size_class: str | None = None) -> bool:
        """Whether a *new* arrival may join the queue (FR-010).

        Refused when the waiting count has reached `max_waiting`, or when the projected
        wait for the newcomer (who would land last) exceeds `max_estimated_wait` —
        whichever trips first. Duplicate/superseding submissions are decided by the
        caller and are not subject to this.
        """
        waiting = self._queue.count_waiting()
        if waiting >= self._max_waiting:
            return False
        projected = self.estimate_wait_seconds(waiting + 1, size_class)
        return projected <= self._max_estimated_wait

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
        """Claim and start as many jobs as there is spare capacity for.

        A claimed job whose owner has not been seen within the grace window is dropped
        as `abandoned` rather than run, and its slot is offered to the next person
        (FR-013). The slot is not consumed by an abandoned job, so the loop continues.
        """
        with self._lock:
            while self._running < self._max:
                job = self._queue.claim(self._lease)
                if job is None:
                    break
                if self._is_abandoned(job):
                    self._queue.set_state(
                        job.job_id, JobState.ABANDONED, error=_ABANDONED_MESSAGE
                    )
                    continue  # capacity goes to the next person, not to nobody
                self._running += 1
                self._pool.submit(
                    self._run_wrapper, job.job_id, job.session_id, job.params_signature
                )

    def _is_abandoned(self, job: AnalysisJob) -> bool:
        """True when the owner has not polled within the grace window (R3, FR-013)."""
        return (time.time() - job.last_seen_at) > self._abandon_grace

    # -- estimation -------------------------------------------------------
    def estimate_wait_seconds(self, position: int, size_class: str | None = None) -> int:
        """Wait estimate: `position / max_concurrent × median(recent durations)`.

        Conditioned on the job's size class when durations for that class exist, so a
        mixed workload does not blow SC-012's ±50% band (data-model "Mixed job sizes").
        Falls back to the overall median, then a cold-start prior. Recomputed on every
        poll, never counted down client-side, so a bad estimate corrects itself.
        """
        durations = self._recent_by_class.get(size_class) if size_class else None
        if durations:
            base = statistics.median(durations)
        elif self._recent:
            base = statistics.median(self._recent)
        else:
            base = self._prior
        return int(round((position / self._max) * base))

    def _record_duration(self, size_class: str | None, elapsed: float) -> None:
        self._recent.append(elapsed)
        if size_class is not None:
            self._recent_by_class.setdefault(size_class, deque(maxlen=20)).append(elapsed)

    # -- execution --------------------------------------------------------
    def _run_wrapper(self, job_id: str, session_id: str, sig: str) -> None:
        start = time.perf_counter()
        cls: str | None = None
        try:
            cls = self._run(job_id, session_id, sig)
        finally:
            elapsed = time.perf_counter() - start
            with self._lock:
                self._running -= 1
                self._record_duration(cls, elapsed)
            # A slot freed — see if anything is waiting.
            self.pump()

    def _run(self, job_id: str, session_id: str, sig: str) -> str | None:
        session = self._store.get(session_id)
        if session is None or session.bac_df is None:
            self._queue.set_state(job_id, JobState.EXPIRED)
            return None
        cls = size_class(session)
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
                return cls
            self._queue.set_state(job_id, JobState.PARTIAL)

            # Stage two: optional ML, layered on top. A failure here is recorded in
            # the payload's `ml` field, not raised (Principle II). The report document
            # is model-free, so it does not change here.
            ml_block = compute_ml_block(session)
            payload = {**payload, "ml": ml_block}
            session.results.set_payload(sig, payload)
            if not self._persist(session_id, session):
                self._queue.set_state(job_id, JobState.EXPIRED)
                return cls
            self._queue.set_state(job_id, JobState.COMPLETE)
        except Exception:  # noqa: BLE001 — never leak internals to the client
            self._queue.set_state(
                job_id,
                JobState.FAILED,
                error="Analysis failed. Please try again.",
            )
        return cls

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
