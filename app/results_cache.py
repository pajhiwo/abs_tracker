"""Per-session result cache (US2; FR-016–FR-020; data-model ResultCache).

Three artifacts are cached per ``params_signature``, because they are genuinely
different documents that the frontend fetches separately (data-model "Three cached
artifacts, not one"):

- ``payload``  — what ``/results`` serves (the built results JSON).
- ``report``   — what ``/report`` serves (``generate_report`` + ``detect_combinations``).
- ``pdf``      — what ``/report/pdf`` serves (rendered from ``report``).

The cache is small and least-recently-used bounded (3–5 entries) so a session that
sweeps a slider back and forth does not accumulate unbounded state. It is cleared
entirely when the uploaded content changes (``content_hash``), which is FR-019.

**Isolation (FR-020)** is structural: a ``ResultCache`` is owned by exactly one
``SessionData`` and is never shared. Two sessions uploading byte-identical files have
the same ``content_hash`` yet compute and cache separately. That is a deliberate
privacy cost, asserted by test rather than left to structure.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

# Bound on distinct parameter sets cached per session. Kept in the 3–5 band the
# data-model specifies: large enough to cover a little slider fiddling, small enough
# that retained state stays trivial.
DEFAULT_MAX_ENTRIES = 4


@dataclass
class CacheEntry:
    """The three artifacts for one ``params_signature``, filled in as produced."""

    payload: dict | None = None
    report: dict | None = None
    pdf: bytes | None = None


class ResultCache:
    """Small LRU cache of :class:`CacheEntry`, keyed by ``params_signature``."""

    def __init__(
        self, max_entries: int = DEFAULT_MAX_ENTRIES, content_hash: str | None = None
    ) -> None:
        self._entries: "OrderedDict[str, CacheEntry]" = OrderedDict()
        self._max = max(1, max_entries)
        self.content_hash = content_hash

    # -- invalidation -----------------------------------------------------
    def note_content(self, content_hash: str | None) -> None:
        """Bind the cache to the current upload, clearing it if content changed.

        FR-019: a new upload (different ``content_hash``) invalidates every cached
        result. Called on upload; a no-op when the content is unchanged.
        """
        if content_hash != self.content_hash:
            self._entries.clear()
            self.content_hash = content_hash

    def clear(self) -> None:
        self._entries.clear()

    # -- internals --------------------------------------------------------
    def _entry(self, sig: str) -> CacheEntry:
        entry = self._entries.get(sig)
        if entry is None:
            entry = CacheEntry()
            self._entries[sig] = entry
        self._entries.move_to_end(sig)
        self._evict()
        return entry

    def _evict(self) -> None:
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)  # drop least-recently-used

    def _read(self, sig: str, field: str):
        entry = self._entries.get(sig)
        if entry is None:
            return None
        value = getattr(entry, field)
        if value is not None:
            self._entries.move_to_end(sig)
        return value

    # -- payload ----------------------------------------------------------
    def set_payload(self, sig: str, payload: dict) -> None:
        self._entry(sig).payload = payload

    def get_payload(self, sig: str) -> dict | None:
        return self._read(sig, "payload")

    # -- report -----------------------------------------------------------
    def set_report(self, sig: str, report: dict) -> None:
        self._entry(sig).report = report

    def get_report(self, sig: str) -> dict | None:
        return self._read(sig, "report")

    # -- pdf --------------------------------------------------------------
    def set_pdf(self, sig: str, pdf: bytes) -> None:
        self._entry(sig).pdf = pdf

    def get_pdf(self, sig: str) -> bytes | None:
        return self._read(sig, "pdf")

    # -- introspection ----------------------------------------------------
    def has(self, sig: str) -> bool:
        return sig in self._entries

    def signatures(self) -> list[str]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)
