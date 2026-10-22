#!/usr/bin/env python3
"""Send a signed inbound e-mail to the email_webhook endpoint.

Simulates a customer e-mailing the brokerage, so a request is created (or a
reply is appended to an existing one) through the real intake pipeline — the
same path a production mailbox takes once the worker picks the job up.

    # new request
    python scripts/dev/send_test_email.py \
        --company 11111111-1111-4111-8111-111111111111 \
        --secret   "$WEBHOOK_SECRET" \
        --from     customer@example.test \
        --subject  "تأخر صرف تعويض" \
        --body     "قدمت طلب تعويض من أسبوعين ولم يصلني رد."

    # reply on an existing ticket: quote its number in the subject
    python scripts/dev/send_test_email.py ... --subject "Re: REQ-2026-000100"

Requires the provider to be connected for that company with a `webhook_secret`,
and the background worker to be running for the job to become a ticket.
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
    parser.add_argument(
        "--company", required=True, help="company UUID the provider is bound to"
    )
    parser.add_argument("--secret", required=True, help="the provider's webhook_secret")
    parser.add_argument("--from", dest="sender", default="customer@example.test")
    parser.add_argument("--name", default="", help="display name, e.g. 'Sara Mounir'")
    parser.add_argument("--subject", default="طلب جديد عبر البريد")
    parser.add_argument(
        "--body", default="هذه رسالة اختبار للتأكد من وصول الطلبات عبر البريد."
    )
    parser.add_argument(
        "--event-id",
        default="",
        help="provider message id; repeating one is ignored as a duplicate "
        "(defaults to a fresh uuid)",
    )
    args = parser.parse_args()

    sender = f"{args.name} <{args.sender}>" if args.name else args.sender
    document = {
        # Idempotency is keyed on this: the same id twice is deduplicated, which
        # is why a fresh one is minted unless you pin it on purpose.
        "event_id": args.event_id or f"dev-{uuid.uuid4()}",
        "from": sender,
        "subject": args.subject,
        "text": args.body,
    }
    raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    timestamp = str(int(time.time()))

    url = f"{args.base_url.rstrip('/')}/api/webhooks/email_webhook/{args.company}"
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
        detail = exc.read().decode("utf-8", "replace")
        print(f"{exc.code} {detail}")
        hint = {
            404: "provider not connected for this company, or no webhook_secret stored",
            401: "signature rejected — wrong secret, or clock drift over 300s",
            403: "signature rejected — wrong secret, or clock drift over 300s",
            422: "payload rejected — needs event_id, from, and text/subject",
        }.get(exc.code)
        if hint:
            print(f"hint: {hint}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
