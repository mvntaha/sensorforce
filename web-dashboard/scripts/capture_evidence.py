"""Capture screenshots of the signed-in pages from a running server.

Headless Chrome has no way to sign in from the command line, so pointing it at
a protected page just photographs the login form. This script signs in with
Python, then puts a small authenticating proxy in front of the application:
Chrome talks to the proxy, the proxy forwards every request with the session
cookie attached. Nothing in the application changes, and no authentication is
bypassed - the proxy holds a real session obtained with real credentials.

Because Chrome reaches the live application, the JavaScript on each page works,
so the dashboard chart appears in the evidence exactly as a user would see it.

Usage:
    python scripts/capture_evidence.py --base http://127.0.0.1:5111 \
        --username admin --password secret
"""

from __future__ import annotations

import argparse
import contextlib
import http.cookiejar
import http.server
import re
import socketserver
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - convenience for direct runs
    sys.path.insert(0, str(REPO_ROOT))

from scripts.screenshot import capture  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "docs" / "evidence"

CSRF_INPUT = re.compile(r'name="csrf_token" value="([^"]+)"')

#: page name -> path on the server
PAGES: dict[str, str] = {
    "scan": "/",
    "dashboard": "/dashboard",
    "item-new": "/item/new",
    "inventory": "/inventory",
    "sensors": "/sensors",
    "alerts": "/alerts",
    "planner": "/planner",
    "reports": "/reports",
    "item": "/item/FG-0001",
    "admin": "/admin/",
}

#: Headers that belong to one hop and must not be forwarded.
HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "host"}
)


def sign_in(base: str, username: str, password: str) -> urllib.request.OpenerDirector:
    """Return an opener holding a signed-in session."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    page = opener.open(f"{base}/login", timeout=20).read().decode("utf-8")
    match = CSRF_INPUT.search(page)
    if not match:
        raise RuntimeError("the sign-in page did not contain a CSRF token")

    form = urllib.parse.urlencode(
        {"username": username, "password": password, "csrf_token": match.group(1)}
    ).encode()
    opener.open(f"{base}/login", form, timeout=20)

    landing = opener.open(f"{base}/", timeout=20)
    if "/login" in landing.geturl():
        raise RuntimeError("sign-in failed: check the username and password")
    return opener


def _handler(base: str, opener: urllib.request.OpenerDirector):
    class AuthenticatingProxy(http.server.BaseHTTPRequestHandler):
        """Forwards every request to the application with the session attached."""

        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:
            """Stay quiet; the capture output is the interesting part."""

        def _forward(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None

            # The scheme is fixed by `base`, which this script controls.
            request = urllib.request.Request(  # noqa: S310
                base + self.path, data=body, method=method
            )
            for name, value in self.headers.items():
                if name.lower() not in HOP_BY_HOP:
                    request.add_header(name, value)

            try:
                with opener.open(request, timeout=30) as upstream:
                    payload = upstream.read()
                    status, headers = upstream.status, upstream.headers
            except urllib.error.HTTPError as error:
                payload = error.read()
                status, headers = error.code, error.headers

            self.send_response(status)
            for name, value in headers.items():
                if name.lower() not in HOP_BY_HOP and name.lower() != "content-length":
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._forward("GET")

        def do_POST(self) -> None:
            self._forward("POST")

    return AuthenticatingProxy


@contextlib.contextmanager
def authenticated_proxy(base: str, opener: urllib.request.OpenerDirector) -> Iterator[str]:
    """Run the proxy for the lifetime of the block and yield its base URL."""

    class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Threaded(("127.0.0.1", 0), _handler(base.rstrip("/"), opener))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:5111")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--desktop-width", type=int, default=1280)
    parser.add_argument("--desktop-height", type=int, default=950)
    parser.add_argument("--mobile-width", type=int, default=360)
    parser.add_argument("--mobile-height", type=int, default=900)
    parser.add_argument(
        "--color-scheme",
        choices=["light", "dark", "both"],
        default="light",
        help="Which theme to photograph. 'both' suffixes the dark set with -dark.",
    )
    args = parser.parse_args(argv)

    opener = sign_in(args.base.rstrip("/"), args.username, args.password)

    # Headless Chrome reports a dark colour-scheme preference by default, so
    # the theme is stated rather than inherited; otherwise every screenshot in
    # the report would quietly come out dark.
    schemes = ["light", "dark"] if args.color_scheme == "both" else [args.color_scheme]

    with authenticated_proxy(args.base, opener) as proxy:
        for name, path in PAGES.items():
            for scheme in schemes:
                suffix = "-dark" if scheme == "dark" else ""
                capture(
                    f"{proxy}{path}",
                    args.output / f"{name}-desktop{suffix}.png",
                    args.desktop_width,
                    args.desktop_height,
                    scheme,
                )
                capture(
                    f"{proxy}{path}",
                    args.output / f"{name}-360px{suffix}.png",
                    args.mobile_width,
                    args.mobile_height,
                    scheme,
                )
            print(f"captured {name}")

    # The sign-in page is the one page nobody is signed in for, so it is taken
    # straight from the server rather than through the authenticating proxy,
    # which would redirect an already-signed-in visitor to the dashboard.
    for scheme in schemes:
        suffix = "-dark" if scheme == "dark" else ""
        capture(
            f"{args.base.rstrip('/')}/login",
            args.output / f"login-desktop{suffix}.png",
            args.desktop_width,
            args.desktop_height,
            scheme,
        )
        capture(
            f"{args.base.rstrip('/')}/login",
            args.output / f"login-360px{suffix}.png",
            args.mobile_width,
            args.mobile_height,
            scheme,
        )
    print("captured login")

    print(f"screenshots written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
