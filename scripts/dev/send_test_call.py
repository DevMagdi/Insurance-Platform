#!/usr/bin/env python3
"""Send a signed inbound call event to the voice webhook.

Opens a request from the phone channel without a telephony account, a real
number, or a tunnel. `mock_voice` exists for exactly this and needs no
credentials beyond a webhook_secret, so the whole intake path — caller
matching, request creation, transcript — can be exercised locally.

    python scripts/dev/send_test_call.py \
        --company 11111111-1111-4111-8111-111111111111 \
        --secret  "$WEBHOOK_SECRET" \
        --caller  +201012345678 \
        --transcript "اتصلت بخصوص تأخر صرف التعويض"

Also drives a real provider in development: pass `--provider twilio`. Twilio's
own signature is only checked when an x-twilio-signature header is present, so
without one the endpoint falls back to this same timestamped HMAC.

Requires the provider connected for that company with a `webhook_secret`, and
the background worker running for the job to become a request.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
import uuid


def sign(secret: str, timestamp: str, raw: bytes) -> str:
    """HMAC the server expects: sha256 over `<timestamp>.<raw body>`."""
    return hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + raw, hashlib.sha256
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--company", required=True, help="company UUID the provider is bound to")
    parser.add_argument("--secret", required=True, help="the provider's webhook_secret")
    parser.add_argument(
        "--provider", default="mock_voice", choices=("mock_voice", "twilio", "vonage")
    )
    parser.add_argument("--caller", default="+201012345678", help="the number that rang in")
    parser.add_argument("--caller-name", default="", help="display name, when the carrier sends one")
    parser.add_argument(
        "--transcript",
        default="اتصلت للاستفسار عن طلب التعويض المقدم من أسبوعين ولم يصلني رد.",
    )
    parser.add_argument(
        "--ticket",
        default="",
        help="continue an existing request instead of opening one, e.g. REQ-2026-000100",
    )
    parser.add_argument("--duration", type=int, default=95, help="call length in seconds")
    parser.add_argument(
        "--no-consent",
        action="store_true",
        help="omit recording consent; intake routes the call to a human instead",
    )
    parser.add_argument("--event-id", default="", help="defaults to a fresh uuid")
    args = parser.parse_args()

    call_id = args.event_id or f"dev-call-{uuid.uuid4()}"
    document = {
        # Idempotency is keyed on this: the same id twice is deduplicated.
        "event_id": call_id,
        "provider_call_id": call_id,
        "caller": args.caller,
        "caller_name": args.caller_name,
        # Read as the spoken text; a quoted REQ-/CMP- number here is enough to
        # join an existing request even without --ticket.
        "transcript": args.transcript,
        "text": args.transcript,
        "consent_confirmed": not args.no_consent,
        "duration_seconds": args.duration,
        "outcome": "completed",
    }
    if args.ticket:
        document["ticket_no"] = args.ticket.upper()

    raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    timestamp = str(int(time.time()))

    url = f"{args.base_url.rstrip('/')}/api/webhooks/{args.provider}/{args.company}"
    request = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-webhook-timestamp": timestamp,
            "x-webhook-signature": sign(args.secret, timestamp, raw),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            print(f"{response.status} {response.read().decode('utf-8')}")
    except urllib.error.HTTPError as exc:
        print(f"{exc.code} {exc.read().decode('utf-8', 'replace')}")
        hint = {
            404: "provider not connected for this company, or no webhook_secret stored",
            401: "signature rejected — wrong secret, or clock drift over 300s",
            403: "signature rejected — wrong secret, or clock drift over 300s",
            422: "payload rejected — needs an event id and a caller number",
        }.get(exc.code)
        if hint:
            print(f"hint: {hint}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
