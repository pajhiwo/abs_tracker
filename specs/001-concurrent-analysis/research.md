# Phase 0 Research: Concurrent Analysis Without Waiting

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-08-13

> **On citations in this document.** References to `app/main.py` and `ml/train.py` now name
> symbols rather than line numbers. Line numbers were tried and abandoned: they went stale three
> times in a single day — when the plan was revised, when `RedisStore` was deleted, and when the
> page template moved out of the static directory — and each round of review spent effort on
> corrections that taught nothing. Symbols survive edits above them. Line numbers remain only
> for `core/parser.py` and `app/sessions.py`, which this feature has not touched; treat those as
> navigation aids and re-verify before acting on them. Cite symbols in anything added from here.

Each entry records the decision, why it was chosen, and what was rejected. Numbers marked
*to be measured* are deliberately not fixed here — Principle V requires evidence, and R8
defines the measurement that produces them.

---

## R1. Where does the blocking work actually happen?

**Finding**: Three separate problems, often conflated.

1. **Parsing on the event loop.** `upload_file` is `async def` and calls `parse_log`
   (`app/main.py` `upload_file`, `NamedTemporaryFile`), which runs openpyxl over the whole workbook.
2. **Training on the event loop.** `_build_results_json` calls `extract_features` and
   `train_personal_model` with `bootstrap=True, n_bootstrap=50` (`app/main.py` `_build_results_json`, ML block).
   `train_personal_model` runs `LassoCV` plus leave-one-week-out cross-validation
   (`ml/train.py` `train_personal_model` via `_leave_one_week_out_mae`), so it is the dominant cost.
3. **Training on paths that do not need it.** `/report` (`app/main.py` `report`) and
   `/report/pdf` (`app/main.py` `report_pdf`) call `_build_results_json(session)["summary"]` and
   discard everything else. `summary` is ten aggregate numbers (`app/main.py` `_build_results_json`, summary block) and
   needs no model at all. **Correction**: an earlier draft of this item said those numbers come
   from `bac_df` alone. They do not — `total_ingredients` and `unique_ingredients` read
   `meals_df`, and `lookback_pairs` reads `lookback_df`. The "needs no model" conclusion holds;
   the "needs only `bac_df`" premise was wrong, and R4a depends on the difference.

**Decision**: Split `_build_results_json` so that `summary` is computed independently of
the ML block, and move all three costs off the request path.

**Why this matters more than it looks**: item 3 means the "recomputation" the spec
complains about is not merely wasteful, it is *gratuitous* — a full LASSO retrain with 50
bootstrap iterations to produce numbers derived from a DataFrame the session already
holds. Fixing item 3 alone will make `/report` and `/report/pdf` dramatically faster even
before caching exists, and it is a small, independently shippable change.

**Alternatives considered**: Caching `summary` while leaving the call structure intact —
rejected because the coupling is the defect; the report path should never have been able
to trigger training.

---

## R2. What executes the analysis?

**Decision — RESOLVED by the T004 measurement (see R8a): a `ThreadPoolExecutor`, contingent
on vectorising `map_lookback`.** The measurement showed that essentially all of the wall time
is a single O(readings × meals) pure-Python loop in `map_lookback` — 436 seconds at twelve
months — while every NumPy/sklearn stage (LASSO training included) stays around two seconds.
Once `map_lookback` is rewritten as a vectorised join, no long GIL-bound Python stretch
remains: the residual cost is parse (I/O plus openpyxl) and sklearn training, both of which
release the GIL. A thread pool therefore gives the needed parallelism without a process pool's
DataFrame pickling across the boundary, and it keeps the in-memory session store trivially
shared. The process-pool rationale below is retained for the record because it was correct
*for the un-vectorised code*; vectorisation is what changes the answer, and it is now a
blocking task (T019), not an option.

**Rationale**:

- **Processes, not threads.** The hot loops are `iterrows`/`apply` over pandas objects
  (`app/main.py` `_build_results_json` and its nested `iterrows` loops; `core/analysis.py`), which execute Python bytecode
  and hold the GIL. Threads would leave the pool serialised on exactly the work that
  currently blocks. NumPy and scikit-learn release the GIL internally, but they are not
  where all of the time goes.
- **One Uvicorn worker at rung 1.** The event loop, once it is no longer computing, is only
  multiplexing I/O, which it does well. One worker also keeps sessions naturally global and
  consistent. This is a starting point rather than a constraint: the SQLite queue and
  file-backed sessions in R5/R12 exist so that adding workers is a configuration change.
- **An explicit queue rather than the pool's implicit one.** `ProcessPoolExecutor` will
  happily accept unbounded submissions and gives no visibility into ordering. FR-006
  (position), FR-009 (cancellation) and FR-010 (two capacity limits) all require a queue
  the application can inspect and manipulate.

**Alternatives considered**:

- **Multiple Uvicorn workers, no pool.** Gives CPU parallelism for free, but fragments
  session state across processes and makes a global queue position impossible without shared
  coordination. Not rejected outright — it is rung 2 of the R5 ladder, reachable once the
  queue is in SQLite and sessions are file-backed. Deferred rather than dismissed, because at
  rung 1 the pool gives the same parallelism with less to build.
- **Broker-backed queue (Celery, RQ, arq).** The standard answer at larger scale, and the
  right one if this ever spans machines. Rejected for now under the constitution's
  requirement that added complexity be justified: it introduces a broker, worker
  processes with separate lifecycles, and a deployment topology, to solve a problem a
  process pool solves inside one container. R5 records the threshold at which to revisit.
- **`run_in_threadpool` only.** The one-line change, and genuinely correct for the I/O
  portions. Rejected as insufficient for the GIL-bound majority, though it remains the
  right tool for incidental blocking calls.

**Sizing**: pool workers default to `min(cpu_count, 4)` with an environment override.
*To be measured* — R8 establishes whether memory or CPU binds first.

**Blocked on R8a.** External review pointed out that this decision was taken as though the
bottleneck were known. It is not (see R8a). If the dominant cost is an O(readings × meals)
Python loop, vectorising it may deliver more than four processes would, and would move the
remaining time into NumPy — which releases the GIL and therefore reopens threads as a
simpler alternative to a process pool. **The pool design MUST NOT be finalised before R8a
reports.** The queue is needed either way; the executor behind it is what is contingent.

---

## R3. How is a person known to be still waiting?

**Decision**: The status poll is the presence signal. A queued job records the time of its
most recent status request; when the job reaches the front, it runs only if that timestamp
is within a grace window, and is otherwise dropped with state `abandoned`.

**Rationale**: FR-012 through FR-014 require holding a place while someone is briefly away
but not spending capacity on someone who has gone. The client must poll anyway to satisfy
FR-007 (position updates), so polling already carries exactly the signal needed, with no
separate heartbeat, no WebSocket, and no server-side connection state. A reload issues a
new poll within a second or two and so preserves the place, which is the behaviour the
user chose.

**Grace window**: minutes, not seconds. An earlier draft proposed a 2s poll with a 30s
grace window; external review correctly identified that as self-defeating. Mobile browsers
throttle or suspend background timers on backgrounded tabs and locked screens, routinely
past 60s — which is the *same* failure mode used to reject WebSockets above. A 30s window
would mark an interested person absent, drop their job (FR-013), and hand away the slot
being held for them, producing exactly the "the site keeps losing my place" impression the
queue exists to prevent.

**Revised design**:

- Grace window measured in minutes (provisionally 5), not tied to the poll interval.
- Client uses the Page Visibility API to poll on resume, so a returning tab reports presence
  immediately rather than waiting for the next timer tick.
- A queued job is never dropped before its session expires unless the person explicitly
  cancels or the grace window has clearly lapsed.
- Poll interval server-driven via `poll_after_seconds`, so the queue can back clients off
  during a spike. At a 2s interval, 50 waiting people generate ~25 requests/second of empty
  polling against the one worker — during precisely the spike this feature targets.

**Tension to accept**: a generous grace window means capacity is briefly held for people
who have genuinely left. That is the correct trade — a wasted slot costs seconds, a false
abandonment costs a user. Explicit cancellation and `beforeunload` (best-effort only)
recover capacity in the common case.

**Alternatives considered**:

- **WebSocket or SSE connection as presence.** More precise and removes polling traffic,
  but ties presence to a connection that proxies and mobile networks drop routinely —
  which would fail the "phone locks its screen" case the user explicitly wanted protected.
- **Cancel on `beforeunload`.** Unreliable (not fired on crash, kill, or connection loss)
  and cannot distinguish a reload from leaving. Useful as an *optimisation* to release
  capacity sooner, never as the mechanism.

---

## R4. What is retained, and what is recomputed?

**Decision**: Retain parsed frames and a small bounded cache of built result payloads.
Never retain `raw_bytes`, `lookback_df`, `scores_all`, or `scores_by_period`.

**Rationale**, field by field, against `app/sessions.py:28-40`:

| Field | Decision | Reason |
|-------|----------|--------|
| `raw_bytes` | **Drop** | Whole workbook, retained solely so `_run_analysis` can re-parse when `split_compounds` flips (`app/main.py` `_run_analysis`). R6 removes that need. |
| `lookback_df` | **Drop from the session** | One row per (BAC reading × ingredient in window), so it grows with the product of logging density and window length — the fastest-growing object in the session. But see the dependency problem below: dropping it is not free. |
| `scores_all`, `scores_by_period` | **Drop from the session** | Derived from `lookback_df` in one pass. Same caveat. |
| `meals_df`, `bac_df`, `med_periods` | **Retain** | Needed to recompute cheaply when `hours`, `min_obs`, `exclude_proteins` or `episode_threshold` change, which is the common interaction. Re-parsing for those would be far more expensive than holding them. |
| `filename`, params | **Retain** | Small, and `filename` is currently passed as `user_id` to `extract_features` (`app/main.py` `_build_results_json`, ML block). |

### R4a. The dependency problem this creates (found by external review)

**Four consumers read the frames R4 drops.** An earlier draft of this document claimed
they "read retained frames"; that was wrong.

| Consumer | Needs | Where |
|----------|-------|------|
| `summary.lookback_pairs` | `lookback_df` | `app/main.py` `_build_results_json`, summary block (`lookback_pairs`) |
| `/report`, `/report/pdf` | `scores_all`, `scores_by_period`, `lookback_df` | `app/main.py` `report` and `report_pdf` |
| `/predict` | `scores_all` (`lookback_df` is passed but unused by `predict_risk`) | `app/main.py` `predict_meal` |
| `/ingredient/{name}` | `lookback_df` only — it has no other data source | `app/main.py` `ingredient_detail` |

Related correction: `summary` is **not** computed from `bac_df` alone. It also reads
`meals_df` for `total_ingredients` and `unique_ingredients`, and `lookback_df` for
`lookback_pairs` (`app/main.py` `_build_results_json`, summary block). R1's claim that `summary` needs no model stands;
the claim that it needs only `bac_df` does not.

**Decision**: the analysis job returns a **compact derived bundle** alongside the result
payload, and the session retains that bundle rather than the full frames.

- **Lift scores** are already in the payload as records
  (`lift_scores_overall`, `lift_scores_by_period`). `generate_report`, `predict_risk` and
  `detect_combinations` take DataFrames, so either they accept records, or the bundle is
  rehydrated to a DataFrame on use. Rehydrating scores is cheap — one row per ingredient,
  not per (reading × ingredient).
- **`lookback_df` is the hard case.** `/ingredient/{name}` needs the reading indices an
  ingredient appears in, and `detect_combinations` needs ingredient sets per reading.
  Neither needs the full pair table. Retain a **compact index** instead: per ingredient,
  the set of `bac_idx` values. That is the same information at a fraction of the size, and
  `lookback_pairs` is then a stored count rather than a `len()`.
- **`lookback_by_reading`** in the payload is the inverse of that index and is the item
  most likely to dominate cached-payload size. Whether to keep, trim, or derive it on
  demand from the compact index is an R8 measurement, not a guess.

**Consequence for scope**: this is a larger change than "stop persisting some fields". The
alternative — retain `lookback_df` and accept the memory — remains available and should be
compared against measured numbers (R8) before the compact-index work is committed to.

**On storing to disk instead of memory**: previously rejected here on the grounds that it
changes where data lives rather than how much. **Superseded by R12.** That reasoning held only
while memory was the binding constraint; disk also unlocks multiple workers on one machine,
which memory cannot, and that is worth more than the volume argument. The cleanup-job cost is
real and now accounted for. Shrinking retained volume remains worthwhile but is no longer the
only lever, which is why R4a is downgraded to contingent.

---

## R5. What shared state is needed, and where does Redis fit?

**Decision** (revised three times; this is the settled version): **Redis is not adopted.**
Put every piece of shared state behind an interface, and back the queue with **SQLite** and
sessions with memory, with session-scoped files available as the scaling lever (R12).

### The scaling ladder, and why Redis is not on it

The user's actual requirement is surviving a spike from 10 to 100 parallel users on day one,
with no way to predict which. The decisive realisation is that **100 simultaneous analyses
cannot be made fast, only orderly**: at 60s per analysis and four-wide concurrency, 100
arrivals is a 25-minute queue regardless of infrastructure. Redis does not shorten it by one
second — it distributes work across machines, and there is one machine.

What shortens it is per-analysis time, which is why R8a gates everything. Vectorising
`map_lookback` plausibly turns 60s into low single digits, which turns that 25-minute queue
into under a minute and means the queue rarely engages at all. **Performance work dominates
scaling work at this scale.**

| Rung | Change | Ceiling |
|------|--------|---------|
| 1 (now) | Vectorise if measured; SQLite queue; sessions in memory; one Uvicorn worker | Bounded by single-analysis time and RAM for live sessions |
| 2 (if needed) | Session frames move to session-scoped files (R12); add Uvicorn workers | Bounded by one machine's cores and disk |
| 3 (only if rung 2 is exhausted) | Shared store — Redis or Postgres — behind the same interfaces | Multi-machine |

Rung 2 is the lever that used to require Redis. Workers on one box share a filesystem even
though they do not share memory, so disk-backed sessions unlock multi-worker without new
infrastructure. Rung 3 is a real threshold, documented, and not this feature's problem.

**Rung 2 has a trap that an earlier draft of this ladder missed** (found by external review):
if each of N Uvicorn workers owns an executor of M processes, total CPU demand is N × M, not M.
Four workers with four-wide pools is sixteen concurrent analyses on a box that may have four
cores — every analysis then runs slower, the wait estimate in FR-007 becomes meaningless, and
memory use multiplies by the same factor. Adding workers would make throughput *worse* while
appearing to add capacity.

So rung 2 is not "add workers". It is **one global execution budget**, enforced in one of two
ways:

- **Divide the pool**: `max_concurrent` per worker becomes `total_budget // worker_count`, so
  the sum is constant. Simple, but wasteful when workers are unevenly loaded, and it breaks if
  the worker count and pool size are configured independently.
- **Let the queue be the budget** (preferred): the SQLite queue already serialises claims
  across processes, so cap *concurrently leased jobs* in the queue rather than sizing pools per
  worker. Each worker's pool then only needs to be large enough to execute what it manages to
  claim, and the global limit is enforced in one place by the component that already spans
  processes.

The second option is another reason SQLite earns its place: a cross-process queue is also a
cross-process concurrency limiter, which an in-process queue could never be. Either way, the
budget MUST be derived from R8's measurements of per-analysis CPU and memory, and adding
workers without adjusting it MUST be treated as a misconfiguration.

### Why SQLite for the queue rather than an in-process structure

Chosen now rather than deferred, because the queue is the one component that would otherwise
need rewriting to climb to rung 2.

- **Standard library.** No dependency, no service to provision or monitor.
- **Atomic leased claims via transactions**, which is exactly what the `JobQueue` protocol
  needs, and correct across processes rather than only within one.
- **Identical behaviour on one worker or four**, so rung 2 costs nothing on the queue side.
- **Inspectable.** A queue you can query with `sqlite3` during an incident is worth a great
  deal to a solo maintainer.

### R5a. SQLite is only correct if these details are specified (external review)

Review objected, correctly, that "SQLite gives leased claims" is hand-waving without the
mechanics. It is a conditional yes, and these are the conditions. Treat them as requirements,
not advice — get any one wrong and the queue is subtly broken under exactly the load it exists
for.

1. **Pragmas at connection open.** `journal_mode=WAL` (readers never block the writer),
   `synchronous=NORMAL`, `foreign_keys=ON`, and a non-zero `busy_timeout` (start at 5000 ms).
   Without `busy_timeout`, a concurrent writer raises `database is locked` immediately rather
   than waiting, which is the classic way SQLite gets wrongly blamed for not scaling.
2. **The claim must be one statement, not read-then-write.** A `SELECT` of the oldest queued
   job followed by a separate `UPDATE` is a race even inside `BEGIN IMMEDIATE`, if written
   carelessly. Use a single atomic statement — `UPDATE ... SET state='running', lease_expires_at=?
   WHERE id = (SELECT id FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1)
   RETURNING id` — so exactly one caller can win.
3. **`BEGIN IMMEDIATE` for every write transaction.** SQLite's default deferred transactions
   acquire the write lock late, which produces `SQLITE_BUSY` on upgrade and cannot be retried
   safely mid-transaction.
4. **Write transactions must be short.** Never hold one open across analysis work, and never
   across an `await`. The queue holds metadata; results live in the session store.
5. **Retry on `SQLITE_BUSY`** with bounded backoff, and treat exhaustion as a `503` rather
   than a 500 — it is capacity pressure, which FR-011 already has a response for.
6. **A lapsed lease is the crash-recovery mechanism**, so it must be exercised: reclaiming a
   job whose lease expired is what stops a killed worker blocking the queue forever. This is
   also what the ephemeral-filesystem case needs — see below.

**Request-path discipline is at stake, not just performance.** `sqlite3` is a blocking C
library, and the constitution forbids blocking work on the event loop. Calling it directly
from an `async def` handler would reintroduce, in miniature, the exact defect this feature
exists to remove. Every queue call from a handler MUST go through `run_in_threadpool` (or an
equivalent), or the queue module must expose a sync API called only from worker threads. This
is cheap — queue operations are sub-millisecond — but it is not optional, and it is easy to
forget because the calls look fast.

**Ephemeral filesystem behaviour must be designed, not assumed.** On a redeploy the database
vanishes with the container, so queued jobs are lost. That is no worse than the in-memory
status quo and the spec already covers it ("application restarted mid-analysis"), but the
behaviour must be deliberate: on startup, sweep the database — reclaim or fail leased jobs,
and clear `queued` entries whose sessions no longer exist — rather than serving stale
positions from a previous process's world view.

### R5b. Polling is a write path, not a read path (external review)

**The earlier "roughly 50 reads/second is nothing" claim was wrong, and wrong in the direction
that matters.** Reviewers independently pointed out that R3's presence mechanism makes every
successful poll update `last_seen_at`. A poll is therefore a **write**, and at 100 waiters on a
two-second interval that is roughly 50 writes per second, all serialised by SQLite's
single-writer rule. WAL mode does not help here — it decouples readers from the writer, not
writers from each other. The load estimate was not merely unmeasured; it counted the wrong
operation.

This is the one finding that could have made the queue the new bottleneck, so presence must be
designed rather than fall out of the polling loop:

- **Do not write presence on every poll.** Write it at most once per grace-window fraction —
  with a grace window in minutes (R3), refreshing every 15–30 seconds is ample, so the great
  majority of polls become pure reads.
- **Keep presence out of the queue's write path where possible.** Presence is per session, not
  per job. If the session store already touches a timestamp on access (`InMemoryStore.get` does
  today), presence can be read from there, and the queue needs no presence column at all. This
  is the preferred option: it removes the write rather than reducing it.
- **Never combine a presence update with a claim** in one transaction. That would put the
  highest-frequency write behind the one that must not be delayed.
- **Measure it.** Count writes per second and `SQLITE_BUSY` occurrences under the Check 7 load
  test, and treat the presence design as unresolved until those numbers exist.

If presence genuinely must live in the queue and cannot be debounced below the contention
threshold, that is the point at which a designated queue-owner process or an in-memory presence
layer in front of the durable queue becomes justified — not Redis.

**If these conditions prove burdensome in practice**, the fallback is not Redis but a single
designated queue-owner process, or accepting one Uvicorn worker permanently (rung 1) and
scaling the pool instead. Revisit only with measurements in hand.

**In-memory remains as a second implementation for tests**, so the protocol has two exercised
backings without building a distributed queue.

**On the existing `RedisStore`**: it was untested (`tests/test_sessions.py` never imported it)
and could not start in deployment (`--extra redis` was absent from `Dockerfile:14` and
`render.yaml:5`), so it was a scaling path that did not work while appearing to exist.
**Deleted** — along with the `redis` optional dependency and the `REDIS_URL` branch in
`create_store()` — under constitution v2.0.0, which withdrew Redis as a sanctioned storage
location. Rung 3 will reintroduce a shared store deliberately, behind the protocol as it
stands at that point.

**What still guarantees the scaling path**: interface discipline, not a second
implementation. Leased claims, id-based job references, and no assumption that the accepting
process executes the job or serves the result — these are what would be expensive to
retrofit, and SQLite requires them anyway, so they get exercised rather than merely
promised.

**The cautionary precedent**: this codebase contained a deferred scaling seam, and it had
rotted. `RedisStore` existed, but neither `Dockerfile:14` nor `render.yaml:5` installed the
`redis` extra, so setting `REDIS_URL` in production would crash at startup on `import redis`,
and `tests/test_sessions.py` never touched it. The escape hatch was unusable and nothing
revealed that, because nothing exercised it. This is the argument for choosing SQLite over an
in-process queue: an unexercised implementation is not a scaling path, so the running
configuration should be the one with the semantics we claim to need.

**The three seams, and the discipline each requires**:

1. **Session store** — already a `Protocol` (`app/sessions.py:43`). Normalise the semantics:
   `InMemoryStore.get()` returns the live object while `RedisStore.get()` returns a copy, so
   in-place mutation followed by a forgotten `set()` works in one backend and silently loses
   the write in the other. Return a copy everywhere, because a file-backed store at rung 2
   necessarily returns copies and any call site relying on live-reference mutation would break
   silently on the switch. Fixing it now makes that bug class surface in development.
2. **Job registry and queue** — new `Protocol` with a SQLite backing and an in-memory one for
   tests. Reference jobs by id, never by object. Never assume the process that accepted the
   upload runs the job or serves its result. Make `claim()` atomic **and** leased. Under
   SQLite these are not anticipatory discipline but hard requirements: a claim genuinely can
   outlive the claiming process, so a lapsed lease is the only thing that prevents a job
   stranded by a crash from blocking the queue forever.
3. **Serialisation** — an explicit schema (Arrow/Parquet for frames, JSON for scalars) rather
   than pickle. A security fix in its own right, and the format rung 2 needs for file-backed
   sessions regardless. Painful to retrofit because every field added meanwhile assumes
   pickle can carry it.

**Explicitly not built**: any Redis component, Redis provisioning, load-balancer
configuration, or multi-instance deployment. The `redis` optional dependency and `RedisStore`
are removed, so nothing advertises a supported path that is not one.

**Obligation that carries**: store tests MUST be written against the `SessionStore` protocol
and run against every implementation that exists, asserting that `get()` returns a copy.
`InMemoryStore` currently returns the live object, and the deleted `RedisStore` returned an
unpickled copy — that divergence is the bug class that made the two backends behave
differently, and it must not recur when file-backed sessions arrive at rung 2, since those
necessarily return copies.

**The threshold, restated**: rung 3 is a second *machine*, not a second worker. This is the
change SQLite plus R12 buys. Previously a second Uvicorn worker meant sessions, the job
registry and the queue all had to move to shared state at once — the queue especially, since
two independent queues cannot produce one true position (FR-006). With the queue in SQLite,
adding workers costs nothing on the queue side, and file-backed sessions handle the rest.

**Position change, recorded deliberately**: earlier analysis in this project concluded Redis
was needed on day one, because using multiple cores implies `uvicorn --workers N`, which
fragments `InMemoryStore` across processes. That was sound for that deployment shape but it
assumed the only shared medium was a network service. A shared *filesystem* serves the same
purpose for workers on one machine, and the filesystem is already there.

**What is given up at rung 1**: a single Uvicorn worker is a single point of failure; a
restart or deploy drops every live session (covered by the spec's "application restarted
mid-analysis" edge case); scale past one machine is unavailable; and a blocking call
reintroduced by a future bug has no second worker to absorb it. Accepted knowingly, with
rung 2 as the documented response.

**On pickle**: `RedisStore` pickled `SessionData`, which is arbitrary code execution on
deserialisation over Article 9 data. Deleting it removed that exposure, so no pickle remains in
the session path today. The explicit schema is still wanted, because file-backed sessions at
rung 2 need a serialisation format and must not reach for pickle by default.

**Alternatives considered**:

- **Sticky sessions at a load balancer.** Rejected: pushes correctness into deployment
  configuration where no test can see it, and does nothing for the shared-queue problem.
- **Redis now.** Rejected. It addresses multi-machine scale, and the constraint at 100
  parallel users is per-analysis time and one machine's cores — neither of which Redis
  improves. It would also add a service to provision, monitor and pay for.
- **A broker-backed queue (Celery/RQ/arq).** Rejected: brings its own Redis or AMQP
  dependency to solve a problem SQLite solves in the standard library at this volume.
- **A plain in-process queue with distributed-looking semantics.** Rejected as the worst
  option — it pays the full complexity cost of leases and id-based claims while remaining
  untested against the concurrency they exist for. SQLite pays the same cost and actually
  exercises it.
- **Interfaces only, with implementations stubbed for later.** Rejected: precisely what
  produced the unusable `RedisStore`. An implementation that is not exercised is not a
  scaling path.

---

## R6. Removing `raw_bytes` without a re-upload penalty

**Decision**: Lift compound splitting out of `parse_log` into
`core/compounds.py::split_compound_ingredients(meals_df)`, a pure DataFrame transform.
The parser retains the original string in a `raw_ingredient` column and no longer takes
`split_compounds`.

**Feasibility confirmed by reading the code**: the split is a pure string operation on
`raw_name` — split on `&`, strip a shared trailing noun from the set
`{soup, stew, salad, bowl, mix}`, title-case the parts — and it emits one row per part
with `quantity_g`, `carbs_g` and `sugars_g` copied unchanged
(`core/parser.py:137-168`, mirrored at `:482`). Nothing depends on workbook state, so it
transposes to a post-parse transform without behaviour change.

**Consequence**: `split_compounds` becomes a cheap re-derivation from retained frames
rather than a re-parse from retained bytes, and `raw_bytes` has no remaining caller.

**Two quirks the transform must reproduce, not tidy up** (found by external review):

1. **Casing is asymmetric.** Split parts are title-cased; an unsplit name keeps its
   original casing (`core/parser.py:148` vs `:150`). A "clean" rewrite that title-cases
   uniformly changes ingredient identity, which changes grouping, which changes every lift
   score.
2. **The shared suffix is stripped from the last segment only.** For
   `"Lentil soup & Kale soup"` today's behaviour yields `["Lentil Soup", "Kale"]`, not
   `["Lentil", "Kale"]` — and the comment at `core/parser.py:481` claiming the suffix is
   "stripped from each part" is itself wrong. Reproduce the code, not the comment.

**Verification requirement**: a test asserting the transform reproduces the old in-parser
output exactly, for both the multi-sheet and legacy parse paths, using
`example/example_log.xlsx` plus cases covering both quirks above. Principle V makes this
mandatory rather than optional — the parser silently shapes every downstream number.

**Test migration**: `tests/test_parser.py` currently asserts split behaviour in
`TestParseExampleLog.test_compound_dish_is_split`,
`test_compound_dish_kept_whole_when_splitting_disabled`, and
`TestParseLogMultiSheet.test_split_compounds`. All three move rather than only the first
two.

**Alternatives considered**: Requiring re-upload when the toggle changes (a real UX
regression this refactor makes unnecessary); parsing both variants eagerly (doubles parse
cost to serve a toggle most people never touch).

---

## R7. Parsing without touching disk

**Decision**: Parse from an in-memory buffer. Both paths already support it —
`openpyxl.load_workbook` (`core/parser.py:334`) and `pd.read_excel`
(`core/parser.py:413`) each accept a file-like object.

**Implementation note**: `parse_log` is typed `path: str | Path` (`core/parser.py:302`)
and the multi-sheet route opens the workbook twice — once in `_is_multi_sheet`
(`core/parser.py:61-66`) and again in `_parse_log_multi` (`:334`). A buffer must be
rewound between those opens, or the second read sees an exhausted stream.

### R7a. This does not make the constitution's claim true (found by external review)

**Starlette spools uploads to disk before the handler runs.** `UploadFile` is backed by a
`SpooledTemporaryFile` with `MultiPartParser.spool_max_size = 1 MiB` (verified). Any upload
above 1 MiB is therefore written to a real file in the system temp directory before
`upload_file` executes — which is precisely the year-scale case SC-006 targets.

Three consequences:

1. **Removing the `NamedTemporaryFile` is necessary but not sufficient.** The claim
   "nothing is written to disk" remains false for large uploads regardless.
2. **The quickstart check was invalid.** Looking for `/tmp/*.xlsx` cannot see spooled
   parts, which carry no suffix. It would have passed while the promise stayed broken.
3. **Principle IV needed amending, not just satisfying.** The constitution also permitted
   Redis in the same sentence as "nothing is written to disk", and Redis persists via
   RDB/AOF unless explicitly configured otherwise. The sentence was not achievable as
   written and contradicted itself.

**Decision**: keep the in-memory parse fix — it removes a write we control and avoids a
re-parse — but treat it as an optimisation rather than a privacy guarantee.

**Resolved**: Principle IV was amended in **constitution v2.0.0** (2026-08-13). The absolute
disk prohibition is withdrawn and replaced with bounded obligations: session-scoped disk is
permitted, data is deleted at session end, a startup sweep removes anything orphaned by an
unclean shutdown, nothing session-scoped reaches a backup or log, and deletion is described as
unlinking rather than claiming erasure. Redis was withdrawn as a sanctioned location in the
same amendment, which is why the bump is MAJOR — it made in-repo code non-compliant.

**Raising the spool threshold** above the largest accepted upload would keep uploads in memory
entirely, but it trades a disk write for a memory spike per concurrent upload — exactly the
resource this feature protects. With disk sanctioned, there is now little reason to pay that
trade. Decide against R8's numbers.

**Rationale**: `app/main.py` `upload_file` and `_run_analysis` write the upload to
`tempfile.NamedTemporaryFile(delete=False)` and unlink it in a `finally`.

**Severity of that particular write**: smaller than it first appears. The file exists only
for the duration of the parse — not for the session — the `finally` covers the failure
path, and `NamedTemporaryFile` creates with mode `0600`. In a container `/tmp` is discarded
with the container. The residual risk is a hard kill landing inside the parse window.

**Priority**: ship early because it is cheap and independent, not because it is urgent —
and read R7a before treating it as closing the Principle IV gap.

---

## R8. Measurement before tuning

**Decision**: Before fixing any constant, generate a year-scale workbook and record
per-field retained size and per-stage wall time, at minimum for: the example workbook,
3 months, 12 months, and 24 months of daily entries.

### R8a. Which stage actually dominates — measure first, and treat it as blocking

R1 asserted that model training is the dominant cost. External review disputed this, and
the code supports the challenge: `map_lookback` is a nested `iterrows` — `bac_df.iterrows()`
wrapping `meals_df.iterrows()` (`core/analysis.py:29-35`) — so it is
O(readings × meals) in interpreted Python. At the 12-month scale SC-006 targets that is
roughly 2,000 × 4,000 ≈ 8 million iterations. `extract_features` adds another `iterrows`
pass (`ml/features.py:136`), and `_build_results_json` iterates readings and lookback rows
again (`app/main.py` `_build_results_json`).

So there are two credible bottlenecks and the plan picked one without evidence. This matters
because they imply different fixes:

- **If training dominates**, a process pool is the right answer and R2 stands.
- **If `map_lookback` dominates**, the first fix is an interval/`merge_asof` join, which
  could plausibly outperform a four-process pool on its own, and would shift the residual
  cost into NumPy where the GIL is released — making threads sufficient and the pool
  unnecessary.

**This is the first measurement to take, and it gates R2.** Time each stage separately at
each fixture size before the executor design is settled. Note that the example workbook is
too small to discriminate: at 3 days, 50 bootstrap LASSO fits will dominate a trivial
lookback join, which is exactly how the wrong conclusion was reached.

**If vectorisation is adopted**, Principle V applies with full force: `map_lookback` shapes
every downstream number, so the rewrite needs equivalence tests against
`example/example_log.xlsx` before it is trusted.

**What to record**:

- Serialised size of each `SessionData` field, before and after the R4 shrink.
- Wall time for parse, `map_lookback`, `compute_lift_scores`, `extract_features`,
  `train_personal_model`, and `generate_pdf`.
- Peak resident memory for one analysis, which multiplied by pool size gives the true
  concurrency ceiling.

**What these numbers decide**: pool size (R2), grace window (R3), the two queue caps
(FR-010, provisionally 50 waiting and 5 minutes), `MAX_SESSIONS` (currently 100, which
evicts mid-session at the spec's target load), and the upload size cap (provisionally
10 MB). Every one of those is currently a guess, including mine.

**Fixture**: `tests/fixtures/generate_year_log.py`, generating synthetic data with the
structural quirks of the real format — aggregate rows, blank padding, compound dishes,
partially filled nutrient columns. It must not contain real patient data (Principle IV),
and should be generated on demand rather than committed.

### Result (T004, measured)

Run: `uv run python tests/fixtures/measure_scale.py --months 1 3 12`
(the 24-month case was abandoned — the un-vectorised `map_lookback` had not finished after
~11 minutes, which is itself the finding).

Wall time per stage, seconds:

| stage | 1mo (160×449) | 3mo (509×1348) | 12mo (2011×5288) |
|---|---|---|---|
| parse | 0.06 | 0.16 | 0.61 |
| **map_lookback** | **2.95** | **28.15** | **435.93** |
| compute_lift_scores | 0.02 | 0.02 | 0.02 |
| lift_scores_by_period | 0.04 | 0.07 | 0.07 |
| extract_features | 0.16 | 0.47 | 1.87 |
| train_personal_model | 1.09 | 1.10 | 2.05 |
| generate_report | 0.00 | 0.01 | 0.01 |
| generate_pdf | 0.05 | 0.11 | 0.17 |

Retained field sizes (pickled) at 12 months: `meals_df` 330 KiB, `lookback_df` 338 KiB,
`bac_df` 111 KiB, `scores_by_period` 10 KiB, `scores_all` 3 KiB. Peak Python allocation for
one 12-month analysis: **29 MiB**.

**What the numbers decide**:

1. **`map_lookback` dominates, overwhelmingly, and scales ~O(readings × meals)** — 2.95 →
   28.15 → 435.93s. It IS the "one analysis freezes the site" defect. R1's premise that
   training dominates is **refuted**: training is ~2s even at twelve months. → **T019
   (vectorise `map_lookback`) is now blocking, not contingent.**
2. **Executor = thread pool (R2 resolved).** After vectorisation the only meaningful CPU is
   sklearn training (~2s), which releases the GIL. Threads suffice; a process pool's DataFrame
   pickling is unjustified overhead. → **T018 builds a `ThreadPoolExecutor`.**
3. **Memory is a non-issue → the `DerivedBundle`/aggressive-shrink work is not justified.**
   One analysis peaks at 29 MiB and retained state is <1 MB/session; 100 sessions ≈ <100 MB.
   → **T031 resolves to "retain the frames, do nothing"; the R6/`raw_bytes` shrink (T045–T047)
   is kept only for its privacy value (fewer copies), not for memory.**
4. **Provisional caps** (revisit after T019 lands, which collapses per-job CPU to sub-second):
   `max_concurrent` = `min(cpu_count, 4)`; `MAX_SESSIONS` = 500 (memory-derived backstop, not
   the normal reclamation path); upload byte cap = 10 MB (a 12-month workbook is well under
   1 MB); complexity metric = estimated lookback pairs = `len(bac_df) × len(meals_df)` with a
   `422 too_complex` ceiling set from the post-T019 timing.

---

## R9. Upload limits

**Decision**: Reject oversized uploads before the body is fully buffered, and reject with a
message naming the limit.

**Rationale**: `contents = await file.read()` (`app/main.py` `upload_file`) reads an unbounded body
into memory with no size check anywhere. A single large upload can therefore affect
everyone, which is precisely what FR-029 forbids.

**Mechanism correction** (found by external review): a `file: UploadFile = File(...)`
parameter cannot be size-limited from inside the handler — by the time the handler runs,
Starlette has already parsed the multipart body and spooled it (see R7a). Enforcing the cap
therefore requires acting earlier: check `Content-Length` before parsing and/or read the
body via `request.stream()` in middleware, aborting once the cap is exceeded.
`Content-Length` alone is insufficient because it can be absent or dishonest, so it is a
fast rejection path rather than the enforcement mechanism.

**Three distinct limits**, per FR-028 and FR-030:

1. A **byte cap** on the upload, enforced as above.
2. A **post-parse complexity cap** on rows or readings, because a modest compressed file
   can expand into an analysis that would occupy a worker for a very long time. This is
   what protects the queue, and it is the limit that actually matters for FR-030.
3. Implicitly, the **spool threshold** interacts with both — see R7a.

The complexity cap needs a concrete metric, currently undefined. Candidate: estimated
lookback pairs (readings × mean meals per window), since that is what drives the dominant
cost if R8a confirms it. *To be set from R8 measurements.*

---

---

## R10. Session capacity: eviction is a correctness bug, not a tuning knob

**Finding** (raised by both external reviewers): `InMemoryStore.set` evicts the
oldest-touched session when at capacity (`app/sessions.py`, `InMemoryStore.set`), and
`MAX_SESSIONS` defaults to 100. At the spec's own design point of 200 arrivals in 10 minutes with
a 30-minute TTL, roughly 200 sessions coexist, so eviction fires on live sessions and their
owners get "upload a file first" mid-visit. That is a direct breach of FR-021 and SC-008,
not a constant to tune. The now-deleted `RedisStore` had **no** cap at all, so the two
backends disagreed on behaviour under load — the same divergence class as the
copy-versus-live-reference problem, and part of why it was removed.

**Decision**: evicting a live session is never acceptable. Under pressure the application
must **refuse new sessions** with the at-capacity response (FR-011) rather than silently
destroying an existing one. Someone who has not started is disappointed; someone
mid-analysis is not betrayed.

**Consequences**:

- Raise `MAX_SESSIONS` well above the concurrency design point and treat it as a
  memory-derived backstop, sized from R8's per-session measurements.
- Expiry, not capacity, becomes the normal reclamation path.
- The at-capacity response now has two causes — queue saturation and session saturation.
  They should read differently to the user, since only the first is worth waiting out.
- Any future store, including the file-backed one at rung 2, MUST carry the same policy. The
  cap belongs to the application, not to one backend's implementation.

---

## R11. Where the uploaded bytes live while a job is queued

**Finding** (raised by external review): the design says parsing moves off the request path
(R1), that `raw_bytes` is not retained (R4), and that the queue carries metadata only and
never uploaded bytes (data-model.md). Those three cannot all hold — a queued parse job needs
the bytes when its turn comes, which may be minutes after the request that accepted them.

**Decision**: parse **before** enqueueing, not inside the queued job.

- The upload handler parses to frames, then enqueues the analysis. Parsing is bounded by the
  upload cap and is the one stage whose input is the raw bytes, so doing it eagerly means
  the bytes never outlive the request.
- The queued job therefore receives *parsed frames plus parameters*, matching the "metadata
  and session-reachable state only" model, and matching how `POST /results` already behaves
  (params change, no re-parse).
- Bytes are discarded when the handler returns, so R4's "no `raw_bytes`" holds literally
  rather than by convention.

**Cost, stated plainly**: parsing is then on the request path, which is what R1 set out to
remove. Two mitigations, to be chosen on R8 numbers: run the parse in the pool but *await*
it within the request (off the event loop, still bounded by the upload cap), or admit
parsing to the queue as a distinct short-job class with its own concurrency limit. The
second reintroduces the byte-lifetime question and would need the bytes held in
session-reachable state with the same TTL and discard guarantees as everything else.

**What this rules out**: enqueueing a job that holds raw bytes for an unbounded queue wait.
That would retain the largest object for the longest time — the exact opposite of R4 — and
would put uploaded health data in a structure that spans sessions.

---

## R12. Session-scoped disk is sanctioned, and it is the rung-2 lever

**Change of premise**: "nothing on disk" was a high-level privacy aspiration, not a hard
requirement. The user has confirmed that persisting session data to disk for the duration of
a session, deleted when the session ends, is acceptable if it simplifies the architecture. It
does, considerably.

**Decision**: do not build it now, but design so it can be added without disturbing call
sites — a third `SessionStore` implementation writing to a per-session directory. Adopt it at
rung 2, when one Uvicorn worker is no longer enough.

**What it unlocks**:

- **Multiple Uvicorn workers on one machine.** Workers do not share memory but do share a
  filesystem, so file-backed sessions remove the reason this previously required Redis. This
  is the single biggest consequence.
- **Memory pressure largely dissolves.** Frames written as Parquet are read on demand rather
  than held per session, so 100 concurrent sessions stop being a RAM calculation.
- **R4a becomes optional.** The compact `DerivedBundle` exists to shrink retained memory; if
  frames live on disk, simply retaining `lookback_df` is viable and much less work. Compare
  both against R8 numbers.
- **R6 becomes optional.** Lifting compound splitting out of the parser exists to remove
  `raw_bytes` from memory; a session-scoped upload file is cheap to keep and delete. The
  refactor is still worth doing on its own merits — it removes a re-parse — but it stops
  being load-bearing.
- **R11 relaxes.** A queued job can reference a session-scoped file rather than forcing the
  parse to happen before enqueueing, so parsing can leave the request path after all.

**What it costs**:

- **Explicit cleanup replaces free expiry.** A TTL sweeper is needed, plus a sweep at startup
  for directories orphaned by a crash. This is the main new failure mode and must be tested,
  including the crash case.
- **Deletion means unlinking.** No filesystem guarantees erasure; the disclosure should say
  deleted, not destroyed.
- **Disk I/O per request**, though Parquet reads are fast and this trades against
  serialisation the memory path also pays.
- **Ephemeral in a container.** Files die with the container — equivalent to memory, and not
  shared across instances, which is why this is rung 2 and not rung 3.

**Privacy assessment, honestly**: in a single-tenant container a `0600` file in a
session-scoped directory has a broadly similar exposure profile to process memory — both fall
to anyone who obtains a shell. The meaningful differences are that files survive a crash
(addressed by the startup sweep) and could reach a host snapshot (not applicable on Render's
ephemeral filesystem). This is a smaller privacy change than it sounds, and a far smaller one
than the gap between today's code and today's claim.

**Constitution consequence**: this makes the R7a amendment cleaner rather than harder.
Instead of defending an absolute that framework upload spooling already breaks, Principle IV
can state what is actually true — data is held in memory and in a session-scoped working area,
never deliberately persisted beyond the session, deleted when the session ends, and never
backed up. Still requires an amendment with rationale and version bump per Governance.

---

## Open items carried into Phase 1

| Item | Resolved by |
|------|-------------|
| **Which stage dominates** — gates the executor choice in R2 | **R8a. Blocking: measure before the executor design is settled.** |
| Whether `lookback_df` is replaced by a compact index or simply retained | R4a versus R8 measurements. R12 makes plain retention viable, so this may resolve as "do nothing" |
| Executor width, grace window, queue caps, `MAX_SESSIONS`, upload cap, complexity metric | R8 measurement, before implementation fixes them |
| Whether parsing is awaited in-request or queued | R11, relaxed by R12 — a session-scoped upload file removes the byte-lifetime objection |
| Whether `lookback_by_reading` stays in the payload as-is | R4a and R8 — it may reintroduce the volume R4 removes |
| ~~Amending Principle IV~~ | **Done** — constitution v2.0.0, 2026-08-13 (R7a, R12) |
| ~~Deleting `RedisStore` and the `redis` extra~~ | **Done** — removed with the v2.0.0 amendment (R5) |
| ~~Interface retention disclosure~~ | **Done** — FR-024 landed. `app/static/index.html` carries a disclosure describing today's behaviour, with the retention window substituted server-side from `SESSION_TTL` so it cannot drift, and `tests/test_retention_disclosure.py` guarding both drift and the reintroduction of overstated claims |
| Disclosure must be revised when disk-backed sessions land | The current wording describes memory-only retention plus transient temp files, which is accurate **today**. Rung 2 changes the facts, so R12 work MUST update the disclosure in the same change |

**Corrected from an earlier draft**: this table previously stated that `/predict` and
`/ingredient/{name}` "read retained frames" and could be left inline. Both read frames that
R4 drops. See R4a.
