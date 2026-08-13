# Phase 1 Quickstart: Validating Concurrent Analysis

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-08-13

Runnable checks that prove the feature works end to end, each tied to the requirement it
validates. Principle V requires claims of completion to be backed by observed output, so
these are the commands whose output constitutes that evidence.

## Prerequisites

```bash
cd /Users/bartoszsypniewski/GIT/abs_tracker
uv sync
```

Generate the year-scale fixture (R8) — required by several checks below:

```bash
uv run python tests/fixtures/generate_year_log.py --months 12 --out /tmp/year_log.xlsx
```

Start the application:

```bash
uv run uvicorn app.main:app --port 8000
```

## Check 1 — Measurements (R8/R8a, Principle V)

Deliberately first. R8a gates the executor choice, so these numbers must exist before any
constant or design decision is fixed. An earlier revision placed this section last while
insisting it be run first; it is now numbered and ordered to match.

```bash
uv run python tests/fixtures/measure_scale.py --months 1 3 12 24
```

Records per-field retained size and per-stage wall time. The decisive question is whether
`map_lookback`'s nested `iterrows` (`core/analysis.py:29-35`) or LASSO training dominates:

```bash
uv run python -m cProfile -s cumtime tests/fixtures/measure_scale.py --months 12 2>&1 \
  | head -40
```

If `map_lookback` dominates, vectorising it is worth more than any executor change and may
make threads sufficient (R2). These numbers also set executor width, the grace window, the
queue caps, `MAX_SESSIONS` and the upload cap — all currently guesses.

## Baseline: capture the defect before fixing it

Worth running first, so the improvement is measured rather than asserted.

```bash
# Upload, then time a report request that should need no computation.
curl -s -c /tmp/c.txt -F "file=@example/example_log.xlsx" localhost:8000/upload > /dev/null
time curl -s -b /tmp/c.txt localhost:8000/report > /dev/null
```

On current `main` this retrains the model to read ten summary numbers (R1). Record the
time; SC-003 requires under 2 seconds after the change, and the gap should be stark on
the year-scale file.

## Check 2 — Concurrent analyses do not block each other (US1, FR-001..FR-004, SC-002)

```bash
# Start several analyses at once, each with its own cookie jar.
for i in 1 2 3 4 5; do
  curl -s -c /tmp/c$i.txt -F "file=@/tmp/year_log.xlsx" localhost:8000/upload &
done

# While those run, the landing page must stay responsive.
for i in $(seq 1 10); do
  curl -s -o /dev/null -w "%{time_total}\n" localhost:8000/
  sleep 1
done
wait
```

**Expected**: landing-page times stay under 2s throughout (SC-002). On current `main` they
will not — this is the defect the feature exists to fix.

## Check 3 — Unchanged re-requests are not recomputed (US2, FR-016..FR-019, SC-003/SC-004)

```bash
curl -s -c /tmp/c.txt -F "file=@/tmp/year_log.xlsx" localhost:8000/upload > /dev/null
# (poll /jobs/{id} until complete)

time curl -s -b /tmp/c.txt localhost:8000/results   > /dev/null   # expect < 2s
time curl -s -b /tmp/c.txt localhost:8000/report    > /dev/null   # expect < 2s
time curl -s -b /tmp/c.txt localhost:8000/report/pdf > /tmp/r.pdf # expect < 2s

# Identical analysis content on repeat (FR-018). Do NOT use `cmp` on the PDFs: a generated
# PDF embeds a creation timestamp, so byte-comparison fails even when the analysis is
# identical. Compare the analysis payload instead.
curl -s -b /tmp/c.txt localhost:8000/results > /tmp/r1.json
curl -s -b /tmp/c.txt localhost:8000/results > /tmp/r2.json
diff <(jq -S . /tmp/r1.json) <(jq -S . /tmp/r2.json)

# Changing a setting MUST recompute (FR-019).
curl -s -b /tmp/c.txt -X POST -H 'Content-Type: application/json' \
  -d '{"hours": 5, "min_obs": 3, "split_compounds": true,
       "exclude_proteins": false, "episode_threshold": 2.0}' \
  localhost:8000/results
```

**Expected**: the three reads are fast and `diff` reports no difference; the settings change
returns `202` with a job, or fresh results reflecting `hours: 5`.

Note that `exclude_proteins: false` above matches `AnalysisParams` (`app/main.py` `AnalysisParams.exclude_proteins` default) but
*not* the `/upload` default of `true` (`app/main.py` `upload_file`). Until that inconsistency is fixed,
this request changes two settings rather than one and will recompute for the wrong reason.

## Check 4 — Queue behaviour under saturation (US3, FR-005..FR-014, SC-001/SC-005)

```bash
ABS_POOL_SIZE=2 uv run uvicorn app.main:app --port 8000
```

Submit more analyses than the pool can run and inspect a queued job:

```bash
curl -s -b /tmp/c7.txt localhost:8000/jobs/$JOB_ID
```

**Expected**: `status: queued` with a `position` that decreases across polls and an
`estimated_wait_seconds` that is revised rather than counted down (SC-012). Past both caps,
submissions return `503` with `Retry-After` (FR-011), never a hang.

Cancellation (FR-009):

```bash
curl -s -b /tmp/c7.txt -X DELETE localhost:8000/jobs/$JOB_ID
```

Presence (FR-012..FR-014): stop polling a queued job, wait past the grace window, and let
its turn arrive. Expect `abandoned` and the capacity given to the next job — not a
computed result nobody requested.

## Check 5 — Session data and privacy (US4, FR-021..FR-025, SC-008/SC-009)

```bash
# Data survives a long sequence of interactions within the session.
for i in $(seq 1 20); do
  curl -s -o /dev/null -w "%{http_code}\n" -b /tmp/c.txt localhost:8000/results
  sleep 5
done
```

**Expected**: `200` every time — zero "upload a file first" responses (SC-008).

Expiry (FR-022):

```bash
SESSION_TTL=10 uv run uvicorn app.main:app --port 8000
# upload, wait 15s, then:
curl -s -b /tmp/c.txt localhost:8000/results    # expect 404 no_data
```

Isolation (FR-020, SC-007) — the check that matters most:

```bash
# Two sessions, byte-identical uploads. Results must be computed separately
# and neither session may read the other's job.
curl -s -c /tmp/a.txt -F "file=@/tmp/year_log.xlsx" localhost:8000/upload
curl -s -c /tmp/b.txt -F "file=@/tmp/year_log.xlsx" localhost:8000/upload
curl -s -b /tmp/b.txt localhost:8000/jobs/$JOB_ID_FROM_A   # expect 404
```

No account at any point (FR-023, SC-010): every command above runs without credentials.

Disk retention is bounded, not absent (Principle IV as amended, R7a/R12). The check is not
"no files exist" — the framework spools uploads above 1 MiB before any handler runs, so files
*will* appear. What must hold is that nothing written by us outlives the session:

```bash
# Watch what the process holds open while an analysis is in flight.
lsof -p $(pgrep -f 'uvicorn app.main') | grep -Ev 'REG.*\.(so|py|pyc)$'

# Then, after the session expires, nothing session-scoped may remain.
SESSION_TTL=10 uv run uvicorn app.main:app --port 8000
# upload, wait 15s, then confirm the session's working directory is gone.
```

Also worth proving the crash path, since files do not expire on their own: kill the process
mid-analysis, restart it, and confirm the startup sweep removes the orphaned directory.

## Check 6 — Large and oversized logs (US5, FR-026..FR-030, SC-006/SC-011)

SC-006 requires a 12-month log to complete within 60 seconds. Timing the upload alone will
*not* measure that, because the upload returns `202` as soon as the job is accepted. The
60 seconds must be measured from submission to job completion:

```bash
START=$(date +%s)
JOB=$(curl -s -c /tmp/c.txt -F "file=@/tmp/year_log.xlsx" localhost:8000/upload | jq -r .job_id)
until [ "$(curl -s -b /tmp/c.txt localhost:8000/jobs/$JOB | jq -r .status)" = "complete" ]; do
  sleep 1
done
echo "elapsed: $(( $(date +%s) - START ))s"   # SC-006: < 60
```

```bash
# Oversized upload is refused promptly, with the limit named (SC-011).
head -c 20000000 /dev/urandom > /tmp/big.xlsx
time curl -s -o /dev/null -w "%{http_code}\n" -F "file=@/tmp/big.xlsx" localhost:8000/upload
```

**Expected**: `413` within 5 seconds, with `limit_bytes` in the body, and other clients
unaffected while it is rejected. Note this check also proves *where* the limit is enforced: if
the rejection time scales with file size, the body is being buffered before the check and the
cap is in the handler rather than in middleware (R9).

## Check 7 — Behaviour under a load spike (SC-001, SC-002)

The spec's design point is an unpredictable spike toward 100 parallel users, so the queue
needs testing at a width the `for` loops above cannot produce.

```bash
uv run --with hey hey -n 200 -c 100 -m GET http://localhost:8000/
```

**Expected**: no failed requests, and a p99 that stays within SC-002 while analyses run in the
background. Then repeat with concurrent uploads to confirm the queue sheds load with `503`
plus `Retry-After` rather than timing out or accepting work it cannot start.

Two things to watch that only appear at width. First, queue **write** contention: if presence is
recorded per poll, ~100 waiters produce ~50 writes/second, which SQLite serialises — WAL does
not help writer-versus-writer (R5b). Count writes and `SQLITE_BUSY` occurrences rather than
assuming, since an earlier draft of this check wrongly described polling as read-only and
asserted there would be no contention:

```bash
# While the load test runs, watch for lock contention rather than trusting WAL.
sqlite3 "$ABS_QUEUE_DB" 'PRAGMA journal_mode; SELECT count(*) FROM jobs WHERE state="queued";'
```

Second, whether memory grows linearly with live sessions in a way that sets the real
`MAX_SESSIONS` (R10).

## Regression suite

```bash
uv run pytest
```

**Expected**: the 120 currently-passing tests (121 collected, one skipped) plus the new suites. The parser tests deserve
attention if R6 lands: moving compound splitting out of `parse_log` means
`test_compound_dish_is_split` and `test_compound_dish_kept_whole_when_splitting_disabled`
in `tests/test_parser.py` move to `tests/test_compounds.py` and must assert the transform
reproduces the old behaviour exactly against `example/example_log.xlsx` — including the
asymmetric casing and last-segment suffix quirks in R6.
