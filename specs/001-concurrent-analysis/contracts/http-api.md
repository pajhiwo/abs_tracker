# Phase 1 Contract: HTTP API

**Feature**: [spec.md](../spec.md) | **Plan**: [plan.md](../plan.md) | **Date**: 2026-08-13

The externally visible interface is the HTTP API consumed by `app/static/app.js`. This
contract describes what changes, what stays, and the response shapes the frontend must
handle. Session identity remains the existing anonymous `session_id` cookie.

## Summary of change

Analysis endpoints stop being synchronous. Anything that may need computation returns
either a completed result (cache hit) or a job reference (cache miss), and the client
polls the job. Endpoints that only read retained state are unchanged.

Upload deliberately parses **before** enqueueing rather than queueing the raw bytes (R11): it
keeps the uploaded file's lifetime short and bounded to one request, means a malformed workbook
is rejected immediately with a useful message instead of failing inside a worker minutes later,
and keeps the job payload free of the largest and most sensitive object in the system. Parsing
is still moved off the event loop; it is awaited, not queued.

| Endpoint | Today | After |
|----------|-------|-------|
| `POST /upload` | Parses, analyses, trains, returns results | Validates, **parses** (awaited, off the event loop), hashes, then returns `202` with an analysis job — or `200` with a cached result |
| `POST /results` | Recomputes inline | `200` cached, or `202` with a job |
| `GET /results` | Rebuilds payload, retrains | `200` from cache; `404` if nothing computed |
| `GET /jobs/{job_id}` | — | **New.** Status, queue position, wait estimate. Doubles as the presence signal |
| `DELETE /jobs/{job_id}` | — | **New.** Cancel a queued job |
| `GET /report` | Retrains to read `summary` | `200` from cache once stage one is done; `202` only while no analysis exists yet |
| `GET /report/pdf` | Retrains to read `summary` | `200` cached PDF; `202` if not yet available |
| `POST /predict` | Inline | Stays inline, but reads `DerivedBundle.lift_scores`, not `scores_all` |
| `GET /ingredient/{name}` | Inline | Stays inline, but reads `DerivedBundle.ingredient_readings`, not `lookback_df` |

**Correction from an earlier draft**: `/predict` and `/ingredient/{name}` were described as
reading "retained frames". They read `scores_all` and `lookback_df`, which R4 drops. They are
still cheap, but only once `DerivedBundle` exists (R4a).

## Staged results and Principle II

A job completes in two stages, and the contract depends on it:

1. **Stage one** — parse-derived summary and lift scores. Cached and servable.
2. **Stage two** — the optional ML block. Updates the cached payload when it finishes.

`GET /report` and `GET /report/pdf` MUST be servable after stage one. If they waited for the
whole job, the optional ML layer would be blocking report generation, which the constitution
forbids (`constitution.md:84`). A job in state `partial` is therefore a success for every
endpoint except the `ml` section of the payload, which reports itself as pending.

## Response shapes

### Job accepted — `202 Accepted`

Returned by any endpoint that had to enqueue work.

```json
{
  "status": "queued",
  "job_id": "opaque-string",
  "position": 4,
  "estimated_wait_seconds": 48,
  "poll_after_seconds": 2
}
```

`poll_after_seconds` lets the server slow clients down under load rather than relying on a
hard-coded client interval.

### Job status — `GET /jobs/{job_id}` → `200 OK`

```json
{
  "status": "queued | running | partial | complete | failed | cancelled | abandoned | expired",
  "job_id": "opaque-string",
  "position": 2,
  "estimated_wait_seconds": 24,
  "poll_after_seconds": 2,
  "message": "human-readable, present when failed/abandoned/expired"
}
```

`partial` is what job staging produces and it is a normal, useful state, not a degraded one:
the deterministic stage (summary and lift scores) has been cached and is readable, while the
optional ML stage is still running. It is what keeps optional intelligence from blocking the
deterministic core (Principle II), so a client that treats it as an error would defeat the
staging. An earlier revision of this contract described the staged flow but omitted `partial`
from this enum.

`position` and `estimated_wait_seconds` are present only while `queued`, and are
recomputed per request (FR-007) — never counted down by the client, so a bad estimate
corrects rather than stalling at zero.

**Every successful poll updates the job's `last_seen_at`.** This is the presence
mechanism behind FR-013; it is a contract obligation, not an implementation detail.

**Authorisation**: a job is readable only by its owning session. A `job_id` belonging to
another session MUST return `404`, not `403` — a `403` would confirm the job exists.

### Completed analysis — `200 OK`

The existing results payload, unchanged in shape (see data-model.md § ResultPayload). The
client SHOULD treat a `200` from `/upload`, `/results` or a completed job identically.

### At capacity — `503 Service Unavailable`

```json
{
  "status": "at_capacity",
  "message": "Too many analyses are running right now. Please try again in a few minutes.",
  "retry_after_seconds": 180
}
```

Also sent as a `Retry-After` header. This is FR-011: an explicit, honest refusal rather
than a hang or an indefinite queue.

### Upload rejected — `413 Content Too Large`

```json
{
  "status": "too_large",
  "message": "That file is larger than the 10 MB limit. Try exporting a shorter date range.",
  "limit_bytes": 10485760
}
```

MUST be returned before the whole body is buffered (R9). Note that this cannot be enforced
from inside a handler taking `file: UploadFile = File(...)` — by then the framework has
already parsed and spooled the body. Enforcement belongs in middleware that checks
`Content-Length` for a fast rejection and reads `request.stream()` to abort a dishonest or
absent one.

The limit is stated in the message (FR-028), and the phrasing should suggest a remedy rather
than only naming the fault.

### Other errors

| Status | When | Body `status` |
|--------|------|---------------|
| `400` | Wrong file type, unparseable workbook, no readings found | `invalid_file` |
| `404` | No session, no data loaded, or unknown/foreign `job_id` | `no_data` |
| `422` | Log within size limit but too large to analyse (FR-030) | `too_complex` |
| `503` | Session capacity reached — distinct from queue saturation (R10) | `at_capacity` |

The two `503` causes MUST read differently to the user: a saturated queue is worth waiting
out, whereas session capacity means the application is holding as many people as it can and
the advice is different. Neither may be served by evicting someone already mid-analysis.

**Parameter defaults must be reconciled.** `AnalysisParams.exclude_proteins` defaults to
`False` (`app/main.py` `AnalysisParams.exclude_proteins` default) while the `/upload` query parameter defaults to `True`
(`app/main.py` `upload_file`). Today an upload followed by a recompute with untouched settings silently
changes the protein filter — and once results are cached by `params_signature`, that
inconsistency would produce two cache entries for what the user considers one setting. Pick
one default and cover it with a regression test.

All error bodies carry `status` and a plain-language `message`. Messages MUST NOT leak
internal exception text; today `raise HTTPException(400, f"Failed to parse file: {e}")`
(`app/main.py` `upload_file`, parse failure) forwards raw exception detail to the client.

## Client flow

```text
POST /upload
  ├─ 200 → render results (cache hit)
  ├─ 202 → poll GET /jobs/{id} every poll_after_seconds
  │         ├─ queued    → show position + estimated wait
  │         ├─ running   → show progress
  │         ├─ partial   → GET /results and render; ML section shows as pending
  │         ├─ complete  → GET /results, render in full
  │         ├─ abandoned → explain, offer restart (no re-upload needed)
  │         └─ failed    → show message
  ├─ 413 → show size limit
  ├─ 422 → explain the log is too large to analyse
  └─ 503 → show at-capacity message and Retry-After
```

Cancelling calls `DELETE /jobs/{id}` and stops polling (FR-009).

**Polling must be visibility-aware** (R3). The client polls on an interval *and* immediately
on `visibilitychange` when the tab becomes visible, because mobile browsers throttle
background timers well past any reasonable grace window. Without the visibility hook, a
person who locks their phone looks absent and loses their place — the exact case FR-012
exists to protect.

## Compatibility

Breaking for the frontend: `POST /upload` and `POST /results` may now return `202` where
they previously always returned results. `app/static/app.js` must be updated in the same
change. No other consumers exist — there is no published API and no CLI path through the
web layer (`abs_tracker.py` calls the core modules directly and is unaffected).
