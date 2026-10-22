#!/usr/bin/env python3
"""Connect an integration provider without curl.

PowerShell aliases `curl` to Invoke-WebRequest, which does not understand
-X/-H/-d, so the usual one-liner cannot be pasted on Windows. This does the
same three steps in stdlib Python: sign in, read the double-submit CSRF
cookie, PUT the provider configuration.

    # see which companies exist, and their ids
    python scripts/dev/connect_provider.py --email you@broker.test --list-companies

    # the phone channel, no telephony account needed
    python scripts/dev/connect_provider.py --email you@broker.test \
        --provider mock_voice --company <UUID> --secret s3cr3t-webhook-key

    # the e-mail channel, no mailbox needed
    python scripts/dev/connect_provider.py --email you@broker.test \
        --provider email_webhook --company <UUID> --secret s3cr3t-webhook-key

The password is prompted for, never passed on the command line, so it stays
out of the PowerShell history. Pair with send_test_call.py / send_test_email.py.
"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# email_webhook and imap are in COMPANY_ONLY_PROVIDERS; mock_voice and twilio
# are configured per company too in every normal setup.
WEBHOOK_PROVIDERS = ("email_webhook", "mock_voice", "twilio", "vonage", "twilio_sms")


def _request(opener, url, *, method="GET", body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("content-type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with opener.open(request, timeout=20) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--email", required=True, help="staff account with integrations.manage")
    parser.add_argument("--password", default="", help="prompted for when omitted")
    parser.add_argument("--list-companies", action="store_true")
    parser.add_argument("--provider", choices=WEBHOOK_PROVIDERS)
    parser.add_argument("--company", help="company UUID; required for these providers")
    parser.add_argument("--secret", help="value stored as webhook_secret")
    parser.add_argument("--disable", action="store_true", help="turn the provider off instead")
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
        print("finish it in the browser, this script only handles password-only accounts")
        return 1
    print("signed in")

    csrf = next((cookie.value for cookie in jar if cookie.name == "csrf_token"), "")
    if not csrf:
        print("no csrf_token cookie was issued — cannot continue")
        return 1

    if args.list_companies:
        companies = _request(opener, f"{base}/api/companies")
        rows = companies if isinstance(companies, list) else companies.get("items", [])
        if not rows:
            print("no companies found")
            return 1
        print(f"\n{'id':38}name")
        for row in rows:
            print(f"{str(row.get('id','')):38}{row.get('name_ar') or row.get('name_en') or ''}")
        return 0

    if not args.provider or not args.secret:
        print("--provider and --secret are required unless --list-companies is used")
        return 1
    if not args.company:
        print(f"--company is required for {args.provider}")
        return 1

    try:
        saved = _request(
            opener,
            f"{base}/api/staff/admin/integration-connections",
            method="PUT",
            body={
                "provider": args.provider,
                "company_id": args.company,
                "enabled": not args.disable,
                "credentials": {"webhook_secret": args.secret},
            },
            headers={"x-csrf-token": csrf},
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"save failed: {exc.code} {detail}")
        hint = {
            403: "the account lacks integrations.manage, or the CSRF cookie was not kept",
            404: "unknown company id",
            422: "provider rejected the payload — check the company scope rules",
        }.get(exc.code)
        if hint:
            print(f"hint: {hint}")
        return 1

    state = "disabled" if args.disable else "enabled"
    print(f"\n{args.provider} {state} for company {args.company}")
    print(f"status: {saved.get('connection_status', 'saved')}")
    if not args.disable:
        script = "send_test_email.py" if args.provider == "email_webhook" else "send_test_call.py"
        print(f"\nnow send one through it:\n  python scripts/dev/{script} \\")
        print(f"      --company {args.company} --secret {args.secret}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
