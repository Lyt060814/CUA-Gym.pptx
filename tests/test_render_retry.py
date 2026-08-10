"""What `render_pptx` does when the conversion does not go well.

Written from a run that lost two decks of ten to rendering, neither of them
because the deck was bad:

  * deck0002 — `soffice` exited non-zero, the `CalledProcessError` escaped, and
    the deck was recorded `crashed`.  That status means "an exception nobody
    expected"; it is where pipeline bugs are parked, and a deck LibreOffice
    declines to convert is not a pipeline bug.
  * deck0010 — the PDF came back a page short.  The shortfall *was* noticed,
    one layer up, and reported honestly — but by then the conversion was over
    and nothing asked it a second time.

Neither deck was ever retried.  These tests pin both halves of the answer, and
the first one pins the boring case: with nothing wrong, one attempt, no sleep,
no retry.  A retry that fires on healthy input is a slower way to fail.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pptxgym import render


class _Soffice:
    """A stand-in for the two subprocesses, scripted per attempt.

    `script` is one entry per *attempt*: an int is how many pages that
    attempt's PDF turns into, an exception instance is what it raises instead.
    """

    def __init__(self, script, pages_wanted=3):
        self.script = list(script)
        self.pages_wanted = pages_wanted
        self.attempt = -1
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd[0])
        if cmd[0] == "soffice":
            self.attempt += 1
            plan = self.script[min(self.attempt, len(self.script) - 1)]
            if isinstance(plan, BaseException):
                raise plan
            outdir = Path(cmd[cmd.index("--outdir") + 1])
            src = Path(cmd[cmd.index("--convert-to") + 2])
            (outdir / (src.stem + ".pdf")).write_bytes(b"%PDF-1.4\n")
            self._pages = plan
        else:                                                   # pdftoppm
            prefix = Path(cmd[-1])
            prefix.parent.mkdir(parents=True, exist_ok=True)
            for i in range(1, self._pages + 1):
                prefix.with_name(f"{prefix.name}-{i:02d}.png").write_bytes(b"\x89PNG")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


@pytest.fixture()
def deck(tmp_path):
    src = tmp_path / "d.pptx"
    src.write_bytes(b"PK\x03\x04")
    return src, tmp_path / "out"


def _no_sleep(monkeypatch):
    monkeypatch.setattr(render.time, "sleep", lambda _s: None)


def test_healthy_input_runs_once(deck, monkeypatch, tmp_path):
    """The control. Nothing wrong -> one soffice call, no sleeping, 3 pages."""
    src, out = deck
    fake = _Soffice([3])
    _no_sleep(monkeypatch)
    monkeypatch.setattr(render.subprocess, "run", fake)
    slept = []
    monkeypatch.setattr(render.time, "sleep", slept.append)

    pages = render.render_pptx(str(src), str(out), "p", expect=3)

    assert len(pages) == 3
    assert fake.calls.count("soffice") == 1
    assert slept == []


def test_transient_exit_is_retried(deck, monkeypatch):
    """Two bad attempts then a good one is a rendered deck, not a lost one."""
    src, out = deck
    boom = subprocess.CalledProcessError(1, ["soffice"], b"", b"fatal error")
    fake = _Soffice([boom, boom, 3])
    _no_sleep(monkeypatch)
    monkeypatch.setattr(render.subprocess, "run", fake)

    pages = render.render_pptx(str(src), str(out), "p", expect=3)

    assert len(pages) == 3
    assert fake.calls.count("soffice") == 3


def test_short_render_is_retried(deck, monkeypatch):
    """deck0010's symptom: 2 of 3 pages is a failed attempt, not a result."""
    src, out = deck
    fake = _Soffice([2, 3])
    _no_sleep(monkeypatch)
    monkeypatch.setattr(render.subprocess, "run", fake)

    pages = render.render_pptx(str(src), str(out), "p", expect=3)

    assert len(pages) == 3
    assert fake.calls.count("soffice") == 2


def test_extra_page_render_is_retried(deck, monkeypatch):
    """A converter inventing a page is no more complete than dropping one."""
    src, out = deck
    fake = _Soffice([4, 3])
    _no_sleep(monkeypatch)
    monkeypatch.setattr(render.subprocess, "run", fake)

    pages = render.render_pptx(str(src), str(out), "p", expect=3)

    assert len(pages) == 3
    assert fake.calls.count("soffice") == 2


def test_short_render_leaves_no_pages_from_the_failed_attempt(deck, monkeypatch):
    """The retry must not be able to count the previous attempt's output.

    Without the unlink, attempt 1's two pages plus attempt 2's two pages sit in
    the same directory under the same names — and `p-01.png` written twice is
    still two files, so a deck that never renders past two pages would look
    like it had reached three the moment the names happened to differ.
    """
    src, out = deck
    fake = _Soffice([2, 2, 2])
    _no_sleep(monkeypatch)
    monkeypatch.setattr(render.subprocess, "run", fake)

    with pytest.raises(render.RenderFailed):
        render.render_pptx(str(src), str(out), "p", expect=3)
    assert len(list(out.glob("p-*.png"))) == 2


def test_persistent_failure_is_typed_not_a_bare_subprocess_error(deck, monkeypatch):
    """deck0002's symptom. The caller keys off the type to say `failed`
    rather than `crashed`, so the type is the fix."""
    src, out = deck
    boom = subprocess.CalledProcessError(1, ["soffice"], b"", b"loading failed")
    fake = _Soffice([boom])
    _no_sleep(monkeypatch)
    monkeypatch.setattr(render.subprocess, "run", fake)

    with pytest.raises(render.RenderFailed) as got:
        render.render_pptx(str(src), str(out), "p", expect=3)

    assert not isinstance(got.value, subprocess.CalledProcessError)
    assert fake.calls.count("soffice") == render.ATTEMPTS
    # and it says which command, and what it said
    assert "soffice exited 1" in str(got.value)
    assert "loading failed" in str(got.value)


def test_timeout_is_also_typed(deck, monkeypatch):
    src, out = deck
    fake = _Soffice([subprocess.TimeoutExpired(["soffice"], 180)])
    _no_sleep(monkeypatch)
    monkeypatch.setattr(render.subprocess, "run", fake)

    with pytest.raises(render.RenderFailed) as got:
        render.render_pptx(str(src), str(out), "p", expect=3)
    assert "did not finish" in str(got.value)


def test_no_expect_still_accepts_whatever_came_back(deck, monkeypatch):
    """Callers that do not know the slide count keep the old behaviour —
    a page count is not something `render_pptx` may invent."""
    src, out = deck
    fake = _Soffice([1])
    _no_sleep(monkeypatch)
    monkeypatch.setattr(render.subprocess, "run", fake)

    assert len(render.render_pptx(str(src), str(out), "p")) == 1
    assert fake.calls.count("soffice") == 1
