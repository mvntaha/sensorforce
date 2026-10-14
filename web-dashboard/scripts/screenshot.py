"""Capture page screenshots for the evidence folder.

Two things about headless Chrome shape this script:

* ``--screenshot`` silently ignores a relative path and writes nothing, so
  every path handed to Chrome is resolved to an absolute one first.
* Chrome on Windows will not open a window narrower than about 500 CSS pixels,
  so a 360 px phone viewport cannot be produced with ``--window-size`` alone.
  Narrow widths are rendered inside an iframe of exactly the requested width,
  which gives the inner document a real viewport of that size, media queries
  included, and the screenshot is cropped back to it.

A ``file://`` wrapper cannot embed an ``http://`` page, so when the target is a
URL the wrapper is served over HTTP from a throwaway local server.

Usage:
    python scripts/screenshot.py page.html out.png --width 360 --height 780
    python scripts/screenshot.py http://127.0.0.1:5000/ out.png --width 1280
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)

#: Below this width Chrome silently widens the window, so we use an iframe.
MIN_NATIVE_WIDTH = 520

IFRAME_WRAPPER = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>html,body{{margin:0;padding:0;background:#fff;overflow:hidden}}
iframe{{display:block;border:0;width:{width}px;height:{height}px}}</style>
</head><body><iframe src="{src}" title="preview"></iframe></body></html>
"""


def find_browser() -> str:
    """Return the first Chrome or Edge binary on this machine."""
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("chrome") or shutil.which("chromium") or shutil.which("google-chrome")
    if found:
        return found
    raise RuntimeError("No Chrome or Edge binary found; cannot capture screenshots.")


def is_url(page: str | Path) -> bool:
    """True when the target is an http(s) address rather than a local file."""
    return str(page).startswith(("http://", "https://"))


def page_url(page: str | Path) -> str:
    """Return a URL for a local file or pass an http(s) address through."""
    return str(page) if is_url(page) else Path(page).resolve().as_uri()


@contextlib.contextmanager
def _serve(directory: Path) -> Iterator[str]:
    """Serve ``directory`` over HTTP for the lifetime of the block."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()


#: Headless Chrome reports its own colour-scheme preference and ignores the
#: flags offered to change it, so the theme is chosen by the page instead:
#: ``?theme=light`` or ``?theme=dark`` on the URL. See web/static/js/theme.js.
COLOR_SCHEMES = ("light", "dark")


def themed(url: str, color_scheme: str) -> str:
    """Return ``url`` with the theme stated, so the shot is not left to chance."""

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}theme={color_scheme}"


def _run_chrome(browser: str, url: str, output: Path, width: int, height: int) -> None:
    """Drive Chrome once. ``output`` must already be absolute."""
    result = subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--virtual-time-budget=5000",
            f"--window-size={width},{height}",
            f"--screenshot={output}",
            url,
        ],
        check=True,
        capture_output=True,
    )
    if not output.is_file():
        raise RuntimeError(
            f"Chrome reported success but wrote nothing to {output}. "
            f"stderr: {result.stderr.decode(errors='replace')[-400:]}"
        )


def capture(
    page: str | Path,
    output: Path,
    width: int = 1280,
    height: int = 860,
    color_scheme: str = "light",
) -> Path:
    """Render ``page`` at ``width`` x ``height`` and write a PNG to ``output``."""

    browser = find_browser()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    url = page_url(page)
    if is_url(page):
        url = themed(url, color_scheme)

    if width >= MIN_NATIVE_WIDTH:
        _run_chrome(browser, url, output, width, height)
        return output

    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        wrapper_dir = Path(tmp)
        (wrapper_dir / "wrapper.html").write_text(
            IFRAME_WRAPPER.format(width=width, height=height, src=url), encoding="utf-8"
        )
        raw = wrapper_dir / "raw.png"

        # A file:// page cannot embed an http:// document, so when the target
        # is a URL the wrapper is served over HTTP as well.
        if is_url(page):
            with _serve(wrapper_dir) as base:
                _run_chrome(browser, f"{base}/wrapper.html", raw, MIN_NATIVE_WIDTH, height)
        else:
            _run_chrome(
                browser, (wrapper_dir / "wrapper.html").as_uri(), raw, MIN_NATIVE_WIDTH, height
            )

        with Image.open(raw) as image:
            image.crop((0, 0, width, height)).save(output)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page", help="HTML file or http(s) URL to render")
    parser.add_argument("output", type=Path, help="PNG file to write")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=860)
    parser.add_argument("--color-scheme", choices=list(COLOR_SCHEMES), default="light")
    args = parser.parse_args(argv)

    written = capture(args.page, args.output, args.width, args.height, args.color_scheme)
    print(f"wrote {written} at {args.width}x{args.height} ({args.color_scheme})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
