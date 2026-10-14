"""Render a page template to a standalone HTML file for screenshots.

Phase 1 has no Flask routes yet, so the templates are rendered directly with
Jinja and the ``url_for`` helper is stubbed to point at the real files on disk.
This exists so design evidence can be captured before the app is wired up; once
routes land, screenshots come from the running server instead.

Usage:
    python scripts/render_preview.py login out/login.html [--dark]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "web" / "templates"
STATIC = REPO_ROOT / "web" / "static"


def make_url_for():
    """Return a ``url_for`` stub that resolves static files and names routes."""

    def url_for(endpoint: str, **values: str) -> str:
        if endpoint == "static":
            target = STATIC / values["filename"]
            return Path(target).resolve().as_uri()
        return f"#{endpoint}"

    return url_for


def render(page: str, output: Path, *, dark: bool = False, context: dict | None = None) -> Path:
    """Render ``<page>.html`` into ``output`` and return the path written."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )
    env.globals.update(
        url_for=make_url_for(),
        get_flashed_messages=lambda **_: [],
        csrf_token="preview-csrf-token",  # noqa: S106 - preview stub, not a credential
        current_user=None,
        nav_items=[],
        theme_class="dark" if dark else "",
    )
    html = env.get_template(f"{page}.html").render(**(context or {}))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page", help="template name without the .html suffix")
    parser.add_argument("output", type=Path, help="file to write")
    parser.add_argument("--dark", action="store_true", help="render with the dark theme")
    args = parser.parse_args(argv)

    written = render(args.page, args.output, dark=args.dark)
    print(f"wrote {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
