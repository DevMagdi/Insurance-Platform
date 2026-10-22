#!/usr/bin/env python3
"""Mint a Gmail refresh_token on your own machine, outside the console.

The console's «ربط حساب Google» button needs PUBLIC_APP_URL to be a address
Google can redirect a browser back to. On a laptop, behind a tunnel, or before
the callback URL is registered, that round trip is the part that breaks — not
the credentials. This does the same OAuth dance against a throwaway localhost
listener instead, prints the refresh_token, and stores nothing.

    python3 scripts/dev/gmail_refresh_token.py \
        --client-id     123-abc.apps.googleusercontent.com \
        --client-secret GOCSPX-xxxxxxxx

Register the printed redirect URI in Google Cloud Console first:
    APIs & Services -> Credentials -> your OAuth client (Web application)
    -> Authorized redirect URIs -> http://localhost:8711/callback

Then paste the token into «refresh_token» on the Gmail provider form and save.
"""

from __future__ import annotations

import argparse
import http.server
import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

# Exactly what app/services/integration_providers.py:1215 asks Google for, so
# the token this mints is accepted by the same provider class at runtime.
SCOPES = (
    "https://www.googleapis.com/auth/gmail.send "
    "https://www.googleapis.com/auth/gmail.modify "
    "openid email"
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class _Catcher(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        _Catcher.code = (query.get("code") or [None])[0]
        _Catcher.state = (query.get("state") or [None])[0]
        _Catcher.error = (query.get("error") or [None])[0]
        body = (
            "<h2>تم الاستلام — ارجع للطرفية</h2>"
            if _Catcher.code
            else f"<h2>فشل: {_Catcher.error}</h2>"
        )
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--port", type=int, default=8711)
    parser.add_argument(
        "--no-browser", action="store_true", help="print the URL instead of opening it"
    )
    args = parser.parse_args()

    redirect_uri = f"http://localhost:{args.port}/callback"
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": args.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            # Without these two Google returns an access token only, and the
            # second time round it stops returning a refresh token at all.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )

    print(f"\nredirect URI to register in Google Cloud Console:\n  {redirect_uri}\n")
    print(f"open this and approve:\n  {url}\n")

    server = http.server.HTTPServer(("localhost", args.port), _Catcher)
    done = threading.Event()

    def serve() -> None:
        # Not `handle_request()`: that answers exactly one request, and a
        # browser cheerfully asks for /favicon.ico as well. The first request
        # it consumed was not always the callback, which lost the code and
        # left this hanging until the timeout.
        while not done.is_set():
            server.handle_request()
            if _Catcher.code or _Catcher.error:
                done.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    if not args.no_browser:
        webbrowser.open(url)
    print(f"waiting for the redirect on localhost:{args.port} …")
    done.wait(timeout=300)
    server.server_close()

    if _Catcher.error or not _Catcher.code:
        print(f"\nauthorisation failed: {_Catcher.error or 'no code received'}")
        return 1
    if _Catcher.state != state:
        print("\nstate mismatch — aborting")
        return 1

    payload = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": _Catcher.code,
            "redirect_uri": redirect_uri,
            "client_id": args.client_id,
            "client_secret": args.client_secret,
        }
    ).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(TOKEN_URL, data=payload, method="POST"), timeout=20
        ) as response:
            token = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        print(f"\ntoken exchange failed: {exc.code} {exc.read().decode('utf-8', 'replace')}")
        return 1

    refresh = token.get("refresh_token")
    if not refresh:
        print(
            "\nGoogle returned no refresh_token. It only issues one on first consent — "
            "revoke the app at https://myaccount.google.com/permissions and run this again."
        )
        return 1
    print("\nrefresh_token:\n")
    print(refresh)
    print("\npaste it into «refresh_token» on the Gmail form, with client_id,")
    print("client_secret and «المرسل من» filled in, then save.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
