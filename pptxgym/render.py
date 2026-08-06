"""Headless rendering helper: pptx -> per-slide PNGs via soffice + pdftoppm.

Uses a throwaway LibreOffice profile per call so parallel invocations don't
fight over the default profile lock.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

DPI = 60

#: How many times a conversion is worth trying.  Not a guess at flakiness: a
#: run with sixteen render workers lost two decks of ten, one to a
#: `CalledProcessError` from `soffice` and one to a PDF that came back a page
#: short, and neither was ever asked a second time.
ATTEMPTS = 3


class RenderFailed(RuntimeError):
    """soffice or pdftoppm could not turn this deck into pictures.

    A distinct type because of what the caller does with it.  Everything here
    used to escape as whatever `subprocess` raised, and an unrecognised
    exception is recorded as `crashed` — "an exception nobody expected" — which
    parks the deck under the heading reserved for pipeline bugs.  A deck
    LibreOffice will not convert is not a pipeline bug; it is a fact about the
    deck, and it should be refused in the same voice as any other refusal.
    """


def render_pptx(pptx_path: str, out_dir: str, prefix: str = "slide",
                dpi: int = DPI, timeout: int = 180,
                expect: int | None = None,
                attempts: int = ATTEMPTS) -> list[str]:
    """Render every slide; returns sorted list of PNG paths named
    {prefix}-NN.png (NN is 1-based).

    `expect` is the slide count the caller knows the deck to have.  Pass it and
    a short render is a *failure of this attempt* rather than a success the
    caller discovers later — which is the difference between one deck retried
    and one deck lost.  The count was already being checked one layer up; doing
    it here is what lets the retry see it.
    """
    pptx_path = os.path.abspath(pptx_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    why = "never ran"
    for attempt in range(1, attempts + 1):
        # A fresh temporary directory, and so a fresh LibreOffice profile,
        # every attempt: retrying into the profile that just failed asks the
        # same question of the same state.
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td) / "profile"
            try:
                subprocess.run(
                    ["soffice", "--headless",
                     f"-env:UserInstallation=file://{profile}",
                     "--convert-to", "pdf", pptx_path, "--outdir", td],
                    check=True, capture_output=True, timeout=timeout,
                )
                pdf = Path(td) / (Path(pptx_path).stem + ".pdf")
                if not pdf.exists():
                    raise RenderFailed(f"soffice produced no pdf for {pptx_path}")
                for f in out.glob(f"{prefix}-*.png"):
                    f.unlink()          # a short attempt must not leave pages
                                        # behind for the next one to count
                subprocess.run(
                    ["pdftoppm", "-png", "-r", str(dpi), str(pdf),
                     str(out / prefix)],
                    check=True, capture_output=True, timeout=timeout,
                )
            except subprocess.CalledProcessError as e:
                tail = (e.stderr or b"").decode("utf-8", "replace").strip()
                why = (f"{Path(e.cmd[0]).name} exited {e.returncode}"
                       + (f": {tail[-300:]}" if tail else ""))
            except subprocess.TimeoutExpired:
                why = f"conversion did not finish inside {timeout}s"
            except RenderFailed as e:
                why = str(e)
            else:
                pages = sorted(str(p) for p in out.glob(f"{prefix}-*.png"))
                if expect is None or len(pages) >= expect:
                    return pages
                why = f"rendered {len(pages)} of {expect} slides"
        if attempt < attempts:
            # Sixteen render workers that fail together will retry together
            # unless they are spread out; a second of stagger per attempt is
            # enough and costs nothing next to a conversion.
            time.sleep(attempt)
    raise RenderFailed(f"{Path(pptx_path).name}: {why} "
                       f"(after {attempts} attempts)")


def pixel_diff_ratio(png_a: str, png_b: str) -> float:
    """Fraction of pixels that differ noticeably between two same-size renders."""
    from PIL import Image, ImageChops

    a = Image.open(png_a).convert("RGB")
    b = Image.open(png_b).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a, b).convert("L")
    hist = diff.histogram()
    changed = sum(hist[24:])          # ignore antialiasing-level noise
    total = a.size[0] * a.size[1]
    return changed / total
