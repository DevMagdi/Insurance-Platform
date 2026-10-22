#!/usr/bin/env python3
"""Ask Twilio to ring your own phone and run the platform's intake script.

Dialling a Twilio number from Egypt costs international rates on your own
line, which makes testing the telephone channel needlessly expensive. Turning
the call around costs nothing: receiving is free here, and the leg Twilio
dials comes out of the free trial credit. The call is answered by the same
TwiML the platform serves to a genuine inbound caller, so what you are
exercising is the real intake path, not a simulation.

    python scripts/dev/place_test_call.py \
        --account-sid ACxxxxxxxx --from "+15005550006" \
        --to "+201004594694" \
        --voice-url "https://abc.ngrok-free.app/api/webhooks/twilio/<COMPANY-UUID>/voice"

A trial account may only call numbers verified in the console, which your own
mobile already is. Answer, press 1 to consent, speak, then hang up.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import re
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
E164 = re.compile(r"\A\+[1-9]\d{6,14}\Z")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--account-sid", required=True, help="ACxxxxxxxx, from the console"
    )
    parser.add_argument("--auth-token", default="", help="prompted for when omitted")
    parser.add_argument(
        "--from", dest="from_number", required=True, help="your Twilio number"
    )
    parser.add_argument("--to", required=True, help="the mobile to ring, in E.164")
    parser.add_argument(
        "--voice-url",
        required=True,
        help="the platform's /api/webhooks/twilio/<company>/voice URL, over https",
    )
    args = parser.parse_args()

    for label, number in (("--from", args.from_number), ("--to", args.to)):
        if not E164.fullmatch(number):
            print(f"{label} must be E.164, like +201004594694")
            return 1
    if not args.voice_url.startswith("https://"):
        # Twilio refuses plaintext callbacks, and the signature the platform
        # checks is computed over an https URL.
        print("--voice-url must be https; expose the API with a tunnel first")
        return 1

    token = args.auth_token or getpass.getpass("Twilio auth token: ")
    body = urllib.parse.urlencode(
        {
            "To": args.to,
            "From": args.from_number,
            "Url": args.voice_url,
            "Method": "POST",
        }
    ).encode("ascii")
    request = urllib.request.Request(
        ENDPOINT.format(sid=args.account_sid), data=body, method="POST"
    )
    credentials = f"{args.account_sid}:{token}".encode()
    request.add_header(
        "authorization", "Basic " + base64.b64encode(credentials).decode("ascii")
    )
    request.add_header("content-type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            created = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"Twilio refused the call: {exc.code} {detail}")
        hints = {
            400: "a trial account may only call verified numbers, and Egypt may need "
            "enabling under Voice geographic permissions",
            401: "the account SID or auth token is wrong",
            403: "the From number is not yours, or is not voice capable",
        }
        if exc.code in hints:
            print(f"hint: {hints[exc.code]}")
        return 1

    print(f"calling {args.to} — call sid {created.get('sid', '')}")
    print("answer, press 1 to consent to the recording, speak, then hang up")
    print("the request appears once the worker picks the job up, within a minute")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
