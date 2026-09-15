#!/usr/bin/env python3
"""Regenerate the README diagrams from their HTML sources.

    python docs/src/render.py

These used to be PNGs with no source anywhere in the repository, which meant the
only way to fix one was to redraw it — so nobody did, and both went stale. The
roster image said "Fifteen agents" for two releases after the roster held
sixteen, and the loops image pointed at `docs/architecture.md`, a path that has
never existed.

The sources are ordinary HTML so they can be edited by anyone and diffed like
anything else. Playwright is already a dependency of the browser agents, and
Chromium is already installed for them, so this adds nothing to the toolchain.

Run it after changing the roster. `test_the_readme_roster_matches_the_shipped_one`
will tell you when the *table* has drifted; nothing can tell you the picture has,
which is the whole reason to keep the source next to it.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOCS = HERE.parent

#: source -> (output, viewport width, viewport height)
DIAGRAMS = {
    "roster.html": ("roster.png", 1720, 1400),
    "loops.html": ("architecture-loops.png", 2000, 1180),
}

#: Retina. These are read on a GitHub page at roughly half their pixel width.
SCALE = 2


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright is not installed. It ships with the dev extra:\n"
            "    uv pip install -e '.[dev]'\n"
            "    npx playwright install chromium",
            file=sys.stderr,
        )
        return 1

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for source, (out, width, height) in DIAGRAMS.items():
            page = browser.new_page(
                viewport={"width": width, "height": height}, device_scale_factor=SCALE
            )
            page.goto((HERE / source).as_uri())
            # Webfonts are deliberately not used -- these render with the system
            # stack so the output is identical on any machine with a browser.
            page.wait_for_timeout(400)
            sheet = page.query_selector(".sheet")
            if sheet is None:
                print(f"{source}: no .sheet element to screenshot", file=sys.stderr)
                return 1
            sheet.screenshot(path=str(DOCS / out))
            print(f"  {source} -> docs/{out}")
            page.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
