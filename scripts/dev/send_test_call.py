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

Attach real audio with `--recording ./clip.mp3`; the file is base64-encoded
into the event, stored encrypted against the call, and becomes playable in
the request's conversation. Consent is implied — the database refuses a
recording without it — so `--recording` and `--no-consent` are incompatible.

Also drives a real provider in development: pass `--provider twilio`. Twilio's
own signature is only checked when an x-twilio-signature header is present, so
without one the endpoint falls back to this same timestamped HMAC.

Requires the provider connected for that company with a `webhook_secret`, and
the background worker running for the job to become a request.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import pathlib
import time
import urllib.error
import urllib.request
import uuid

# The endpoint caps a webhook body at 1 MiB and base64 inflates by a third,
# so the raw clip has to leave room for the encoding and the JSON around it.
MAX_RECORDING_BYTES = 740_000

# Magic numbers for the containers the API serves inline. Mirrors the server's
# allowlist closely enough to warn early; the server decides for real.
_AUDIO_MAGIC: tuple[tuple[int, bytes, str], ...] = (
    (0, b"ID3", "mp3"),
    (0, b"RIFF", "wav"),
    (0, b"OggS", "ogg"),
    (0, b"fLaC", "flac"),
    (4, b"ftyp", "m4a"),
    (0, b"FORM", "aiff"),
)


def describe_audio(data: bytes) -> str | None:
    """Name the container, or None when these bytes are not known audio."""
    if len(data) < 12:
        return None
    for offset, magic, label in _AUDIO_MAGIC:
        if not data.startswith(magic, offset):
            continue
        if magic == b"RIFF" and not data.startswith(b"WAVE", 8):
            continue
        if magic == b"FORM" and not data.startswith(b"AIFF", 8):
            continue
        return label
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    return None


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
    parser.add_argument(
        "--provider", default="mock_voice", choices=("mock_voice", "twilio", "vonage")
    )
    parser.add_argument(
        "--caller", default="+201012345678", help="the number that rang in"
    )
    parser.add_argument(
        "--caller-name", default="", help="display name, when the carrier sends one"
    )
    parser.add_argument(
        "--transcript",
        default="اتصلت للاستفسار عن طلب التعويض المقدم من أسبوعين ولم يصلني رد.",
    )
    parser.add_argument(
        "--ticket",
        default="",
        help="continue an existing request instead of opening one, e.g. REQ-2026-000100",
    )
    parser.add_argument(
        "--duration", type=int, default=95, help="call length in seconds"
    )
    parser.add_argument(
        "--no-consent",
        action="store_true",
        help="omit recording consent; intake routes the call to a human instead",
    )
    parser.add_argument("--event-id", default="", help="defaults to a fresh uuid")
    parser.add_argument(
        "--recording",
        default="",
        help="path to an audio file to attach as the call recording",
    )
    args = parser.parse_args()

    recording_b64 = ""
    if args.recording:
        if args.no_consent:
            # The worker never even decodes audio without consent, and the
            # database refuses to store it, so this combination is a typo.
            print("error: --recording needs consent; drop --no-consent")
            return 2
        path = pathlib.Path(args.recording).expanduser()
        try:
            audio = path.read_bytes()
        except OSError as exc:
            print(f"error: cannot read {path}: {exc.strerror}")
            return 2
        if len(audio) > MAX_RECORDING_BYTES:
            print(
                f"error: {path.name} is {len(audio) / 1024:,.0f} KB; the webhook body"
                f" caps at 1 MiB and base64 adds a third, so keep the clip under"
                f" {MAX_RECORDING_BYTES // 1024} KB"
            )
            return 2
        if describe_audio(audio) is None:
            print(
                f"warning: {path.name} is not a recognised audio container, so the"
                " console will offer it as a download instead of playing it"
            )
        recording_b64 = base64.b64encode(audio).decode("ascii")

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
    if recording_b64:
        document["recording_b64"] = recording_b64
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
    except urllib.error.URLError as exc:
        # Nothing answered at all: a stack trace here tells the reader less
        # than the address they got wrong.
        print(f"error: cannot reach {url}: {exc.reason}")
        print("hint: is the API up, and is --base-url the address it listens on?")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
