"""Upload limits: a byte cap enforced before the body is buffered, and a post-parse
complexity cap (US5; FR-028–FR-030; research R9).

Two distinct guards, because a small file can still be expensive:

1. **Byte cap** — a pure-ASGI middleware in front of the app. It rejects an oversized
   upload *before* the handler runs, so a single large file cannot consume analysis
   capacity or spool a huge body into memory (FR-029). A handler taking
   ``file: UploadFile`` cannot do this: by the time it runs, the framework has already
   parsed and spooled the multipart body (research R7a/R9). Enforcement therefore has to
   happen earlier, in middleware.

2. **Complexity cap** — a post-parse check on the parsed row counts. A modest file can
   expand into an analysis that would occupy a worker far too long; this cap turns that
   into an immediate, clear ``422`` rather than an indefinite wait (FR-030).
"""

from __future__ import annotations

import json
import os

# 10 MB. A 12-month workbook measures well under 1 MB (research T004), so this leaves
# generous headroom for multi-year logs while bounding a single upload's cost. Overridable
# for tests and for a future environment with different limits.
MAX_UPLOAD_BYTES = int(os.environ.get("ABS_MAX_UPLOAD_BYTES", 10 * 1024 * 1024))

# Complexity proxy: readings × meals, the crude upper bound on the lookback join that
# dominates analysis cost (research R8a). One year is ≈ 2000 × 5300 ≈ 10.6 M pairs, so the
# 50 M ceiling admits well beyond a year (FR-026) while refusing pathological inputs before
# they occupy a worker (FR-030). Provisional per R8a; revisit against post-vectorisation
# timing. Read at call time so tests can lower it.
MAX_LOOKBACK_PAIRS = int(os.environ.get("ABS_MAX_LOOKBACK_PAIRS", 50_000_000))

# Paths whose POST body is size-capped. Everything else streams through untouched, so no
# other request pays for this guard (FR-029: a rejection must not affect anyone else).
_LIMITED_PATHS = ("/upload",)


def _limit_phrase(max_bytes: int = MAX_UPLOAD_BYTES) -> str:
    """Human phrasing of the byte cap, e.g. '10 MB'."""
    mb = max_bytes / (1024 * 1024)
    return f"{mb:.0f} MB" if mb == int(mb) else f"{mb:.1f} MB"


def too_large_body(max_bytes: int = MAX_UPLOAD_BYTES) -> dict:
    """The `413` response body (contracts: `too_large`)."""
    return {
        "status": "too_large",
        "message": (
            f"That file is larger than the {_limit_phrase(max_bytes)} limit. "
            "Try exporting a shorter date range."
        ),
        "limit_bytes": max_bytes,
    }


def too_complex_body() -> dict:
    """The `422` response body (contracts: `too_complex`)."""
    return {
        "status": "too_complex",
        "message": (
            "That log is too large to analyse in a reasonable time. "
            "Try analysing a shorter date range."
        ),
    }


def estimate_lookback_pairs(bac_df, meals_df) -> int:
    """Readings × meals — the size proxy for the dominant lookback join (research R8a)."""
    return len(bac_df) * len(meals_df)


def exceeds_complexity(bac_df, meals_df) -> bool:
    """True when a within-size log is nonetheless too big to analyse (FR-030).

    Reads ``MAX_LOOKBACK_PAIRS`` from the module at call time so it can be overridden.
    """
    return estimate_lookback_pairs(bac_df, meals_df) > MAX_LOOKBACK_PAIRS


class LimitUploadSizeMiddleware:
    """Pure-ASGI middleware that caps the request body on the upload path.

    Two rejection paths (research R9):

    - **Honest ``Content-Length``**: reject immediately, before reading any body — the
      cheapest case and the common one.
    - **Absent or dishonest length**: read the body but stop the moment it crosses the
      cap, so an unbounded upload is never fully buffered. A within-cap body is buffered
      (bounded by the cap) and replayed to the app, which is what the handler would read
      anyway via ``await file.read()``.

    Only POST requests to ``_LIMITED_PATHS`` are inspected; all other traffic passes
    straight through so a rejection here never touches anyone else's request (FR-029).
    """

    def __init__(self, app, max_upload_size: int = MAX_UPLOAD_BYTES, path_prefixes=None):
        self.app = app
        self.max_upload_size = max_upload_size
        self.path_prefixes = tuple(path_prefixes) if path_prefixes else _LIMITED_PATHS

    def _applies(self, scope) -> bool:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return False
        path = scope.get("path", "")
        return any(path.startswith(p) for p in self.path_prefixes)

    async def __call__(self, scope, receive, send):
        if not self._applies(scope):
            return await self.app(scope, receive, send)

        # Fast path: an honest Content-Length over the cap is refused before any body read.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_upload_size:
                        return await self._reject(send)
                except ValueError:
                    pass
                break

        # Enforcement path: count bytes as they arrive; abort the moment the cap is passed.
        body = bytearray()
        over = False
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                if len(body) > self.max_upload_size:
                    over = True
                    break
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                # Let the app observe the disconnect and unwind.
                return await self.app(scope, self._replay(bytes(body), disconnect=True), send)
            else:  # pragma: no cover - defensive
                break

        if over:
            return await self._reject(send)

        return await self.app(scope, self._replay(bytes(body)), send)

    @staticmethod
    def _replay(body: bytes, disconnect: bool = False):
        """A fresh `receive` that hands the buffered body to the app exactly once."""
        sent = False

        async def receive():
            nonlocal sent
            if disconnect:
                return {"type": "http.disconnect"}
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        return receive

    async def _reject(self, send):
        payload = json.dumps(too_large_body(self.max_upload_size)).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
