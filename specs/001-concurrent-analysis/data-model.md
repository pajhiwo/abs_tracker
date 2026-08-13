# Phase 1 Data Model: Concurrent Analysis Without Waiting

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-08-13

Entities below map to the Key Entities section of the spec. There is no database for domain
data. All state is session-scoped and time-limited.

**Where state lives** (revised; earlier drafts claimed all state is in-process and nothing
reaches disk, and both were wrong):

| State | Rung 1 (this feature) | Rung 2 (if needed) |
|-------|----------------------|--------------------|
| Session frames and metadata | Process memory | Session-scoped files, Parquet frames plus JSON scalars (R12) |
| Queue and job records | SQLite, WAL mode (R5) | Unchanged — already cross-process |
| Result cache | Process memory, bounded per session | Alongside session files |
| Uploaded bytes | Three copies today, not one: framework temp spool above 1 MiB (R7a); an application `NamedTemporaryFile` written on **every** upload and again on every re-parse (`app/main.py` `upload_file` and `_run_analysis`, both via `NamedTemporaryFile`); and `session.raw_bytes` retained in memory for the whole session (`app/main.py` `upload_file`, `session.raw_bytes` assignment, `app/sessions.py:40`). R11 and R4 shorten this. | Session-scoped file, deleted at session end |

The accurate claim is therefore: data is held in memory and in a session-scoped working area,
never deliberately persisted beyond the session, deleted when the session ends, and never
backed up. This is now the constitution's own wording — Principle IV was amended to match in
**v2.0.0**, since the previous absolute was never achievable given framework upload spooling.

**Cleanup obligation** that comes with disk, and required by the amended principle: a TTL
sweeper plus a sweep at startup for directories orphaned by a crash. Files do not expire by
themselves the way memory does, and this is the main new failure mode.

**Deletion is unlinking**, not erasure. The disclosure must not imply otherwise (Principle IV).

---

## Session

Anonymous, time-limited context for one person's visit. Identified by an opaque cookie
value; carries no identity (Principle VI).

Replaces the current `SessionData` (`app/sessions.py:24-40`).

| Field | Type | Notes |
|-------|------|-------|
| `meals_df` | DataFrame \| None | Parsed meals, retained. Now carries `raw_ingredient` alongside `ingredient` (R6). |
| `bac_df` | DataFrame \| None | Parsed readings, retained. |
| `med_periods` | dict \| None | Medication periods, retained. |
| `content_hash` | str \| None | Digest of the uploaded bytes. Change detection only, never a shared cache key. |
| `filename` | str \| None | Display name; also the `user_id` passed to `extract_features`. |
| `params` | AnalysisParams | Most recent settings. |
| `derived` | DerivedBundle \| None | Compact replacement for the dropped intermediate frames — see below. **Contingent**: R12 makes retaining `lookback_df` viable, in which case this entity is unnecessary. |
| `results` | ResultCache | Bounded per-session cache of built payloads. |
| `active_job_id` | str \| None | The job this session is currently waiting on, if any. |

**Removed from the current model**: `raw_bytes` (R4), which becomes obsolete once R6 lands or
once a session-scoped upload file replaces it (R12).

**Contingent on measurement**: whether `lookback_df`, `scores_all`, and `scores_by_period` are
replaced by DerivedBundle or simply retained. They cannot merely be deleted — four endpoints
read them (R4a) — but with session-scoped disk available, retaining them is both viable and
much less work. R8 sizes decide.

**Lifecycle**: created on first request; TTL refreshed on access; on expiry the session
and everything reachable from it is discarded (FR-022). Expiry MUST also cancel any queued
or running job belonging to it (FR-015).

**Capacity (R10)**: a live session MUST NOT be evicted to make room for a new one. When at
capacity the application refuses *new* sessions with the at-capacity response. This differs
from current behaviour, where `InMemoryStore.set` deletes the oldest-touched session
(`app/sessions.py:76-78`). The now-deleted `RedisStore` had no cap at all, so the two backends
disagreed under load (R5, R10).

**Store semantics**: `get()` MUST return a copy in every implementation. `InMemoryStore`
returns the live object today, while the deleted `RedisStore` returned an unpickled copy — so
in-place mutation without a following `set()` succeeded in one backend and silently lost the
write in the other. Fixing this now matters because a file-backed store at rung 2 necessarily
returns copies, and call sites that rely on live-reference mutation would break on the switch.

**Validation**:
- A session with `bac_df is None` has no analysis and MUST yield the "upload a file first"
  response rather than an empty result.
- `content_hash` and `meals_df`/`bac_df` are set together or not at all.

---

## DerivedBundle

Compact stand-in for the dropped frames, required because four consumers read them (R4a).

| Field | Replaces | Consumers |
|-------|----------|-----------|
| `lift_scores` | `scores_all`, `scores_by_period` | `/report`, `/report/pdf`, `/predict` |
| `ingredient_readings` | `lookback_df` — per ingredient, the set of `bac_idx` values it appears in | `/ingredient/{name}`, `detect_combinations` |
| `lookback_pair_count` | `len(lookback_df)` | `summary.lookback_pairs` |

**Why this shape**: `lift_scores` is one row per ingredient, so retaining it is cheap.
`lookback_df` is one row per (reading × ingredient in window), which is the object R4 is
trying to remove — but its consumers need only the membership relation, not the pair table
with timings. `ingredient_readings` carries that relation at a fraction of the size, and the
pair count becomes a stored integer.

**Consumers take DataFrames today**: `generate_report`, `predict_risk` and
`detect_combinations` accept frames (`app/main.py` `report` and `predict_meal`). Either they are
adapted to the bundle, or the bundle is rehydrated on use. Rehydrating lift scores is cheap;
rehydrating a full `lookback_df` would defeat the purpose, so `detect_combinations` needs
adapting to the membership form.

**Contingent**: R8 must confirm the saving is real before this is built. Simply retaining
`lookback_df` is cheaper to implement and remains the fallback.

---

## AnalysisParams

The settings that, with the upload, fully determine a result. Already exists
(`app/main.py` `AnalysisParams` defaults); becomes the cache key component.

| Field | Type | Default |
|-------|------|---------|
| `hours` | float | 3.0 |
| `min_obs` | int | 3 |
| `split_compounds` | bool | true |
| `exclude_proteins` | bool | false |
| `episode_threshold` | float | 2.0 |

**Signature**: a stable, order-independent digest over all five fields. Adding a field
without adding it to the signature would serve stale results, so the signature MUST be
derived from the model rather than hand-maintained.

---

## AnalysisJob

One queued or running unit of work. Lives in the job registry, not the session; the
session holds only `active_job_id`.

| Field | Type | Notes |
|-------|------|-------|
| `job_id` | str | Opaque, unguessable. |
| `session_id` | str | Owner. Enforces FR-020 isolation. |
| `params_signature` | str | Where the result will be cached. |
| `state` | JobState | See transitions below. |
| `queued_at` | float | For wait estimation. |
| `started_at` | float \| None | |
| `last_seen_at` | float | Updated by every status poll — the presence signal (R3). |
| `lease_expires_at` | float \| None | Set when claimed. A `running` job whose lease lapses without renewal is reclaimable, so a dead worker cannot strand it. Required by the SQLite backing rather than merely anticipated, since a claim there genuinely can outlive the claiming process (R5). |
| `error` | str \| None | User-facing message when `failed`. |

**What a job does NOT carry**: uploaded bytes. Parsing happens before enqueueing (R11), so a
job references a session that already holds parsed frames. This is what lets the queue span
sessions while carrying no health data, and what makes "`raw_bytes` is not retained" true
literally rather than by convention.

**Staging (Principle II)**: a job completes in two stages. Stage one produces `summary` and
lift scores and caches them; stage two runs the optional ML block and updates the cached
payload. A person can read their report after stage one, so optional intelligence never
blocks report generation. Without this, `/report` returning `202` until the whole job
finishes would make ML a blocker, which the constitution forbids.

**States and transitions**:

```text
queued ──> running ──> partial ──> complete
   │          │           │
   │          └───────────┴──────> failed
   ├──> cancelled     (person cancelled — FR-009)
   ├──> abandoned     (absent at turn — FR-013)
   └──> expired       (session gone — FR-015)
```

`partial` means stage one is cached and servable while stage two runs. A `failed` stage two
leaves the job `partial` with an error recorded against the `ml` block only.

`queued` is the only state from which `cancelled`, `abandoned` or `expired` are reachable
by policy; a `running` job may still reach `expired` if its session dies mid-flight, in
which case its result is discarded rather than cached.

**Validation**:
- A job MUST NOT be readable by any session other than its owner, including by guessing
  `job_id`.
- `position` is derived from queue order at read time, never stored — a stored position
  goes stale the moment anything ahead of it finishes.

---

## AnalysisQueue

Ordered set of `queued` jobs plus the executor running them.

**Backing store**: SQLite in WAL mode (R5), with the correctness conditions in R5a — a
single-statement atomic claim, `BEGIN IMMEDIATE` writes, `busy_timeout`, bounded retry, and
every call offloaded off the event loop because `sqlite3` blocks. Ordering, position queries,
atomic leased claims and cancellation are all transactions, which makes them correct across
processes as well as within one, so adding Uvicorn workers at rung 2 requires no queue changes.
An in-memory implementation of the same protocol exists for tests.

**Polling is not read-only** (R5b, corrected after review): if presence is recorded by updating
`last_seen_at` on each poll, every poll is a write and SQLite serialises writers. Presence must
therefore be debounced, or read from the session store instead, before the write volume can be
called insignificant. Unresolved until measured.

| Property | Notes |
|----------|-------|
| `max_concurrent` | Executor width. Jobs beyond this wait. Value and executor type contingent on R8a. |
| `max_waiting` | Cap on queue length (FR-010). |
| `max_estimated_wait` | Cap on projected wait for a new arrival (FR-010). |
| `recent_durations` | Rolling window of completed job durations, for estimation. |

**At-capacity rule**: a submission is refused when `len(waiting) >= max_waiting` **or**
projected wait `> max_estimated_wait`, whichever trips first (FR-010, FR-011).

**Wait estimation**: `position / max_concurrent × median(recent_durations)`. Deliberately
crude. It MUST be revised on every poll rather than counted down client-side, so a bad
estimate corrects itself instead of stalling at zero (spec edge case; SC-012).

Two known weaknesses, both to be addressed before SC-012 can be met:

- **Cold start.** With `recent_durations` empty after a deploy, there is no estimate at all.
  A seeded prior from the R8 measurements is needed, or the first arrivals get a position
  without a time.
- **Mixed job sizes.** A median over a mixture of 3-day and year-scale logs describes
  neither. SC-012's ±50% band will fail whenever sizes mix, so the estimate should be
  conditioned on a size class derived from the parsed frames.

**Interaction between the two caps**: the caps in FR-010 must be consistent with each other.
At four-wide concurrency and 60-second jobs (SC-006), a 5-minute wait cap is reached at
roughly position 20 — so a 50-waiting cap would never fire and is dead letter. Both numbers
come from R8; they must be chosen together rather than independently.

**Duplicate suppression**: a submission whose `(session_id, params_signature)` matches an
already-queued or running job returns that job rather than enqueueing a second — this is
what stops a double-click consuming two places (spec edge case).

**Settings changed mid-analysis**: a different `params_signature` is a genuinely different
job, so it is enqueued rather than suppressed. Since `Session.active_job_id` holds one job,
submitting a second MUST supersede the first: the earlier job is cancelled and its capacity
released, so the results a person eventually sees correspond unambiguously to their latest
settings (spec edge case).

---

## ResultCache

Per-session map from params signature to a built payload.

| Property | Notes |
|----------|-------|
| Key | `params_signature`, scoped by the owning session. |
| Value | The results payload, the report document, and the rendered PDF — each cached once produced. |
| Bound | Small (3–5 entries), evicting least-recently-used. |
| Invalidation | Cleared entirely when `content_hash` changes (FR-019). |

**Three cached artifacts, not one**: FR-016 and FR-017 name the report and the PDF
separately, and they are genuinely different documents. `/results` serves the payload;
`/report` serves `generate_report` output plus `detect_combinations`; `/report/pdf` serves
`generate_pdf` of the report. Caching only the payload would leave `/report` recomputing.

**Isolation (FR-020)**: the cache is reachable only through its session. Two sessions
uploading byte-identical files have identical `content_hash` values and still compute
separately. This is a deliberate cost: a shared cache would be faster and would violate
the privacy model, so the isolation MUST be asserted by test rather than left to
structure.

**Fidelity (FR-018, Principle III)**: a cached artifact MUST be equivalent in *analysis
content* to a fresh computation from the same inputs. `low_confidence`, `always_present` and
observation counts travel inside the payload, so an equivalence test protects both
requirements at once.

**Not byte-identity for PDFs**: `generate_pdf` embeds a creation timestamp, so two
independently rendered PDFs of identical analysis will differ as bytes. Byte comparison is
valid for a *cached* PDF returned twice (the same bytes are replayed), and invalid as a test
of fresh-versus-cached equivalence. The equivalence test must compare the report data, not
the rendered file.

---

## ResultPayload

What a pool worker returns and the cache stores — the existing `_build_results_json`
output (`app/main.py` `_build_results_json`, ML block), with one structural change.

**Change**: `summary` is computed independently of `ml`, so the report paths can read
`summary` without touching the model (R1). The `ml` block remains optional and MUST
degrade to absent-or-error without affecting the rest of the payload (Principle II), and it
is produced in stage two so it cannot delay the rest.

Fields unchanged: `filename`, `hours`, `min_obs`, `split_compounds`, `summary`,
`bac_readings`, `medication_periods`, `lift_scores_overall`, `lift_scores_by_period`,
`lookback_by_reading`, `meal_carbs`, `period_lifts`, `ml`.

**`summary` composition, corrected**: it reads `bac_df` for the reading aggregates,
`meals_df` for `total_ingredients` and `unique_ingredients`, and the lookback for
`lookback_pairs` (`app/main.py` `_build_results_json`, summary block). An earlier draft described it as `bac_df` only.
It needs no model, which is R1's actual point.

**Size note**: `lookback_by_reading` expands into JSON the same relation whose DataFrame form
R4 drops, and it is cached 3–5 times over by `ResultCache`. It could therefore reintroduce
most of the volume R4 removes, for exactly the year-scale logs SC-006 targets. Whether to
trim it, page it, or derive it on demand from `DerivedBundle.ingredient_readings` is an R8
measurement — but it must not be forgotten, because the shrink is not real until this is
settled.

**`exclude_proteins` default (pre-existing bug)**: `AnalysisParams` defaults it to `False`
(`app/main.py` `AnalysisParams.exclude_proteins` default) while the `/upload` handler defaults it to `True` (`app/main.py` `upload_file`), so
an upload and a subsequent recompute with untouched settings filter differently. The
`params_signature` must be computed from resolved values, and the defaults should be
reconciled with a regression test.

---

## Entity relationships

```text
Session 1 ──── 0..1 AnalysisJob    (active_job_id; job back-references session_id)
Session 1 ──── 1    ResultCache    (owned; never shared)
Session 1 ──── 1    AnalysisParams
Session 1 ──── 0..1 DerivedBundle  (replaces the dropped intermediate frames)
ResultCache 1 ── 0..n ResultPayload + ReportDocument + PDF  (keyed by params signature)
AnalysisQueue 1 ─ 0..n AnalysisJob (ordered; across all sessions)
```

The queue is the only structure spanning sessions, and it carries job metadata only — never
parsed data, results, or uploaded bytes. That holds because parsing happens before
enqueueing (R11): a job points at a session whose frames already exist, rather than carrying
an upload through an unbounded wait.
