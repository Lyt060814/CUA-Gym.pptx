"""Headless rendering helper: pptx -> per-slide PNGs via soffice + pdftoppm.

Uses a throwaway LibreOffice profile per call so parallel invocations don't
fight over the default profile lock.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

DPI = 60


def render_pptx(pptx_path: str, out_dir: str, prefix: str = "slide",
                dpi: int = DPI, timeout: int = 180) -> list[str]:
    """Render every slide; returns sorted list of PNG paths named
    {prefix}-NN.png (NN is 1-based)."""
    pptx_path = os.path.abspath(pptx_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        profile = Path(td) / "profile"
        subprocess.run(
            ["soffice", "--headless",
             f"-env:UserInstallation=file://{profile}",
             "--convert-to", "pdf", pptx_path, "--outdir", td],
            check=True, capture_output=True, timeout=timeout,
        )
        pdf = Path(td) / (Path(pptx_path).stem + ".pdf")
        if not pdf.exists():
            raise RuntimeError(f"soffice produced no pdf for {pptx_path}")
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), str(pdf), str(out / prefix)],
            check=True, capture_output=True, timeout=timeout,
        )
    return sorted(str(p) for p in out.glob(f"{prefix}-*.png"))


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
