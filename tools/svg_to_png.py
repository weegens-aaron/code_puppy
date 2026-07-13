"""Rasterize a render-capture SVG frame to PNG using a local Chromium.

The render-capture harness emits SVG (faithful, but not ideal for phone
viewing). This converts it to a crisp PNG. It auto-detects a Playwright /
Chromium binary; override with --chrome or the CHROME_PATH env var.

Usage:
    python tools/svg_to_png.py tools/render_out/frame.svg
    python tools/svg_to_png.py in.svg --out out.png --scale 2
"""

from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path


def _find_chrome() -> str:
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    patterns = [
        "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
        "/opt/pw-browsers/chromium-*/chrome-linux/headless_shell",
    ]
    for pattern in patterns:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    raise SystemExit(
        "No Chromium found. Set CHROME_PATH or pass --chrome. "
        "On this environment it lives under /opt/pw-browsers/."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("svg", type=Path, help="input SVG path")
    parser.add_argument("--out", type=Path, default=None, help="output PNG path")
    parser.add_argument("--scale", type=int, default=2, help="device scale factor")
    parser.add_argument("--chrome", default=None, help="path to chrome binary")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    svg = args.svg.resolve()
    png = (args.out or args.svg.with_suffix(".png")).resolve()
    chrome = args.chrome or _find_chrome()

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chrome)
        page = browser.new_page(device_scale_factor=args.scale)
        page.goto(svg.as_uri())
        element = page.query_selector("svg")
        element.screenshot(path=str(png))
        browser.close()
    print(f"Wrote {png}")


if __name__ == "__main__":
    main()
