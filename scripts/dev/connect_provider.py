#!/usr/bin/env python3
"""Connect an integration provider without curl.

PowerShell aliases `curl` to Invoke-WebRequest, which does not understand
-X/-H/-d, so the usual one-liner cannot be pasted on Windows. This does the
same three steps in stdlib Python: sign in, read the double-submit CSRF
cookie, PUT the provider configuration.

    # which companies exist, and their ids
    python scripts/dev/connect_provider.py --email you@broker.test --list-companies

    # the phone channel, no telephony account needed
    python scripts/dev/connect_provider.py --email you@broker.test \
        --provider mock_voice --company <UUID> --secret s3cr3t-webhook-key

    # a real WhatsApp Cloud number
    python scripts/dev/connect_provider.py --email you@broker.test \
        --provider whatsapp_cloud --company <UUID> \
        --set access_token=EAAG... --set phone_number_id=1234567890 \
        --set app_secret=abc123 --set verify_token=my-verify-token

Any credential field the API accepts can be supplied with --set, repeated as
often as needed. The password is prompted for, never passed on the command
line, so it stays out of shell history.
"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

# Mirrors PROVIDER_REQUIRED_FIELDS in app/services/integration_providers.py.
# Checked here so a missing field is reported before the round trip.
REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "whatsapp_cloud": frozenset({"access_token", "phone_number_id", "app_secret"}),
    "email_webhook": frozenset({"webhook_secret"}),
    "twilio": frozenset(
        {"account_sid", "auth_token", "from_number", "status_callback_url"}
    ),
    "twilio_sms": frozenset({"account_sid", "auth_token", "from_number"}),
    "vonage": frozenset(
        {"application_id", "private_key", "from_number", "status_callback_url"}
    ),
    "mock_voice": frozenset(),
}

# Fields the API does not demand but nothing works without. The webhook
# authenticates with webhook_secret, and Meta's subscription handshake
# compares hub.verify_token against the stored verify_token.
PRACTICALLY_REQUIRED: dict[str, tuple[str, ...]] = {
    "mock_voice": ("webhook_secret",),
    "twilio": ("webhook_secret",),
    "vonage": ("webhook_secret",),
    "whatsapp_cloud": ("verify_token",),
}


def _request(opener, url, *, method="GET", body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("content-type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with opener.open(request, timeout=20) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _credentials(args: argparse.Namespace) -> dict[str, str]:
    values: dict[str, str] = {}
    if args.secret:
        values["webhook_secret"] = args.secret
    for item in args.set or []:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise SystemExit(f"--set expects key=value, got {item!r}")
        values[key.strip()] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--email", required=True, help="staff account with integrations.manage"
    )
    parser.add_argument("--password", default="", help="prompted for when omitted")
    parser.add_argument("--list-companies", action="store_true")
    parser.add_argument("--provider", choices=sorted(REQUIRED_FIELDS))
    parser.add_argument("--company", help="company UUID")
    parser.add_argument("--secret", help="shorthand for --set webhook_secret=...")
    parser.add_argument(
        "--set",
        action="append",
        metavar="KEY=VALUE",
        help="a credential field; repeat for each one",
    )
    parser.add_argument(
        "--disable", action="store_true", help="turn the provider off instead"
    )
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    password = args.password or getpass.getpass(f"password for {args.email}: ")
    try:
        result = _request(
            opener,
            f"{base}/api/auth/staff/login",
            method="POST",
            body={"email": args.email, "password": password},
        )
    except urllib.error.HTTPError as exc:
        print(f"login failed: {exc.code} {exc.read().decode('utf-8', 'replace')}")
        return 1
    step = result.get("next_step")
    if step and step != "authenticated":
        print(f"login needs a further step: {step}")
        print(
            "finish it in the browser; this script only handles password-only accounts"
        )
        return 1

    session = next((cookie for cookie in jar if cookie.name == "access_token"), None)
    if session is None:
        print("login returned no session cookie — cannot continue")
        return 1
    # A Secure cookie is never sent over http by urllib, so every later call
    # would arrive unauthenticated and the API would answer a confusing
    # "Authentication required" even though the password was correct.
    # Browsers exempt localhost from this rule; Python does not.
    if session.secure and urllib.parse.urlsplit(base).scheme != "https":
        print("signed in, but the session cookie is marked Secure and this URL is http")
        print(
            "Python will not send it, so the next call would fail as unauthenticated."
        )
        print("For local development set COOKIE_SECURE=false on the api service, e.g.")
        print("  docker-compose.override.yml:")
        print(
            "    services:\n      api:\n        environment:\n          COOKIE_SECURE: 'false'"
        )
        print("then: docker compose up -d api")
        return 1

    csrf = next((cookie.value for cookie in jar if cookie.name == "csrf_token"), "")
    if not csrf:
        print("no csrf_token cookie was issued — cannot continue")
        return 1
    print("signed in")

    if args.list_companies:
        # The authenticated catalogue, not the public /api/companies list:
        # that one needs no session at all and would report success even
        # when the credentials never reached the server.
        companies = _request(opener, f"{base}/api/staff/admin/companies")
        rows = companies if isinstance(companies, list) else companies.get("items", [])
        if not rows:
            print("no companies found")
            return 1
        print(f"\n{'id':38}name")
        for row in rows:
            print(
                f"{row.get('id', '')!s:38}{row.get('name_ar') or row.get('name_en') or ''}"
            )
        return 0

    if not args.provider:
        print("--provider is required unless --list-companies is used")
        return 1
    if not args.company:
        print("--company is required")
        return 1

    credentials = _credentials(args)
    missing = sorted(REQUIRED_FIELDS[args.provider] - set(credentials))
    if missing and not args.disable:
        print(f"{args.provider} also needs: {', '.join(missing)}")
        print("supply each one with --set key=value")
        return 1
    for field in PRACTICALLY_REQUIRED.get(args.provider, ()):
        if field not in credentials and not args.disable:
            print(
                f"note: {args.provider} usually needs {field} as well; continuing without it"
            )

    try:
        saved = _request(
            opener,
            f"{base}/api/staff/admin/integration-connections",
            method="PUT",
            body={
                "provider": args.provider,
                "company_id": args.company,
                "enabled": not args.disable,
                "credentials": credentials,
            },
            headers={"x-csrf-token": csrf},
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"save failed: {exc.code} {detail}")
        hint = {
            401: "the session cookie did not reach the server",
            403: "the account lacks integrations.manage, or the CSRF cookie was not kept",
            404: "unknown company id",
            422: "a credential field was rejected — check the spelling against the API",
        }.get(exc.code)
        if hint:
            print(f"hint: {hint}")
        return 1

    state = "disabled" if args.disable else "enabled"
    print(f"\n{args.provider} {state} for company {args.company}")
    print(f"status: {saved.get('connection_status', 'saved')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
