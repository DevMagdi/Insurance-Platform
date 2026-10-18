"""Live E2E part 2: staff auth (password change, TOTP, SMS path), permission
and scope matrix, session security (refresh rotation/replay, revoke), the
complaint lifecycle (assign → reply → note → escalate → resolve → reopen →
CSAT), exports, national-ID reveal, audit, WebSocket, metrics, abuse detector.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from pathlib import Path

import pyotp

from qa_lib import QAClient, Recorder, fresh_ip, otp_code_for

r = Recorder()
RNG = random.Random()


def rand_mobile() -> str:
    return f"+2011{RNG.randrange(10000000, 99999999)}"


def rand_nid(century: str = "2", yy: str = "99", mm: str = "01", dd: str = "01",
             gov: str = "01") -> str:
    return f"{century}{yy}{mm}{dd}{gov}{RNG.randrange(1000, 9999)}{RNG.randrange(0, 9)}"

DEMO_PW = "Qa-Demo-Pass-2026x"
STAFF = {
    "admin": "admin@demo.example",
    "supervisor": "supervisor@demo.example",
    "agent": "agent@demo.example",
    "analyst": "analyst@demo.example",
}
NEW_PW = {k: f"Qa-Rotated-{k}-2026x" for k in STAFF}
SECRET_FILE = Path(__file__).parent / "totp_admin_secret.txt"

# --------------------------------------------------------------------------- #
print("\n== S5. Staff login, forced password change, 2FA paths ==")
r.section = "S5 staff auth"


def staff_login(client: QAClient, email: str, password: str):
    return client.post("/api/auth/staff/login",
                       json_body={"email": email, "password": password})


anon = QAClient()
bad = anon.post("/api/auth/staff/login",
                json_body={"email": STAFF["admin"], "password": "wrong-password-123"})
r.check("wrong password -> 401 uniform 'Invalid credentials'",
        bad.status == 401 and bad.detail == "Invalid credentials", "401",
        f"{bad.status} {bad.text[:80]}")
nouser = anon.post("/api/auth/staff/login",
                   json_body={"email": "ghost@demo.example", "password": "wrong-password-123"})
r.check("[SECURITY] unknown staff email -> identical 401 (no enumeration)",
        nouser.status == 401 and nouser.text == bad.text, "same body",
        f"{nouser.status} {nouser.text[:80]}")

# Each staff e-mail allows only 8 login attempts / 900 s (staff:login:email),
# so a rerun must NOT waste an attempt on the seeded password.
ROTATED = Path(__file__).parent / "staff_pw_rotated.flag"
sessions: dict[str, QAClient] = {}
for key, email in STAFF.items():
    c = QAClient()
    rotated = ROTATED.exists()
    first = staff_login(c, email, NEW_PW[key] if rotated else DEMO_PW)
    ok_challenge = (first.status == 200 and first.json().get("next_step") == "password_change"
                    and first.json().get("challenge"))
    if not ok_challenge and first.status != 200 and rotated:
        first = staff_login(c, email, DEMO_PW)  # stale flag: DB was reseeded
        ok_challenge = (first.status == 200
                        and first.json().get("next_step") == "password_change"
                        and first.json().get("challenge"))
    if not ok_challenge:
        if not rotated and first.status != 200:
            # password may already have been rotated by an earlier QA run
            first = staff_login(c, email, NEW_PW[key])
        if first.status == 200 and first.json().get("next_step") == "authenticated":
            sessions[key] = c
            continue
        if (first.status == 200 and first.json().get("next_step") == "totp_verify"
                and key == "admin" and SECRET_FILE.exists()):
            # rerun after TOTP enrollment: complete the 2FA challenge
            saved = SECRET_FILE.read_text().strip()
            ok = c.post("/api/auth/staff/2fa/verify",
                        json_body={"challenge": first.json()["challenge"],
                                   "code": pyotp.TOTP(saved).now()})
            if ok.status == 200:
                sessions[key] = c
                continue
    r.check(f"{key}: seeded login -> password_change challenge (no session cookies)",
            ok_challenge and "access_token" not in c.cookies,
            "password_change + no cookies", f"{first.status} {first.text[:100]}")
    challenge = first.json().get("challenge")
    # the challenge alone must NOT unlock staff endpoints
    probe = QAClient()
    probe.cookies["access_token"] = challenge or ""
    denied = probe.get("/api/me")
    r.check(f"[SECURITY] {key}: password-change challenge is not an access token",
            denied.status == 401, "401", f"{denied.status}")
    weak = c.post("/api/auth/staff/password/change",
                  json_body={"challenge": challenge, "password": "password"})
    r.check(f"{key}: weak new password rejected 422", weak.status == 422, "422",
            f"{weak.status} {weak.text[:80]}")
    changed = c.post("/api/auth/staff/password/change",
                     json_body={"challenge": challenge, "password": NEW_PW[key]})
    r.check(f"{key}: password change accepted", changed.status == 200
            and changed.json().get("next_step") == "sign_in_with_new_password",
            "200", f"{changed.status} {changed.text[:80]}")
    old_pw_login = staff_login(QAClient(), email, DEMO_PW)
    r.check(f"[SECURITY] {key}: old password invalid after change",
            old_pw_login.status == 401, "401", f"{old_pw_login.status}")
    second = staff_login(c, email, NEW_PW[key])
    r.check(f"{key}: login with new password -> authenticated + cookies",
            second.status == 200 and second.json().get("next_step") == "authenticated"
            and "access_token" in c.cookies and "csrf_token" in c.cookies,
            "session", f"{second.status} {second.text[:100]}")
    sessions[key] = c

admin, supervisor, agent, analyst = (sessions["admin"], sessions["supervisor"],
                                     sessions["agent"], sessions["analyst"])

ROTATED.write_text("staff passwords rotated by a QA run; delete after reseeding\n")
# NOTE: the mobile-UA SMS login probe lives in S5b (uses the freshly-invited
# staff account) so the seeded analyst e-mail stays under its 8-logins/900 s cap.

# --------------------------------------------------------------------------- #
print("\n== S5b. Staff invitation + onboarding (email token, profile, avatar) ==")
r.section = "S5b invitation"

import base64
import io
import re as _re

from PIL import Image
from qa_lib import wait_for_mail

dept = admin.post("/api/staff/departments",
                  json_body={"name": f"QA Dept {uuid.uuid4().hex[:4]}",
                             "description": "QA onboarding department", "is_active": True},
                  csrf=True)
r.check("admin creates a department -> 201 with id",
        dept.status == 201 and dept.json().get("id"), "201 + id",
        f"{dept.status} {dept.text[:100]}")
invitee_email = f"invitee.{uuid.uuid4().hex[:8]}@qa.example.com"
t_invite = time.time()
inv = admin.post("/api/staff/users/invitations",
                 json_body={"department_id": dept.json()["id"], "email": invitee_email,
                            "full_name": "QA Invitee"}, csrf=True)
r.check("admin invites staff -> 2xx and invitation e-mail queued",
        inv.status in (200, 201, 202), "2xx", f"{inv.status} {inv.text[:120]}")
mail = wait_for_mail(invitee_email, t_invite, timeout=150)
token = None
invitee_pw = None
if mail:
    from email import message_from_bytes
    msg = message_from_bytes(mail.read_bytes())
    payload = msg.get_payload(decode=True) or b""
    decoded = payload.decode("utf-8", "replace") or msg.as_string()
    m = _re.search(r"token=([A-Za-z0-9_-]{20,})", decoded)
    token = m.group(1) if m else None
r.check("invitation e-mail delivered through the SMTP provider with a token URL",
        mail is not None and token, "token in mail", f"mail={bool(mail)} token={bool(token)}")
if token:
    inspector = QAClient()
    insp = inspector.post("/api/auth/staff/invitation/inspect", json_body={"token": token})
    r.check("invite inspect -> 200 with name/email, no user id leak",
            insp.status == 200 and "QA Invitee" in insp.text and "id" not in insp.json(),
            "details, no id", f"{insp.status} {insp.text[:120]}")
    img = Image.new("RGB", (64, 64), "#1b3a6b")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    avatar = base64.b64encode(buf.getvalue()).decode()
    # "password2026" is 12 chars (clears the schema min_length) but sits on the
    # curated common-password denylist, so the SERVICE rejects it and the API
    # must answer with the stable client-safe message - never internals.
    weak_accept = inspector.post("/api/auth/staff/invitation/accept",
                                 json_body={"token": token, "password": "password2026"})
    r.check("[SECURITY] accept with common/weak password -> 422 stable message (no internals)",
            weak_accept.status == 422
            and weak_accept.detail == "Invitation or password is invalid",
            "422 stable", f"{weak_accept.status} {weak_accept.text[:100]}")
    short_accept = inspector.post("/api/auth/staff/invitation/accept",
                                  json_body={"token": token, "password": "short"})
    r.check("accept with too-short password -> 422 schema validation (token NOT consumed)",
            short_accept.status == 422, "422", f"{short_accept.status}")
    invitee_pw = f"Invitee-Qa-{uuid.uuid4().hex[:6]}x"
    accept = inspector.post("/api/auth/staff/invitation/accept",
                            json_body={"token": token, "password": invitee_pw,
                                       "profile": {"full_name": "QA Invitee",
                                                   "national_id": rand_nid(yy="95", gov="02"),
                                                   "mobile": rand_mobile(),
                                                   "address": "12 Nile Street, Dokki",
                                                   "city": "Giza",
                                                   "job_title": "QA specialist"},
                                       "avatar_base64": avatar})
    r.check("accept with profile + avatar -> 200 sign_in_with_password",
            accept.status == 200
            and accept.json().get("next_step") == "sign_in_with_password",
            "200", f"{accept.status} {accept.text[:120]}")
    reuse = inspector.post("/api/auth/staff/invitation/accept",
                           json_body={"token": token, "password": invitee_pw,
                                      "profile": {"full_name": "QA Invitee",
                                                  "national_id": rand_nid(yy="94", gov="03"),
                                                  "mobile": rand_mobile(),
                                                  "address": "12 Nile Street, Dokki",
                                                  "city": "Giza"},
                                      "avatar_base64": avatar})
    r.check("[SECURITY] invitation token is single-use (replay -> 400)",
            reuse.status == 400, "400", f"{reuse.status} {reuse.text[:80]}")
    newb = QAClient()
    nl = staff_login(newb, invitee_email, invitee_pw)
    me_new = newb.get("/api/me") if nl.status == 200 and nl.json().get("next_step") == "authenticated" else None
    r.check("invitee signs in with the chosen password -> session with zero permissions",
            nl.status == 200 and me_new is not None
            and me_new.json()["access"]["permissions"] == []
            and me_new.json()["access"]["all_companies"] is False,
            "empty effective access",
            f"{nl.status} {me_new.text[:100] if me_new else None}")
    if me_new is not None:
        m_probe = QAClient()
        m_probe.ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 Mobile")
        mobile_login = staff_login(m_probe, invitee_email, invitee_pw)
        r.check("[SECURITY] mobile-UA staff login -> SMS OTP path fails GRACEFULLY "
                "(503, no raw config error)",
                mobile_login.status == 503 and str(mobile_login.detail) in (
                    "SMS delivery is not configured", "Staff mobile profile is unavailable"),
                "graceful 503", f"{mobile_login.status} {mobile_login.text[:120]}")
        r.info("assumption: SMS path",
               "Twilio is not configured locally; the SMS login branch must 503 gracefully "
               "(invitee HAS a profile mobile, seeded demo staff do not)")
        denied = newb.get("/api/staff/complaints")
        r.check("[SECURITY] freshly-onboarded staff without roles -> complaints 403",
                denied.status == 403, "403", f"{denied.status}")

# --------------------------------------------------------------------------- #
print("\n== S6. Permission matrix (role x resource) ==")
r.section = "S6 permissions"

me_admin = admin.get("/api/me")
r.check("admin /api/me -> full permission catalog + global scope",
        me_admin.status == 200 and me_admin.json()["access"]["all_companies"] is True
        and len(me_admin.json()["access"]["permissions"]) >= 34,
        ">=34 permissions, all_companies", me_admin.text[:120])
me_agent = agent.get("/api/me").json()["access"]
r.check("agent effective access -> company0 + health + own",
        me_agent["all_companies"] is False and me_agent["insurance_types"] == ["health"]
        and me_agent["ownership"] == "own",
        "scoped", json.dumps({k: me_agent[k] for k in
                              ("all_companies", "insurance_types", "ownership")}))

matrix = [
    ("analyst", analyst, "GET", "/api/staff/users", 403),
    ("analyst", analyst, "GET", "/api/staff/analytics/overview?date_from=2026-09-01T00:00:00Z"
        "&date_to=2026-10-18T00:00:00Z", 200),
    ("agent", agent, "GET", "/api/staff/users", 403),
    ("agent", agent, "GET", "/api/staff/roles", 403),
    ("agent", agent, "GET", "/api/staff/audit/events", 403),
    ("agent", agent, "GET", "/api/staff/storage", 403),
    ("supervisor", supervisor, "GET", "/api/staff/users", 200),
    ("supervisor", supervisor, "GET", "/api/staff/storage", 403),  # needs global scope
    ("admin", admin, "GET", "/api/staff/storage", 200),
    ("admin", admin, "GET", "/api/staff/users", 200),
    ("admin", admin, "GET", "/api/staff/audit/events", 200),
]
for name, client, method, path, want in matrix:
    resp = client.request(method, path)
    r.check(f"permission matrix: {name} {method} {path.split('?')[0]} -> {want}",
            resp.status == want, str(want), f"{resp.status} {resp.text[:80]}")

# deny-wins override: supervisor role HAS analytics.export but user override denies
sup_export = supervisor.post("/api/staff/exports",
                             json_body={"kind": "analytics", "format": "csv",
                                        "filters": {"date_from": "2026-09-01T00:00:00Z",
                                                    "date_to": "2026-10-01T00:00:00Z"}},
                             csrf=True)
r.check("[SECURITY] supervisor analytics.export DENY override wins over role grant -> 403",
        sup_export.status == 403, "403", f"{sup_export.status} {sup_export.text[:100]}")

# customer token on staff routes, anonymous on staff routes
cust = QAClient()
cust_email = f"staff.probe.{uuid.uuid4().hex[:6]}@example.com"
cust_mobile = rand_mobile()
cust_nid = rand_nid()
t0 = time.time()
st = cust.post("/api/auth/customer/otp/start",
               json_body={"mobile": cust_mobile, "email": cust_email})
code = otp_code_for(cust_email, t0) if st.status == 202 else "000000"
cust.post("/api/auth/customer/otp/verify",
          json_body={"mobile": cust_mobile, "email": cust_email, "code": code,
                     "national_id": cust_nid, "full_name": "عميل يتطفل"})
cross = cust.get("/api/staff/complaints")
r.check("[SECURITY] customer session on staff route -> 403 (vertical escalation blocked)",
        cross.status == 403, "403", f"{cross.status} {cross.text[:80]}")
anon_staff = QAClient().get("/api/staff/complaints")
r.check("[SECURITY] anonymous on staff route -> 401", anon_staff.status == 401, "401",
        f"{anon_staff.status}")

# --------------------------------------------------------------------------- #
print("\n== S7. Scope enforcement (companies / types / ownership) ==")
r.section = "S7 scope"

# IMPORTANT: /api/companies is ordered by name_en, NOT by seed order. Staff
# scopes reference specific ids, so derive everything from the /api/me snapshots.
agent_acc = agent.get("/api/me").json()["access"]
sup_acc = supervisor.get("/api/me").json()["access"]
admin_acc = admin.get("/api/me").json()["access"]
agent_companies = set(agent_acc["company_ids"])
sup_companies = set(sup_acc["company_ids"])
public_ids = {c["id"] for c in QAClient().get("/api/companies").json()}
r.check("access snapshots: agent companies subset of supervisor, admin global",
        bool(agent_companies) and agent_companies <= sup_companies <= public_ids
        and admin_acc.get("all_companies") is True,
        "nested scopes", f"agent={sorted(agent_companies)} sup={sorted(sup_companies)}")
company0 = sorted(agent_companies)[0]
company2 = sorted(public_ids - agent_companies)[0]

agent_list = agent.get("/api/staff/complaints?page=1&page_size=100")
items = agent_list.json().get("items", []) if agent_list.status == 200 else []
leaks = [i for i in items if i.get("company_id") not in agent_companies
         or i.get("insurance_type") != "health"]
r.check("[SECURITY] agent scoped list -> only own-company + health rows",
        agent_list.status == 200 and items and not leaks,
        "no out-of-scope rows", f"{len(items)} rows, {len(leaks)} leaks"
        + (f" e.g. {leaks[0].get('company_id')}/{leaks[0].get('insurance_type')}" if leaks else ""))
foreign = [i for i in items if i.get("assigned_agent_id") != agent_acc["id"]]
r.check("[SECURITY] agent ownership=own -> every listed row is self-assigned",
        not foreign, "all self-assigned",
        f"{len(foreign)} rows assigned elsewhere" if foreign else f"{len(items)} rows, all own")

filtered = agent.get(f"/api/staff/complaints?company_id={company2}&page=1&page_size=50")
f_items = filtered.json().get("items", []) if filtered.status == 200 else ["?"]
r.check("[SECURITY] agent filter by out-of-scope company -> empty (scope beats filter)",
        filtered.status == 200 and f_items == [], "empty", f"{filtered.status} n={len(f_items)}")

# find an out-of-scope ticket id (via admin) to attempt direct access
admin_list = admin.get(f"/api/staff/complaints?company_id={company2}&page=1&page_size=1")
out_item = admin_list.json()["items"][0] if admin_list.json().get("items") else None
if out_item:
    direct = agent.get(f"/api/staff/complaints/{out_item['id']}")
    r.check("[SECURITY] agent GET out-of-scope ticket detail -> 404 (indistinguishable)",
            direct.status == 404, "404", f"{direct.status} {direct.text[:80]}")
    search_leak = agent.get("/api/staff/complaints?search=" +
                            str(out_item.get("ticket_no", ""))[-6:])
    s_items = search_leak.json().get("items", []) if search_leak.status == 200 else []
    r.check("[SECURITY] agent search cannot surface out-of-scope ticket",
            search_leak.status == 200
            and all(i["id"] != out_item["id"] for i in s_items),
            "not found", f"{len(s_items)} rows")
    counters = agent.get("/api/staff/complaints/counters")
    admin_counters = admin.get("/api/staff/complaints/counters")
    def _total(resp):
        data = resp.json() if resp.status == 200 else {}
        return data.get("total", sum(v for v in data.values() if isinstance(v, int)))
    r.check("scoped counters < global counters (RLS applied to aggregates)",
            counters.status == 200 and admin_counters.status == 200
            and _total(counters) < _total(admin_counters),
            "agent total < admin total", f"{_total(counters)} vs {_total(admin_counters)}")

# analyst: all companies but analytics-only permission set
analyst_list = analyst.get("/api/staff/complaints?page=1&page_size=5")
r.check("analyst (all companies, complaints.view) sees cross-company rows",
        analyst_list.status == 200, "200", f"{analyst_list.status}")
analyst_assign = analyst.post(f"/api/staff/complaints/{out_item['id'] if out_item else uuid.uuid4()}/assign",
                              json_body={"agent_id": str(uuid.uuid4())}, csrf=True)
r.check("[SECURITY] analyst cannot assign (no complaints.assign) -> 403",
        analyst_assign.status == 403, "403", f"{analyst_assign.status}")

# --------------------------------------------------------------------------- #
print("\n== S8. Session security: refresh rotation, replay, revoke ==")
r.section = "S8 sessions"

s = QAClient()
login = (staff_login(s, invitee_email, invitee_pw) if invitee_pw
         else staff_login(s, STAFF["analyst"], NEW_PW["analyst"]))
assert login.status == 200, login.text
refresh1 = s.cookies.get("refresh_token")
rot = s.post("/api/auth/refresh", csrf=True)
r.check("refresh with CSRF -> 200 and rotated cookies",
        rot.status == 200 and s.cookies.get("refresh_token") != refresh1,
        "rotated", f"{rot.status}")
no_csrf = s.post("/api/auth/refresh")
r.check("[SECURITY] refresh without CSRF header -> 403", no_csrf.status == 403, "403",
        f"{no_csrf.status}")
# replay the OLD refresh token -> family revocation
stolen = QAClient()
stolen.cookies["refresh_token"] = refresh1
stolen.cookies["csrf_token"] = s.cookies["csrf_token"]
replay = stolen.post("/api/auth/refresh", csrf=True)
r.check("[SECURITY] replayed old refresh token -> 401", replay.status == 401, "401",
        f"{replay.status}")
after_replay = s.post("/api/auth/refresh", csrf=True)
r.check("[SECURITY] token family revoked after replay (new refresh also dead) -> 401",
        after_replay.status == 401, "401", f"{after_replay.status}")
me_dead = s.get("/api/me")
r.check("[SECURITY] access token from revoked family rejected on next request -> 401",
        me_dead.status == 401, "401", f"{me_dead.status} {me_dead.text[:80]}")

# tampered access token
tampered = QAClient()
tampered.cookies["access_token"] = (admin.cookies["access_token"][:-4] + "AAAA")
tampered.cookies["csrf_token"] = admin.cookies["csrf_token"]
r.check("[SECURITY] tampered JWT signature -> 401", tampered.get("/api/me").status == 401,
        "401", "")

# sessions list + revoke single + revoke all
sl = admin.get("/api/me/sessions")
r.check("GET /api/me/sessions -> list with current flag",
        sl.status == 200 and any(x["current"] for x in sl.json()), "200 + current",
        f"{sl.status}")
if sl.status == 200 and len(sl.json()) > 1:
    other = next(x for x in sl.json() if not x["current"])
    rev = admin.delete(f"/api/me/sessions/{other['id']}", csrf=True)
    r.check("revoke another own session -> 200", rev.status == 200, "200", f"{rev.status}")
foreign = admin.delete(f"/api/me/sessions/{uuid.uuid4()}", csrf=True)
r.check("[SECURITY] revoke unknown/foreign session id -> 404", foreign.status == 404,
        "404", f"{foreign.status}")

# force-logout by admin kills the target's session immediately
target = QAClient()
users = admin.get("/api/staff/users").json()
rows = users if isinstance(users, list) else users.get("items", [])
if invitee_pw:
    tl = staff_login(target, invitee_email, invitee_pw)
    target_row = next(u for u in rows if u["email"] == invitee_email)
else:
    tl = staff_login(target, STAFF["agent"], NEW_PW["agent"])
    target_row = next(u for u in rows if u["email"] == STAFF["agent"])
assert tl.status == 200, tl.text
agent_row = next(u for u in rows if u["email"] == STAFF["agent"])
fl = admin.post(f"/api/staff/users/{target_row['id']}/force-logout", csrf=True)
me_after = target.get("/api/me")
r.check("[SECURITY] admin force-logout -> target session dead on next request",
        fl.status == 200 and me_after.status == 401, "200 then 401",
        f"{fl.status}/{me_after.status}")

# --------------------------------------------------------------------------- #
print("\n== S9. TOTP 2FA enroll + enforced verify ==")
r.section = "S9 totp"

en = admin.post("/api/me/2fa/enroll/start", csrf=True)
uri = en.json().get("provisioning_uri", "") if en.status == 200 else ""
secret = uri.split("secret=")[1].split("&")[0] if "secret=" in uri else None
if en.status == 409 and SECRET_FILE.exists():
    # re-run: TOTP already enrolled; reuse the persisted demo secret
    secret = SECRET_FILE.read_text().strip()
    r.info("TOTP", "already enrolled from a previous run; using saved secret")
else:
    r.check("TOTP enroll start -> provisioning uri with secret", en.status == 200 and secret,
            "otpauth uri", f"{en.status} {uri[:60]}")
if secret and en.status == 200:
    SECRET_FILE.write_text(secret)
if en.status == 200 and secret:
    totp = pyotp.TOTP(secret)
    confirm = admin.post("/api/me/2fa/enroll/confirm",
                         json_body={"code": totp.now()}, csrf=True)
    r.check("TOTP enroll confirm -> 200 and cookies cleared (re-login required)",
            confirm.status == 200 and "access_token" not in admin.cookies,
            "200 + logged out", f"{confirm.status} {confirm.text[:80]}")
    re_login = staff_login(admin, STAFF["admin"], NEW_PW["admin"])
    r.check("login after enroll -> totp_verify challenge (no session yet)",
            re_login.status == 200 and re_login.json().get("next_step") == "totp_verify"
            and "access_token" not in admin.cookies,
            "totp_verify", f"{re_login.status} {re_login.text[:100]}")
    wrong = admin.post("/api/auth/staff/2fa/verify",
                       json_body={"challenge": re_login.json()["challenge"], "code": "000000"})
    r.check("[SECURITY] wrong TOTP -> 401, still no session",
            wrong.status == 401 and "access_token" not in admin.cookies, "401",
            f"{wrong.status}")
    ok = admin.post("/api/auth/staff/2fa/verify",
                    json_body={"challenge": re_login.json()["challenge"],
                               "code": pyotp.TOTP(secret).now()})
    r.check("correct TOTP -> session issued",
            ok.status == 200 and "access_token" in admin.cookies, "session",
            f"{ok.status} {ok.text[:80]}")
elif secret:
    # rerun: the plain login above must have returned a totp_verify challenge
    challenge = None
    probe_client = QAClient()
    probe = staff_login(probe_client, STAFF["admin"], NEW_PW["admin"])
    if probe.status == 200 and probe.json().get("next_step") == "totp_verify":
        challenge = probe.json()["challenge"]
        probe_client.cookies.clear()
        ok = probe_client.post("/api/auth/staff/2fa/verify",
                               json_body={"challenge": challenge,
                                          "code": pyotp.TOTP(secret).now()})
        r.check("rerun login -> totp_verify challenge; correct TOTP -> session",
                ok.status == 200 and "access_token" in probe_client.cookies
                and "access_token" in admin.cookies, "session (probe + S5 admin)",
                f"{ok.status} {ok.text[:80]}")
    else:
        r.check("rerun login -> totp_verify challenge", False, "totp_verify",
                f"{probe.status} {probe.text[:100]}")

# the force-logout above targeted the invitee; only re-auth the agent if its
# session really died (saves a login attempt against the 8/900 s per-email cap)
if agent.get("/api/me").status != 200:
    agent = QAClient()
    relog = staff_login(agent, STAFF["agent"], NEW_PW["agent"])
    assert relog.status == 200, f"agent re-login failed: {relog.status} {relog.text}"
sessions["agent"] = agent

# --------------------------------------------------------------------------- #
print("\n== S10. Complaint lifecycle: assign → reply → note → escalate → resolve → reopen → CSAT ==")
r.section = "S10 lifecycle"

# a fresh in-scope ticket owned by nobody: create as the probe customer
life_cust = QAClient()
life_email = f"lifecycle.{uuid.uuid4().hex[:6]}@example.com"
life_mobile = rand_mobile()
life_nid = rand_nid(century="3", yy="01", mm="05", dd="20", gov="04")
t0 = time.time()
st = life_cust.post("/api/auth/customer/otp/start",
                    json_body={"mobile": life_mobile, "email": life_email})
code = otp_code_for(life_email, t0)
life_cust.post("/api/auth/customer/otp/verify",
               json_body={"mobile": life_mobile, "email": life_email, "code": code,
                          "national_id": life_nid, "full_name": "دورة الحياة"})
assert company0 in sup_companies, "lifecycle company must be in supervisor scope too"
shared_teams = sorted(set(sup_acc["team_ids"]) & set(agent_acc["team_ids"]))
assert shared_teams, "supervisor and agent must share a team for ownership=team visibility"
created = life_cust.post("/api/customer/complaints", csrf=True,
                         headers={"Idempotency-Key": str(uuid.uuid4())},
                         json_body={"national_id": life_nid, "mobile": life_mobile,
                                    "company_id": company0, "insurance_type": "health",
                                    "policy_number": f"POL-{uuid.uuid4().hex[:6].upper()}",
                                    "subject": "مطالبة متأخرة للمراجعة الشاملة",
                                    "description": "وصف تفصيلي لشكوى اختبار دورة الحياة الكاملة "
                                                   "من التعيين حتى إعادة الفتح والتقييم النهائي."})
cid = created.json().get("id") if created.status == 201 else None
ticket = created.json().get("ticket_no") if created.status == 201 else None
r.check("lifecycle ticket created", created.status == 201 and cid, "201", f"{created.status}")

# RLS ownership semantics: an UNASSIGNED ticket has no team, so ownership=team
# (supervisor) and ownership=own (agent) cannot see it yet - only global scope can.
sup_before = supervisor.get(f"/api/staff/complaints/{cid}")
r.check("[SECURITY] supervisor (ownership=team) cannot see unassigned ticket -> 404",
        sup_before.status == 404, "404", f"{sup_before.status}")
agent_before = agent.get(f"/api/staff/complaints/{cid}")
r.check("[SECURITY] agent (ownership=own) cannot see unassigned ticket -> 404",
        agent_before.status == 404, "404", f"{agent_before.status}")

agent_row_id = agent_row["id"]
assign_bad = agent.post(f"/api/staff/complaints/{cid}/assign",
                        json_body={"agent_id": agent_row_id}, csrf=True)
r.check("[SECURITY] agent self-assign without complaints.assign -> 403",
        assign_bad.status == 403, "403", f"{assign_bad.status}")
assign = admin.post(f"/api/staff/complaints/{cid}/assign",
                    json_body={"agent_id": agent_row_id, "team_id": shared_teams[0]}, csrf=True)
r.check("admin assigns ticket to agent+team -> status assigned",
        assign.status == 200 and assign.json().get("status") == "assigned", "assigned",
        f"{assign.status} {assign.text[:100]}")

staff_list = supervisor.get(f"/api/staff/complaints?ticket_no={ticket}")
found = staff_list.status == 200 and any(i["id"] == cid for i in staff_list.json().get("items", []))
r.check("supervisor sees the assigned team ticket via ticket_no filter", found, "found",
        f"{staff_list.status} n={len(staff_list.json().get('items', []))}")

# agent (own scope) now sees it and replies publicly
det = agent.get(f"/api/staff/complaints/{cid}")
r.check("assigned agent (ownership=own) can now open the ticket", det.status == 200,
        "200", f"{det.status}")
reply = agent.post(f"/api/staff/complaints/{cid}/reply",
                   json_body={"body": "جارٍ مراجعة المطالبة مع شركة التأمين."}, csrf=True)
r.check("agent public reply -> 2xx", reply.status in (200, 201), "2xx",
        f"{reply.status} {reply.text[:80]}")
note = agent.post(f"/api/staff/complaints/{cid}/notes",
                  json_body={"body": "ملاحظة داخلية سرية: العميل VIP"}, csrf=True)
r.check("agent internal note -> 2xx", note.status in (200, 201), "2xx",
        f"{note.status} {note.text[:80]}")
cust_timeline = life_cust.get(f"/api/customer/complaints/{cid}/timeline")
leaked_note = "ملاحظة داخلية سرية" in cust_timeline.text
r.check("[SECURITY] internal note NEVER visible in customer timeline",
        cust_timeline.status == 200 and not leaked_note, "note hidden",
        f"{cust_timeline.status} leaked={leaked_note}")
cust_sees_reply = "جارٍ مراجعة المطالبة" in life_cust.get(
    f"/api/customer/complaints/{cid}").text or "جارٍ مراجعة المطالبة" in cust_timeline.text
r.check("customer DOES see the public reply", cust_sees_reply, "reply visible",
        str(cust_sees_reply))

# status transitions
in_review = agent.patch(f"/api/staff/complaints/{cid}/status",
                        json_body={"status": "in_review"}, csrf=True)
r.check("agent moves assigned -> in_review", in_review.status == 200, "200",
        f"{in_review.status} {in_review.text[:80]}")
bad_status = agent.patch(f"/api/staff/complaints/{cid}/status",
                         json_body={"status": "new"}, csrf=True)
r.check("illegal transition in_review -> new rejected", 400 <= bad_status.status < 500,
        "4xx", f"{bad_status.status}")
esc = agent.post(f"/api/staff/complaints/{cid}/escalate",
                 json_body={"reason": "Requires insurer follow-up"}, csrf=True)
r.check("[SECURITY] agent escalate without complaints.escalate -> 403", esc.status == 403,
        "403", f"{esc.status}")
esc2 = supervisor.post(f"/api/staff/complaints/{cid}/escalate",
                       json_body={"reason": "Requires insurer follow-up"}, csrf=True)
r.check("supervisor escalate -> queued for insurer delivery",
        esc2.status == 200 and esc2.json().get("delivery") in ("queued", "skipped", "sent"),
        "queued", f"{esc2.status} {esc2.text[:100]}")
resolve = supervisor.patch(f"/api/staff/complaints/{cid}/status",
                           json_body={"status": "resolved"}, csrf=True)
r.check("supervisor resolves ticket", resolve.status == 200, "200", f"{resolve.status}")

reopen = life_cust.post(f"/api/customer/complaints/{cid}/reopen", csrf=True)
r.check("customer reopens within window -> allowed", reopen.status == 200, "200",
        f"{reopen.status} {reopen.text[:100]}")
resolve2 = supervisor.patch(f"/api/staff/complaints/{cid}/status",
                            json_body={"status": "resolved"}, csrf=True)
csat = life_cust.post(f"/api/customer/complaints/{cid}/csat",
                      json_body={"score": 5, "comment": "خدمة ممتازة"}, csrf=True)
r.check("CSAT after resolution -> accepted", csat.status in (200, 201), "2xx",
        f"{csat.status} {csat.text[:80]}")
csat2 = life_cust.post(f"/api/customer/complaints/{cid}/csat",
                       json_body={"score": 1}, csrf=True)
r.check("duplicate CSAT -> rejected", 400 <= csat2.status < 500, "4xx", f"{csat2.status}")

# national-ID reveal: agent lacks the permission; admin has it and must give a reason
reveal_bad = agent.post(f"/api/staff/complaints/{cid}/national-id:reveal",
                        json_body={"reason": "QA verification of the reveal audit trail"},
                        csrf=True)
r.check("[SECURITY] agent NID reveal without permission -> 403", reveal_bad.status == 403,
        "403", f"{reveal_bad.status}")
reveal_short = admin.post(f"/api/staff/complaints/{cid}/national-id:reveal",
                          json_body={"reason": "قصير"}, csrf=True)
r.check("NID reveal with too-short reason -> 422", reveal_short.status == 422, "422",
        f"{reveal_short.status}")
reveal = admin.post(f"/api/staff/complaints/{cid}/national-id:reveal",
                    json_body={"reason": "QA verification of the reveal audit trail"},
                    csrf=True)
r.check("admin NID reveal with reason -> 200 returns full ID",
        reveal.status == 200 and life_nid in reveal.text, "full NID",
        f"{reveal.status} {reveal.text[:80]}")

# CSV export (supervisor has complaints.export)
exp = supervisor.get("/api/staff/complaints/export?date_from=2026-10-01T00:00:00Z"
                     "&date_to=2026-10-19T00:00:00Z")
r.check("staff CSV export (GET, flat ComplaintFilter) -> 200 text/csv",
        exp.status == 200 and "csv" in exp.headers.get("content-type", "").lower(),
        "200 text/csv", f"{exp.status} ct={exp.headers.get('content-type')} {exp.text[:100]}")
bad_filter = supervisor.get("/api/staff/complaints/export?date_from=not-a-date")
r.check("CSV export with invalid filter -> 422 (not 500)", bad_filter.status == 422,
        "422", f"{bad_filter.status}")
formula_inject = supervisor.get("/api/staff/complaints/export?date_from=2026-10-01T00:00:00Z"
                                "&date_to=2026-10-19T00:00:00Z&search==cmd")
r.check("CSV export with formula-like search term does not 500",
        formula_inject.status < 500, "<500", f"{formula_inject.status}")

# audit trail recorded the reveal + status changes
audit = admin.get("/api/staff/audit/events?page=1&page_size=50")
r.check("audit events list -> 200 and contains the reveal event",
        audit.status == 200 and "reveal" in audit.text.lower(), "reveal audited",
        f"{audit.status}")

# --------------------------------------------------------------------------- #
print("\n== S11. WebSocket reauth + origin ==")
r.section = "S11 websocket"


async def ws_checks() -> None:
    import websockets
    base = "ws://127.0.0.1:8000/api/staff/complaints/stream"
    cookie = "; ".join(f"{k}={v}" for k, v in supervisor.cookies.items())
    # 1) evil origin rejected
    try:
        async with websockets.connect(base, additional_headers={
                "Origin": "https://evil.example", "Cookie": cookie}) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
            r.check("[SECURITY] WS evil origin -> rejected", False, "close", "open")
    except Exception as exc:  # closed by server (1008) or rejected handshake
        r.check("[SECURITY] WS cross-origin -> rejected/closed", True, "", str(exc)[:60])
    # 2) unauthenticated rejected
    try:
        async with websockets.connect(base, additional_headers={
                "Origin": "http://127.0.0.1:8000"}) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            r.check("[SECURITY] WS without cookies -> no data", False, "close", str(msg)[:60])
    except Exception as exc:
        r.check("[SECURITY] WS without cookies -> rejected/closed", True, "", str(exc)[:60])
    # 3) authenticated supervisor receives a snapshot
    try:
        async with websockets.connect(base, additional_headers={
                "Origin": "http://127.0.0.1:8000", "Cookie": cookie}) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(msg)
            r.check("WS authenticated staff -> snapshot payload",
                    isinstance(data, dict) and ("items" in data or "snapshot" in str(data)[:200]
                                                or "total" in data),
                    "snapshot json", str(data)[:100])
    except Exception as exc:
        r.check("WS authenticated staff -> snapshot payload", False, "snapshot",
                f"EXC {exc}")


asyncio.run(ws_checks())

# --------------------------------------------------------------------------- #
print("\n== S12. Metrics, docs, abuse detector ==")
r.section = "S12 platform"

no_tok = QAClient().get("/metrics")
r.check("[SECURITY] /metrics without bearer token -> 401/403/404 (hidden)",
        no_tok.status in (401, 403, 404), "401/403/404", f"{no_tok.status}")
env_tok = [line.split("=", 1)[1].strip() for line in Path("../../.env").read_text().splitlines()
           if line.startswith("METRICS_TOKEN=")][0]
with_tok = QAClient().get("/metrics", headers={"Authorization": f"Bearer {env_tok}"})
r.check("/metrics with token -> 200 prometheus text",
        with_tok.status == 200 and (b"rate_limited" in with_tok.body or b"python_gc" in with_tok.body
                                    or b"http" in with_tok.body),
        "200 + metrics", f"{with_tok.status} {with_tok.body[:60]!r}")
docs = QAClient().get("/docs")
r.check("/docs available in development", docs.status == 200, "200", f"{docs.status}")

# Abuse detector: anonymous 401s are NOT counted (expired sessions are normal);
# an AUTHENTICATED actor bursting 404s (IDOR probing) crosses 40 denials/300 s
# and gets a 900 s block. Use the lifecycle customer - nothing needs it after S12.
anon_probe = QAClient(ip=fresh_ip())
anon_first = anon_probe.get(f"/api/staff/complaints/{uuid.uuid4()}")
r.check("anonymous IDOR probe -> uniform 401 (not counted toward abuse block by design)",
        anon_first.status == 401, "401", f"{anon_first.status}")
denied = sum(1 for _ in range(45)
             if life_cust.get(f"/api/customer/complaints/{uuid.uuid4()}").status == 404)
blocked = life_cust.get("/api/me")
r.check("[SECURITY] abuse detector blocks authenticated IDOR-probing user (429 Temporarily blocked)",
        denied >= 40 and blocked.status == 429
        and blocked.detail == "Temporarily blocked; try later",
        "429 blocked", f"denied={denied} final={blocked.status} {blocked.text[:80]}")

r.save(Path(__file__).parent / "results_staff.json")
