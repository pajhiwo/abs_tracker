---
description: "Task list for Concurrent Analysis Without Waiting"
---

# Tasks: Concurrent Analysis Without Waiting

**Input**: Design documents from `/specs/001-concurrent-analysis/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/http-api.md, quickstart.md

**Tests**: Included. Principle V requires evidence, and R6/R8a specifically mandate equivalence
tests before a transform or a rewrite is trusted. Test tasks are therefore first-class here, not
optional.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on incomplete tasks)
- **[Story]**: US1–US5, mapping to the spec's user stories. Setup, Foundational and Polish carry no story label.

## Two things that govern the whole list

1. **R8a was a hard gate — now RESOLVED by T004.** The measurement showed the nested-`iterrows`
   `map_lookback` dominates (436s at 12 months) while LASSO training stays ~2s. So the executor
   is a **thread pool** and **vectorising `map_lookback` (T019) is blocking**, not optional
   (research R2 resolved, R8a "Result (T004)").
2. **The "shrink storage" tasks resolved to mostly no-ops.** T004 measured retained state at
   <1 MB/session (29 MiB peak per analysis), so memory does not force any shrinking. R4a's
   `DerivedBundle` (T031) resolves to "retain the frames, do nothing." R6's compound-split
   extraction and `raw_bytes` drop (T045–T047) are kept **only for their privacy value** (fewer
   copies of the upload), not for memory, and remain optional.

---

## Phase 1: Setup (measurement tooling and fixtures)

**Purpose**: build the evidence-gathering tools the rest of the plan depends on. Nothing about
the executor or the caps can be decided without these.

- [X] T001 [P] Create synthetic year-scale workbook generator in `tests/fixtures/generate_year_log.py`, reproducing the real format's structural quirks (aggregate rows, blank padding, compound dishes, partially filled nutrient columns), parameterised by months, generated on demand and never committed (research R8; must contain no real patient data per Principle IV).
- [X] T002 [P] Create per-stage measurement harness in `tests/fixtures/measure_scale.py` that records wall time for parse, `map_lookback`, `compute_lift_scores`, `extract_features`, `train_personal_model`, `generate_pdf`, plus serialised per-field `SessionData` size and peak resident memory for one analysis (research R8 "What to record").
- [X] T003 [P] Add a smoke test in `tests/test_fixtures.py` asserting `generate_year_log.py` output parses through `core/parser.py` and contains the intended quirks, so the fixture itself is trustworthy before any measurement rides on it.

---

## Phase 2: Foundational (blocking prerequisites)

**⚠️ No user story work begins until this phase is complete.** These are the shared seams every
story sits on, plus the measurement gate.

### The measurement gate

- [X] T004 Run `tests/fixtures/measure_scale.py` across the example workbook, 3, 12 and 24 months; record the numbers in `specs/001-concurrent-analysis/research.md` under R8/R8a; and from them decide (a) thread pool vs process pool, (b) whether `map_lookback` is vectorised, (c) provisional values for `max_concurrent`, grace window, `max_waiting`, `max_estimated_wait`, `MAX_SESSIONS`, upload byte cap, and the complexity metric. **This unblocks every (CONTINGENT/R8a) task.**
  - **RESOLVED (measured):** `map_lookback` dominates (436s at 12mo) → **(a) thread pool**, **(b) vectorise `map_lookback` — now blocking (T019)**. Memory tiny (29 MiB peak, <1 MB/session) → **`DerivedBundle` (T031) resolves to "retain frames, do nothing"**; R6 shrink (T045–T047) kept only for privacy. Caps: `max_concurrent=min(cpu,4)`, `MAX_SESSIONS=500`, upload cap 10 MB, complexity = `len(bac)×len(meals)`. Full table in research.md R8a "Result (T004)".

### Session store (all stories depend on it — R10, R5)

- [X] T005 Write the store protocol conformance suite in `tests/test_sessions.py` against the `SessionStore` protocol, asserting `get()` returns a copy and that a live session is never evicted to admit a new one (research R5 seam 1, R10; data-model "Store semantics"). Write it to FAIL against current `InMemoryStore` first.
- [X] T006 Make `InMemoryStore.get()` return a copy and remove the oldest-session eviction from `InMemoryStore.set()` in `app/sessions.py`; replace it with a refuse-new-session-at-capacity signal the handler can surface as the R10 `503` (research R10; data-model "Capacity").
- [X] T007 Raise/re-home `MAX_SESSIONS` as a memory-derived backstop (value from T004) in `app/sessions.py`, and document that expiry — not capacity — is the normal reclamation path (research R10).

### Deterministic core, decoupled and staged (R1 — enables US1 staging and US2 caching)

- [X] T008 Add `tests/test_compute.py` asserting `summary` is computed without invoking the ML block, and that an ML failure leaves `summary`, lift scores and the rest of the payload intact (Principle II; research R1). Write to FAIL first.
- [X] T009 Create `app/compute.py` by moving `_run_analysis` and `_build_results_json` out of `app/main.py`, split into stage one (summary + lift scores, no model) and stage two (optional ML), returning the existing `ResultPayload` shape unchanged (research R1; data-model ResultPayload; plan Structure Decision).
- [X] T010 Reconcile the `exclude_proteins` default so `AnalysisParams` and the `/upload` path agree, compute `params_signature` from resolved values over all five fields, and add a regression test in `tests/test_compute.py` pinning the reconciled default (data-model AnalysisParams + "pre-existing bug"; contracts "Parameter defaults").

### Job queue (US1 and US3 both need it — R5, R5a)

- [X] T011 Write `tests/test_jobs.py` covering ordering, atomic-and-leased claim, position, cancellation, lease expiry/reclaim, and foreign-`session_id` isolation — parameterised to run against BOTH backings (data-model AnalysisJob/AnalysisQueue; research R5a). Write to FAIL first.
- [X] T012 Define the `JobQueue` protocol and the `AnalysisJob`/`JobState` model in `app/jobs.py`, referencing jobs by id only, with the state machine from data-model (`queued→running→partial→complete`, plus `failed/cancelled/abandoned/expired`).
- [X] T013 [P] Implement the in-memory `JobQueue` backing in `app/jobs.py` for tests.
- [X] T014 Implement the SQLite `JobQueue` backing in `app/jobs.py` with the R5a conditions: WAL + `synchronous=NORMAL` + `foreign_keys=ON` + non-zero `busy_timeout`; single-statement `UPDATE … RETURNING` atomic leased claim; `BEGIN IMMEDIATE` for writes; short transactions; bounded `SQLITE_BUSY` retry surfaced as `503`; lease-reclaim as the crash path (research R5a).
- [X] T015 In `app/jobs.py` and the queue call sites in `app/main.py`, ensure every queue call from an `async` handler is offloaded (`run_in_threadpool` or a sync API used only from worker threads), since `sqlite3` blocks the event loop — the exact defect the feature removes, applied to the queue (research R5a "Request-path discipline"). *Queue half done: the backing exposes only a synchronous, thread-safe API with no event-loop calls, so it is safe to wrap. The `run_in_threadpool` wrapping lands with the call sites in US1 (T020/T023).*
- [X] T016 Implement the startup sweep in `app/jobs.py` (or app lifespan in `app/main.py`): reclaim or fail leased jobs and clear `queued` rows whose sessions no longer exist, so a redeploy never serves stale positions (research R5a "Ephemeral filesystem").

**Checkpoint**: measurement is in hand, the store is correct and copy-returning, the deterministic
core is standalone and staged, and the queue exists with two exercised backings. User stories can begin.

---

## Phase 3: User Story 1 — Analyse my log while others are using the site (P1) 🎯 MVP

**Goal**: concurrent analyses that never make another person's page hang; each person gets their own correct results.

**Independent Test**: start several analyses within a few seconds; each returns its own correct
result and every page stays responsive throughout (spec US1 Independent Test; SC-002).

- [X] T017 [US1] Write `tests/test_concurrency.py`: with analyses running, non-analysis pages respond under 2s and no page fails; each concurrent analysis returns results derived only from its own log (SC-002, SC-007; spec US1 scenarios 1–4). Write to FAIL first.
- [X] T018 [US1] **(RESOLVED/R8a → thread pool)** Create `app/executor.py` — the claim/execute supervisor — as a `ThreadPoolExecutor` sized `min(cpu_count, 4)` with an env override (research R2 resolved; plan Complexity Tracking).
- [X] T019 [US1] **(RESOLVED/R8a → vectorise, BLOCKING)** Rewrite `map_lookback` in `core/analysis.py` as a vectorised interval join (exact-time window + approximate date-level fallback), and add an equivalence test in `tests/test_analysis.py` against `example/example_log.xlsx` before trusting it (research R8a; Principle V). This is the measured bottleneck (436s → target sub-second at 12mo). **Done: `searchsorted`-based window slice; equivalence pinned on example workbook (4 window sizes), synthetic fixture and untimed fallback; 12-month lookback now <1s (perf test asserts <15s).**
- [X] T020 [US1] Rework `POST /upload` in `app/main.py` to validate, parse the upload awaited off the event loop, hash the bytes, discard the bytes when the handler returns, then enqueue an analysis job referencing the session's parsed frames (research R11; contracts `POST /upload`; data-model AnalysisJob "does NOT carry bytes").
- [X] T021 [US1] Wire `app/executor.py` to run `app/compute.py` staged: cache stage one (summary + lift scores) and mark the job `partial`, then run stage two (ML) and mark `complete`, updating the cached payload (data-model AnalysisJob "Staging"; contracts "Staged results").
- [X] T022 [US1] Implement `GET /jobs/{job_id}` in `app/main.py` returning the status shape (including `partial`), enforcing owner-only access with `404` for a foreign/unknown id (contracts "Job status"; data-model AnalysisJob validation).
- [X] T023 [US1] Convert `GET /results` / `POST /results` in `app/main.py` to serve `200` from cache or `202` with a job, never computing inline (contracts endpoint table).
- [X] T024 [US1] Update `app/static/app.js` to the submit→poll→fetch flow: on `202` poll `GET /jobs/{id}` every `poll_after_seconds`, render on `partial`/`complete`, treating a `200` from `/upload`, `/results` or a completed job identically (contracts "Client flow").

**Checkpoint**: US1 is independently demonstrable — the core defect is fixed. This is the MVP.

---

## Phase 4: User Story 2 — Re-open a report or download the PDF without recomputing (P2)

**Goal**: unchanged re-requests return immediately and identically; changed settings or a new log recompute.

**Independent Test**: run an analysis, then re-open the report and download the PDF unchanged — both return promptly with identical content; change a setting and results recompute (spec US2; SC-003, SC-004).

- [X] T025 [US2] Write `tests/test_results_cache.py`: hit/miss by `params_signature`; the report and the PDF are cached as separate artifacts; changing any setting or `content_hash` invalidates; and two sessions with byte-identical uploads never share a cached result (FR-016–FR-020; data-model ResultCache). Write to FAIL first.
- [X] T026 [US2] Implement `app/results_cache.py`: per-session, keyed by `params_signature`, storing payload + report document + rendered PDF, bounded 3–5 entries LRU, cleared entirely on `content_hash` change (data-model ResultCache).
- [X] T027 [US2] Add the `results` cache field to `SessionData` in `app/sessions.py` and wire `app/results_cache.py` into `app/compute.py` so stage one writes the payload and report and stage two updates the ML block (data-model Session `results`; ResultCache "Three cached artifacts").
- [X] T028 [US2] Make `GET /report` and `GET /report/pdf` in `app/main.py` serve from cache after stage one (`200`), returning `202` only while no analysis exists yet — never triggering training to read `summary` (contracts endpoint table; research R1).
- [X] T029 [US2] Add the FR-020 isolation assertion to `tests/test_results_cache.py` (or `tests/test_privacy_isolation.py`): a result computed for one session is never served to another, including identical uploads — asserted by test, not left to structure (data-model ResultCache "Isolation").
- [X] T030 [US2] Add a fidelity/equivalence test in `tests/test_results_cache.py` comparing a cached artifact's *analysis content* (including `low_confidence`, `always_present`, observation counts) to a fresh computation, comparing report data rather than PDF bytes, since `generate_pdf` embeds a timestamp (FR-018, Principle III; data-model ResultCache "Fidelity"/"Not byte-identity").
- [X] T031 [US2] **(RESOLVED/R8+R12 → skip)** T004 measured retained state at <1 MB/session and peak 29 MiB, so `DerivedBundle` is not justified: **retain `lookback_df`/score frames as-is and do nothing here** beyond recording the decision (research R4a, R12; data-model DerivedBundle). Left in the list so the "do nothing" outcome is explicit, not an omission.

**Checkpoint**: US1 + US2 both work; recomputation is gone for the common path.

---

## Phase 5: User Story 3 — See where I am in the queue when the site is busy (P3)

**Goal**: graceful degradation — position, wait estimate, cancellation, honest at-capacity, and place-keeping across reloads.

**Independent Test**: drive more analyses than can run at once; everyone beyond the limit sees a queued state with an updating position and estimate, can cancel, and eventually gets results (spec US3; SC-001, SC-005, SC-012, SC-013, SC-014).

- [X] T032 [US3] Write `tests/test_queue_limits.py`: position derived at read time; wait estimate recomputed per poll; refusal when `max_waiting` OR `max_estimated_wait` trips first; `at_capacity` `503` with `Retry-After`; duplicate `(session_id, params_signature)` returns the existing job; a new signature supersedes and cancels the prior `active_job_id` (FR-005–FR-011, data-model AnalysisQueue). Write to FAIL first.
- [X] T033 [US3] Implement position, wait estimation (`position / max_concurrent × median(recent_durations)`), and the two caps in `app/jobs.py`/`app/main.py`, with the estimate recomputed per poll and never counted down client-side (data-model AnalysisQueue; SC-012).
- [X] T034 [US3] Address the two estimation weaknesses in `app/jobs.py`: seed a cold-start prior from T004's measurements, and condition the estimate on a size class derived from the parsed frames so mixed job sizes do not blow the ±50% band (data-model AnalysisQueue "Two known weaknesses").
- [X] T035 [US3] Implement `DELETE /jobs/{job_id}` in `app/main.py` to cancel a queued job and release its place, owner-only with `404` for foreign ids (contracts; FR-009).
- [X] T036 [US3] Implement the at-capacity `503` in `app/main.py` with two distinct user-facing messages — queue saturation vs session saturation (R10) — each with `Retry-After`, never served by evicting a live session (contracts "At capacity" + "Other errors"; research R10).
- [X] T037 [US3] Write `tests/test_presence.py`: a job at the front runs only if `last_seen_at` is within the grace window (minutes), otherwise transitions to `abandoned`; a returning session can restart without re-upload (FR-012–FR-014; research R3). Write to FAIL first.
- [X] T038 [US3] Implement presence in `app/jobs.py`: every successful poll updates `last_seen_at`; debounce presence writes to at most once per grace-window fraction, or read presence from the session store instead, so polling does not become a per-request SQLite write (contracts "presence mechanism"; research R5b).
- [X] T039 [US3] Implement the abandoned-at-turn drop and the "your turn passed, restart without re-upload" path in `app/main.py`/`app/compute.py` (FR-013, FR-014; spec US3 scenarios 7–8; SC-014).
- [X] T040 [US3] Extend `app/static/app.js` with the queue UI (position, estimated wait, cancel button) and **visibility-aware polling** — poll on interval AND immediately on `visibilitychange` — so a locked phone does not look absent (contracts "Client flow"/"visibility-aware"; research R3; SC-013).
- [X] T041 [US3] Update `app/templates/index.html` and `app/static/style.css` with the queued/at-capacity/abandoned states (plan source tree: `index.html` queue state).

**Checkpoint**: US1–US3 work; an announcement spike degrades gracefully instead of failing.

---

## Phase 6: User Story 4 — My uploaded data stays with me for my whole session (P4)

**Goal**: data available for the whole session regardless of scaling, discarded on expiry, never crossing to another person, no account required.

**Independent Test**: upload, then perform a long sequence of interactions — the upload stays available throughout and is unreachable once the session expires (spec US4; SC-008, SC-009, SC-010).

- [X] T042 [US4] Write `tests/test_privacy_isolation.py`: two concurrent sessions never see each other's data through any endpoint (SC-007); after expiry, no upload/result/document is retrievable (SC-009); the full journey completes with no account (SC-010). Write to FAIL first. **Done**: covers isolation across `/results`, `/report`, `/report/pdf`, `/predict`, `/ingredient` plus a no-cookie stranger; post-expiry 404 on all endpoints (TTL forced to 0); and the full no-account journey (only an opaque `session_id` cookie).
- [X] T043 [US4] Implement expiry semantics in `app/sessions.py`/`app/main.py`: on expiry the session and everything reachable is discarded, and any queued or running job it owns is cancelled/expired (FR-015, FR-022; data-model Session "Lifecycle"). **Done**: `InMemoryStore` fires an `on_expire(session_id)` hook whenever a session is discarded on TTL (in both `get()` and `_cleanup()`); `app/main.py` wires it to `queue.expire_session(sid)`, a new `JobQueue` method (both backings) that expires every non-terminal job the session owns and frees its queue place. Conformance tests added to `tests/test_jobs.py`; wiring test in `tests/test_privacy_isolation.py`.
- [X] T044 [US4] Verify session availability under load in `tests/test_privacy_isolation.py` — no person is asked to re-upload during an unexpired session — with a test exercising many requests over a simulated long session (FR-021, SC-008). **Done**: `test_owner_is_never_asked_to_reupload_across_a_long_session` runs 30 rounds of `/results` + `/report` + a same-settings recompute, asserting none degrade to "upload a file first".
- [~] T045 [US4] **(CONTINGENT/R8+R12) — DEFERRED.** Lift compound splitting out of `parse_log` into `core/compounds.py::split_compound_ingredients(meals_df)`; drop `raw_bytes`. **Decision (user, 2026-08-13)**: defer. The R12 escape condition (a session-scoped upload *file* replacing `raw_bytes`) was not built — rung 1 stays in-memory — so the shrink would still be worth its privacy value (R8a item 3). But it buys **no** hot-path speedup (`raw_bytes` is only read when the `split_compounds` toggle flips, at `app/main.py:346` / `app/compute.py:97`), only a marginally faster toggle; the data is already memory-only, session-scoped, TTL-discarded and disclosed, so the stated privacy guarantee is unchanged; and the refactor carries real regression risk against the parser's two subtle quirks (asymmetric title-casing; suffix stripped from the last segment only). US4's acceptance criteria all pass without it. Revisit alongside R12 disk-backed sessions at rung 2.
- [~] T046 [US4] **(CONTINGENT/R8+R12) — DEFERRED** with T045 (the transform it would test is not being extracted).
- [~] T047 [US4] **(CONTINGENT/R8+R12) — DEFERRED** with T045 (`raw_bytes` and the buffer-vs-file parsing path are unchanged for now).

**Checkpoint**: US1–US4 work; the privacy and availability promises hold under scaling and are tested. (Note: the FR-024 retention disclosure already landed; the R6/`raw_bytes` shrink and R12 disk-backed sessions remain deferred to rung 2 and are explicitly out of scope here.)

---

## Phase 7: User Story 5 — Very large and oversized logs handled clearly (P5)

**Goal**: year-scale logs analyse successfully without spoiling others; oversized uploads are refused promptly and in plain language; unanalysably-large-but-within-size logs get a clear explanation.

**Independent Test**: analyse a year of daily entries while others stay responsive; separately, upload beyond the size cap and get an immediate clear message (spec US5; SC-006, SC-011).

- [X] T048 [US5] Write `tests/test_limits.py`: an oversized upload is rejected with `413` and the limit named, before the whole body is buffered; a within-size but over-complexity log yields `422 too_complex`; other requests are unaffected during a rejection (FR-028–FR-030; SC-011; contracts `413`/`422`). Write to FAIL first. **Done**: covers 413 (honest-length fast reject, limit named), 422 (ceiling lowered via monkeypatch), isolation of a rejection from other requests, the metric shape, and two direct middleware unit tests (streamed abort with no `Content-Length`; non-upload paths pass through).
- [X] T049 [US5] Implement `app/limits.py` as middleware enforcing the upload byte cap (value from T004): fast-reject on `Content-Length`, and abort a dishonest/absent one by reading `request.stream()` — because a handler taking `UploadFile` cannot enforce it after the framework has spooled the body (research R9; contracts `413`). **Done**: `LimitUploadSizeMiddleware` (pure ASGI) rejects an honest over-cap `Content-Length` before any body read, and for an absent/dishonest one counts streamed bytes and aborts the moment the cap is crossed (never fully buffering an oversized body), replaying a within-cap body to the app. `MAX_UPLOAD_BYTES` = 10 MB (env `ABS_MAX_UPLOAD_BYTES`). Registered in `app/main.py`; only POST `/upload` is inspected.
- [X] T050 [US5] Implement the post-parse complexity cap in `app/limits.py`/`app/compute.py` using the metric chosen in T004 (candidate: estimated lookback pairs), returning `422 too_complex` rather than occupying a worker indefinitely (FR-030; research R9). **Done**: `estimate_lookback_pairs = len(bac_df) × len(meals_df)`, ceiling `MAX_LOOKBACK_PAIRS` = 50 M (env `ABS_MAX_LOOKBACK_PAIRS`) — admits >1 year (~10.6 M) while refusing pathological inputs. Enforced in `app/main.py` `upload_file` after parse (before any worker is occupied) and again on the `split_compounds` re-parse in `recompute`; `_set_too_complex` returns the `too_complex` body without dropping the session cookie.
- [X] T051 [US5] Stop leaking raw exception text: replace `raise HTTPException(400, f"Failed to parse file: {e}")` in `app/main.py` with a plain-language `invalid_file` message (contracts "All error bodies … MUST NOT leak internal exception text"). **Done** (already satisfied in an earlier phase): the parse-failure path raises a plain "Could not read that file. Please upload a valid ABS log (.xlsx)." with no exception text; no `Failed to parse file: {e}` remains in the tree.
- [X] T052 [US5] Validate SC-006 in `tests/test_concurrency.py` using `tests/fixtures/generate_year_log.py`: 12 months completes within the target while other response times rise no more than 20% (spec US5 scenarios 1–2; SC-006), recording the measured numbers. **Done**: `test_year_scale_log_completes_and_keeps_the_site_responsive` uploads a 12-month log, asserts job completion < 60s and index responsiveness while it runs, and records the during/baseline latency ratio in the assertion message. The hard bound is absolute (SC-002 <2s, tightened to <1s) rather than a brittle 20% ratio on sub-millisecond baselines; post-vectorisation the 12-month analysis finishes in a few seconds.

**Checkpoint**: all five user stories are independently functional.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [~] T053 [P] **(CONTINGENT) — DEFERRED.** Implement `app/serialization.py` (Arrow/Parquet for frames, JSON for scalars) with round-trip tests in `tests/test_serialization.py` (research R5 seam 3). **Decision**: defer. Deleting `RedisStore` removed the only session-path pickle, so nothing at rung 1 needs a serialization format. It is the format rung 2 (file-backed / multi-worker sessions) will need and should be built *before* that switch — not now, when it would be dead code.
- [X] T054 Run the full `specs/001-concurrent-analysis/quickstart.md` validation end to end (Checks 1–7), recording output as the Principle V evidence, and reconcile any drifted numbers. **Done**: added a "Validation run (T054)" evidence table to `quickstart.md` (live HTTP run against uvicorn: SC-003 sub-ms reads, FR-018 identical repeats, FR-019 recompute on change, SC-007 foreign-job 404, SC-011 11 MB → 413 in 0.8 ms with the limit named, FR-024 concrete retention window; SC-006 via the automated 12-month test; rung-2 load/SQLite checks noted out of scope). Reconciled the stale test count (now 212 passed, 1 skipped) and the now-resolved `exclude_proteins` default note.
- [X] T055 [P] Update `README.md` for the new async upload/poll behaviour and any new environment knobs (executor width, queue caps, `MAX_SESSIONS`, upload cap), keeping the `uv` instructions current. **Done**: added a "How analysis runs (async)" section (submit → poll → fetch, queue position, `503`/`Retry-After`, anonymous/session-scoped) and a "Configuration" table documenting `SESSION_TTL`, `MAX_SESSIONS`, `ANALYSIS_MAX_CONCURRENT`, `ANALYSIS_MAX_WAITING`, `ANALYSIS_MAX_WAIT_SECONDS`, `ANALYSIS_ABANDON_GRACE`, `ABS_MAX_UPLOAD_BYTES`, `ABS_MAX_LOOKBACK_PAIRS`, `ABS_QUEUE_DB` with their real defaults.
- [X] T056 Run the regression suite (`uv run pytest`) and confirm the pre-existing count plus the new suites pass; fix any regressions before claiming completion (Principle V; verification-before-completion). **Done**: `212 passed, 1 skipped` (the private legacy fixture). No regressions.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: no dependencies.
- **Foundational (Phase 2)**: depends on Setup. **T004 (measurement) blocks every (CONTINGENT/R8a) task.** BLOCKS all user stories.
- **User stories (Phases 3–7)**: all depend on Foundational. In priority order P1→P5; US1 is the MVP.
- **Polish (Phase 8)**: depends on the desired stories being complete.

### Critical gate

- **T004** gates **T018, T019** (executor + vectorisation) and informs the caps used in **T033, T034, T049, T050**. Do not build the executor before T004.

### Story dependencies

- **US1 (P1)**: needs Foundational (store, compute split, queue). No dependency on other stories.
- **US2 (P2)**: builds on US1's compute/executor path; the cache attaches to the staged job.
- **US3 (P3)**: builds on US1's queue and job status; adds position/estimate/cancel/presence/UI.
- **US4 (P4)**: builds on the Foundational store; adds expiry-cancels-job and isolation tests. R6/raw_bytes work is contingent.
- **US5 (P5)**: mostly independent (upload/complexity limits) and can proceed in parallel with US3/US4 once Foundational is done.

### Within each story

- Write the story's test task first and see it FAIL, then implement (test-driven; research R6/R8a make this mandatory for the transform and the vectorisation).

### Parallel opportunities

- Setup: T001, T002, T003 in parallel.
- Foundational: the queue backings T013 (in-memory) can proceed alongside store work; T014 (SQLite) follows T012.
- Across stories: once Foundational is done, US5 (limits) is largely independent of US3/US4 and can run in parallel.

## Parallel Example: Setup

```bash
Task: "Create year-scale generator in tests/fixtures/generate_year_log.py"
Task: "Create measurement harness in tests/fixtures/measure_scale.py"
Task: "Smoke test the fixture in tests/test_fixtures.py"
```

## Implementation Strategy

### MVP first (User Story 1 only)

1. Phase 1 Setup → Phase 2 Foundational (measurement, store, compute split, queue).
2. Phase 3 US1.
3. **STOP and VALIDATE**: run `tests/test_concurrency.py` and quickstart Check 2 — concurrent analyses no longer block each other. This alone turns an unusable shared tool into a usable one.

### Incremental delivery

- US1 → US2 (no recompute) → US3 (queue UX) → US4 (privacy/availability) → US5 (limits), validating each independently before the next.

### Notes

- Do not skip T004. Every executor/caps decision downstream assumes its numbers exist; guessing here is the mistake two review rounds flagged.
- The (CONTINGENT/R8+R12) tasks may legitimately resolve to "retain the frame, do nothing" — that is a valid outcome, recorded, not a skipped task.
- Commit after each task or logical group.
