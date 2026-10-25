"""Insurance operations: issuing workflow, policy lifecycle, payments.

Phase 1 completions living here: the staff **Client Folder** (contract +
documents + insured employees + every linked request), **caller lookup**
by mobile, **Excel import** of existing clients/employees, and the
**insurer-performance** evidence report. Phase 2: issuing checklists,
installment policies with ~5% commission settlements, renewal alerts,
smart document search, and Fawry/Paymob payment webhooks.

Sales CRM used to share this router under ``/api/staff/crm``; since the
CRM unification (migration 041) the only CRM is the unified module
under ``/api/crm`` — issuing requests may still link a CRM lead
(``crm_leads``) by id. Every write is audit-logged and gated by a
``resource.action`` permission; PostgreSQL RLS confines these tables to
the staff actor scope, and insurer scope is repeated in the repository.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.api.deps import RequestContext, require_permission
from app.db import purpose_transaction
from app.domain.enums import IssuingStatus, PaymentProviderName
from app.domain.insurance_ops_schemas import (
    BrokeragePolicyCreate,
    ChecklistItemUpdate,
    InstallmentPay,
    IssuingCreate,
    IssuingUpdate,
    PaymentIntentCreate,
    SettlementUpdate,
)
from app.models.insurance_ops import (
    BrokeragePolicy,
    CommissionSettlement,
    ImportJob,
    IssuingChecklistItem,
    IssuingRequest,
    PaymentTransaction,
    PolicyInstallment,
)
from app.models.tickets import AuditLog
from app.repositories.insurance_ops import InsuranceOpsRepository
from app.security.national_id import validate_national_id
from app.security.tokens import csrf_required
from app.services.crm_pipeline import slugify_client_name
from app.services.insurance_ops import commission_amount, installment_schedule, settlement_status
from app.services.payments import verify_fawry, verify_paymob
from app.services.tabular import normalize_mobile, parse_tabular

router = APIRouter(prefix="/api/staff/insurance-ops", tags=["insurance-ops"])
payments_router = APIRouter(prefix="/api/payments", tags=["payments"])

# Client Folder / caller lookup / evidence reads keep their historical
# crm.view gate (the folder is where a converted CRM lead lands).
CRM_VIEW = Depends(require_permission("crm.view"))
POLICIES_VIEW = Depends(require_permission("policies.view"))
POLICIES_MANAGE = Depends(require_permission("policies.manage"))
ISSUING_VIEW = Depends(require_permission("issuing.view"))
ISSUING_MANAGE = Depends(require_permission("issuing.manage"))
IMPORTS_MANAGE = Depends(require_permission("imports.manage"))
PAYMENTS_MANAGE = Depends(require_permission("payments.manage"))

_MAX_IMPORT_BYTES = 5_242_880
repo = InsuranceOpsRepository()


async def _audit(
    ctx: RequestContext,
    action: str,
    resource_type: str,
    resource_id: UUID,
    after: dict[str, Any],
) -> None:
    async with ctx.db.begin_nested():
        ctx.db.add(
            AuditLog(
                actor_staff_id=ctx.actor.id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                after_state=after,
            )
        )
    await ctx.db.flush()


def _conflict(exc: DBAPIError, detail: str) -> HTTPException:
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if sqlstate == "23505":
        return HTTPException(status_code=409, detail=detail)
    if sqlstate in {"23503", "23514", "22023", "22P02"}:
        return HTTPException(status_code=422, detail=detail)
    raise exc


# --- Policies: installments, commission, renewals ----------------------------


@router.get("/policies")
async def list_policies(ctx: RequestContext = POLICIES_VIEW) -> dict[str, Any]:
    return {"policies": await repo.policies(ctx.db, ctx.scope)}


@router.post("/policies", status_code=201)
async def create_policy(
    item: BrokeragePolicyCreate,
    ctx: RequestContext = POLICIES_MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    """Create a policy, its installment schedule and its commission settlement."""
    commission = commission_amount(item.premium, item.commission_rate)
    policy = BrokeragePolicy(
        id=uuid4(),
        tenant_id=repo.tenant_id,
        corporate_client_id=item.corporate_client_id,
        insurer_company_id=item.insurer_company_id,
        policy_no=item.policy_no,
        insurance_type=item.insurance_type.value,
        starts_on=item.starts_on,
        ends_on=item.ends_on,
        premium=item.premium,
        currency=item.currency,
        installment_plan=item.installment_plan.value,
        commission_rate=item.commission_rate,
        commission_amount=commission,
        status="active",
        issuing_request_id=item.issuing_request_id,
        created_by=ctx.actor.id,
    )
    ctx.db.add(policy)
    for spec in installment_schedule(item.premium, item.installment_plan, item.starts_on):
        ctx.db.add(
            PolicyInstallment(
                id=uuid4(),
                tenant_id=repo.tenant_id,
                policy_id=policy.id,
                sequence_no=spec.sequence_no,
                due_date=spec.due_date,
                amount=spec.amount,
                status="pending",
            )
        )
    ctx.db.add(
        CommissionSettlement(
            id=uuid4(),
            tenant_id=repo.tenant_id,
            policy_id=policy.id,
            insurer_company_id=item.insurer_company_id,
            expected_amount=commission,
            status="pending",
        )
    )
    try:
        await ctx.db.flush()
    except DBAPIError as exc:
        raise _conflict(exc, "That policy number already exists") from exc
    await _audit(
        ctx,
        "brokerage_policy_created",
        "brokerage_policy",
        policy.id,
        {
            "policy_no": item.policy_no,
            "premium": str(item.premium),
            "plan": item.installment_plan.value,
            "commission": str(commission),
        },
    )
    detail = await repo.policy_detail(ctx.db, ctx.scope, policy.id)
    return detail if detail is not None else repo.policy_dict(policy)


@router.get("/policies/{policy_id}")
async def get_policy(policy_id: UUID, ctx: RequestContext = POLICIES_VIEW) -> dict[str, Any]:
    detail = await repo.policy_detail(ctx.db, ctx.scope, policy_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    return detail


@router.get("/renewals")
async def renewals(
    ctx: RequestContext = POLICIES_VIEW, days: int = Query(default=30, ge=1, le=365)
) -> dict[str, Any]:
    return {"renewals": await repo.renewals(ctx.db, ctx.scope, days)}


@router.patch("/settlements/{settlement_id}")
async def update_settlement(
    settlement_id: UUID,
    item: SettlementUpdate,
    ctx: RequestContext = POLICIES_MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    settlement = (
        await ctx.db.execute(
            select(CommissionSettlement).where(CommissionSettlement.id == settlement_id)
        )
    ).scalar_one_or_none()
    if settlement is None:
        raise HTTPException(status_code=404, detail="Settlement not found")
    settlement.received_amount = item.received_amount
    settlement.status = settlement_status(settlement.expected_amount, item.received_amount)
    settlement.settled_at = datetime.now(UTC) if settlement.status == "settled" else None
    if item.notes:
        settlement.notes = item.notes
    settlement.updated_at = datetime.now(UTC)
    await ctx.db.flush()
    await _audit(
        ctx,
        "commission_settlement_updated",
        "commission_settlement",
        settlement_id,
        {"received": str(item.received_amount), "status": settlement.status},
    )
    return {"id": str(settlement_id), "status": settlement.status}


async def _installment(ctx: RequestContext, installment_id: UUID) -> PolicyInstallment:
    row = (
        await ctx.db.execute(
            select(PolicyInstallment).where(PolicyInstallment.id == installment_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Installment not found")
    return row


@router.post("/installments/{installment_id}/pay")
async def pay_installment(
    installment_id: UUID,
    item: InstallmentPay,
    ctx: RequestContext = PAYMENTS_MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    row = await _installment(ctx, installment_id)
    row.status = "paid"
    row.paid_at = datetime.now(UTC)
    row.payment_provider = item.provider.value
    row.payment_reference = item.reference
    row.updated_at = datetime.now(UTC)
    await ctx.db.flush()
    await _audit(
        ctx,
        "installment_paid",
        "policy_installment",
        installment_id,
        {"provider": item.provider.value, "amount": str(row.amount)},
    )
    return {"id": str(installment_id), "status": "paid"}


@router.post("/installments/{installment_id}/payment-intent", status_code=201)
async def payment_intent(
    installment_id: UUID,
    item: PaymentIntentCreate,
    request: Request,
    ctx: RequestContext = PAYMENTS_MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    """Open a Fawry/Paymob charge. Fail-closed until credentials are configured."""
    settings = request.app.state.settings
    configured = (
        item.provider is PaymentProviderName.PAYMOB and bool(settings.paymob_api_key)
    ) or (item.provider is PaymentProviderName.FAWRY and bool(settings.fawry_merchant_code))
    if not configured:
        raise HTTPException(status_code=503, detail="payment_provider_unconfigured")
    row = await _installment(ctx, installment_id)
    reference = f"{item.provider.value}-{installment_id}-{secrets.token_hex(4)}"
    ctx.db.add(
        PaymentTransaction(
            id=uuid4(),
            tenant_id=repo.tenant_id,
            installment_id=installment_id,
            provider=item.provider.value,
            provider_reference=reference,
            amount=row.amount,
            status="initiated",
        )
    )
    await ctx.db.flush()
    await _audit(
        ctx,
        "payment_intent_created",
        "policy_installment",
        installment_id,
        {"provider": item.provider.value, "reference": reference},
    )
    return {"provider": item.provider.value, "reference": reference, "amount": str(row.amount)}


@payments_router.post("/{provider}/webhook", status_code=202)
async def payment_webhook(provider: PaymentProviderName, request: Request) -> dict[str, Any]:
    """Signed gateway callback: marks the referenced installment paid exactly once."""
    settings = request.app.state.settings
    body = await request.body()
    if len(body) > 65_536:
        raise HTTPException(status_code=413, detail="Webhook payload is too large")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="Webhook body must be JSON") from exc
    reference = str(payload.get("reference") or payload.get("merchantRefNumber") or "")
    if provider is PaymentProviderName.PAYMOB:
        obj = {str(k): str(v) for k, v in (payload.get("obj") or {}).items()}
        reference = reference or str(
            (payload.get("obj") or {}).get("order", {}).get("merchant_order_id", "")
        )
        valid = verify_paymob(settings.paymob_hmac_secret, obj, request.headers.get("hmac"))
        paid = obj.get("success", "").lower() == "true"
    elif provider is PaymentProviderName.FAWRY:
        try:
            amount = Decimal(str(payload.get("paymentAmount", "0")))
        except InvalidOperation as exc:
            raise HTTPException(status_code=422, detail="Invalid payment amount") from exc
        valid = verify_fawry(
            settings.fawry_secret_key,
            settings.fawry_merchant_code or "",
            reference,
            amount,
            str(payload.get("messageSignature") or "") or None,
        )
        paid = str(payload.get("orderStatus", "")).upper() == "PAID"
    else:
        raise HTTPException(status_code=404, detail="Unknown payment provider")
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    async with purpose_transaction(request.app.state.factory, "webhook") as db:
        await db.execute(
            text("SELECT app_security.record_gateway_payment(:ref, :provider, :paid)"),
            {"ref": reference, "provider": provider.value, "paid": paid},
        )
    return {"accepted": True}


# --- Issuing workflow ----------------------------------------------------------


@router.get("/issuing")
async def list_issuing(ctx: RequestContext = ISSUING_VIEW) -> dict[str, Any]:
    return {"issuing": await repo.issuing(ctx.db, ctx.scope)}


@router.post("/issuing", status_code=201)
async def create_issuing(
    item: IssuingCreate, ctx: RequestContext = ISSUING_MANAGE, _: None = Depends(csrf_required)
) -> dict[str, Any]:
    """Open an issuing file with the required-documents checklist for its type."""
    request_row = IssuingRequest(
        id=uuid4(),
        tenant_id=repo.tenant_id,
        lead_id=item.lead_id,
        corporate_client_id=item.corporate_client_id,
        insurer_company_id=item.insurer_company_id,
        insurance_type=item.insurance_type.value,
        status=IssuingStatus.DRAFT.value,
        assigned_staff_id=item.assigned_staff_id or ctx.actor.id,
        notes=item.notes,
        created_by=ctx.actor.id,
    )
    ctx.db.add(request_row)
    for template in await repo.checklist_templates(ctx.db, item.insurance_type.value):
        ctx.db.add(
            IssuingChecklistItem(
                id=uuid4(),
                tenant_id=repo.tenant_id,
                issuing_request_id=request_row.id,
                document_name=template.document_name,
                is_required=template.is_required,
                status="pending",
            )
        )
    try:
        await ctx.db.flush()
    except DBAPIError as exc:
        raise _conflict(exc, "Issuing request could not be created") from exc
    await _audit(
        ctx,
        "issuing_created",
        "issuing_request",
        request_row.id,
        {"insurance_type": item.insurance_type.value},
    )
    return await repo.issuing_dict(ctx.db, request_row)


@router.patch("/issuing/{issuing_id}")
async def update_issuing(
    issuing_id: UUID,
    item: IssuingUpdate,
    ctx: RequestContext = ISSUING_MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    """Internal review → sent to insurer → issued, with the checklist gate."""
    row = (
        await ctx.db.execute(select(IssuingRequest).where(IssuingRequest.id == issuing_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Issuing request not found")
    if item.status is IssuingStatus.SENT_TO_INSURER:
        missing = await ctx.db.scalar(
            select(IssuingChecklistItem.id)
            .where(
                IssuingChecklistItem.issuing_request_id == issuing_id,
                IssuingChecklistItem.is_required.is_(True),
                IssuingChecklistItem.status.not_in(("received", "verified")),
            )
            .limit(1)
        )
        if missing is not None:
            raise HTTPException(status_code=422, detail="Required documents are still missing")
        row.submitted_at = datetime.now(UTC)
    if item.status is IssuingStatus.ISSUED:
        row.issued_at = datetime.now(UTC)
    if item.status is not None:
        row.status = item.status.value
    if item.assigned_staff_id is not None:
        row.assigned_staff_id = item.assigned_staff_id
    if item.insurer_reference is not None:
        row.insurer_reference = item.insurer_reference
    if item.notes is not None:
        row.notes = item.notes
    row.updated_at = datetime.now(UTC)
    await ctx.db.flush()
    await _audit(ctx, "issuing_updated", "issuing_request", issuing_id, {"status": row.status})
    return await repo.issuing_dict(ctx.db, row)


@router.patch("/issuing/items/{item_id}")
async def update_checklist_item(
    item_id: UUID,
    item: ChecklistItemUpdate,
    ctx: RequestContext = ISSUING_MANAGE,
    _: None = Depends(csrf_required),
) -> dict[str, Any]:
    row = (
        await ctx.db.execute(select(IssuingChecklistItem).where(IssuingChecklistItem.id == item_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Checklist item not found")
    row.status = item.status.value
    if item.status.value in ("received", "verified"):
        row.received_at = row.received_at or datetime.now(UTC)
    if item.status.value == "verified":
        row.verified_by = ctx.actor.id
    if item.notes:
        row.notes = item.notes
    row.updated_at = datetime.now(UTC)
    await ctx.db.flush()
    await _audit(
        ctx,
        "issuing_checklist_updated",
        "issuing_request",
        row.issuing_request_id,
        {"item": str(item_id), "status": row.status},
    )
    return {"id": str(item_id), "status": row.status}


# --- Excel import (Phase 1 adoption) ---------------------------------------------


async def _read_upload(file: UploadFile) -> list[dict[str, str]]:
    content = await file.read(_MAX_IMPORT_BYTES + 1)
    if len(content) > _MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="Import file is too large (5 MB max)")
    try:
        return parse_tabular(file.filename or "", content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _finish_import(
    ctx: RequestContext,
    kind: str,
    filename: str,
    corporate_client_id: UUID | None,
    total: int,
    succeeded: int,
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    job = ImportJob(
        id=uuid4(),
        tenant_id=repo.tenant_id,
        kind=kind,
        filename=filename[:200],
        corporate_client_id=corporate_client_id,
        rows_total=total,
        rows_succeeded=succeeded,
        rows_failed=len(errors),
        errors=errors[:200],
        created_by=ctx.actor.id,
    )
    ctx.db.add(job)
    await ctx.db.flush()
    await _audit(
        ctx,
        "excel_import_completed",
        "import_job",
        job.id,
        {
            "kind": kind,
            "rows_total": total,
            "rows_succeeded": succeeded,
            "rows_failed": len(errors),
        },
    )
    return {
        "id": str(job.id),
        "rows_total": total,
        "rows_succeeded": succeeded,
        "rows_failed": len(errors),
        "errors": errors[:200],
    }


@router.post("/imports/employees", status_code=201)
async def import_employees(
    ctx: RequestContext = IMPORTS_MANAGE,
    _: None = Depends(csrf_required),
    file: UploadFile = File(...),
    corporate_client_id: UUID = Form(...),
) -> dict[str, Any]:
    """Import insured employees of a corporate client from Excel/CSV."""
    rows = await _read_upload(file)
    protector = ctx.complaints.protector
    succeeded, errors = 0, []
    for index, row in enumerate(rows, start=2):  # row 1 is the header
        full_name = (row.get("full_name") or "").strip()
        mobile = normalize_mobile(row.get("mobile") or "")
        if len(full_name) < 2 or mobile is None:
            errors.append(
                {"row": index, "error": "full_name and a valid Egyptian mobile are required"}
            )
            continue
        national_id = (row.get("national_id") or "").replace(" ", "")
        ciphertext = blind = last4 = None
        if national_id:
            try:
                validate_national_id(national_id)
            except ValueError:
                errors.append({"row": index, "error": "national_id is invalid"})
                continue
            ciphertext, blind, last4 = (
                protector.encrypt(national_id),
                protector.blind_index(national_id),
                national_id[-4:],
            )
        try:
            async with ctx.db.begin_nested():
                await ctx.db.execute(
                    text(
                        "SELECT app_security.import_insured_employee(:name, :mobile, :email, :code,"
                        " :client, :cipher, :blind, :last4)"
                    ),
                    {
                        "name": full_name,
                        "mobile": mobile,
                        "email": (row.get("email") or None),
                        "code": (row.get("employee_code") or None),
                        "client": corporate_client_id,
                        "cipher": ciphertext,
                        "blind": blind,
                        "last4": last4,
                    },
                )
            succeeded += 1
        except DBAPIError:
            errors.append(
                {"row": index, "error": "row could not be saved (duplicate or invalid reference)"}
            )
    return await _finish_import(
        ctx, "employees", file.filename or "", corporate_client_id, len(rows), succeeded, errors
    )


@router.post("/imports/clients", status_code=201)
async def import_clients(
    ctx: RequestContext = IMPORTS_MANAGE,
    _: None = Depends(csrf_required),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Import existing corporate clients from Excel/CSV."""
    rows = await _read_upload(file)
    succeeded, errors = 0, []
    for index, row in enumerate(rows, start=2):
        name = (row.get("company_name") or row.get("full_name") or "").strip()
        if len(name) < 2:
            errors.append({"row": index, "error": "company_name is required"})
            continue
        slug = slugify_client_name(name, f"{index}")
        try:
            async with ctx.db.begin_nested():
                await ctx.db.execute(
                    text(
                        "SELECT app_security.import_corporate_client("
                        ":slug, :name, :contact, :email, :mobile, :tenant)"
                    ),
                    {
                        "slug": slug,
                        "name": name,
                        "contact": (row.get("contact_name") or None),
                        "email": (row.get("email") or None),
                        "mobile": normalize_mobile(row.get("mobile") or ""),
                        "tenant": repo.tenant_id,
                    },
                )
            succeeded += 1
        except DBAPIError:
            errors.append({"row": index, "error": "row could not be saved (duplicate slug?)"})
    return await _finish_import(
        ctx, "clients", file.filename or "", None, len(rows), succeeded, errors
    )


@router.get("/imports")
async def list_imports(ctx: RequestContext = IMPORTS_MANAGE) -> dict[str, Any]:
    return {"imports": await repo.imports(ctx.db)}


# --- Client folder, caller lookup, insurer performance, smart search --------------


@router.get("/client-folder/{corporate_client_id}")
async def client_folder(
    corporate_client_id: UUID, ctx: RequestContext = CRM_VIEW
) -> dict[str, Any]:
    folder = await repo.client_folder(ctx.db, corporate_client_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return folder


@router.get("/caller-lookup")
async def caller_lookup(
    ctx: RequestContext = CRM_VIEW, mobile: str = Query(min_length=11, max_length=15)
) -> dict[str, Any]:
    """Identify a caller by mobile: employee, his company and his policy."""
    normalized = normalize_mobile(mobile)
    if normalized is None:
        raise HTTPException(status_code=422, detail="A valid Egyptian mobile is required")
    result = await ctx.db.scalar(
        text("SELECT app_security.caller_by_mobile(:mobile)"), {"mobile": normalized}
    )
    return {"mobile": normalized, "caller": result}


@router.get("/insurer-performance")
async def insurer_performance(
    ctx: RequestContext = CRM_VIEW, days: int = Query(default=90, ge=1, le=730)
) -> dict[str, Any]:
    end = datetime.now(UTC)
    return {"insurers": await repo.insurer_performance(ctx.db, end - timedelta(days=days), end)}


@router.get("/document-search")
async def document_search(
    ctx: RequestContext = CRM_VIEW, q: str = Query(min_length=2, max_length=120)
) -> dict[str, Any]:
    return await repo.document_search(ctx.db, q)
