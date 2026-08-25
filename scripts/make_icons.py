#!/usr/bin/env python3
"""Rasterise lmsrequest/static/icons/favicon.svg into the PNG sizes and favicon.ico.

The SVG is the single source of truth; the PNGs are build products, committed so
that deploying LMS Request needs neither Chrome nor Pillow. Only run this after
editing the SVG.

    python3 scripts/make_icons.py

Needs a Chrome/Chromium binary (for rendering) and Pillow (for the .ico).
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ICONS = Path(__file__).resolve().parent.parent / "lmsrequest" / "static" / "icons"
SVG = ICONS / "favicon.svg"

# name -> pixel size
TARGETS = {
    "favicon-16.png": 16,
    "favicon-32.png": 32,
    "favicon-48.png": 48,
    "apple-touch-icon.png": 180,   # iOS home screen
    "icon-192.png": 192,           # webmanifest
    "icon-512.png": 512,           # webmanifest / splash
}

WRAPPER = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;padding:0;background:transparent}}
img{{display:block;width:{size}px;height:{size}px}}
</style></head><body><img src="favicon.svg"></body></html>"""


def chrome() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    sys.exit("no Chrome/Chromium binary found; cannot rasterise")


def main() -> int:
    if not SVG.exists():
        sys.exit(f"missing {SVG}")
    binary = chrome()

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copy(SVG, work / "favicon.svg")

        for name, size in TARGETS.items():
            page = work / f"{size}.html"
            page.write_text(WRAPPER.format(size=size))
            out = work / name
            subprocess.run(
                [
                    binary, "--headless=new", "--disable-gpu", "--no-sandbox",
                    "--hide-scrollbars",
                    "--default-background-color=00000000",  # keep corners clear
                    f"--window-size={size},{size}",
                    f"--screenshot={out}",
                    "--virtual-time-budget=2000",
                    str(page),
                ],
                check=True, capture_output=True,
            )
            shutil.copy(out, ICONS / name)
            print(f"  {name:<22} {size}x{size}")

    try:
        from PIL import Image
    except ImportError:
        print("Pillow not installed - skipped favicon.ico")
        return 0

    # One .ico holding 16/32/48 for browsers that still ask for /favicon.ico.
    base = Image.open(ICONS / "favicon-48.png").convert("RGBA")
    base.save(ICONS / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print("  favicon.ico            16/32/48")
    return 0


if __name__ == "__main__":
    sys.exit(main())
