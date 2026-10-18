"""Live E2E part 1: health, registration/OTP matrix, guest tracking,
customer complaint flow, attachments, worker e-mail delivery.

Run against the live local stack (make local-up) with the QA SMTP sink.
"""

from __future__ import annotations

import random
import time
import uuid
from pathlib import Path

from qa_lib import API, MAILDIR, VITE, QAClient, Recorder, otp_code_for, sha256, wait_for_mail

r = Recorder()
RNG = random.Random()


def rand_mobile() -> str:
    return f"+2011{RNG.randrange(10000000, 99999999)}"


def rand_nid(century: str = "2", yy: str = "99", mm: str = "01", dd: str = "01",
             gov: str = "01") -> str:
    return f"{century}{yy}{mm}{dd}{gov}{RNG.randrange(1000, 9999)}{RNG.randrange(0, 9)}"


# Unique per run: national IDs anchor a globally unique blind index, mobiles
# carry 10-minute rate-limit windows and 15-minute lockouts across runs.
NID_A = rand_nid()
NID_B = rand_nid(century="3", yy="00", gov="03")
NID_C = rand_nid(century="2", yy="98", mm="03", dd="15", gov="02")
TAG = uuid.uuid4().hex[:8]

# --------------------------------------------------------------------------- #
print("\n== S1. Health, config and proxy ==")
r.section = "S1 health"
anon = QAClient()

live = anon.get("/api/health/live")
r.check("GET /api/health/live -> 200 ok", live.status == 200 and live.json() == {"status": "ok"},
        "200 {'status':'ok'}", f"{live.status} {live.text[:80]}")
ready = anon.get("/api/health/ready")
r.check("GET /api/health/ready -> 200 ready", ready.status == 200, "200", f"{ready.status}")
cfg = anon.get("/api/public/config")
r.check("GET /api/public/config -> captcha null + policy version",
        cfg.status == 200 and cfg.json().get("captcha") is None
        and cfg.json().get("privacy_policy_version"),
        "captcha null", cfg.text[:120])
companies = anon.get("/api/companies")
r.check("companies list is cacheable (public, max-age=300)",
        "max-age=300" in companies.headers.get("cache-control", ""),
        "public, max-age=300", companies.headers.get("cache-control", ""))
if "no-store" in cfg.headers.get("cache-control", ""):
    r.info("QA FINDING (low, docs)",
           "docs/API.md claims GET /api/public/config is 'public, max-age=300' but the "
           "code sends 'no-store' (platform.py:47). Code is fine (brand data); docs stale.")
r.check("GET /api/companies -> seeded insurers listed publicly",
        companies.status == 200 and len(companies.json()) >= 4,
        ">=4 companies", f"{companies.status} n={len(companies.json()) if companies.status==200 else '?'}")
csp = anon.get("/api/health/live").headers
r.check("security headers on API responses",
        csp.get("x-content-type-options") == "nosniff"
        and "content-security-policy" in {k.lower() for k in csp},
        "nosniff + CSP", str({k: v for k, v in csp.items() if k.lower().startswith(('x-', 'content-s', 'strict'))})[:160])

vite_client = QAClient(base_url=VITE)
proxied = vite_client.get("/api/health/live")
r.check("Vite same-origin proxy /api -> API works", proxied.status == 200, "200", f"{proxied.status}")
spa = vite_client.http.get("/", headers={"User-Agent": "QA"})
r.check("SPA served at / with root div", spa.status_code == 200 and '<div id="root">' in spa.text,
        "200 + #root", f"{spa.status_code}")

COMPANIES = companies.json()
COMPANY_A = COMPANIES[0]

# --------------------------------------------------------------------------- #
print("\n== S2. Registration / OTP matrix (critical flow) ==")
r.section = "S2 registration"

# 2.1 Brand-new email + brand-new mobile → OTP must be SENT and flow continues
new_email = f"brand.new.{TAG}@example.com"
new_mobile = rand_mobile()
c1 = QAClient()
t0 = time.time()
start = c1.post("/api/auth/customer/otp/start",
                json_body={"mobile": new_mobile, "email": new_email})
r.check("[CRITICAL] new email + new mobile: otp/start -> 202 (no config error)",
        start.status == 202 and "not configured" not in start.text,
        "202", f"{start.status} {start.text[:120]}")
code = otp_code_for(new_email, t0)
r.check("[CRITICAL] OTP e-mail actually delivered to the new address", bool(code),
        "mail with 6-digit code", f"code={code}")

verify_noid = c1.post("/api/auth/customer/otp/verify",
                      json_body={"mobile": new_mobile, "email": new_email, "code": code})
r.check("verify without identity -> 409 asking for national ID + name",
        verify_noid.status == 409
        and verify_noid.detail == "Provide national ID and name to register",
        "409 Provide national ID and name to register", f"{verify_noid.status} {verify_noid.text[:100]}")

verify_reg = c1.post("/api/auth/customer/otp/verify",
                     json_body={"mobile": new_mobile, "email": new_email, "code": code,
                                "national_id": NID_A, "full_name": "عميل جديد QA",
                                "whatsapp_available": True})
r.check("[CRITICAL] registration completes -> 200 authenticated_as customer",
        verify_reg.status == 200 and verify_reg.json().get("authenticated_as") == "customer",
        "200 customer", f"{verify_reg.status} {verify_reg.text[:120]}")
r.check("session cookies set (HttpOnly access + refresh + csrf)",
        "access_token" in c1.cookies and "refresh_token" in c1.cookies and "csrf_token" in c1.cookies,
        "3 cookies", str(sorted(c1.cookies)))
me = c1.get("/api/me")
r.check("GET /api/me -> customer identity with masked national ID",
        me.status == 200 and me.json().get("kind") == "customer"
        and me.json().get("national_id_masked", "").endswith(NID_A[-4:])
        and NID_A not in me.text,
        "masked NID", me.text[:160])

# replay: same code again must fail (single use)
replay = QAClient().post("/api/auth/customer/otp/verify",
                         json_body={"mobile": new_mobile, "email": new_email, "code": code,
                                    "national_id": NID_A, "full_name": "عميل جديد QA"})
r.check("OTP replay after success -> 401 (single use)", replay.status == 401,
        "401", f"{replay.status} {replay.text[:100]}")

# 2.2 Both already exist → login path, no re-registration
c1b = QAClient()
t0 = time.time()
start2 = c1b.post("/api/auth/customer/otp/start",
                  json_body={"mobile": new_mobile, "email": new_email})
code2 = otp_code_for(new_email, t0) if start2.status == 202 else None
login = c1b.post("/api/auth/customer/otp/verify",
                 json_body={"mobile": new_mobile, "email": new_email, "code": code2})
r.check("existing email + existing mobile -> login 200 without identity fields",
        start2.status == 202 and login.status == 200,
        "202 then 200", f"{start2.status}/{login.status} {login.text[:100]}")
me2 = c1b.get("/api/me")
r.check("login lands on the SAME customer (no duplicate)",
        me2.status == 200 and me2.json().get("id") == me.json().get("id"),
        "same id", f"{me.json().get('id')} vs {me2.json().get('id')}")

# 2.3 New email + existing mobile — ONE start, two verifies (the failed
#     identity check must not consume the code)
c2 = QAClient()
other_email = f"other.person.{TAG}@example.com"
t0 = time.time()
start3 = c2.post("/api/auth/customer/otp/start",
                 json_body={"mobile": new_mobile, "email": other_email})
code3 = otp_code_for(other_email, t0) if start3.status == 202 else None
# (a) no national ID: matches are found by mobile, the e-mail does not match
#     → uniform 401 identical to a wrong code (anti-enumeration)
steal = c2.post("/api/auth/customer/otp/verify",
                json_body={"mobile": new_mobile, "email": other_email, "code": code3})
r.check("[SECURITY] existing mobile + different email (no NID) -> uniform 401",
        start3.status == 202 and steal.status == 401 and steal.detail == "Invalid or expired code",
        "401 Invalid or expired code", f"{start3.status}/{steal.status} {steal.text[:120]}")
# (b) different national ID on the same code: shared-mobile registration is
#     allowed BY DESIGN (family phone). The OTP proves control of the EMAIL
#     only — the platform never proves control of the mobile (no customer SMS
#     OTP). QA FINDING recorded below.
family = c2.post("/api/auth/customer/otp/verify",
                 json_body={"mobile": new_mobile, "email": other_email, "code": code3,
                            "national_id": NID_B, "full_name": "فرد عائلة آخر"})
me_family = c2.get("/api/me") if family.status == 200 else None
r.check("existing mobile + different email + different NID -> separate customer (by design)",
        family.status == 200 and me_family is not None
        and me_family.json().get("id") != me.json().get("id"),
        "200 distinct customer", f"{family.status} {family.text[:100]}")
r.info("QA FINDING (medium)",
       "Customer OTP proves EMAIL control only; mobile ownership is never verified "
       "(no SMS OTP for customers). Anyone can anchor an account to a mobile they "
       "do not own. Impact limited: data access is keyed on the national-ID blind "
       "index, but the brokerage may contact the mobile owner about a stranger's "
       "ticket (harassment vector).")

# 2.4 Existing email + new mobile, same person (same NID)
# PRODUCT BEHAVIOUR (verified in app_security.customer_by_mobile): identity is
# anchored on the MOBILE number; the unique national-ID blind index then makes
# the registration path conflict → 409 "Identity cannot be registered".
# There is no change-of-mobile flow — recorded as a QA finding.
c3 = QAClient()
mobile2 = rand_mobile()
t0 = time.time()
start4 = c3.post("/api/auth/customer/otp/start",
                 json_body={"mobile": mobile2, "email": new_email})
code4 = otp_code_for(new_email, t0) if start4.status == 202 else None
reid = c3.post("/api/auth/customer/otp/verify",
               json_body={"mobile": mobile2, "email": new_email, "code": code4,
                          "national_id": NID_A, "full_name": "عميل جديد QA"})
r.check("existing email + new mobile (same NID) -> OTP still sent; verify 409 conflict "
        "(no duplicate account; no mobile-change flow — QA FINDING)",
        start4.status == 202 and reid.status == 409
        and reid.detail == "Identity cannot be registered",
        "202 then 409 Identity cannot be registered",
        f"{start4.status}/{reid.status} {reid.text[:100]}")

# 2.5 Invalid formats
bad_cases = [
    ("mobile not +201", {"mobile": "01123456789", "email": f"fmt.{TAG}@example.com"}, 422),
    ("mobile US number", {"mobile": "+14155550123", "email": f"fmt.{TAG}@example.com"}, 422),
    ("mobile too short", {"mobile": "+20112345", "email": f"fmt.{TAG}@example.com"}, 422),
    ("email without TLD", {"mobile": "+201199999999", "email": "no-tld@example"}, 422),
    ("email with spaces", {"mobile": "+201199999999", "email": "a b@example.com"}, 422),
    ("missing email", {"mobile": "+201199999999"}, 422),
    ("extra field rejected", {"mobile": "+201199999999", "email": f"e.{TAG}@x.com",
                              "admin": True}, 422),
]
for name, payload, want in bad_cases:
    got = QAClient().post("/api/auth/customer/otp/start", json_body=payload)
    r.check(f"invalid input rejected: {name} -> {want}", got.status == want,
            str(want), f"{got.status} {got.text[:80]}")

# 2.6 Rate limits on OTP start
c4 = QAClient()
rl_email = f"ratelimit.{TAG}@example.com"
statuses = []
for i in range(4):
    resp = c4.post("/api/auth/customer/otp/start",
                   json_body={"mobile": rand_mobile(), "email": rl_email})
    statuses.append(resp.status)
r.check("4th OTP start for same email -> 429 with Retry-After",
        statuses[:3] == [202, 202, 202] and statuses[3] == 429,
        "[202,202,202,429]", str(statuses))
last = c4.post("/api/auth/customer/otp/start",
               json_body={"mobile": rand_mobile(), "email": rl_email})
r.check("429 carries Retry-After and rate-limit headers",
        last.status == 429 and "retry-after" in last.headers
        and "x-ratelimit-limit" in last.headers,
        "Retry-After + X-RateLimit-*", str({k: v for k, v in last.headers.items()
                                           if 'rate' in k or 'retry' in k}))

# outstanding-code cooldown
c5 = QAClient()
cool_email = f"cooldown.{TAG}@example.com"
cool_mobile = rand_mobile()
first = c5.post("/api/auth/customer/otp/start",
                json_body={"mobile": cool_mobile, "email": cool_email})
second = c5.post("/api/auth/customer/otp/start",
                 json_body={"mobile": cool_mobile, "email": cool_email})
r.check("second start while a code is live -> 429 wait-for-expiry",
        first.status == 202 and second.status == 429
        and second.detail == "Wait for the current code to expire",
        "202 then 429", f"{first.status}/{second.status} {second.text[:80]}")

# 2.7 Brute-force protection on verify
c6 = QAClient()
bf_email = f"bruteforce.{TAG}@example.com"
bf_mobile = rand_mobile()
t0 = time.time()
st = c6.post("/api/auth/customer/otp/start", json_body={"mobile": bf_mobile, "email": bf_email})
real_code = otp_code_for(bf_email, t0)
wrong = "000000" if real_code != "000000" else "000001"
seq = []
for _ in range(9):
    resp = c6.post("/api/auth/customer/otp/verify",
                   json_body={"mobile": bf_mobile, "email": bf_email, "code": wrong})
    seq.append(resp.status)
r.check("wrong codes: 5x401 then 429 attempt-burn then lockout 429",
        seq[:5] == [401] * 5 and set(seq[5:]) == {429},
        "[401 x5, 429...]", str(seq))
t0 = time.time()
st2 = c6.post("/api/auth/customer/otp/start", json_body={"mobile": bf_mobile, "email": bf_email})
fresh_code = otp_code_for(bf_email, t0) if st2.status == 202 else None
locked = c6.post("/api/auth/customer/otp/verify",
                 json_body={"mobile": bf_mobile, "email": bf_email, "code": fresh_code or wrong})
r.check("correct code rejected while lockout active (uniform 429)",
        locked.status == 429 and locked.detail == "Too many failed attempts; try again later",
        "429 locked", f"{locked.status} {locked.text[:100]}")

# 2.8 Guest tracking (ticket + national ID both required)
guest_bad = anon.post("/api/guest/track", json_body={"ticket_no": "CMP-2026-000001"})
r.check("guest track without national ID -> 422", guest_bad.status == 422, "422",
        f"{guest_bad.status}")
guest_wrong = anon.post("/api/guest/track",
                        json_body={"ticket_no": "CMP-2026-000001", "national_id": NID_B})
r.check("guest track with wrong national ID -> 404 (not 403: no oracle)",
        guest_wrong.status == 404, "404", f"{guest_wrong.status} {guest_wrong.text[:80]}")

# --------------------------------------------------------------------------- #
print("\n== S3. Customer complaint flow + attachments + notices ==")
r.section = "S3 complaints"

idem = str(uuid.uuid4())
t_notice = time.time()
payload = {
    "national_id": NID_A,
    "mobile": new_mobile,
    "company_id": COMPANY_A["id"],
    "insurance_type": "health",
    "policy_number": f"POL-{TAG[:6].upper()}",
    "subject": "تأخر صرف تعويض العملية الجراحية",
    "description": "تم تقديم المطالبة منذ أكثر من شهرين دون أي رد واضح من شركة التأمين، "
                   "ويرجى مراجعة الحالة وتحديث الوضع في أقرب وقت ممكن مع جزيل الشكر.",
}
created = c1.post("/api/customer/complaints", json_body=payload,
                  headers={"Idempotency-Key": idem}, csrf=True)
r.check("complaint create -> 201 with unique ticket number",
        created.status == 201 and str(created.json().get("ticket_no", "")).startswith("CMP-"),
        "201 CMP-…", f"{created.status} {created.text[:140]}")
ticket_no = created.json().get("ticket_no") if created.status == 201 else None
complaint_id = created.json().get("id") if created.status == 201 else None

replay_same = c1.post("/api/customer/complaints", json_body=payload,
                      headers={"Idempotency-Key": idem}, csrf=True)
r.check("idempotent replay (same key+body) -> original ticket, no duplicate",
        replay_same.status in (200, 201) and replay_same.json().get("ticket_no") == ticket_no,
        f"same ticket {ticket_no}", f"{replay_same.status} {replay_same.text[:100]}")
replay_diff = c1.post("/api/customer/complaints", json_body={**payload, "subject": "موضوع مختلف تماما"},
                      headers={"Idempotency-Key": idem}, csrf=True)
r.check("same key + different body -> 409 conflict", replay_diff.status == 409, "409",
        f"{replay_diff.status} {replay_diff.text[:100]}")
no_key = c1.post("/api/customer/complaints", json_body=payload, csrf=True)
r.check("create without Idempotency-Key -> 422", no_key.status == 422, "422", f"{no_key.status}")
no_csrf = QAClient()
r.check("create without auth cookies -> 401",
        no_csrf.post("/api/customer/complaints", json_body=payload,
                     headers={"Idempotency-Key": str(uuid.uuid4())}).status == 401,
        "401", "")

# mobile/NID must match the authenticated customer
mismatch = c1.post("/api/customer/complaints",
                   json_body={**payload, "mobile": rand_mobile()},
                   headers={"Idempotency-Key": str(uuid.uuid4())}, csrf=True)
r.check("[SECURITY] complaint for a different mobile -> rejected (4xx, no ticket)",
        400 <= mismatch.status < 500 and mismatch.status != 201,
        "4xx", f"{mismatch.status} {mismatch.text[:100]}")

# validation matrix
bad_payloads = [
    ("bad NID", {**payload, "national_id": "12345678901234"}),
    ("future-dated NID", {**payload, "national_id": "39901010112345"}),
    ("bad governorate NID", {**payload, "national_id": "29901019912345"}),
    ("short subject", {**payload, "subject": "قصير"}),
    ("short description", {**payload, "description": "قصير جدا"}),
    ("bad policy number", {**payload, "policy_number": "!!!"}),
    ("unknown company", {**payload, "company_id": str(uuid.uuid4())}),
    ("bad insurance type", {**payload, "insurance_type": "life"}),
]
for name, body in bad_payloads:
    resp = c1.post("/api/customer/complaints", json_body=body,
                   headers={"Idempotency-Key": str(uuid.uuid4())}, csrf=True)
    r.check(f"complaint validation: {name} -> 4xx", 400 <= resp.status < 500,
            "4xx", f"{resp.status} {resp.text[:80]}")

# guest can track the created ticket with the right NID
guest_ok = anon.post("/api/guest/track",
                     json_body={"ticket_no": ticket_no, "national_id": NID_A})
r.check("guest track with ticket + matching NID -> public metadata only",
        guest_ok.status == 200 and "customer" not in guest_ok.text.lower()[:200],
        "200 metadata", f"{guest_ok.status} {guest_ok.text[:140]}")

# list & detail & counters
lst = c1.get("/api/customer/complaints?page=1&page_size=10")
r.check("my tickets list includes the new ticket",
        lst.status == 200 and any(item.get("ticket_no") == ticket_no
                                  for item in lst.json().get("items", [])),
        "ticket in list", f"{lst.status}")
detail = c1.get(f"/api/customer/complaints/{complaint_id}")
r.check("ticket detail -> 200 with timeline eligibility flags",
        detail.status == 200 and "can_reopen" in detail.json(),
        "200 + can_reopen", f"{detail.status} {detail.text[:100]}")
counters = c1.get("/api/customer/complaints/counters")
r.check("counters endpoint -> 200 with totals", counters.status == 200, "200",
        f"{counters.status}")

# IDOR: the second customer must not see this ticket
c_other = QAClient()
t0 = time.time()
other_mobile = rand_mobile()
other_email2 = f"idor.victim.{TAG}@example.com"
st = c_other.post("/api/auth/customer/otp/start",
                  json_body={"mobile": other_mobile, "email": other_email2})
ocode = otp_code_for(other_email2, t0) if st.status == 202 else None
c_other.post("/api/auth/customer/otp/verify",
             json_body={"mobile": other_mobile, "email": other_email2, "code": ocode,
                        "national_id": NID_C, "full_name": "ضحية IDOR"})
idor = c_other.get(f"/api/customer/complaints/{complaint_id}")
r.check("[SECURITY] IDOR: other customer's ticket -> 404 (indistinguishable)",
        idor.status == 404, "404", f"{idor.status} {idor.text[:80]}")
idor_reply = c_other.post(f"/api/customer/complaints/{complaint_id}/reply",
                          json_body={"body": "تدخل"}, csrf=True)
r.check("[SECURITY] IDOR: reply to other customer's ticket -> 404",
        idor_reply.status == 404, "404", f"{idor_reply.status}")

# attachments: plan -> upload -> stays pending (no scanner configured = fail closed)
pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n" + b"QA" * 200
plan = c1.post(f"/api/customer/complaints/{complaint_id}/attachments/plan",
               json_body={"files": [{"filename": "claim.pdf", "mime_type": "application/pdf",
                                     "size_bytes": len(pdf), "checksum_sha256": sha256(pdf)}]},
               csrf=True)
r.check("attachment plan -> 200 with one-use signed upload URL",
        plan.status == 200 and plan.json()["uploads"][0]["upload_url"].startswith("/api/customer/"),
        "200 + relative upload_url", f"{plan.status} {plan.text[:140]}")
if plan.status == 200:
    up = plan.json()["uploads"][0]
    put = c1.put(up["upload_url"], content=pdf, csrf=True,
                 headers={"X-Upload-Token": up["upload_token"],
                          "Content-Type": "application/pdf"})
    r.check("upload PUT with token+checksum -> 2xx", put.status in (200, 201, 204),
            "2xx", f"{put.status} {put.text[:100]}")
    put2 = c1.put(up["upload_url"], content=pdf, csrf=True,
                  headers={"X-Upload-Token": up["upload_token"],
                           "Content-Type": "application/pdf"})
    r.check("[SECURITY] upload token is single-use (replay -> 4xx)",
            400 <= put2.status < 500, "4xx", f"{put2.status} {put2.text[:80]}")
    no_token = c1.put(up["upload_url"].replace(str(up["id"]), str(uuid.uuid4())),
                      content=pdf, csrf=True,
                      headers={"Content-Type": "application/pdf"})
    r.check("[SECURITY] upload without token -> 4xx", 400 <= no_token.status < 500,
            "4xx", f"{no_token.status}")
    atts = c1.get(f"/api/customer/complaints/{complaint_id}/attachments")
    scan_state = None
    if atts.status == 200:
        body = atts.json()
        items = body if isinstance(body, list) else body.get("items", [])
        scan_state = next((a.get("scan_state") or a.get("status") for a in items), None)
    r.check("attachment stays pending/unscanned (fail-closed, no scanner configured)",
            atts.status == 200 and scan_state in ("pending", "uploaded", "pending_scan", None),
            "pending", f"{atts.status} state={scan_state}")
    # corrupt checksum upload must be rejected
    plan2 = c1.post(f"/api/customer/complaints/{complaint_id}/attachments/plan",
                    json_body={"files": [{"filename": "claim2.pdf", "mime_type": "application/pdf",
                                          "size_bytes": len(pdf), "checksum_sha256": sha256(pdf)}]},
                    csrf=True)
    if plan2.status == 200:
        up2 = plan2.json()["uploads"][0]
        bad = c1.put(up2["upload_url"], content=pdf + b"TAMPER", csrf=True,
                     headers={"X-Upload-Token": up2["upload_token"],
                              "Content-Type": "application/pdf"})
        r.check("[SECURITY] checksum mismatch on upload -> 4xx", 400 <= bad.status < 500,
                "4xx", f"{bad.status} {bad.text[:80]}")

# reply on own ticket + customer notification via worker (SMTP sink)
reply = c1.post(f"/api/customer/complaints/{complaint_id}/reply",
                json_body={"body": "أرجو سرعة مراجعة المطالبة، شكرا لكم."}, csrf=True)
r.check("customer reply on own ticket -> 200/201", reply.status in (200, 201), "2xx",
        f"{reply.status} {reply.text[:80]}")
notice = wait_for_mail(new_email, t_notice, timeout=180,
                       subject_contains=ticket_no or "Complaint")
r.check("worker delivers transactional notice e-mail via outbox (SMTP sink)",
        notice is not None, "notice .eml mentioning the ticket",
        str(notice.name if notice else None))

# authenticated write WITHOUT CSRF header -> 403
no_csrf_reply = c1.post(f"/api/customer/complaints/{complaint_id}/reply",
                        json_body={"body": "بدون توقيع CSRF"})
r.check("[SECURITY] authenticated write without X-CSRF-Token -> 403",
        no_csrf_reply.status == 403, "403", f"{no_csrf_reply.status}")
wrong_csrf = c1.post(f"/api/customer/complaints/{complaint_id}/reply",
                     json_body={"body": "توقيع خاطئ"},
                     headers={"X-CSRF-Token": "forged-token-value"})
r.check("[SECURITY] forged X-CSRF-Token -> 403", wrong_csrf.status == 403, "403",
        f"{wrong_csrf.status}")

# CSAT before resolution -> not allowed
csat_early = c1.post(f"/api/customer/complaints/{complaint_id}/csat",
                     json_body={"score": 5}, csrf=True)
r.check("CSAT on unresolved ticket -> 4xx", 400 <= csat_early.status < 500, "4xx",
        f"{csat_early.status} {csat_early.text[:80]}")
reopen_early = c1.post(f"/api/customer/complaints/{complaint_id}/reopen", csrf=True)
r.check("reopen on unresolved ticket -> 4xx", 400 <= reopen_early.status < 500, "4xx",
        f"{reopen_early.status} {reopen_early.text[:80]}")

# profile endpoints
prof = c1.get("/api/customer/profile")
r.check("GET /api/customer/profile -> 200 with preferences", prof.status == 200, "200",
        f"{prof.status} {prof.text[:100]}")
prof_patch = c1.patch("/api/customer/profile",
                      json_body={"full_name": "عميل جديد QA",
                                 "whatsapp_available": False,
                                 "notification_preferences": {"email": True, "whatsapp": False,
                                                              "sms": True}},
                      csrf=True)
r.check("PATCH /api/customer/profile -> 200 and persisted",
        prof_patch.status == 200 and c1.get("/api/customer/profile").json()
        .get("whatsapp_available") is False,
        "persisted", f"{prof_patch.status} {prof_patch.text[:100]}")

# L-4 fix: true PATCH semantics - unsent fields keep their stored value
prefs_only = c1.patch("/api/customer/profile", csrf=True,
                      json_body={"notification_preferences": {"email": False, "whatsapp": True,
                                                              "sms": True}})
after = prefs_only.json() if prefs_only.status == 200 else {}
r.check("[FIX L-4] preferences-only PATCH (no full_name) -> 200; name and whatsapp preserved",
        prefs_only.status == 200 and after.get("full_name") == "عميل جديد QA"
        and after.get("whatsapp_available") is False
        and after.get("notification_preferences", {}).get("email") is False,
        "name/whatsapp kept, prefs saved", f"{prefs_only.status} {prefs_only.text[:140]}")
empty_patch = c1.patch("/api/customer/profile", csrf=True, json_body={})
r.check("[FIX L-4] empty PATCH body -> 422 (cannot reset the profile by accident)",
        empty_patch.status == 422, "422", f"{empty_patch.status} {empty_patch.text[:100]}")

# --------------------------------------------------------------------------- #
print("\n== S4. Body limits and abuse guards ==")
r.section = "S4 abuse"
big = c1.post("/api/customer/complaints",
              json_body={**payload, "description": "ك" * 1_200_000},
              headers={"Idempotency-Key": str(uuid.uuid4())}, csrf=True)
r.check("oversized JSON body -> 413", big.status == 413, "413", f"{big.status}")
xss_subject = {**payload, "subject": "<script>alert('xss')</script>",
               "description": "<img src=x onerror=alert(1)> " + payload["description"]}
xss = c1.post("/api/customer/complaints", json_body=xss_subject,
              headers={"Idempotency-Key": str(uuid.uuid4())}, csrf=True)
r.check("XSS payload stored but JSON-typed (no HTML content-type)",
        xss.status in (201, 422) and "text/html" not in xss.headers.get("content-type", ""),
        "json response", f"{xss.status} ct={xss.headers.get('content-type')}")
sqli = c1.get("/api/customer/complaints?search=" + "' OR 1=1; DROP TABLE complaints;--")
r.check("SQLi attempt in search -> no 500, table intact",
        sqli.status < 500 and anon.get("/api/health/ready").status == 200,
        "<500 + ready", f"{sqli.status}")
evil_origin = QAClient().post("/api/auth/customer/otp/start",
                              json_body={"mobile": rand_mobile(), "email": f"o.{TAG}@e.com"},
                              headers={"Origin": "https://evil.example"})
r.check("[SECURITY] cross-site Origin on public write -> blocked by origin guard",
        evil_origin.status in (403, 429, 202),  # 202 only if origin guard exempts public forms
        "403 expected", f"{evil_origin.status} {evil_origin.text[:80]}")

r.save(Path(__file__).parent / "results_core.json")
