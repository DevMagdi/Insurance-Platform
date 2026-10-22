#!/usr/bin/env python3
"""Send a signed inbound WhatsApp message to the Cloud API webhook.

Opens a request from the WhatsApp channel without a Meta app, a business
number, or a tunnel. The body is the real Cloud API envelope, so what the
endpoint parses here is what it parses in production.

    python scripts/dev/send_test_whatsapp.py \
        --company 11111111-1111-4111-8111-111111111111 \
        --secret  "$APP_SECRET" \
        --sender  +201004594694 \
        --body    "محتاج أعرف موقف طلب التعويض"

WhatsApp does NOT share the signature scheme the other webhooks use. Meta
signs the raw body with the app secret and no timestamp, under
`x-hub-signature-256`, so the secret here is the provider's `app_secret`
rather than its `webhook_secret`. Getting that wrong is an opaque 401,
which is most of the reason this script exists.

Requires `whatsapp_cloud` connected for the company with an `app_secret`,
and the background worker running for the job to become a request. The
`access_token` and `phone_number_id` the provider also stores are only read
when sending a reply, so placeholder values are fine for inbound tests.
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

# Arabic-Indic (٠-٩) and Extended Arabic-Indic (۰-۹) folded to ASCII.
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def sign(secret: str, raw: bytes) -> str:
    """Meta's signature: sha256 over the raw body alone, 'sha256=' prefixed."""
    digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def normalise_sender(value: str) -> str:
    """Meta sends wa_id as bare ASCII digits; the endpoint skips anything else.

    Arabic-Indic numerals are the trap worth handling here. Python counts
    ٠١٠٠ as digits, so both this script and the endpoint's own `isdigit()`
    would wave them through, and the request would be filed against a
    customer number no lookup can ever match. Fold them to ASCII instead.
    """
    folded = value.translate(_ARABIC_DIGITS)
    return "".join(
        character for character in folded if character.isascii() and character.isdigit()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--company", required=True, help="company UUID the provider is bound to"
    )
    parser.add_argument(
        "--secret",
        required=True,
        help="the provider's app_secret (NOT webhook_secret)",
    )
    parser.add_argument(
        "--sender",
        default="+201004594694",
        help="the number that wrote in; also how intake finds the customer",
    )
    parser.add_argument(
        "--sender-name",
        default="عميل تجريبي",
        help="WhatsApp profile name, often the only name the brokerage gets",
    )
    parser.add_argument(
        "--body",
        default="السلام عليكم، قدمت طلب تعويض من أسبوعين ولسه مجاليش رد.",
    )
    parser.add_argument(
        "--ticket",
        default="",
        help="continue an existing request instead of opening one",
    )
    parser.add_argument(
        "--message-id", default="", help="Meta's wamid; defaults to a fresh one"
    )
    args = parser.parse_args()

    sender = normalise_sender(args.sender)
    if not sender:
        print(f"error: --sender {args.sender!r} has no digits to send from")
        return 2
    if not args.body.strip():
        # The endpoint drops a message carrying neither text nor media, and
        # the whole delivery then 422s with nothing supported in it.
        print("error: --body is empty, and a message with no text is discarded")
        return 2

    message_id = args.message_id or f"wamid.dev-{uuid.uuid4()}"
    message: dict[str, object] = {
        # Idempotency is keyed on this: the same id twice is deduplicated.
        "id": message_id,
        "from": sender,
        "timestamp": str(int(time.time())),
        "type": "text",
        # A quoted REQ-/CMP- number inside the text joins that request on its
        # own; --ticket is the explicit form of the same thing.
        "text": {"body": args.body},
    }
    if args.ticket:
        message["context"] = {"ticket_no": args.ticket.upper()}

    document = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "dev-waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [
                                {
                                    "wa_id": sender,
                                    "profile": {"name": args.sender_name},
                                }
                            ],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }

    raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    url = f"{args.base_url.rstrip('/')}/api/webhooks/whatsapp_cloud/{args.company}"
    request = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": sign(args.secret, raw),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            print(f"{response.status} {response.read().decode('utf-8')}")
    except urllib.error.HTTPError as exc:
        print(f"{exc.code} {exc.read().decode('utf-8', 'replace')}")
        hint = {
            404: "whatsapp_cloud not connected for this company, or no app_secret",
            401: "signature rejected — --secret must be the app_secret",
            422: "payload rejected — needs a digit-only sender and a message body",
        }.get(exc.code)
        if hint:
            print(f"hint: {hint}")
        return 1
    except urllib.error.URLError as exc:
        print(f"error: cannot reach {url}: {exc.reason}")
        print("hint: is the API up, and is --base-url the address it listens on?")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
