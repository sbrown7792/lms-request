#!/usr/bin/env python3
"""Render docs/social/og.html to the repository's social preview image.

GitHub wants 1280x640 (Settings -> Social preview). Rendered at 2x and scaled
back down, because headless Chrome's text rasterisation is noticeably cleaner
that way.

    python3 scripts/make_social.py        # needs Chrome/Chromium and Pillow
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "social" / "og.html"
OUTPUT = ROOT / "docs" / "social" / "social-preview.png"
WIDTH, HEIGHT = 1280, 640


def chrome() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser",
                 "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    sys.exit("no Chrome/Chromium on PATH")


def main() -> int:
    if not SOURCE.exists():
        sys.exit(f"missing {SOURCE}")
    with tempfile.TemporaryDirectory() as tmp:
        shot = Path(tmp) / "og.png"
        subprocess.run(
            [chrome(), "--headless=new", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--force-device-scale-factor=2",
             f"--window-size={WIDTH},{HEIGHT}", f"--user-data-dir={tmp}/profile",
             "--virtual-time-budget=3000", f"--screenshot={shot}",
             SOURCE.as_uri()],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        image = Image.open(shot).convert("RGB")
        if image.size != (WIDTH, HEIGHT):
            image = image.resize((WIDTH, HEIGHT), Image.LANCZOS)
        image.save(OUTPUT, "PNG", optimize=True)

    print(f"{OUTPUT.relative_to(ROOT)}  {image.size[0]}x{image.size[1]}  "
          f"{OUTPUT.stat().st_size // 1024}K")
    return 0


if __name__ == "__main__":
    sys.exit(main())
