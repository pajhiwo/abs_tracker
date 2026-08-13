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

1. **R8a is a hard gate.** The dominant cost — a nested-`iterrows` `map_lookback` versus LASSO
   training — is unmeasured, and it decides whether the executor is a thread pool or a process
   pool, and whether `map_lookback` gets vectorised (research R2, R8a; plan Complexity Tracking).
   Every task tagged **(CONTINGENT/R8a)** MUST NOT be started until T008 has produced numbers.
   Building the process pool first is the specific mistake the plan calls out.
2. **Many "shrink storage" tasks are now contingent on a decision, not just measurement.** R12
   sanctioned session-scoped disk but deferred it to rung 2, which makes R4a's `DerivedBundle`
   and R6's compound-split extraction *optional*. They are kept in the list because they still
   pay off (less memory, one fewer re-parse), but each is tagged **(CONTINGENT/R8+R12)** and may
   resolve to "retain the frame, do nothing" once T008's sizes are known.

---

## Phase 1: Setup (measurement tooling and fixtures)

**Purpose**: build the evidence-gathering tools the rest of the plan depends on. Nothing about
the executor or the caps can be decided without these.

- [ ] T001 [P] Create synthetic year-scale workbook generator in `tests/fixtures/generate_year_log.py`, reproducing the real format's structural quirks (aggregate rows, blank padding, compound dishes, partially filled nutrient columns), parameterised by months, generated on demand and never committed (research R8; must contain no real patient data per Principle IV).
- [ ] T002 [P] Create per-stage measurement harness in `tests/fixtures/measure_scale.py` that records wall time for parse, `map_lookback`, `compute_lift_scores`, `extract_features`, `train_personal_model`, `generate_pdf`, plus serialised per-field `SessionData` size and peak resident memory for one analysis (research R8 "What to record").
- [ ] T003 [P] Add a smoke test in `tests/test_fixtures.py` asserting `generate_year_log.py` output parses through `core/parser.py` and contains the intended quirks, so the fixture itself is trustworthy before any measurement rides on it.

---

## Phase 2: Foundational (blocking prerequisites)

**⚠️ No user story work begins until this phase is complete.** These are the shared seams every
story sits on, plus the measurement gate.

### The measurement gate

- [ ] T004 Run `tests/fixtures/measure_scale.py` across the example workbook, 3, 12 and 24 months; record the numbers in `specs/001-concurrent-analysis/research.md` under R8/R8a; and from them decide (a) thread pool vs process pool, (b) whether `map_lookback` is vectorised, (c) provisional values for `max_concurrent`, grace window, `max_waiting`, `max_estimated_wait`, `MAX_SESSIONS`, upload byte cap, and the complexity metric. **This unblocks every (CONTINGENT/R8a) task.**

### Session store (all stories depend on it — R10, R5)

- [ ] T005 Write the store protocol conformance suite in `tests/test_sessions.py` against the `SessionStore` protocol, asserting `get()` returns a copy and that a live session is never evicted to admit a new one (research R5 seam 1, R10; data-model "Store semantics"). Write it to FAIL against current `InMemoryStore` first.
- [ ] T006 Make `InMemoryStore.get()` return a copy and remove the oldest-session eviction from `InMemoryStore.set()` in `app/sessions.py`; replace it with a refuse-new-session-at-capacity signal the handler can surface as the R10 `503` (research R10; data-model "Capacity").
- [ ] T007 Raise/re-home `MAX_SESSIONS` as a memory-derived backstop (value from T004) in `app/sessions.py`, and document that expiry — not capacity — is the normal reclamation path (research R10).

### Deterministic core, decoupled and staged (R1 — enables US1 staging and US2 caching)

- [ ] T008 Add `tests/test_compute.py` asserting `summary` is computed without invoking the ML block, and that an ML failure leaves `summary`, lift scores and the rest of the payload intact (Principle II; research R1). Write to FAIL first.
- [ ] T009 Create `app/compute.py` by moving `_run_analysis` and `_build_results_json` out of `app/main.py`, split into stage one (summary + lift scores, no model) and stage two (optional ML), returning the existing `ResultPayload` shape unchanged (research R1; data-model ResultPayload; plan Structure Decision).
- [ ] T010 Reconcile the `exclude_proteins` default so `AnalysisParams` and the `/upload` path agree, compute `params_signature` from resolved values over all five fields, and add a regression test in `tests/test_compute.py` pinning the reconciled default (data-model AnalysisParams + "pre-existing bug"; contracts "Parameter defaults").

### Job queue (US1 and US3 both need it — R5, R5a)

- [ ] T011 Write `tests/test_jobs.py` covering ordering, atomic-and-leased claim, position, cancellation, lease expiry/reclaim, and foreign-`session_id` isolation — parameterised to run against BOTH backings (data-model AnalysisJob/AnalysisQueue; research R5a). Write to FAIL first.
- [ ] T012 Define the `JobQueue` protocol and the `AnalysisJob`/`JobState` model in `app/jobs.py`, referencing jobs by id only, with the state machine from data-model (`queued→running→partial→complete`, plus `failed/cancelled/abandoned/expired`).
- [ ] T013 [P] Implement the in-memory `JobQueue` backing in `app/jobs.py` for tests.
- [ ] T014 Implement the SQLite `JobQueue` backing in `app/jobs.py` with the R5a conditions: WAL + `synchronous=NORMAL` + `foreign_keys=ON` + non-zero `busy_timeout`; single-statement `UPDATE … RETURNING` atomic leased claim; `BEGIN IMMEDIATE` for writes; short transactions; bounded `SQLITE_BUSY` retry surfaced as `503`; lease-reclaim as the crash path (research R5a).
- [ ] T015 In `app/jobs.py` and the queue call sites in `app/main.py`, ensure every queue call from an `async` handler is offloaded (`run_in_threadpool` or a sync API used only from worker threads), since `sqlite3` blocks the event loop — the exact defect the feature removes, applied to the queue (research R5a "Request-path discipline").
- [ ] T016 Implement the startup sweep in `app/jobs.py` (or app lifespan in `app/main.py`): reclaim or fail leased jobs and clear `queued` rows whose sessions no longer exist, so a redeploy never serves stale positions (research R5a "Ephemeral filesystem").

**Checkpoint**: measurement is in hand, the store is correct and copy-returning, the deterministic
core is standalone and staged, and the queue exists with two exercised backings. User stories can begin.

---

## Phase 3: User Story 1 — Analyse my log while others are using the site (P1) 🎯 MVP

**Goal**: concurrent analyses that never make another person's page hang; each person gets their own correct results.

**Independent Test**: start several analyses within a few seconds; each returns its own correct
result and every page stays responsive throughout (spec US1 Independent Test; SC-002).

- [ ] T017 [US1] Write `tests/test_concurrency.py`: with analyses running, non-analysis pages respond under 2s and no page fails; each concurrent analysis returns results derived only from its own log (SC-002, SC-007; spec US1 scenarios 1–4). Write to FAIL first.
- [ ] T018 [US1] **(CONTINGENT/R8a)** Create `app/executor.py` — the claim/execute supervisor — as a thread pool or process pool per T004's decision; do not build before T004 (research R2; plan Complexity Tracking). Size from T004.
- [ ] T019 [US1] **(CONTINGENT/R8a)** If T004 selected vectorisation, rewrite `map_lookback` in `core/analysis.py` as an interval/`merge_asof` join, and add an equivalence test in `tests/test_analysis.py` against `example/example_log.xlsx` before trusting it (research R8a; Principle V).
- [ ] T020 [US1] Rework `POST /upload` in `app/main.py` to validate, parse the upload awaited off the event loop, hash the bytes, discard the bytes when the handler returns, then enqueue an analysis job referencing the session's parsed frames (research R11; contracts `POST /upload`; data-model AnalysisJob "does NOT carry bytes").
- [ ] T021 [US1] Wire `app/executor.py` to run `app/compute.py` staged: cache stage one (summary + lift scores) and mark the job `partial`, then run stage two (ML) and mark `complete`, updating the cached payload (data-model AnalysisJob "Staging"; contracts "Staged results").
- [ ] T022 [US1] Implement `GET /jobs/{job_id}` in `app/main.py` returning the status shape (including `partial`), enforcing owner-only access with `404` for a foreign/unknown id (contracts "Job status"; data-model AnalysisJob validation).
- [ ] T023 [US1] Convert `GET /results` / `POST /results` in `app/main.py` to serve `200` from cache or `202` with a job, never computing inline (contracts endpoint table).
- [ ] T024 [US1] Update `app/static/app.js` to the submit→poll→fetch flow: on `202` poll `GET /jobs/{id}` every `poll_after_seconds`, render on `partial`/`complete`, treating a `200` from `/upload`, `/results` or a completed job identically (contracts "Client flow").

**Checkpoint**: US1 is independently demonstrable — the core defect is fixed. This is the MVP.

---

## Phase 4: User Story 2 — Re-open a report or download the PDF without recomputing (P2)

**Goal**: unchanged re-requests return immediately and identically; changed settings or a new log recompute.

**Independent Test**: run an analysis, then re-open the report and download the PDF unchanged — both return promptly with identical content; change a setting and results recompute (spec US2; SC-003, SC-004).

- [ ] T025 [US2] Write `tests/test_results_cache.py`: hit/miss by `params_signature`; the report and the PDF are cached as separate artifacts; changing any setting or `content_hash` invalidates; and two sessions with byte-identical uploads never share a cached result (FR-016–FR-020; data-model ResultCache). Write to FAIL first.
- [ ] T026 [US2] Implement `app/results_cache.py`: per-session, keyed by `params_signature`, storing payload + report document + rendered PDF, bounded 3–5 entries LRU, cleared entirely on `content_hash` change (data-model ResultCache).
- [ ] T027 [US2] Add the `results` cache field to `SessionData` in `app/sessions.py` and wire `app/results_cache.py` into `app/compute.py` so stage one writes the payload and report and stage two updates the ML block (data-model Session `results`; ResultCache "Three cached artifacts").
- [ ] T028 [US2] Make `GET /report` and `GET /report/pdf` in `app/main.py` serve from cache after stage one (`200`), returning `202` only while no analysis exists yet — never triggering training to read `summary` (contracts endpoint table; research R1).
- [ ] T029 [US2] Add the FR-020 isolation assertion to `tests/test_results_cache.py` (or `tests/test_privacy_isolation.py`): a result computed for one session is never served to another, including identical uploads — asserted by test, not left to structure (data-model ResultCache "Isolation").
- [ ] T030 [US2] Add a fidelity/equivalence test in `tests/test_results_cache.py` comparing a cached artifact's *analysis content* (including `low_confidence`, `always_present`, observation counts) to a fresh computation, comparing report data rather than PDF bytes, since `generate_pdf` embeds a timestamp (FR-018, Principle III; data-model ResultCache "Fidelity"/"Not byte-identity").
- [ ] T031 [US2] **(CONTINGENT/R8+R12)** If T004's sizes justify it, build `DerivedBundle` (`lift_scores`, `ingredient_readings`, `lookback_pair_count`) in `app/compute.py`, adapt `/predict` and `/ingredient/{name}` in `app/main.py` to read it, and adapt `detect_combinations` to the membership form; otherwise retain `lookback_df`/score frames and record the decision (research R4a, R12; data-model DerivedBundle).

**Checkpoint**: US1 + US2 both work; recomputation is gone for the common path.

---

## Phase 5: User Story 3 — See where I am in the queue when the site is busy (P3)

**Goal**: graceful degradation — position, wait estimate, cancellation, honest at-capacity, and place-keeping across reloads.

**Independent Test**: drive more analyses than can run at once; everyone beyond the limit sees a queued state with an updating position and estimate, can cancel, and eventually gets results (spec US3; SC-001, SC-005, SC-012, SC-013, SC-014).

- [ ] T032 [US3] Write `tests/test_queue_limits.py`: position derived at read time; wait estimate recomputed per poll; refusal when `max_waiting` OR `max_estimated_wait` trips first; `at_capacity` `503` with `Retry-After`; duplicate `(session_id, params_signature)` returns the existing job; a new signature supersedes and cancels the prior `active_job_id` (FR-005–FR-011, data-model AnalysisQueue). Write to FAIL first.
- [ ] T033 [US3] Implement position, wait estimation (`position / max_concurrent × median(recent_durations)`), and the two caps in `app/jobs.py`/`app/main.py`, with the estimate recomputed per poll and never counted down client-side (data-model AnalysisQueue; SC-012).
- [ ] T034 [US3] Address the two estimation weaknesses in `app/jobs.py`: seed a cold-start prior from T004's measurements, and condition the estimate on a size class derived from the parsed frames so mixed job sizes do not blow the ±50% band (data-model AnalysisQueue "Two known weaknesses").
- [ ] T035 [US3] Implement `DELETE /jobs/{job_id}` in `app/main.py` to cancel a queued job and release its place, owner-only with `404` for foreign ids (contracts; FR-009).
- [ ] T036 [US3] Implement the at-capacity `503` in `app/main.py` with two distinct user-facing messages — queue saturation vs session saturation (R10) — each with `Retry-After`, never served by evicting a live session (contracts "At capacity" + "Other errors"; research R10).
- [ ] T037 [US3] Write `tests/test_presence.py`: a job at the front runs only if `last_seen_at` is within the grace window (minutes), otherwise transitions to `abandoned`; a returning session can restart without re-upload (FR-012–FR-014; research R3). Write to FAIL first.
- [ ] T038 [US3] Implement presence in `app/jobs.py`: every successful poll updates `last_seen_at`; debounce presence writes to at most once per grace-window fraction, or read presence from the session store instead, so polling does not become a per-request SQLite write (contracts "presence mechanism"; research R5b).
- [ ] T039 [US3] Implement the abandoned-at-turn drop and the "your turn passed, restart without re-upload" path in `app/main.py`/`app/compute.py` (FR-013, FR-014; spec US3 scenarios 7–8; SC-014).
- [ ] T040 [US3] Extend `app/static/app.js` with the queue UI (position, estimated wait, cancel button) and **visibility-aware polling** — poll on interval AND immediately on `visibilitychange` — so a locked phone does not look absent (contracts "Client flow"/"visibility-aware"; research R3; SC-013).
- [ ] T041 [US3] Update `app/templates/index.html` and `app/static/style.css` with the queued/at-capacity/abandoned states (plan source tree: `index.html` queue state).

**Checkpoint**: US1–US3 work; an announcement spike degrades gracefully instead of failing.

---

## Phase 6: User Story 4 — My uploaded data stays with me for my whole session (P4)

**Goal**: data available for the whole session regardless of scaling, discarded on expiry, never crossing to another person, no account required.

**Independent Test**: upload, then perform a long sequence of interactions — the upload stays available throughout and is unreachable once the session expires (spec US4; SC-008, SC-009, SC-010).

- [ ] T042 [US4] Write `tests/test_privacy_isolation.py`: two concurrent sessions never see each other's data through any endpoint (SC-007); after expiry, no upload/result/document is retrievable (SC-009); the full journey completes with no account (SC-010). Write to FAIL first.
- [ ] T043 [US4] Implement expiry semantics in `app/sessions.py`/`app/main.py`: on expiry the session and everything reachable is discarded, and any queued or running job it owns is cancelled/expired (FR-015, FR-022; data-model Session "Lifecycle").
- [ ] T044 [US4] Verify session availability under load in `tests/test_privacy_isolation.py` — no person is asked to re-upload during an unexpired session — with a test exercising many requests over a simulated long session (FR-021, SC-008).
- [ ] T045 [US4] **(CONTINGENT/R8+R12)** Lift compound splitting out of `parse_log` into `core/compounds.py::split_compound_ingredients(meals_df)` as a pure transform, retaining `raw_ingredient` in the parser and removing `split_compounds` from `parse_log`; then drop `raw_bytes` from `SessionData` (research R6; plan source tree). If T004+R12 make retaining the upload file cheaper, record that and skip the extraction.
- [ ] T046 [US4] **(CONTINGENT/R8+R12)** Write `tests/test_compounds.py` asserting the transform reproduces the old in-parser output EXACTLY for both the multi-sheet and legacy paths, covering the two quirks: title-case only on split parts (asymmetric casing) and shared-suffix stripped from the last segment only (`"Lentil soup & Kale soup"` → `["Lentil Soup", "Kale"]`) — reproduce the code, not the comment (research R6). Move the three existing split tests out of `tests/test_parser.py`.
- [ ] T047 [US4] **(CONTINGENT/R8+R12)** Update `tests/test_parser.py` for buffer-based parsing (no disk write we control) with the stream rewound between the two workbook opens, and remove the split assertions moved in T046 (research R6, R7).

**Checkpoint**: US1–US4 work; the privacy and availability promises hold under scaling and are tested. (Note: the FR-024 retention disclosure already landed; R12 disk-backed sessions remain deferred to rung 2 and are explicitly out of scope here.)

---

## Phase 7: User Story 5 — Very large and oversized logs handled clearly (P5)

**Goal**: year-scale logs analyse successfully without spoiling others; oversized uploads are refused promptly and in plain language; unanalysably-large-but-within-size logs get a clear explanation.

**Independent Test**: analyse a year of daily entries while others stay responsive; separately, upload beyond the size cap and get an immediate clear message (spec US5; SC-006, SC-011).

- [ ] T048 [US5] Write `tests/test_limits.py`: an oversized upload is rejected with `413` and the limit named, before the whole body is buffered; a within-size but over-complexity log yields `422 too_complex`; other requests are unaffected during a rejection (FR-028–FR-030; SC-011; contracts `413`/`422`). Write to FAIL first.
- [ ] T049 [US5] Implement `app/limits.py` as middleware enforcing the upload byte cap (value from T004): fast-reject on `Content-Length`, and abort a dishonest/absent one by reading `request.stream()` — because a handler taking `UploadFile` cannot enforce it after the framework has spooled the body (research R9; contracts `413`).
- [ ] T050 [US5] Implement the post-parse complexity cap in `app/limits.py`/`app/compute.py` using the metric chosen in T004 (candidate: estimated lookback pairs), returning `422 too_complex` rather than occupying a worker indefinitely (FR-030; research R9).
- [ ] T051 [US5] Stop leaking raw exception text: replace `raise HTTPException(400, f"Failed to parse file: {e}")` in `app/main.py` with a plain-language `invalid_file` message (contracts "All error bodies … MUST NOT leak internal exception text").
- [ ] T052 [US5] Validate SC-006 in `tests/test_concurrency.py` using `tests/fixtures/generate_year_log.py`: 12 months completes within the target while other response times rise no more than 20% (spec US5 scenarios 1–2; SC-006), recording the measured numbers.

**Checkpoint**: all five user stories are independently functional.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T053 [P] **(CONTINGENT)** Implement `app/serialization.py` (Arrow/Parquet for frames, JSON for scalars) with round-trip tests in `tests/test_serialization.py` — no longer urgent since deleting `RedisStore` removed the only session-path pickle, but it is the format rung 2 needs; build it before file-backed sessions, not after (research R5 seam 3; plan Complexity Tracking).
- [ ] T054 Run the full `specs/001-concurrent-analysis/quickstart.md` validation end to end (Checks 1–7), recording output as the Principle V evidence, and reconcile any drifted numbers.
- [ ] T055 [P] Update `README.md` for the new async upload/poll behaviour and any new environment knobs (executor width, queue caps, `MAX_SESSIONS`, upload cap), keeping the `uv` instructions current.
- [ ] T056 Run the regression suite (`uv run pytest`) and confirm the pre-existing count plus the new suites pass; fix any regressions before claiming completion (Principle V; verification-before-completion).

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
