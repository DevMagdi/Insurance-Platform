#!/usr/bin/env python3
"""Run Meta's subscription handshake against the webhook, before Meta does.

Meta's "Verify and Save" button is all-or-nothing: it either turns green or
reports that your endpoint did not respond correctly, and it never says which
of the half-dozen reasons applied. This sends the identical GET and reports
the reason.

    python scripts/dev/verify_whatsapp_webhook.py \
        --base-url "https://your-tunnel.ngrok-free.dev" \
        --company 11111111-1111-4111-8111-111111111111 \
        --token  "the verify_token you stored"

Meta sends `hub.mode=subscribe`, `hub.verify_token` and `hub.challenge`, and
requires the challenge echoed back as the entire plain-text body. A JSON
wrapper, surrounding quotes, or a trailing newline all fail verification, so
the body is compared exactly rather than merely searched.

Both URL spellings the platform answers are checked, because Meta registers
one callback URL and there is no second chance to discover the other works.
"""

from __future__ import annotations

import argparse
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID

# Chat clients rewrite a pasted link as [url](url); pasted into a shell that
# becomes the literal argument and every request 404s against a nonsense host.
MARKDOWN_LINK = re.compile(r"\A\[(?P<label>[^\]]+)\]\((?P<href>[^)]+)\)\Z")

FAILURES = {
    403: (
        "verify token mismatch — the stored verify_token is not what you passed"
        " (or hub.mode was not 'subscribe')"
    ),
    404: "whatsapp_cloud is not connected for this company, or it is disabled",
    503: "the stored configuration could not be decrypted",
}


def check(url: str, token: str) -> tuple[bool, str]:
    """Return (verified, explanation) for one callback URL."""
    challenge = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)
    query = urllib.parse.urlencode(
        {
            "hub.mode": "subscribe",
            "hub.verify_token": token,
            "hub.challenge": challenge,
        }
    )
    request = urllib.request.Request(f"{url}?{query}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return False, f"HTTP {exc.code} — {FAILURES.get(exc.code, detail)}"
    except urllib.error.URLError as exc:
        return False, f"unreachable — {exc.reason}"

    if body == challenge:
        return True, f"HTTP {status}, challenge echoed exactly"
    if body.strip() == challenge:
        return False, (
            "the challenge is padded with whitespace or a newline; Meta compares"
            " the body exactly and will reject it"
        )
    if body.strip('"') == challenge:
        return False, (
            "the challenge came back JSON-quoted; Meta needs it as bare text"
        )
    return False, f"HTTP {status}, body was {body[:120]!r}, expected {challenge!r}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        required=True,
        help="the public origin Meta will call, e.g. https://x.ngrok-free.dev",
    )
    parser.add_argument("--company", required=True, help="company UUID")
    parser.add_argument(
        "--token", required=True, help="the verify_token stored on the provider"
    )
    args = parser.parse_args()

    base = args.base_url.strip()
    mangled = MARKDOWN_LINK.match(base)
    if mangled:
        print(f"error: --base-url arrived as a markdown link: {base}")
        print(f"hint: paste just the address — {mangled.group('href')}")
        return 2
    try:
        company = UUID(str(args.company))
    except ValueError:
        print(f"error: --company {args.company!r} is not a UUID")
        return 2

    base = base.rstrip("/")
    scheme = urllib.parse.urlsplit(base).scheme
    if scheme not in {"http", "https"}:
        print(f"error: --base-url needs an http:// or https:// scheme, got {base!r}")
        return 2

    results: list[tuple[str, bool, str]] = []
    for spelling in ("whatsapp", "whatsapp_cloud"):
        url = f"{base}/api/webhooks/{spelling}/{company}"
        verified, explanation = check(url, args.token)
        results.append((url, verified, explanation))
        print(f"{'PASS' if verified else 'FAIL'}  {url}\n      {explanation}")

    if not any(verified for _, verified, _ in results):
        return 1
    if scheme != "https":
        # Worth saying even on success: the handshake works, but Meta will
        # not accept the URL, and that looks like a different failure.
        print("\nnote: Meta only accepts an https callback URL; this one is http.")
        print("Run this again against your tunnel address before saving it in Meta.")
    else:
        print("\nPaste this as the Callback URL in WhatsApp -> Configuration:")
        print(f"  {results[0][0]}")
        print("and the same --token value as the Verify Token.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
