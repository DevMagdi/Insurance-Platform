"""Live E2E part 3: resilience (SMTP sink down, Redis down, Postgres down) and
JWT hardening (expired / alg=none / HS256-confusion / type-confusion tokens).

Run from anywhere:  .venv/bin/python .local-stack/qa/e2e_resilience.py
Assumes `make local-up` stack + mail_sink.py are running (it kills and restarts
them itself). Results land in .local-stack/qa/results_resilience.json.
"""
from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import jwt

from qa_lib import QAClient, Recorder, extract_code, wait_for_mail  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
QA = ROOT / ".local-stack" / "qa"
STATE = ROOT / ".local-stack"
LOGS = STATE / "logs"
API = "http://127.0.0.1:8000"
SINK_HOST, SINK_PORT = "169.254.0.21", 2525
PG_BIN = ROOT / ".venv/lib/python3.11/site-packages/pgserver/pginstall/bin"
REDIS_BIN = ROOT / ".venv/lib/python3.11/site-packages/redislite/bin/redis-server"
OTP_DELIVERY_FAILED = (
    "We could not send your verification code right now. Please try again in a few minutes."
)
# words that would betray infrastructure internals to an end user
LEAK_WORDS = ("not configured", "smtp", "traceback", "aiosmtpd", "connection refused",
              "redis", "asyncpg", "postgres", "errno", "connectionerror", "operationalerror")

r = Recorder()
RNG = __import__("random").Random()


def rand_mobile() -> str:
    return f"+2012{RNG.randrange(10000000, 99999999)}"


def rand_nid() -> str:
    # 14 digits: century(1) yy(2) mm(2) dd(2) gov(2) seq(5); birth years keep
    # the holder an adult (19xx 60-99 or 20xx 00-08).
    century = RNG.choice(("2", "3"))
    yy = f"{RNG.randrange(60, 100) if century == '2' else RNG.randrange(0, 9):02d}"
    mm = f"{RNG.randrange(1, 13):02d}"
    dd = f"{RNG.randrange(1, 29):02d}"
    gov = f"{RNG.randrange(1, 36):02d}"
    return f"{century}{yy}{mm}{dd}{gov}{RNG.randrange(10000, 100000)}"


def new_identity() -> dict[str, str]:
    return {"email": f"resilience.{uuid.uuid4().hex[:8]}@example.com",
            "mobile": rand_mobile(), "national_id": rand_nid()}


def port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def wait_port(host: str, port: int, up: bool, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(host, port) == up:
            return True
        time.sleep(0.4)
    return port_open(host, port) == up


def spawn(cmd: list[str], log: Path) -> subprocess.Popen:
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as sink:
        return subprocess.Popen(cmd, cwd=ROOT, stdout=sink, stderr=sink,
                                start_new_session=True)


def no_leak(text: str) -> bool:
    low = text.lower()
    return not any(w in low for w in LEAK_WORDS)


def worker_alive() -> bool:
    pid_file = STATE / "worker.pid"
    if not pid_file.exists():
        return False
    try:
        os.kill(int(pid_file.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def restart_worker() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import local_stack as ls
    if not worker_alive():
        ls.start_worker(ls.read_env())
        time.sleep(2)


def start_otp(client: QAClient, ident: dict[str, str]):
    return client.post("/api/auth/customer/otp/start",
                       json_body={"mobile": ident["mobile"], "email": ident["email"]})


# --------------------------------------------------------------------------- #
print("== R1. SMTP provider down during registration (HIGHEST PRIORITY) ==")
r.section = "R1 smtp-down"

subprocess.run(["pkill", "-f", "mail_sink.py"], check=False)
sink_down = wait_port(SINK_HOST, SINK_PORT, up=False, timeout=15)
r.check("SMTP sink stopped (port 2525 closed)", sink_down, "port closed",
        f"open={port_open(SINK_HOST, SINK_PORT)}")

c = QAClient()
ident = new_identity()
st = start_otp(c, ident)
body = st.text
r.check("[CRITICAL-FIX] new registration with mail provider DOWN -> graceful 503 "
        "(NOT the old 422 'Email delivery is not configured')",
        st.status == 503 and st.detail == OTP_DELIVERY_FAILED,
        f"503 {OTP_DELIVERY_FAILED!r}", f"{st.status} {body[:160]}")
r.check("[SECURITY] 503 body leaks no infrastructure internals", no_leak(body),
        "no leak words", body[:160])
r.check("503 carries Retry-After: 120", st.headers.get("retry-after") == "120",
        "Retry-After 120", str(st.headers.get("retry-after")))

# restart the sink and prove full recovery
proc = spawn([str(ROOT / ".venv/bin/python"), str(QA / "mail_sink.py")],
             LOGS / "mail_sink.log")
sink_up = wait_port(SINK_HOST, SINK_PORT, up=True, timeout=30)
r.check("SMTP sink restarted", sink_up and proc.poll() is None, "port open",
        f"up={sink_up} poll={proc.poll()}")
ident2 = new_identity()
t0 = time.time()
c2 = QAClient()
st2 = start_otp(c2, ident2)
mail = wait_for_mail(ident2["email"], t0, timeout=60) if st2.status == 202 else None
code = extract_code(mail) if mail else None
r.check("registration recovers after provider returns -> 202 + real OTP e-mail",
        st2.status == 202 and mail is not None and bool(code),
        "202 + code", f"{st2.status} mail={bool(mail)} code={code}")

# --------------------------------------------------------------------------- #
print("\n== R2. Redis down ==")
r.section = "R2 redis-down"

redis_pid = int((STATE / "redis.pid").read_text().strip())
os.kill(redis_pid, signal.SIGTERM)
redis_down = wait_port("127.0.0.1", 6379, up=False, timeout=15)
r.check("redis stopped (port 6379 closed)", redis_down, "closed", str(redis_down))

live = QAClient().get("/api/health/live")
ready = QAClient().get("/api/health/ready")
r.check("/api/health/live stays 200 (process alive)", live.status == 200, "200",
        f"{live.status}")
r.check("/api/health/ready -> 503 'Dependencies unavailable'",
        ready.status == 503 and ready.detail == "Dependencies unavailable",
        "503 Dependencies unavailable", f"{ready.status} {ready.text[:100]}")

comp = QAClient().get("/api/companies")
r.check("[FIX L-7] GET /api/companies during redis outage -> graceful JSON 503 + Retry-After",
        comp.status == 503 and comp.detail == "Service temporarily unavailable; please retry shortly."
        and comp.headers.get("retry-after") == "30" and no_leak(comp.text),
        "503 JSON, no internals", f"{comp.status} {comp.text[:120]}")
ident3 = new_identity()
otp_redis = start_otp(QAClient(), ident3)
r.check("[FIX L-7] OTP start during redis outage -> graceful JSON 503 (rate limiter fails safe)",
        otp_redis.status == 503 and otp_redis.detail == "Service temporarily unavailable; please retry shortly."
        and no_leak(otp_redis.text),
        "503 JSON, no internals", f"{otp_redis.status} {otp_redis.text[:140]}")

rp = spawn([str(REDIS_BIN), "--port", "6379", "--bind", "127.0.0.1",
            "--dir", str(STATE / "redis"), "--dbfilename", "dump.rdb", "--save", ""],
           LOGS / "redis.log")
(STATE / "redis.pid").write_text(str(rp.pid) + "\n")
redis_up = wait_port("127.0.0.1", 6379, up=True, timeout=30)
ready2 = None
deadline = time.time() + 30
while time.time() < deadline:
    ready2 = QAClient().get("/api/health/ready")
    if ready2.status == 200:
        break
    time.sleep(1)
r.check("redis restarted -> /api/health/ready recovers to 200",
        redis_up and ready2 is not None and ready2.status == 200, "200",
        f"up={redis_up} ready={ready2.status if ready2 else None}")

ident4 = new_identity()
t0 = time.time()
c4 = QAClient()
st4 = start_otp(c4, ident4)
mail4 = wait_for_mail(ident4["email"], t0, timeout=60) if st4.status == 202 else None
r.check("full OTP registration works again after redis recovery",
        st4.status == 202 and mail4 is not None, "202 + mail",
        f"{st4.status} mail={bool(mail4)}")
survived_redis = worker_alive()
r.check("[RESILIENCE] arq worker survives a Redis (broker) outage",
        survived_redis, "worker alive",
        "alive" if survived_redis else
        "DEAD - arq exits on broker ConnectionError; needs supervisor restart")
restart_worker()
r.info("worker self-heal", f"worker restarted: alive={worker_alive()} "
       "(docker-compose restart-policy covers this in production; the bare process "
       "does not reconnect to a lost broker on its own)")

# --------------------------------------------------------------------------- #
print("\n== R3. Postgres down ==")
r.section = "R3 postgres-down"

pgdata = STATE / "pgdata"
stop = subprocess.run([str(PG_BIN / "pg_ctl"), "-w", "-D", str(pgdata),
                       "stop", "-m", "fast"], capture_output=True, text=True, timeout=60)
pg_down = wait_port("127.0.0.1", 5432, up=False, timeout=20)
r.check("postgres stopped (pg_ctl fast)", stop.returncode == 0 and pg_down, "stopped",
        f"rc={stop.returncode} down={pg_down} {stop.stderr[:100]}")

live = QAClient().get("/api/health/live")
ready = QAClient().get("/api/health/ready")
r.check("live stays 200 while DB is down", live.status == 200, "200", f"{live.status}")
r.check("ready -> 503 'Dependencies unavailable' while DB is down",
        ready.status == 503 and ready.detail == "Dependencies unavailable",
        "503", f"{ready.status} {ready.text[:100]}")
comp = QAClient().get("/api/companies")
r.check("[FIX L-7] GET /api/companies during DB outage -> graceful JSON 503 (middleware-level)",
        comp.status == 503 and comp.detail == "Service temporarily unavailable; please retry shortly." and no_leak(comp.text),
        "503 JSON, no internals", f"{comp.status} {comp.text[:140]}")

start = subprocess.run(
    [str(PG_BIN / "pg_ctl"), "-w", "-D", str(pgdata),
     "-o", f"-h 127.0.0.1 -p 5432 -k {STATE / 'pgsock'} -c listen_addresses=127.0.0.1",
     "-l", str(STATE / "postgres.log"), "start"],
    capture_output=True, text=True, timeout=90)
pg_up = wait_port("127.0.0.1", 5432, up=True, timeout=30)
ready3 = None
deadline = time.time() + 90
while time.time() < deadline:
    ready3 = QAClient().get("/api/health/ready")
    if ready3.status == 200:
        break
    time.sleep(1)
r.check("postgres restarted -> ready recovers to 200 (pool reconnects)",
        start.returncode == 0 and pg_up and ready3 is not None and ready3.status == 200,
        "200", f"rc={start.returncode} up={pg_up} ready={ready3.status if ready3 else None}")

comp2 = None
deadline = time.time() + 60
while time.time() < deadline:
    comp2 = QAClient().get("/api/companies")
    if comp2.status == 200:
        break
    time.sleep(1)
r.check("API fully recovers after DB outage (companies 200)",
        comp2 is not None and comp2.status == 200, "200",
        f"{comp2.status if comp2 else None}")

survived_pg = worker_alive()
r.check("[RESILIENCE] arq worker survives a Postgres outage (broker stays up)",
        survived_pg, "worker alive", "alive" if survived_pg else "DEAD")
restart_worker()

# --------------------------------------------------------------------------- #
print("\n== R4. JWT hardening (expired / alg=none / confusion / type mix-ups) ==")
r.section = "R4 jwt"

env = {}
for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
priv_pem = base64.b64decode(env["JWT_PRIVATE_KEY_B64"])
pub_pem = base64.b64decode(env["JWT_PUBLIC_KEY_B64"])

# register a fresh customer to obtain real tokens
ident5 = new_identity()
t0 = time.time()
c5 = QAClient()
st5 = start_otp(c5, ident5)
code5 = otp = None
mail5 = wait_for_mail(ident5["email"], t0, timeout=60) if st5.status == 202 else None
code5 = extract_code(mail5) if mail5 else None
ver = c5.post("/api/auth/customer/otp/verify",
              json_body={"mobile": ident5["mobile"], "email": ident5["email"],
                         "code": code5, "national_id": ident5["national_id"],
                         "full_name": "اختبار الصلابة"})
live_access = c5.cookies.get("access_token")
live_refresh = c5.cookies.get("refresh_token")
r.check("baseline customer registered for token tests",
        ver.status == 200 and live_access and live_refresh, "session",
        f"{ver.status} {ver.text[:120]} access={bool(live_access)}")
assert live_access and live_refresh, f"baseline registration failed: {ver.status} {ver.text[:200]}"

claims = jwt.decode(live_access, options={"verify_signature": False,
                                          "verify_exp": False, "verify_aud": False})


def sign(claim_overrides: dict, *, key=priv_pem, alg="EdDSA", kid="active",
         drop_kid: bool = False) -> str:
    payload = {**claims, **claim_overrides}
    headers = {"kid": kid} if not drop_kid else {}
    return jwt.encode(payload, key, algorithm=alg, headers=headers)


now = int(time.time())
expired = sign({"exp": now - 300, "nbf": now - 600, "iat": now - 900,
                "jti": str(uuid.uuid4())})
e = QAClient()
e.cookies["access_token"] = expired
e.cookies["csrf_token"] = c5.cookies.get("csrf_token", "")
res = e.get("/api/me")
r.check("[SECURITY] correctly-signed but EXPIRED access token -> 401",
        res.status == 401, "401", f"{res.status} {res.text[:80]}")

b64 = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
none_tok = b64({"alg": "none", "typ": "JWT", "kid": "active"}) + "." + b64(
    {**claims, "exp": now + 600}) + "."
n = QAClient()
n.cookies["access_token"] = none_tok
res = n.get("/api/me")
r.check("[SECURITY] alg=none forgery -> 401", res.status == 401, "401",
        f"{res.status} {res.text[:80]}")

# PyJWT refuses to HMAC-sign with a PEM key (as it should), so forge the classic
# algorithm-confusion token by hand: HS256 signature keyed with the PUBLIC key.
import hashlib
import hmac as _hmac
hs_header = b64({"alg": "HS256", "typ": "JWT", "kid": "active"})
hs_payload = b64({**claims, "exp": now + 600, "jti": str(uuid.uuid4())})
hs_sig = base64.urlsafe_b64encode(
    _hmac.new(pub_pem, f"{hs_header}.{hs_payload}".encode(), hashlib.sha256).digest()
).rstrip(b"=").decode()
hs = f"{hs_header}.{hs_payload}.{hs_sig}"
h = QAClient()
h.cookies["access_token"] = hs
res = h.get("/api/me")
r.check("[SECURITY] HS256 algorithm-confusion (public key as HMAC secret) -> 401",
        res.status == 401, "401", f"{res.status} {res.text[:80]}")

bad_kid = sign({"exp": now + 600, "jti": str(uuid.uuid4())}, kid="rotated-2019")
k = QAClient()
k.cookies["access_token"] = bad_kid
res = k.get("/api/me")
r.check("[SECURITY] unknown kid header -> 401", res.status == 401, "401",
        f"{res.status} {res.text[:80]}")

no_kid = sign({"exp": now + 600, "jti": str(uuid.uuid4())}, drop_kid=True)
nk = QAClient()
nk.cookies["access_token"] = no_kid
res = nk.get("/api/me")
r.check("[SECURITY] missing kid header -> 401", res.status == 401, "401",
        f"{res.status} {res.text[:80]}")

g = QAClient()
g.cookies["access_token"] = "garbage.not.a.jwt"
res = g.get("/api/me")
r.check("[SECURITY] malformed cookie -> 401 (no 500)", res.status == 401, "401",
        f"{res.status} {res.text[:80]}")

# type confusion: access token presented as refresh token and vice versa
tc = QAClient()
tc.cookies["refresh_token"] = live_access
tc.cookies["csrf_token"] = c5.cookies.get("csrf_token", "")
res = tc.post("/api/auth/refresh", csrf=True)
r.check("[SECURITY] access token used as refresh token -> 401 (typ confusion blocked)",
        res.status == 401, "401", f"{res.status} {res.text[:80]}")
ac = QAClient()
ac.cookies["access_token"] = live_refresh
res = ac.get("/api/me")
r.check("[SECURITY] refresh token used as access token -> 401 (typ confusion blocked)",
        res.status == 401, "401", f"{res.status} {res.text[:80]}")

# subject swap: claim tampering without a valid signature
tampered_payload = b64({**claims, "sub": str(uuid.uuid4()), "exp": now + 600})
sig = live_access.rsplit(".", 1)[1]
t = QAClient()
t.cookies["access_token"] = live_access.split(".")[0] + "." + tampered_payload + "." + sig
res = t.get("/api/me")
r.check("[SECURITY] payload swap under original signature -> 401",
        res.status == 401, "401", f"{res.status} {res.text[:80]}")

r.save(QA / "results_resilience.json")
sys.exit(1 if any(x["status"] == "FAIL" for x in r.results) else 0)
