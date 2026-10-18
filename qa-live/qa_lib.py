"""Shared QA helpers for live end-to-end testing of the running stack.

Talks to the real API over HTTP (127.0.0.1:8000) and, for proxy checks, to the
Vite origin (127.0.0.1:5173). Cookies are managed manually because the platform
sets ``Secure`` cookies and the sandbox API speaks plain HTTP — a browser would
behave the same over TLS-terminated preview, but httpx's jar would drop them.
OTP codes are read from the QA SMTP sink mailbox (.local-stack/qa/mail).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import httpx

API = "http://127.0.0.1:8000"
VITE = "http://127.0.0.1:5173"
MAILDIR = Path(__file__).resolve().parent / "mail"

IP_COUNTER = {"n": __import__("random").randrange(0, 200)}


def fresh_ip(prefix: str = "198.51") -> str:
    """A unique documentation-range client IP for X-Forwarded-For rotation."""
    IP_COUNTER["n"] += 1
    n = IP_COUNTER["n"]
    return f"{prefix}.{(n >> 8) & 0xFF}.{(n & 0xFF) or 1}"


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body or b"null")

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def detail(self) -> Any:
        try:
            return self.json().get("detail")
        except Exception:
            return None


class QAClient:
    """Cookie-aware HTTP client with per-request client-IP rotation."""

    def __init__(self, base_url: str = API, ip: str | None = None) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=30.0)
        self.cookies: dict[str, str] = {}
        self.ip = ip or fresh_ip()
        self.ua = "QA-E2E/1.0 desktop"

    # -- cookie plumbing ----------------------------------------------------
    def _absorb(self, response: httpx.Response) -> None:
        for header in response.headers.get_list("set-cookie"):
            jar: SimpleCookie = SimpleCookie()
            try:
                jar.load(header)
            except Exception:
                continue
            for name, morsel in jar.items():
                value = morsel.value or ""
                expires_now = morsel["max-age"] == "0" or (
                    morsel["expires"] and "1970" in morsel["expires"]
                )
                if expires_now or value == "":
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = value

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        headers = {
            "X-Forwarded-For": self.ip,
            "User-Agent": self.ua,
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
        }
        headers.update(extra or {})
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
        csrf: bool = False,
        content: bytes | None = None,
    ) -> Response:
        extra = dict(headers or {})
        if csrf:
            token = self.cookies.get("csrf_token")
            assert token, "csrf_token cookie missing — cannot sign the write"
            extra["X-CSRF-Token"] = token
        raw = self.http.request(
            method, path, json=json_body, data=data, files=files,
            content=content, headers=self._headers(extra),
        )
        self._absorb(raw)
        return Response(raw.status_code, dict(raw.headers), raw.content)

    def get(self, path: str, **kw: Any) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Response:
        return self.request("POST", path, **kw)

    def patch(self, path: str, **kw: Any) -> Response:
        return self.request("PATCH", path, **kw)

    def put(self, path: str, **kw: Any) -> Response:
        return self.request("PUT", path, **kw)

    def delete(self, path: str, **kw: Any) -> Response:
        return self.request("DELETE", path, **kw)

    def close(self) -> None:
        self.http.close()


# --------------------------------------------------------------------------- #
# mailbox helpers (QA SMTP sink)
# --------------------------------------------------------------------------- #
def wait_for_mail(address: str, since: float, timeout: float = 20.0,
                  subject_contains: str | None = None) -> Path | None:
    deadline = time.time() + timeout
    address = address.lower()
    while time.time() < deadline:
        for path in sorted(MAILDIR.glob("*.eml"), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.stat().st_mtime < since - 1:
                continue
            raw = path.read_bytes().decode("utf-8", "replace")
            m = re.search(r"X-Envelope-To: (.*)", raw)
            if not m or address not in m.group(1).lower():
                continue
            if subject_contains and subject_contains.lower() not in raw.lower():
                continue
            return path
        time.sleep(0.4)
    return None


def extract_code(path: Path) -> str | None:
    raw = path.read_bytes().decode("utf-8", "replace")
    match = re.search(r"code is=?\s*(\d{6})", raw.replace("=\r\n", "").replace("=\n", ""))
    return match.group(1) if match else None


def otp_code_for(address: str, since: float) -> str:
    path = wait_for_mail(address, since)
    assert path is not None, f"no OTP mail arrived for {address}"
    code = extract_code(path)  # type: ignore[arg-type]
    assert code, f"OTP mail for {address} had no code"
    return code


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# result recording
# ---------------------------------------------------------------------------
@dataclass
class Recorder:
    section: str = ""
    results: list[dict[str, Any]] = field(default_factory=list)

    def check(self, name: str, condition: bool, expected: str = "", actual: str = "") -> bool:
        self.results.append({
            "section": self.section, "name": name,
            "status": "PASS" if condition else "FAIL",
            "expected": expected, "actual": actual,
        })
        mark = "✔" if condition else "✘"
        print(f"  {mark} {name}" + (f"  [expected {expected} | got {actual}]"
                                    if not condition and (expected or actual) else ""),
              flush=True)
        return bool(condition)

    def info(self, name: str, detail: str) -> None:
        self.results.append({
            "section": self.section, "name": name, "status": "INFO",
            "expected": "", "actual": detail,
        })
        print(f"  ℹ {name}: {detail}", flush=True)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.results, indent=2, ensure_ascii=False), encoding="utf-8")
        failed = [r for r in self.results if r["status"] == "FAIL"]
        print(f"\n{len(self.results)} checks recorded, {len(failed)} FAILED -> {path}")
