"""
Test the retention disclosure served on the landing page.

Constitution Principle IV requires retention to be stated accurately wherever a
user uploads data, and the stated retention to be one the implementation can
honour. These tests guard against the two ways that promise rots: the figure
drifting away from the configured SESSION_TTL, and the disclosure regaining
claims the code cannot keep.

Run with: python -m pytest tests/test_retention_disclosure.py -v
"""

import re

import pytest
from fastapi.testclient import TestClient

from app import main, sessions

client = TestClient(main.app)


def _landing_page() -> str:
    resp = client.get("/")
    assert resp.status_code == 200
    return resp.text


def _flatten(markup: str) -> str:
    """Tag-free, whitespace-collapsed, lowercased text.

    Assertions run against this rather than raw markup so they survive
    reformatting and line wrapping in the HTML.
    """
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup)).strip().lower()


def _landing_text() -> str:
    return _flatten(_landing_page())


def test_disclosure_is_present_without_javascript():
    """The disclosure ships in the HTML, not injected by a script.

    A privacy notice that only appears if client-side JS runs is not a
    disclosure. Asserting on the served markup keeps it unconditional.
    """
    html = _landing_page()
    assert 'class="retention-notice"' in html
    assert "held in the server's memory" in html


def test_essential_facts_are_visible_without_expanding():
    """The headline facts must not be hidden behind a click.

    Principle IV requires retention to be stated wherever data is uploaded. A
    collapsed <details> satisfies that only for whoever opens it, so the summary
    paragraph carries retention, temp-file use, no-pooling and no-egress.
    """
    html = _landing_page()
    summary = re.search(
        r'<p class="retention-summary">(.*?)</p>', html, re.DOTALL
    )
    assert summary, "expected an always-visible retention summary"
    text = _flatten(summary.group(1))

    assert "no account is needed" in text
    assert "temporary file" in text
    assert "never combined" in text
    assert "not sent to any other company" in text


def test_retention_is_stated_as_an_upper_bound(monkeypatch):
    """Capacity eviction can discard a session early, so retention is a maximum.

    InMemoryStore.set() evicts the oldest live session at MAX_SESSIONS, so
    promising the data survives for exactly the window would be untrue.
    """
    monkeypatch.setattr(sessions, "SESSION_TTL", 1800)
    text = _landing_text()
    assert "up to" in text
    assert "at most" in text
    assert "discarded sooner if the server is busy" in text


def test_stated_window_matches_configured_ttl(monkeypatch):
    """The figure shown to users tracks SESSION_TTL rather than a hardcoded 30."""
    monkeypatch.setattr(sessions, "SESSION_TTL", 600)
    assert "10 minutes" in _landing_page()

    monkeypatch.setattr(sessions, "SESSION_TTL", 1800)
    assert "30 minutes" in _landing_page()


def test_stated_window_is_never_zero_or_plural_one(monkeypatch):
    """Sub-minute TTLs round up to '1 minute'.

    Rounding up is the conservative direction: it never tells someone their data
    is gone sooner than it is.
    """
    monkeypatch.setattr(sessions, "SESSION_TTL", 10)
    html = _landing_page()
    assert "1 minute" in html
    assert "0 minutes" not in html
    assert "1 minutes" not in html


def test_no_unsubstituted_placeholder_remains(monkeypatch):
    """Every retention-window span is replaced, not just the first."""
    monkeypatch.setattr(sessions, "SESSION_TTL", 300)
    html = _landing_page()
    windows = re.findall(r'<span class="retention-window">([^<]*)</span>', html)
    assert len(windows) >= 2, "expected the window stated in both summary and detail"
    assert set(windows) == {"5 minutes"}


def test_page_cannot_be_served_unrendered_via_static_mount():
    """There must be no route that serves the page without substitution.

    index.html previously sat inside the directory mounted at /static, so
    /static/index.html returned a fully working upload UI whose retention figure
    had never been substituted. FR-024 requires stating plainly how long data is
    kept, and that URL stated nothing. The template now lives outside the mount.
    """
    assert client.get("/static/index.html").status_code == 404
    assert not (main.STATIC_DIR / "index.html").exists()
    assert (main.TEMPLATE_DIR / "index.html").exists()


def test_every_served_page_states_a_concrete_duration(monkeypatch):
    """The rendered page always carries a number, never the raw placeholder."""
    monkeypatch.setattr(sessions, "SESSION_TTL", 60)
    windows = re.findall(
        r'<span class="retention-window">(.*?)</span>', _landing_page(), re.DOTALL
    )
    assert windows
    for window in windows:
        assert re.search(r"\d", window), f"served page has no duration: {window!r}"


@pytest.mark.parametrize(
    "forbidden",
    [
        "no data is retained",
        "nothing is written to disk",
        "we do not store",
        "your data is never stored",
        "fully anonymous",
        "end-to-end encrypted",
    ],
)
def test_disclosure_makes_no_claim_the_code_cannot_keep(forbidden):
    """Guard against the overstatement Principle IV was amended to prevent.

    The original constitution promised nothing was written to disk while the
    framework was already spooling large uploads to a temp file. These phrases
    are the ones that would reintroduce that class of untruth.
    """
    assert forbidden not in _landing_text()


def test_disclosure_admits_temporary_disk_use():
    """The spooling and temp-file writes are disclosed rather than omitted."""
    html = _landing_text()
    assert "temporary file" in html
    assert "temporary storage" in html


def test_disclosure_admits_crash_can_orphan_a_temp_file():
    """Temp files are created with delete=False and unlinked in a finally block.

    A SIGKILL between those two points leaves the copy on disk, so "restarting
    erases everything" would be false. Until a startup sweep exists, the notice
    must say so.
    """
    html = _landing_text()
    assert "crashes mid-read" in html
    assert "erases everything immediately" not in html


def test_disclosure_does_not_claim_unidentifiability():
    """A session cookie and an IP address are identifiers under GDPR.

    The earlier wording said "nothing here identifies you", which the cookie and
    the CDN's view of the visitor's IP both contradict.
    """
    html = _landing_text()
    assert "nothing here identifies you" not in html
    assert "ip address" in html


def test_disclosure_states_no_cross_user_pooling():
    """Principle IV scopes cross-user pooling out; the user is told so."""
    html = _landing_text()
    assert "never combined" in html
    assert "your file alone" in html


def test_disclosure_states_no_account_required():
    """Principle VI: anonymous use is first-class, and advertised as such."""
    assert "no account is needed" in _landing_text()
