#!/usr/bin/env python3
"""Send a real WhatsApp message through Meta's Cloud API to your own phone.

This talks to Meta directly rather than to the platform, which is the point:
it proves the access token, the phone number id and the recipient allowlist
are right before any of them are blamed for a problem somewhere else.

    python scripts/dev/send_whatsapp_message.py \
        --phone-number-id 123456789012345 \
        --to "+201004594694"

With no --text that sends the `hello_world` template, which every new test
number already has approved. A template is also the only thing Meta accepts
until the recipient has written to the business within the last 24 hours:

    # after you have replied from your phone, free text is allowed
    python scripts/dev/send_whatsapp_message.py \
        --phone-number-id 123456789012345 \
        --to "+201004594694" --text "تمام، وصلت الرسالة"

The token is prompted for rather than taken from the command line, so it
stays out of shell history. Temporary tokens from the App Dashboard expire
after 24 hours; a system user token does not.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import urllib.error
import urllib.request

GRAPH = "https://graph.facebook.com/{version}/{phone_id}/messages"
E164 = re.compile(r"\A\+[1-9]\d{6,14}\Z")
VERSION = re.compile(r"\Av\d+\.\d+\Z")

# The errors a test-number setup actually produces, in the order they bite.
ERRORS = {
    131030: (
        "the recipient is not on the test number's allowlist — add it under"
        " WhatsApp > API Setup > To > Manage phone number list (max 5), and"
        " enter the code Meta sends"
    ),
    131047: (
        "the 24-hour customer service window is closed, so only a template"
        " may be sent — drop --text, or reply from the phone first"
    ),
    131026: (
        "undeliverable — the number is not on WhatsApp, or the format is"
        " wrong; use full international form with no leading zero"
    ),
    132001: (
        "no approved template by that name in that language — 'en' and"
        " 'en_US' are different languages to Meta"
    ),
    132000: "the template expects a different number of variables",
    190: (
        "the access token expired or is invalid — dashboard tokens last 24"
        " hours; generate a system user token for anything longer"
    ),
    10: "the app is missing the whatsapp_business_messaging permission",
    200: "the system user has no role on this WhatsApp Business Account",
    368: "the WhatsApp Business Account is restricted",
    100: "invalid parameter — check the phone number id and the recipient",
}


def explain(body: str) -> str:
    """Turn Meta's error envelope into one actionable line."""
    try:
        error = json.loads(body).get("error", {})
    except (json.JSONDecodeError, AttributeError):
        return body[:300]
    code = error.get("code")
    message = str(error.get("message") or "").strip()
    details = str(error.get("error_data", {}).get("details") or "").strip()
    known = ERRORS.get(code if isinstance(code, int) else -1)
    parts = [f"Meta error {code}: {message}" if code else message or body[:200]]
    if details and details not in message:
        parts.append(f"  details: {details}")
    if known:
        parts.append(f"  hint: {known}")
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phone-number-id",
        required=True,
        help="the numeric id from WhatsApp > API Setup, NOT the phone number",
    )
    parser.add_argument("--to", required=True, help="recipient in +E.164 form")
    parser.add_argument("--token", default="", help="prompted for when omitted")
    parser.add_argument(
        "--text", default="", help="free text; requires an open 24-hour window"
    )
    parser.add_argument(
        "--template", default="hello_world", help="used when --text is absent"
    )
    parser.add_argument("--language", default="en_US", help="template locale")
    parser.add_argument("--api-version", default="v21.0")
    args = parser.parse_args()

    phone_id = args.phone_number_id.strip()
    if not phone_id.isdecimal():
        # Pasting the visible test number here instead of its id is the
        # single most common setup mistake, and Meta answers it with a
        # generic 100 that names nothing.
        print(f"error: --phone-number-id must be digits only, got {phone_id!r}")
        print("hint: it is the id shown under the From number, not the number")
        return 2
    recipient = args.to.strip()
    if not E164.fullmatch(recipient):
        print(f"error: --to {recipient!r} is not +E.164, e.g. +201004594694")
        return 2
    if not VERSION.fullmatch(args.api_version):
        print(f"error: --api-version {args.api_version!r} should look like v21.0")
        return 2

    if args.text:
        payload: dict[str, object] = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": args.text},
        }
        describing = f"free text ({len(args.text)} chars)"
    else:
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "template",
            "template": {
                "name": args.template,
                "language": {"code": args.language},
            },
        }
        describing = f"template {args.template} [{args.language}]"

    token = args.token or getpass.getpass("WhatsApp access token: ")
    if not token.strip():
        print("error: an access token is required")
        return 2

    url = GRAPH.format(version=args.api_version, phone_id=phone_id)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token.strip()}",
        },
    )
    print(f"sending {describing} to {recipient}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(explain(exc.read().decode("utf-8", "replace")))
        return 1
    except urllib.error.URLError as exc:
        print(f"error: cannot reach {url}: {exc.reason}")
        return 1

    messages = result.get("messages") or [{}]
    contacts = result.get("contacts") or [{}]
    print("accepted by Meta")
    print(f"  message id: {messages[0].get('id', '?')}")
    print(f"  wa_id     : {contacts[0].get('wa_id', '?')}")
    print("\nIt should arrive on the handset within a few seconds.")
    print("Reply to it: that opens the 24-hour window and, once the webhook")
    print("is configured, the reply lands in the platform as a request.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
