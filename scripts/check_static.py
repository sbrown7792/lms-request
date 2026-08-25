#!/usr/bin/env python3
"""Static sanity check for the three front-end pages.

Two failure modes here are silent -- the view just quietly stops updating --
so they are worth checking mechanically:

  1. JavaScript reaching for an element id the markup does not define.
  2. A page fetching an API path the server does not serve.

    python3 scripts/check_static.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "lmsrequest" / "static"
PAGES = ["guest.html", "host.html", "tv.html"]

# Classes the JS toggles at runtime; each must exist in the stylesheet.
RUNTIME_CLASSES = ["show", "bad", "sel", "done", "tap", "on", "shout", "star", "req"]


def server_routes() -> set[str]:
    app = (ROOT / "lmsrequest" / "app.py").read_text()
    routes = set(re.findall(r'@app\.(?:get|post|delete|put)\("([^"]+)"\)', app))
    # Collapse path params so /api/host/queue/{index} matches a built URL.
    return {re.sub(r"\{[^}]+\}", "*", r) for r in routes}


def normalise(path: str) -> str:
    path = path.split("?")[0]
    # Anything interpolated becomes the wildcard the routes were collapsed to.
    path = re.sub(r"\$\{[^}]*\}", "*", path)
    return path.rstrip("/") or "/"


def main() -> int:
    problems: list[str] = []
    routes = {normalise(r) for r in server_routes()}

    for name in PAGES:
        text = (STATIC / name).read_text()

        defined = set(re.findall(r'\bid="([^"]+)"', text))
        used = set(re.findall(r'\$\("([^"]+)"\)', text))
        used |= set(re.findall(r'getElementById\("([^"]+)"\)', text))
        missing = sorted(used - defined)

        # Both quote styles, since template literals build the host URLs.
        fetched = set(re.findall(r'fetch\(\s*["\'`]([^"\'`]+)', text))
        fetched |= set(re.findall(r'(?:url|api)\(\s*["\'`]([^"\'`]+)', text))
        unknown = sorted(
            f for f in fetched
            if normalise(f).startswith("/") and normalise(f) not in routes
        )

        ok = not missing and not unknown
        print(f"{name:<12} {'ok' if ok else 'PROBLEM':<8} "
              f"ids:{len(defined):<3} refs:{len(used):<3} fetches:{len(fetched)}")
        for m in missing:
            problems.append(f"{name}: JS references #{m}, not in markup")
        for u in unknown:
            problems.append(f"{name}: fetches {u}, no matching route in app.py")

        stale = sorted(defined - used)
        if stale:
            print(f"  ids not touched by JS (fine if only styled): {stale}")

    css = (STATIC / "style.css").read_text()
    for cls in RUNTIME_CLASSES:
        if f".{cls}" not in css:
            problems.append(f"style.css: no rule for .{cls}, applied by JS")

    print()
    for p in problems:
        print("  " + p)
    print("FAIL" if problems else "all pages consistent")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
