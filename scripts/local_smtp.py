"""Minimal SMTP sink for the Docker-free local stack (stands in for Mailpit).

Messages are written to ``.local-stack/mail/`` as ``.eml`` files and the
one-time codes are printed to stdout, so a developer can complete an OTP or
password-reset flow without an external mailbox. Stdlib only, on purpose:
this file is a development convenience, not part of the product.
"""

from __future__ import annotations

import argparse
import re
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

MAX_MESSAGE_BYTES = 1_000_000
CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
CODE_HINT = re.compile(r"code[^0-9]{0,24}(\d{6})", re.IGNORECASE)


def _extract_code(payload: bytes) -> str | None:
    """Pull a six-digit one-time code out of a delivered message."""
    text = payload.decode("utf-8", errors="replace")
    # Drop the transport headers; the code only ever appears in the body.
    body = text.split("\n\n", 1)[-1]
    hinted = CODE_HINT.search(body)
    if hinted:
        return hinted.group(1)
    match = CODE_PATTERN.search(body)
    return match.group(1) if match else None


class SMTPSinkHandler(socketserver.StreamRequestHandler):
    """Speak just enough SMTP for aiosmtplib to hand over a message."""

    server: SMTPSinkServer  # type: ignore[assignment]

    def _send(self, line: str) -> None:
        self.wfile.write(f"{line}\r\n".encode())
        self.wfile.flush()

    def handle(self) -> None:
        self._send("220 localhost ESMTP local-stack")
        mail_from = ""
        recipients: list[str] = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").strip()
            upper = line.upper()
            if upper.startswith(("EHLO", "HELO")):
                self._send("250-localhost")
                self._send("250 SIZE 10485760")
            elif upper.startswith("MAIL FROM:"):
                mail_from = line.partition(":")[2].strip()
                recipients = []
                self._send("250 2.1.0 Ok")
            elif upper.startswith("RCPT TO:"):
                recipients.append(line.partition(":")[2].strip())
                self._send("250 2.1.5 Ok")
            elif upper == "DATA":
                self._send("354 End data with <CR><LF>.<CR><LF>")
                body = self._read_data()
                self._store(mail_from, recipients, body)
                self._send("250 2.0.0 Ok: queued")
            elif upper == "RSET":
                mail_from, recipients = "", []
                self._send("250 2.0.0 Ok")
            elif upper == "NOOP":
                self._send("250 2.0.0 Ok")
            elif upper == "QUIT":
                self._send("221 2.0.0 Bye")
                return
            else:
                self._send("502 5.5.2 Command not implemented")

    def _read_data(self) -> bytes:
        body = bytearray()
        while True:
            line = self.rfile.readline()
            if not line or line in (b".\r\n", b".\n"):
                break
            body += line[1:] if line.startswith(b"..") else line
            if len(body) > MAX_MESSAGE_BYTES:
                break
        return bytes(body)

    def _store(self, mail_from: str, recipients: list[str], body: bytes) -> None:
        directory = self.server.maildir
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time())}-{threading.get_ident() % 100000}.eml"
        path = directory / name
        header = f"X-Local-Mail-From: {mail_from}\nX-Local-Mail-To: {', '.join(recipients)}\n\n"
        path.write_bytes(header.encode() + body)
        code = _extract_code(body)
        if code:
            print(f"[smtp] one-time code {code} -> {', '.join(recipients)}", flush=True)
        else:
            print(f"[smtp] message stored at {path}", flush=True)


class SMTPSinkServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], maildir: Path) -> None:
        super().__init__(address, SMTPSinkHandler)
        self.maildir = maildir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1025)
    parser.add_argument(
        "--maildir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".local-stack" / "mail",
    )
    args = parser.parse_args()

    socket.setdefaulttimeout(None)
    server = SMTPSinkServer((args.host, args.port), args.maildir)
    print(
        f"[smtp] sink listening on {args.host}:{args.port} -> {args.maildir}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
