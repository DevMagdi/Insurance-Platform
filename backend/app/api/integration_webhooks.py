"""HMAC-authenticated provider webhooks. Payloads enter the durable integration queue."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from app.api.deps import client_ip, integration_service
from app.db import purpose_transaction

router = APIRouter(prefix="/api/webhooks", tags=["public-webhooks"])
Provider = Literal[
    "email_webhook", "whatsapp_cloud", "whatsapp", "mock_voice", "twilio", "twilio_sms", "vonage"
]
# Meta registers a single callback URL and uses it for both the subscription
# handshake and every message delivery, so both spellings of the WhatsApp path
# have to answer both methods. Deliveries to /whatsapp are the same provider.
_PROVIDER_ALIASES = {"whatsapp": "whatsapp_cloud"}
_MAX_BODY_BYTES = 1_048_576


async def _limit(request: Request, bucket: str, limit: int) -> None:
    auth = getattr(request.app.state, "auth", None)
    limiter = getattr(auth, "limits", None)
    if limiter is not None:
        await limiter.check(bucket, client_ip(request), limit, 60)


@router.post("/{provider}/{company_id}", status_code=202)
async def provider_webhook(
    provider: Provider, company_id: UUID, request: Request
) -> dict[str, Any]:
    await _limit(request, "integration:webhook:ip", 600)
    content_length = request.headers.get("content-length")
    if content_length and (not content_length.isdecimal() or int(content_length) > _MAX_BODY_BYTES):
        raise HTTPException(status_code=413, detail="Webhook payload is too large")
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Webhook payload is too large")
    async with purpose_transaction(request.app.state.factory, "webhook") as db:
        return await integration_service(request).receive_webhook(
            db,
            company_id=company_id,
            provider=_PROVIDER_ALIASES.get(provider, provider),
            raw_body=body,
            headers={key.lower(): value for key, value in request.headers.items()},
        )


async def _twilio_form(request: Request) -> dict[str, list[str]]:
    """Twilio always posts form-encoded calls; its signature covers those fields."""
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Webhook payload is too large")
    try:
        return parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="Twilio body must be form encoded") from exc


def _twiml(document: str) -> Response:
    # Twilio reads the response as the call's script and needs an XML media
    # type to parse it; anything else ends the call with a spoken error.
    return Response(content=document, media_type="text/xml; charset=utf-8")


@router.post("/twilio/{company_id}/voice", response_class=Response)
async def twilio_voice_answer(company_id: UUID, request: Request) -> Response:
    """The number's 'A call comes in' URL: greets and asks for consent."""
    await _limit(request, "integration:webhook:ip", 600)
    form = await _twilio_form(request)
    async with purpose_transaction(request.app.state.factory, "webhook") as db:
        document = await integration_service(request).inbound_voice_answer(
            db,
            company_id,
            path=request.url.path,
            query=request.url.query,
            form=form,
            signature=request.headers.get("x-twilio-signature"),
        )
    return _twiml(document)


@router.post("/twilio/{company_id}/voice/consent", response_class=Response)
async def twilio_voice_consent(company_id: UUID, request: Request) -> Response:
    """Either start recording or open the request without audio."""
    await _limit(request, "integration:webhook:ip", 600)
    form = await _twilio_form(request)
    async with purpose_transaction(request.app.state.factory, "webhook") as db:
        document = await integration_service(request).inbound_voice_consent(
            db,
            company_id,
            path=request.url.path,
            query=request.url.query,
            form=form,
            signature=request.headers.get("x-twilio-signature"),
        )
    return _twiml(document)


@router.post("/twilio/{company_id}/voice/recording", status_code=202)
async def twilio_voice_recording(
    company_id: UUID, request: Request, caller: str = Query(default="")
) -> dict[str, Any]:
    """Twilio's recordingStatusCallback, which omits the caller's number."""
    await _limit(request, "integration:webhook:ip", 600)
    form = await _twilio_form(request)
    async with purpose_transaction(request.app.state.factory, "webhook") as db:
        return await integration_service(request).inbound_voice_recording(
            db,
            company_id,
            path=request.url.path,
            query=request.url.query,
            form=form,
            signature=request.headers.get("x-twilio-signature"),
            caller=caller,
        )


@router.get("/whatsapp/{company_id}", response_class=PlainTextResponse)
@router.get("/whatsapp_cloud/{company_id}", response_class=PlainTextResponse)
async def verify_whatsapp_webhook(
    company_id: UUID,
    request: Request,
    mode: str | None = Query(default=None, alias="hub.mode"),
    verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    async with purpose_transaction(request.app.state.factory, "webhook") as db:
        value = await integration_service(request).verify_whatsapp_subscription(
            db, company_id, mode, verify_token, challenge
        )
    return PlainTextResponse(value, status_code=200)
