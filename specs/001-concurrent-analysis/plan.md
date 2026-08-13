# Implementation Plan: Concurrent Analysis Without Waiting

**Branch**: `001-concurrent-analysis` | **Date**: 2026-08-13 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-concurrent-analysis/spec.md`

## Summary

**Status**: revised after independent external review (see the Review Findings section
below). Several Phase 0 conclusions changed; one decision is now blocked on measurement.

`POST /upload` runs Excel parsing, correlation and LASSO training synchronously inside an
`async def` handler, and `GET /results`, `/report` and `/report/pdf` retrain on every call,
so a single person's analysis occupies the event loop and every other request stalls behind
it. Worse, `/report` and `/report/pdf` retrain purely to read the `summary` block, which
needs no model at all — so re-opening a report costs as much as the original analysis.

The plan addresses both with one structural change: **handlers stop computing.** CPU-bound
work moves to a bounded process pool fronted by an explicit queue, and handlers become
submit/poll/fetch operations against a per-session result cache keyed by a content hash of
the upload plus the analysis parameters. A cache hit is a dictionary lookup; a miss is a
queued job with a position and a wait estimate.

Three consequences fall out of that shape and resolve the rest of the spec:

- **Storage shrinks rather than relocates.** The session retains parsed frames plus a
  compact derived bundle, not `lookback_df` (which grows with readings ×
  ingredients-in-window) or the intermediate score frames. Four consumers currently read
  those dropped frames, so this needs the compact-index design in R4a rather than simply
  deleting fields.
- **`raw_bytes` disappears.** It exists only so `split_compounds` can trigger a re-parse.
  Compound splitting is a string transform over one column, so lifting it out of the parser
  into a post-parse transform removes the last reason to retain the workbook — provided two
  casing quirks are reproduced exactly (R6).
- **The status poll doubles as a presence signal**, which makes "drop the job if the person
  has left by the time their turn comes" (FR-013) implementable without a separate
  heartbeat — with a grace window measured in minutes, since mobile browsers throttle
  background timers (R3).

**Contingent**: the executor behind the queue is not yet settled. `map_lookback` is a
nested `iterrows` (`core/analysis.py:29-35`), so the dominant cost may be an
O(readings × meals) Python loop rather than model training. If so, vectorising it may
outperform a process pool and move the residual work into NumPy, where threads would
suffice. R8a measures this and **gates** the R2 decision. The queue is required either way.

## The scaling question, answered directly

The requirement is surviving an unpredictable spike from 10 to 100 parallel users on day one.
The governing fact is that **100 simultaneous analyses cannot be made fast, only orderly** —
at 60s each and four-wide concurrency, 100 arrivals is a 25-minute queue whatever
infrastructure sits underneath. So the queue is what survives the spike, and per-analysis
time is what determines whether the queue is tolerable. Vectorising `map_lookback` plausibly
matters more than any scaling machinery, which is why R8a blocks the executor choice.

| Rung | Change | Ceiling |
|------|--------|---------|
| 1 (this feature) | Vectorise if measured; SQLite-backed queue; sessions in memory; one Uvicorn worker | Single-analysis time and RAM for live sessions |
| 2 (if needed) | Session frames to session-scoped files (R12); add Uvicorn workers **under one global execution budget** | One machine's cores and disk |
| 3 (only if 2 is exhausted) | Shared store behind the same interfaces | Multi-machine |

**Rung 2 is not simply "add workers."** If each of N workers owns an M-wide executor, CPU
demand is N × M and adding workers makes throughput worse while looking like added capacity
(R5). The global limit must live in the SQLite queue, capping concurrently leased jobs across
all processes — which is a second reason the queue belongs there rather than in memory.

**Redis is not adopted** (R5). With disk sanctioned as the rung-2 lever, workers on one box
share a filesystem and Redis buys nothing until there is a second machine. `RedisStore` is
deleted rather than left to activate silently on `REDIS_URL`.

**SQLite's correctness conditions are specified, not assumed** (R5a): WAL plus `busy_timeout`,
a single-statement atomic claim with `RETURNING`, `BEGIN IMMEDIATE` for writes, short
transactions, bounded `SQLITE_BUSY` retry surfacing as `503`, lease reclaim as the crash path,
and — easy to overlook — every queue call from a handler offloaded off the event loop, since
`sqlite3` blocks and the constitution's request-path rule applies to the queue too.

**Session-scoped disk is now sanctioned** (R12) — the "nothing on disk" rule was an
aspiration, not a requirement, and relaxing it simplifies several decisions: it makes R4a's
compact index optional, makes R6 non-load-bearing, and relaxes R11's constraint on where
parsing happens. The cost is an explicit TTL sweeper plus a startup sweep for directories
orphaned by a crash.

## Technical Context

**Language/Version**: Python 3.14 (`requires-python = ">=3.14"`)

**Primary Dependencies**: FastAPI, Uvicorn, pandas 3.x, openpyxl, scikit-learn, NumPy,
fpdf2, plus stdlib `sqlite3` for the queue. Frontend is vanilla JavaScript (`app/static/app.js`), no
build step.

**Storage**: No relational database for domain data. Session state in memory, with
session-scoped files as the rung-2 option (R12). Queue state in SQLite (stdlib, WAL mode).
`RedisStore` and the `redis` extra are removed (R5). Session-scoped disk is sanctioned by
constitution v2.0.0.

**Testing**: pytest, 121 tests collected, 120 passing (one skipped: the private legacy fixture is absent). `example/example_log.xlsx` is the
committed integration fixture. This feature adds a generated year-scale fixture.

**Target Platform**: Linux container (Docker), deployed to Render today, moving to a
resourced environment before the support-group announcement.

**Project Type**: Single-process web service with a static frontend.

**Performance Goals**: Spec SC-001 to SC-014. Load-bearing ones: results or an
acknowledged queue position within 5s for 50 simultaneous submissions (SC-001); unchanged
re-request under 2s (SC-003); analysis computed exactly once per distinct
(log, settings) pair (SC-004); 12 months of daily entries analysed within 60s while
others degrade by no more than 20% (SC-006).

**Constraints**: Anonymous path must stay fully functional (Principle VI). Session-scoped
retention only, discarded at `SESSION_TTL` (Principle IV). Result reuse must never cross
sessions, even for byte-identical uploads (FR-020). Deterministic analysis must not depend
on the ML layer (Principle II).

**Scale/Scope**: Design points of 50 simultaneous submissions and 200 arrivals within 10
minutes, drawn from a 3000-member group with unknown participation. Graceful degradation
past those points is the actual requirement.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Gate | Source | Status | Notes |
|------|--------|--------|-------|
| Request-path discipline | Security & Scale | **FAILS today** | `parse_log` runs in the `/upload` handler (`app/main.py` `upload_file`, `NamedTemporaryFile`); `train_personal_model` runs via `_build_results_json` from `/upload`, `GET /results`, `POST /results`, `/report` and `/report/pdf` (`app/main.py` `_build_results_json`, ML block). This feature exists to fix it. |
| No recomputation when inputs unchanged | Security & Scale | **FAILS today** | `/report` and `/report/pdf` call `_build_results_json` for `summary` alone (`app/main.py` `report` and `report_pdf`), retraining LASSO with 50 bootstrap iterations each time. |
| Retention is stated accurately | Principle IV (v2.0.0) | **PASSES for FR-024; a wider privacy-notice gap remains** | The old absolute ("nothing written to disk") was withdrawn in v2.0.0 as both unachievable — Starlette spools uploads above 1 MiB before any handler runs — and self-contradictory, since it permitted Redis. FR-024's disclosure landed with it: retention, temp-file writes, early eviction under load, no cross-user pooling, no egress, and the CDN's view of the visitor's IP, with the window substituted from `SESSION_TTL` at render time and tested against drift. **Two obligations carry**: the wording describes memory-only retention, so R12 must revise it when disk-backed sessions land; and FR-024 is a *retention* disclosure, not a GDPR Article 13 privacy notice — see the gap recorded below. |
| Article 13 privacy notice | Principle IV / GDPR | **Gap — outside this feature** | Separate from FR-024 above. No controller identity, lawful basis, Article 9(2) condition, data-subject rights, complaint route, or processor list exists anywhere. Needs the operator's own decisions and its own specification. |
| No cross-user data flow | Principle IV | Pass by design | Result cache is keyed inside the session; content hash is used only for change detection, never as a shared cache key. Enforced by FR-020. |
| Deterministic core survives failure | Principle II | **Conditional** | Isolating the `ml` block stops an ML *error* affecting reports, but not the ML *wait*: if `/report` returns `202` until the job that also trains LASSO completes, optional intelligence is blocking report generation, which the constitution forbids (`constitution.md:84`). Requires the job to be staged so summary and lift scores are cacheable before training starts. |
| Session availability under load | Principle IV / FR-021 | **FAILS today** | `InMemoryStore` evicts the oldest live session at `MAX_SESSIONS=100` (`app/sessions.py:76-78`, `:21`), which destroys in-progress sessions at the spec's own 200-arrival design point. A correctness issue, not a tuning knob (R10). |
| Uncertainty flags reach the interface | Principle III | Pass | Caching stores the built payload verbatim; `low_confidence` and `always_present` travel with it. Guarded by an equivalence test. |
| Anonymous use unaffected | Principle VI | Pass | Queueing, polling and cache lookup key on the existing anonymous session cookie. No identity introduced. |
| Complexity justified | Governance | See Complexity Tracking | Process pool and queue are new machinery and are justified below. |
| Tests and evidence | Principle V | Planned | New concurrency, cache and transform tests; year-scale fixture generator; measurements recorded before tuning any constant. |

Three gates fail against current code, one is conditional, and one gap sits outside this
feature's scope. The failures are the subject of this feature rather than blockers to it, so
Phase 0 proceeds — but they are defects against the constitution today, not merely
improvements. Session eviction (R10) needs fixing regardless of anything else here. The
constitution amendment the disk claim required (R7a) has landed as v2.0.0, and the Article 13
privacy notice needs its own specification.

## Project Structure

### Documentation (this feature)

```text
specs/001-concurrent-analysis/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── http-api.md      # Phase 1 output
├── checklists/
│   └── requirements.md  # Spec quality checklist
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
app/
├── main.py           # MODIFIED: handlers become submit/poll/fetch, no computation
├── sessions.py       # MODIFIED: drop raw_bytes, add result cache, never evict a live
│                     #           session, return copies from get()
│                     #           (RedisStore already deleted)
├── jobs.py           # NEW: JobQueue protocol; SQLite backing (WAL, leased atomic claim,
│                     #      position, cancel, presence) plus an in-memory one for tests.
│                     #      Works unchanged with one Uvicorn worker or several.
├── executor.py       # NEW: the claim/execute supervisor. Thread pool or process pool —
│                     #      CONTINGENT on R8a; do not build before it reports.
├── compute.py        # NEW: pure analysis entrypoint, staged so summary and lift scores
│                     #      complete and cache before the optional ML stage
├── results_cache.py  # NEW: per-session cache keyed by content hash + params
├── limits.py         # NEW: upload cap enforced before the body is buffered (middleware),
│                     #      plus the post-parse complexity cap
├── serialization.py  # NEW: explicit schema replacing pickle (Arrow/Parquet + JSON)
└── static/
    ├── app.js        # MODIFIED: submit/poll flow, queue UI, visibility-aware polling
    └── index.html    # MODIFIED: queue state (retention disclosure already landed,
                      #           but must be revised if R12 disk-backed sessions ship)

core/
├── parser.py         # MODIFIED: parse from a buffer, retain raw ingredient name,
│                     #           stop splitting compounds during parse
├── compounds.py      # NEW: split_compound_ingredients() as a DataFrame transform
└── analysis.py       # MODIFIED (CONTINGENT on R8a): vectorise map_lookback's nested
                      #          iterrows if measurement confirms it dominates

tests/
├── test_jobs.py            # NEW: queue ordering, limits, cancellation, presence, lease
│                           #      expiry — run against SQLite and in-memory backings
├── test_results_cache.py   # NEW: hit/miss, param sensitivity, session isolation
├── test_compounds.py       # NEW: transform equals old in-parser behaviour, including
│                           #      the asymmetric casing and last-segment suffix quirks
├── test_concurrency.py     # NEW: responsiveness under simultaneous analyses
├── test_serialization.py   # NEW: round-trip fidelity without pickle
├── test_limits.py          # NEW: upload cap rejects before buffering; complexity cap
├── test_sessions.py        # MODIFIED: written against the store protocol; asserts get()
│                           #      returns a copy and live sessions are never evicted
├── test_analysis.py        # MODIFIED (CONTINGENT): map_lookback equivalence if vectorised
├── test_parser.py          # MODIFIED: parser no longer splits; buffer-based parsing
└── fixtures/
    ├── generate_year_log.py  # NEW: synthetic year-scale workbook generator
    └── measure_scale.py      # NEW: per-stage timing and per-field size measurement
```

**Store test requirement**: `test_sessions.py` MUST be written against the `SessionStore`
protocol and run against every implementation that exists, asserting identical observable
behaviour — including that `get()` returns a copy, which `InMemoryStore` does not do today.
That divergence is the bug class that made memory and Redis behave differently, and it must
not recur when file-backed sessions arrive at rung 2, since a file-backed store necessarily
returns copies. `RedisStore` is deleted (R5), so there is no second backend to cover yet.

**The queue gets two backings, one of them real**: SQLite for running, in-memory for tests,
both behind the same protocol. SQLite is what makes rung 2 free on the queue side.

**Structure Decision**: The existing flat top-level package layout (`app/`, `core/`,
`ai/`, `ml/`, `report/`) is kept — it is coherent, the codebase is ~2,600 lines of source,
and restructuring would obscure this feature's diff. New concerns become new modules
inside `app/`, except compound splitting, which belongs beside the parser it is being
lifted out of. `app/main.py` should shrink substantially: `_run_analysis` and
`_build_results_json` move into `app/compute.py`, where they run in a pool worker instead
of a request handler.

## Post-Design Constitution Re-Check

*Re-evaluated after Phase 1, then again after external review. Design artifacts:
[research.md](./research.md), [data-model.md](./data-model.md),
[contracts/http-api.md](./contracts/http-api.md), [quickstart.md](./quickstart.md).*

| Gate | Post-design status |
|------|--------------------|
| Request-path discipline | **Mostly resolved.** Analysis moves to the executor; handlers submit, poll, or read cache. Two residues: parsing is awaited in-request under R11, and upload receipt still occupies the single acceptor until the R9 cap rejects it. |
| No recomputation when inputs unchanged | **Resolved by design.** Per-session `ResultCache` keyed by params signature, plus R1 severing `summary` from the ML block. |
| Retention is stated accurately | **Resolved as scoped.** Constitution v2.0.0 (2026-08-13) replaced the unachievable absolute with bounded, verifiable obligations (R7a, R12). Redis was withdrawn as a sanctioned location in the same amendment, and `RedisStore` plus the `redis` extra were deleted as a consequence. The bump is MAJOR because a plan that passed the Principle IV gate under v1.1.0 (Redis-backed sessions) now fails it — narrowing what a principle permits, not deleting code, is what makes it backward-incompatible. FR-024's disclosure landed with it. **Not resolved**: the app has no Article 13 privacy notice at all, which is a separate and larger obligation than FR-024 (see below). |
| Article 13 privacy notice | **Not resolved, and out of scope.** Distinct from FR-024. Needs controller identity, lawful basis, the Article 9(2) condition, data-subject rights, a complaint route and a processor list — none derivable from the code. Warrants its own specification before the tool is announced. |
| No cross-user data flow | **Held.** Cache is session-owned; identical uploads across sessions still compute separately. Foreign `job_id` returns `404`, not `403`. Asserted by test, not left to structure. |
| Deterministic core survives failure | **Conditional on staging.** Isolating `ml` in the payload stops an ML error propagating but not the ML wait. Resolved only if the job is staged so summary and lift scores cache before training begins (contracts/http-api.md). Previously recorded as "strengthened", which was too confident. |
| Uncertainty flags reach the interface | **Held, with a caveat.** Cached payloads are identical to fresh ones, so the flags cannot be lost to caching. FR-018 must be read as equivalence of analysis content, not byte-identity of PDFs, which embed timestamps. |
| Session availability under load | **Resolved by decision, not yet by design.** R10: refuse new sessions rather than evict live ones, and size `MAX_SESSIONS` from measurement. Applies to both backends. |
| Anonymous use unaffected | **Held.** Jobs, polling and cache all key on the existing anonymous cookie. |
| Complexity justified | **Held, and reduced twice.** R5 rescoped after two review rounds: Redis dropped entirely, the queue backed by stdlib SQLite with an in-memory twin for tests, and one session-store implementation. Net effect is less machinery than the first draft, not more. |
| Tests and evidence | **Specified, and R8a is now blocking.** The executor choice may not be finalised before the bottleneck is measured. |

## Review Findings

An independent adversarial review by two models produced findings that changed this plan.
Recorded here so the reasoning is not lost.

**Corrected factual claims**:

- `summary` is not computed from `bac_df` alone — it reads `meals_df` and `lookback_df`
  (`app/main.py` `_build_results_json`, summary block). The "needs no model" claim stands; "needs only `bac_df`" did not.
- Four consumers read the frames R4 proposed dropping (R4a). An earlier open-items entry
  asserted the opposite.
- Starlette spools large uploads to disk before the handler runs (R7a), so R7 cannot make
  Principle IV's claim true.
- `map_lookback` is a nested `iterrows` and may be the real bottleneck (R8a), so R2's
  executor choice was made without evidence.
- The 30-second presence grace window would false-abandon backgrounded mobile tabs (R3) —
  the same failure mode used to reject WebSockets.
- `MAX_SESSIONS` eviction is a spec breach, not a knob (R10).
- The upload cap cannot be enforced from inside a handler taking `UploadFile` (R9).

**Pre-existing bug found, unrelated to this feature**: `exclude_proteins` defaults to
`False` in `AnalysisParams` (`app/main.py` `AnalysisParams.exclude_proteins` default) but `True` on the `/upload` handler
(`app/main.py` `upload_file`), so an upload and a subsequent recompute with untouched settings apply
different protein filtering. Worth fixing, and worth a regression test.

**Dissent recorded, and accepted**: both reviewers considered building two implementations of
every seam premature. R5 was rescoped twice in response and now goes further — Redis is
dropped from the plan entirely, and the queue is backed by SQLite, which delivers the
cross-process semantics the seam existed to protect while being stdlib. Both reviewers also
rated vectorising `map_lookback` above the pool in value; R8a settles that with measurement
rather than argument, and the scaling ladder now treats it as the primary lever.

**Out of scope for this feature but newly documented — no GDPR privacy notice exists.**
External review is right that FR-024 delivers a *retention* disclosure, not an Article 13
privacy notice. For special-category health data the following are absent entirely: the
identity and contact details of the data controller, the lawful basis under Article 6, the
Article 9(2) condition relied on (explicit consent is the realistic one), the data subject's
rights and how to exercise them, the route to complain to a supervisory authority, and the
recipients or processors involved (the hosting provider and the CDN). None of this can be
drafted from the code — it needs decisions only the operator can make, starting with who the
controller is. Recorded here so it is a known gap rather than an oversight; it warrants its own
specification and should be settled before the tool is announced to a 3,000-member group.

**Also outside this feature, but fixed while it was found**: the ML section of the interface
described associations causally — "that thing **raises** your BAC", a chart axis reading
"Raises BAC", and a verdict claiming effects were "stable enough to guide dietary experiments".
Principle I requires association language and forbids directing diet, so the wording was
corrected to associative phrasing pointing at a clinician conversation. The deterministic
template engine (`ai/template_engine.py:242`) was already phrased correctly; only the ML
surface and `ml/train.py` verdict strings were wrong.

**Cleanup that followed from dropping Redis — already done**: `RedisStore`, the `REDIS_URL`
branch in `create_store()`, the `redis` optional dependency, and the README's `--extra redis`
instruction are removed; `uv.lock` is regenerated and the suite passes at 120 of 121 collected. The
alternative — leaving an untested backend that activated on `REDIS_URL` and crashed on
`import redis`, because neither `Dockerfile:14` nor `render.yaml:5` installed the extra — was
worse than either keeping or removing it properly.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| Explicit job queue | FR-001 to FR-015 require concurrent analyses, a queue position, a wait estimate, cancellation, and two capacity limits. None is expressible while work runs inline in handlers. | A broker-backed queue (Celery/RQ/arq) was rejected as unjustified infrastructure at this scale — see research.md R2. Required regardless of which executor is chosen. |
| Process pool (**contingent**) | Justified only if R8a confirms training dominates. Threads cannot parallelise `iterrows`/`apply` loops, which hold the GIL. | If `map_lookback` dominates instead, vectorising it moves the residual cost into NumPy, which releases the GIL — making a thread pool sufficient and the process pool unjustifiable. Do not build before R8a reports. |
| Vectorising `map_lookback` (**contingent**) | Possibly the single largest performance win, and a prerequisite for the thread-pool option. | Leaving the nested `iterrows` and buying processes to run it was the original plan; review argued that scales the wrong thing. Deferring to a separate feature was rejected because it determines this feature's architecture. |
| Per-session result cache | FR-016 to FR-020 and SC-004. | Recomputing is the current behaviour and is precisely what the spec forbids. A process-wide cache keyed on content hash alone was rejected: it would serve one person's results to another who uploaded an identical file, violating FR-020. |
| SQLite-backed queue rather than an in-process structure | The user requires a design that survives 10→100 parallel users without being redone. SQLite is the one choice that makes adding Uvicorn workers (rung 2) cost nothing on the queue side, and it is stdlib. | A plain `asyncio.Queue` was rejected because it is exactly what forces a rewrite to add workers. A Redis or broker-backed queue was rejected as infrastructure for a machine count that does not exist. |
| Non-pickle serialisation | An explicit schema is needed for file-backed sessions at rung 2 regardless, and pickle deserialisation is arbitrary code execution over Article 9 data. | Deleting `RedisStore` already removed the only pickle in the session path, so this is no longer urgent — but Parquet/Arrow is the format rung 2 needs, and choosing it late means every field added meanwhile assumed pickle could carry it. |
| Compact derived bundle replacing `lookback_df` (**contingent, now likely unnecessary**) | R4a: four consumers read the frames R4 drops, so shrinking requires an alternative data source, not just deletion. | With session-scoped disk sanctioned (R12), retaining `lookback_df` is viable and much less work. Decide on R8's measured sizes — "do nothing" is now a live option. |
| Lifting compound splitting out of the parser | Removes the only remaining purpose of `raw_bytes`, which is the largest single retained object. | Keeping `raw_bytes` was rejected because it dominates session memory for exactly the year-scale logs SC-006 targets. Requiring re-upload on toggle was rejected as a user-experience regression the refactor makes unnecessary. |
