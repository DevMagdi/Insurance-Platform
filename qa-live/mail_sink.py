"""QA-only SMTP sink with STARTTLS for live registration/OTP testing.

Stores every accepted message as an .eml file under .local-stack/qa/mail so
E2E scripts can extract OTP codes exactly like a mailbox would. This is a test
harness outside the product: the platform itself keeps its fail-closed policy
(no sandbox fallback) — this sink plays the role of a *real* TLS-enabled SMTP
provider on a routable (non-blocklisted) address.
"""

import asyncio
import ssl
import time
from pathlib import Path

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP

HERE = Path(__file__).resolve().parent
MAIL = HERE / "mail"
CERTS = HERE / "certs"
MAIL.mkdir(parents=True, exist_ok=True)


class Sink:
    async def handle_DATA(self, server: SMTP, session, envelope) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = MAIL / f"{stamp}-{abs(hash(envelope.mail_from + str(time.time_ns()))) % 10**8:08d}.eml"
        header = (
            f"X-Envelope-From: {envelope.mail_from}\n"
            f"X-Envelope-To: {', '.join(envelope.rcpt_tos)}\n"
        ).encode()
        target.write_bytes(header + envelope.content)
        print(f"[sink] stored {target.name} for {envelope.rcpt_tos}", flush=True)
        return "250 OK"


def main() -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERTS / "server.pem", CERTS / "server.key")
    controller = Controller(
        Sink(),
        hostname="0.0.0.0",
        port=2525,
        tls_context=ctx,
        require_starttls=False,
    )
    controller.start()
    print("[sink] listening on 0.0.0.0:2525 (STARTTLS advertised)", flush=True)
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        controller.stop()


if __name__ == "__main__":
    main()
