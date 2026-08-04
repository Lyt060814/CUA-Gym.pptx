"""What WPS changes just by opening and saving a deck.

`roundtrip.py` measures the same thing through LibreOffice, because that is
the renderer we can drive headlessly.  It is a proxy, and the numbers it
produces set every position tolerance the reward will use — so it is worth
knowing how far off the proxy is.  WPS is the application the tasks are
actually solved in.

WPS has no working headless converter on Linux: `wpp --headless` exits
silently and `--convert-to` is not implemented in the Linux build.  What does
work is the same thing a solver does — open the file in the GUI, press
Ctrl+S — driven on a virtual display so it never touches anyone's desktop.

Needs `Xvfb` and `xdotool`.  On this machine only xdotool was installed; a
first attempt without a virtual display sat on the real one for four minutes
and died on a first-run dialog.

    python3 -m pptxgym.wps_roundtrip work/deck0001/source.pptx
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

WPP = "/opt/kingsoft/wps-office/office6/wpp"
DISPLAY = ":99"
SCREEN = "1920x1200"
# fixed screen, so these are stable: the notes pane along the bottom and the
# save button in the quick-access row left of the ribbon tabs
NOTES_XY = (500, 1143)
SAVE_XY = (139, 50)
DIRTY_MARK = "ZZ"
LOAD_WAIT = 20          # the font-check dialog arrives seconds after the window


class WpsUnavailable(RuntimeError):
    pass


def _have(*names) -> list[str]:
    return [n for n in names if not shutil.which(n)]


def preflight() -> list[str]:
    """What is missing, in the words someone would need to fix it."""
    out = []
    missing = _have("Xvfb", "xdotool")
    if missing:
        out.append(f"not installed: {', '.join(missing)} "
                   f"(sudo apt-get install -y xvfb xdotool)")
    if not Path(WPP).exists() and not shutil.which("wpp"):
        out.append("WPS Presentation not found")
    return out


class _Screen:
    """A private X display, so nothing appears on the user's desktop."""

    def __init__(self, num: str = DISPLAY):
        self.num = num
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            ["Xvfb", self.num, "-screen", "0", SCREEN + "x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            time.sleep(0.25)
            if subprocess.run(["xdotool", "search", "--name", "."],
                              env={**os.environ, "DISPLAY": self.num},
                              capture_output=True).returncode in (0, 1):
                break
        return self

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        return False

    @property
    def env(self):
        return {**os.environ, "DISPLAY": self.num}


def _xdo(env, *args) -> str:
    return subprocess.run(["xdotool", *args], env=env,
                          capture_output=True, text=True).stdout.strip()


def _dismiss_dialogs(env, rounds: int = 4) -> list[str]:
    """Close whatever WPS puts up before it will do anything else.

    The first launch shows a font check ("Some formula symbols might not be
    displayed correctly due to missing fonts Symbol") and sits there.  Its
    Close button ignores clicks and Escape, and `xdotool windowkill` takes the
    whole application down with it — it kills the X client, not the window.
    `windowclose` sends WM_DELETE_WINDOW and the dialog goes quietly.
    """
    seen = []
    for _ in range(rounds):
        ids = [w for w in _xdo(env, "search", "--onlyvisible", "--name",
                               "System Check|Tip|Prompt").splitlines() if w]
        if not ids:
            break
        for wid in ids:
            seen.append(_xdo(env, "getwindowname", wid) or wid)
            _xdo(env, "windowclose", wid)
        time.sleep(2)
    return seen


def roundtrip_wps(pptx: str, timeout: int = 240) -> Path:
    """Open the deck in WPS, save it, return the saved copy."""
    missing = preflight()
    if missing:
        raise WpsUnavailable("; ".join(missing))

    src = Path(pptx).resolve()
    work = Path(tempfile.mkdtemp(prefix="wpsrt-"))
    target = work / src.name
    shutil.copy2(src, target)
    before = target.stat().st_mtime_ns

    binary = WPP if Path(WPP).exists() else "wpp"
    with _Screen() as screen:
        env = screen.env
        proc = subprocess.Popen([binary, str(target)], env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + timeout
            # The main window exists almost immediately, but the font-check
            # dialog arrives several seconds later and is modal — polling for
            # the window and pressing on swallowed every click that followed.
            # Wait for the document to be up, then clear dialogs, then insist
            # nothing modal is left before touching anything.
            time.sleep(LOAD_WAIT)
            _dismiss_dialogs(env, rounds=4)
            found = [w for w in _xdo(env, "search", "--onlyvisible",
                                     "--name", "Presentation").splitlines() if w]
            if not found:
                raise WpsUnavailable(
                    f"WPS never showed a window for {src.name}")
            left = [w for w in _xdo(env, "search", "--onlyvisible", "--name",
                                    "System Check|Tip|Prompt").splitlines() if w]
            if left:
                raise WpsUnavailable(
                    f"a modal dialog would not close: "
                    f"{[_xdo(env, 'getwindowname', w) for w in left]}")

            # Dirty the document, then undo the edit by hand.
            #
            # Neither Ctrl+S nor the toolbar's save button writes anything
            # while the file is unmodified — WPS treats saving an untouched
            # document as a no-op, exactly as PowerPoint does.  Three rounds
            # of debugging went into blaming focus, the window manager and
            # synthetic key events before that turned out to be the whole
            # story: clicks and keys had been working the entire time.
            #
            # Typing into the notes pane and deleting it again leaves the
            # deck's content where it was and the dirty flag set.
            _xdo(env, "mousemove", str(NOTES_XY[0]), str(NOTES_XY[1]))
            _xdo(env, "click", "1")
            time.sleep(2)
            _xdo(env, "type", "--delay", "120", DIRTY_MARK)
            time.sleep(2)
            for _ in range(len(DIRTY_MARK) + 2):
                _xdo(env, "key", "--clearmodifiers", "BackSpace")
            time.sleep(2)

            _xdo(env, "mousemove", str(SAVE_XY[0]), str(SAVE_XY[1]))
            _xdo(env, "click", "1")
            time.sleep(3)
            _dismiss_dialogs(env, rounds=1)     # "keep this format?", if asked

            while time.time() < deadline:
                if target.stat().st_mtime_ns != before:
                    time.sleep(4)          # let the write settle
                    return target
                time.sleep(2)
            raise WpsUnavailable(f"WPS never wrote {target.name} within {timeout}s")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


def check(pptx: str) -> dict:
    """Round-trip through WPS and report what it did, in `roundtrip`'s terms."""
    from . import roundtrip as lo

    saved = roundtrip_wps(pptx)
    rep = lo.compare(pptx, str(saved))
    rep["renderer"] = "wps"
    structural = (rep["counts"].get("missing", 0)
                  + rep["counts"].get("kind_changed", 0)
                  + rep["counts"].get("text_changed", 0)
                  + abs(rep["slides_before"] - rep["slides_after"]))
    rep["structural"] = structural
    rep["verdict"] = ("fragile" if structural or rep["changed_frac"] > 0.25
                      else "noisy" if rep["changed_frac"] > 0.05
                      else "stable")
    return rep


def main():
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pptx", nargs="+")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    missing = preflight()
    if missing:
        raise SystemExit("cannot run: " + "; ".join(missing))
    for f in args.pptx:
        try:
            r = check(f)
        except WpsUnavailable as e:
            print(f"{Path(f).name[:44]:<46}FAILED — {e}")
            continue
        if args.json:
            print(json.dumps(r, ensure_ascii=False))
        else:
            print(f"{Path(f).name[:44]:<46}{r['verdict']:<9}"
                  f"{r['changed']}/{r['shapes']} ({r['changed_frac']:.1%})  "
                  f"{r['counts']}")


if __name__ == "__main__":
    main()
