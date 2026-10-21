"""Complaint queries always include AccessScope AND transaction-local PostgreSQL RLS."""

from __future__ import annotations

import builtins
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access import AccessScope
from app.domain.enums import ComplaintStatus, InsuranceType, MessageDirection, MessageVisibility
from app.domain.schemas import ComplaintCreate, ComplaintFilter
from app.models.core import ComplaintIdempotency
from app.models.identity import Customer, InsuranceCompany
from app.models.tickets import (
    Attachment,
    AuditLog,
    Complaint,
    ComplaintEvent,
    ComplaintKey,
    ComplaintMessage,
    Notification,
)


def _escape_like(value: str) -> str:
    """Neutralise LIKE wildcards so a typed ``%`` cannot widen the filter."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ComplaintRepository:
    def base_query(self, scope: AccessScope, filters: ComplaintFilter) -> Select[tuple[Complaint]]:
        """Single filter builder reused by list, count, counters, search and export."""
        stmt = select(Complaint).where(scope.predicate())
        if filters.status is not None:
            stmt = stmt.where(Complaint.status == filters.status)
        if filters.company_id is not None:
            stmt = stmt.where(Complaint.company_id == filters.company_id)
        if filters.insurance_type is not None:
            stmt = stmt.where(Complaint.insurance_type == filters.insurance_type)
        if filters.assigned_agent_id is not None:
            stmt = stmt.where(Complaint.assigned_agent_id == filters.assigned_agent_id)
        if filters.priority is not None:
            stmt = stmt.where(Complaint.priority == filters.priority)
        if filters.category_id is not None:
            stmt = stmt.where(Complaint.category_id == filters.category_id)
        if filters.channel is not None:
            stmt = stmt.where(Complaint.channel == filters.channel)
        if filters.date_from is not None:
            stmt = stmt.where(Complaint.created_at >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(Complaint.created_at <= filters.date_to)
        if filters.ticket_no is not None:
            stmt = stmt.where(Complaint.ticket_no == filters.ticket_no)
        if filters.card_number is not None:
            stmt = stmt.where(Complaint.card_number.ilike(f"%{_escape_like(filters.card_number)}%"))
        if filters.policy_number is not None:
            stmt = stmt.where(
                Complaint.policy_number.ilike(f"%{_escape_like(filters.policy_number)}%")
            )
        if filters.intake_source is not None:
            stmt = stmt.where(Complaint.intake_source == filters.intake_source.value)
        if filters.corporate_client_id is not None:
            stmt = stmt.where(Complaint.corporate_client_id == filters.corporate_client_id)
        # Mobile and name live on the customer, so they become a correlated
        # EXISTS rather than a join: the outer query keeps returning exactly one
        # Complaint row per ticket and the RLS predicate above is untouched.
        if filters.mobile is not None or filters.customer_name is not None:
            conditions = [Customer.id == Complaint.customer_id]
            if filters.mobile is not None:
                # The column is stored as +201XXXXXXXXX. Matching a digit
                # suffix lets an agent type the last four digits, the local
                # 010… form, or the full international number.
                conditions.append(Customer.mobile.like(f"%{_escape_like(filters.mobile)}"))
            if filters.customer_name is not None:
                conditions.append(
                    Customer.full_name.ilike(f"%{_escape_like(filters.customer_name)}%")
                )
            stmt = stmt.where(select(1).where(*conditions).exists())
        if filters.search is not None:
            stmt = stmt.where(
                Complaint.search_vector.op("@@")(func.plainto_tsquery("simple", filters.search))
            )
        return stmt

    async def list(
        self,
        db: AsyncSession,
        scope: AccessScope,
        filters: ComplaintFilter,
        *,
        export_limit: int | None = None,
    ) -> list[Complaint]:
        column = getattr(Complaint, filters.sort_by)  # Literal allowlist in ComplaintFilter.
        primary = column.asc() if filters.sort_dir == "asc" else column.desc()
        stmt = self.base_query(scope, filters).order_by(
            primary, Complaint.created_at.desc(), Complaint.id.desc()
        )
        if export_limit is None:
            stmt = stmt.offset((filters.page - 1) * filters.page_size).limit(filters.page_size)
        else:
            stmt = stmt.limit(export_limit)
        return list((await db.scalars(stmt)).all())

    async def filter_options(
        self, db: AsyncSession, scope: AccessScope
    ) -> builtins.list[dict[str, Any]]:
        if scope.actor.scope != "staff" or not (scope.all_companies or scope.company_ids):
            return []
        stmt = select(
            InsuranceCompany.id,
            InsuranceCompany.name_en,
            InsuranceCompany.name_ar,
            InsuranceCompany.supported_types,
        ).where(InsuranceCompany.is_active.is_(True))
        if not scope.all_companies:
            stmt = stmt.where(InsuranceCompany.id.in_(scope.company_ids))
        rows = await db.execute(stmt.order_by(InsuranceCompany.name_en))
        return [
            {
                "id": row.id,
                "name_en": row.name_en,
                "name_ar": row.name_ar,
                "insurance_types": [
                    kind
                    for kind in row.supported_types
                    if not scope.insurance_types or kind in scope.insurance_types
                ],
            }
            for row in rows
        ]

    async def agent_options(self, db: AsyncSession) -> builtins.list[dict[str, Any]]:
        """Return only agents attached to complaints visible under the current DB scope."""
        rows = await db.execute(
            text("SELECT id, full_name FROM app_security.visible_complaint_agents()")
        )
        return [dict(row._mapping) for row in rows]

    async def count(self, db: AsyncSession, scope: AccessScope, filters: ComplaintFilter) -> int:
        subquery = self.base_query(scope, filters).with_only_columns(Complaint.id).subquery()
        result = await db.scalar(select(func.count()).select_from(subquery))
        return int(result or 0)

    async def counters(
        self, db: AsyncSession, scope: AccessScope, filters: ComplaintFilter
    ) -> dict[str, int]:
        # Status is excluded so the breakdown still shows all matching statuses.
        without_status = filters.model_copy(update={"status": None})
        filtered = (
            self.base_query(scope, without_status).with_only_columns(Complaint.status).subquery()
        )
        result = await db.execute(
            select(filtered.c.status, func.count()).group_by(filtered.c.status)
        )
        counts = {status.value: 0 for status in ComplaintStatus}
        for status, count in result:
            counts[str(status)] = int(count)
        counts["total"] = sum(counts.values())
        counts["closed"] = counts[ComplaintStatus.RESOLVED.value]
        counts["open"] = counts["total"] - counts["closed"]
        return counts

    async def get(
        self, db: AsyncSession, scope: AccessScope, complaint_id: UUID
    ) -> Complaint | None:
        return (
            await db.scalars(
                select(Complaint).where(scope.predicate(), Complaint.id == complaint_id)
            )
        ).one_or_none()

    async def sla_state(
        self, db: AsyncSession, scope: AccessScope, complaint_id: UUID
    ) -> dict[str, Any] | None:
        """Deadlines and clock state for one visible ticket (never computed in SQL)."""
        row = (
            await db.execute(
                select(
                    Complaint.id,
                    Complaint.ticket_no,
                    Complaint.status,
                    Complaint.created_at,
                    Complaint.response_due_at,
                    Complaint.sla_due_at,
                    Complaint.sla_alert_at,
                    Complaint.sla_alerted_at,
                    Complaint.first_response_at,
                    Complaint.response_breached_at,
                    Complaint.sla_breached_at,
                    Complaint.resolved_at,
                ).where(scope.predicate(), Complaint.id == complaint_id)
            )
        ).one_or_none()
        if row is None:
            return None
        record = dict(row._mapping)
        now = datetime.now(UTC)
        earliest = record["sla_due_at"] or record["response_due_at"]
        if record["response_breached_at"] or record["sla_breached_at"]:
            state = "breached"
        elif earliest is None or record["resolved_at"] is not None or now <= earliest:
            state = "on_track"
        else:
            state = "due"
        if (
            state == "on_track"
            and earliest is not None
            and (
                (record["sla_alert_at"] is not None and now >= record["sla_alert_at"])
                or (earliest - now) <= timedelta(minutes=60)
            )
        ):
            state = "at_risk"
        return {
            **record,
            "state": state,
            "remaining_minutes": (
                max(int((earliest - now).total_seconds() // 60), 0) if earliest else None
            ),
            "responded": record["first_response_at"] is not None,
        }

    async def detail(
        self, db: AsyncSession, scope: AccessScope, complaint_id: UUID
    ) -> tuple[Complaint, str, str | None, str] | None:
        row = (
            await db.execute(
                select(
                    Complaint,
                    Customer.full_name,
                    Customer.national_id_last4,
                    InsuranceCompany.name_en,
                )
                .join(Customer, Complaint.customer_id == Customer.id)
                .join(InsuranceCompany, Complaint.company_id == InsuranceCompany.id)
                .where(scope.predicate(), Complaint.id == complaint_id)
            )
        ).one_or_none()
        return (row[0], row[1], row[2], row[3]) if row else None

    async def timeline(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint: Complaint,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        # Both child queries join a scoped complaint, not only its guessed UUID.
        predicates = (
            scope.predicate(),
            Complaint.id == complaint.id,
            Complaint.created_at == complaint.created_at,
        )
        events = (
            await db.scalars(
                select(ComplaintEvent)
                .join(
                    Complaint,
                    (Complaint.id == ComplaintEvent.complaint_id)
                    & (Complaint.created_at == ComplaintEvent.complaint_created_at),
                )
                .where(*predicates)
                .order_by(ComplaintEvent.occurred_at, ComplaintEvent.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        messages = (
            await db.scalars(
                select(ComplaintMessage)
                .join(
                    Complaint,
                    (Complaint.id == ComplaintMessage.complaint_id)
                    & (Complaint.created_at == ComplaintMessage.complaint_created_at),
                )
                .where(*predicates)
                .order_by(ComplaintMessage.created_at, ComplaintMessage.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return {
            "events": [
                {
                    "id": e.id,
                    "event_type": e.event_type,
                    "from_status": e.from_status,
                    "to_status": e.to_status,
                    "occurred_at": e.occurred_at,
                    "metadata": e.event_metadata,
                }
                for e in events
            ],
            "messages": [
                {
                    "id": m.id,
                    "body": m.body,
                    "visibility": m.visibility,
                    "direction": m.direction,
                    "channel": m.channel,
                    "created_at": m.created_at,
                }
                for m in messages
            ],
            "page": page,
        }

    async def assigned_agent_name(self, db: AsyncSession, complaint: Complaint) -> str | None:
        """Display name only, via a definer that re-checks complaint scope."""
        if complaint.assigned_agent_id is None:
            return None
        result: str | None = await db.scalar(
            text("SELECT app_security.assigned_agent_display(:id, :created)"),
            {"id": complaint.id, "created": complaint.created_at},
        )
        return result

    async def active_company(
        self,
        db: AsyncSession,
        company_id: UUID,
        insurance_type: InsuranceType,
    ) -> bool:
        return bool(
            await db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM insurance_companies WHERE id = :id "
                    "AND is_active AND CAST(:kind AS insurance_type) = ANY(supported_types))"
                ),
                {"id": company_id, "kind": insurance_type.value},
            )
        )

    async def corporate_client_for_customer(
        self, db: AsyncSession, customer_id: UUID
    ) -> UUID | None:
        """The employer covering this member, when they are on a group policy."""
        return await db.scalar(
            select(Customer.corporate_client_id).where(Customer.id == customer_id)
        )

    async def identity_matches(self, db: AsyncSession, mobile: str, blind: bytes | None) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.verify_customer_identity(:mobile, :blind)"),
                {"mobile": mobile, "blind": blind},
            )
        )

    async def policy_for_customer(
        self,
        db: AsyncSession,
        customer_id: UUID,
        item: ComplaintCreate,
    ) -> UUID | None:
        """Resolve the policy row, when the request carries a policy number.

        A card-only request has no policy to attach: the brokerage looks the
        member up by card at the insurer. Returning ``None`` is correct, not an
        error, and the composite foreign key tolerates it (MATCH SIMPLE).
        """
        if not item.policy_number:
            return None
        # The helper binds ownership to the authenticated transaction actor, not an input ID.
        result: UUID | None = await db.scalar(
            text("SELECT app_security.ensure_customer_policy(:company, :kind, :number)"),
            {
                "company": item.company_id,
                "kind": item.insurance_type.value,
                "number": item.policy_number,
            },
        )
        if result is None:
            raise HTTPException(status_code=409, detail="Policy unavailable for this customer")
        return result

    async def reserve_idempotency(
        self,
        db: AsyncSession,
        customer_id: UUID,
        key_digest: bytes,
        request_digest: bytes,
    ) -> ComplaintIdempotency:
        await db.execute(
            insert(ComplaintIdempotency)
            .values(
                customer_id=customer_id,
                key_digest=key_digest,
                request_digest=request_digest,
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
            .on_conflict_do_nothing(index_elements=["customer_id", "key_digest"])
        )
        value = await db.scalar(
            select(ComplaintIdempotency)
            .where(
                ComplaintIdempotency.customer_id == customer_id,
                ComplaintIdempotency.key_digest == key_digest,
            )
            .with_for_update()
        )
        assert value is not None
        return value

    async def create(
        self,
        db: AsyncSession,
        *,
        customer_id: UUID,
        item: ComplaintCreate,
        policy_id: UUID | None,
        idempotency: ComplaintIdempotency,
        corporate_client_id: UUID | None = None,
    ) -> Complaint:
        now = datetime.now(UTC)
        ticket = await db.scalar(
            text("SELECT app_security.issue_ticket_number(:created)"), {"created": now}
        )
        assert isinstance(ticket, str)
        complaint_id = uuid4()
        db.add(ComplaintKey(id=complaint_id, created_at=now, ticket_no=ticket))
        await db.flush()
        record = Complaint(
            id=complaint_id,
            created_at=now,
            ticket_no=ticket,
            customer_id=customer_id,
            company_id=item.company_id,
            insurance_type=item.insurance_type,
            policy_id=policy_id,
            policy_number=item.policy_number,
            card_number=item.card_number,
            intake_source="portal",
            corporate_client_id=corporate_client_id,
            subject=item.subject,
            description=item.description,
        )
        db.add(record)
        await db.flush()
        db.add(
            ComplaintEvent(
                complaint_id=record.id,
                complaint_created_at=record.created_at,
                event_type="created",
                to_status=ComplaintStatus.NEW,
                actor_customer_id=customer_id,
            )
        )
        idempotency.complaint_id = record.id
        idempotency.complaint_created_at = record.created_at
        idempotency.ticket_no = record.ticket_no
        await db.execute(
            text("SELECT app_security.audit_customer_action(:id, :created, 'complaint_created')"),
            {"id": record.id, "created": record.created_at},
        )
        await self.notify(db, record, "created")
        # The same customer action fans out to scoped staff so the console
        # bell lights up without anyone refreshing a page.
        await self.notify_staff(db, record, "created")
        await db.flush()
        return record

    async def notify(self, db: AsyncSession, complaint: Complaint, kind: str) -> UUID:
        result = await db.scalar(
            text("SELECT app_security.queue_customer_notice(:id, :created, :kind)"),
            {"id": complaint.id, "created": complaint.created_at, "kind": kind},
        )
        assert isinstance(result, UUID)
        return result

    async def notify_staff(self, db: AsyncSession, complaint: Complaint, kind: str) -> int:
        """Fan a customer-visible event out to staff whose scope covers it.

        The SQL definer re-checks the actor, the kind and every candidate's
        permission/scope, so this is a no-op for staff or guest actors.
        """
        result = await db.scalar(
            text("SELECT app_security.queue_staff_notice(:id, :created, :kind)"),
            {"id": complaint.id, "created": complaint.created_at, "kind": kind},
        )
        return int(result or 0)

    async def reply(
        self,
        db: AsyncSession,
        scope: AccessScope,
        complaint: Complaint,
        body: str,
        visibility: MessageVisibility = MessageVisibility.PUBLIC,
    ) -> ComplaintMessage:
        actor = scope.actor
        record = ComplaintMessage(
            complaint_id=complaint.id,
            complaint_created_at=complaint.created_at,
            body=body,
            visibility=visibility,
            direction=MessageDirection.INBOUND
            if actor.scope == "customer"
            else MessageDirection.OUTBOUND,
            sender_customer_id=actor.id if actor.scope == "customer" else None,
            sender_staff_id=actor.id if actor.scope == "staff" else None,
        )
        db.add(record)
        await db.flush()
        if actor.scope == "customer":
            await db.execute(
                text("SELECT app_security.audit_customer_action(:id, :created, 'reply')"),
                {"id": complaint.id, "created": complaint.created_at},
            )
            # A customer message is exactly what the staff inbox must show
            # immediately; the definer ignores non-customer actors itself.
            await self.notify_staff(db, complaint, "reply")
        else:
            db.add(
                AuditLog(
                    actor_staff_id=actor.id,
                    action="reply",
                    resource_type="complaint",
                    resource_id=complaint.id,
                    complaint_id=complaint.id,
                    complaint_created_at=complaint.created_at,
                    after_state={"visibility": visibility.value},
                )
            )
            if visibility == MessageVisibility.PUBLIC:
                await self.notify(db, complaint, "reply")
        return record

    async def reopen(self, db: AsyncSession, complaint: Complaint) -> bool:
        return bool(
            await db.scalar(
                text("SELECT app_security.reopen_complaint(:id, :created)"),
                {"id": complaint.id, "created": complaint.created_at},
            )
        )

    async def csat(
        self,
        db: AsyncSession,
        complaint: Complaint,
        score: int,
        comment: str | None,
    ) -> bool:
        return bool(
            await db.scalar(
                text(
                    "SELECT app_security.submit_csat("
                    ":id, :created, CAST(:score AS smallint), :comment)"
                ),
                {
                    "id": complaint.id,
                    "created": complaint.created_at,
                    "score": score,
                    "comment": comment,
                },
            )
        )

    async def change_status(
        self,
        db: AsyncSession,
        complaint: Complaint,
        status: ComplaintStatus,
    ) -> bool:
        return bool(
            await db.scalar(
                text(
                    "SELECT app_security.staff_change_status("
                    ":id, :created, CAST(:status AS complaint_status))"
                ),
                {"id": complaint.id, "created": complaint.created_at, "status": status.value},
            )
        )

    async def reveal_national_id(self, db: AsyncSession, customer_id: UUID) -> bytes | None:
        result: bytes | None = await db.scalar(
            text("SELECT app_security.read_national_id_ciphertext(:id)"), {"id": customer_id}
        )
        return result

    async def reveal_customer_national_id(
        self, db: AsyncSession, customer_id: UUID
    ) -> bytes | None:
        """Ciphertext for the admin full-profile reveal (global scope gated in SQL)."""
        result: bytes | None = await db.scalar(
            text("SELECT app_security.read_customer_profile_national_id_ciphertext(:id)"),
            {"id": customer_id},
        )
        return result

    async def audit_customer_reveal(
        self,
        db: AsyncSession,
        *,
        staff_id: UUID,
        customer_id: UUID,
        reason: str,
    ) -> None:
        db.add(
            AuditLog(
                actor_staff_id=staff_id,
                action="national_id_revealed",
                resource_type="customer",
                resource_id=customer_id,
                after_state={"reason": reason, "surface": "customer_profile"},
            )
        )
        await db.flush()

    async def audit_reveal(
        self,
        db: AsyncSession,
        *,
        staff_id: UUID,
        complaint: Complaint,
        reason: str,
    ) -> None:
        db.add(
            AuditLog(
                actor_staff_id=staff_id,
                action="national_id_revealed",
                resource_type="customer",
                resource_id=complaint.customer_id,
                complaint_id=complaint.id,
                complaint_created_at=complaint.created_at,
                after_state={"reason": reason},
            )
        )
        await db.flush()

    async def guest_track(
        self,
        db: AsyncSession,
        ticket: str,
        blind: bytes,
    ) -> dict[str, Any] | None:
        row = (
            await db.execute(
                text("SELECT * FROM app_security.guest_ticket_status(:ticket, :blind)"),
                {"ticket": ticket, "blind": blind},
            )
        ).one_or_none()
        return dict(row._mapping) if row else None

    async def notifications(
        self,
        db: AsyncSession,
        scope: AccessScope,
        limit: int = 30,
    ) -> builtins.list[Notification]:
        recipient = (
            Notification.recipient_customer_id == scope.actor.id
            if scope.actor.scope == "customer"
            else Notification.recipient_staff_id == scope.actor.id
        )
        return list(
            (
                await db.scalars(
                    select(Notification)
                    .outerjoin(
                        Complaint,
                        (Complaint.id == Notification.complaint_id)
                        & (Complaint.created_at == Notification.complaint_created_at),
                    )
                    .where(
                        recipient,
                        or_(Notification.complaint_id.is_(None), scope.predicate()),
                    )
                    .order_by(Notification.created_at.desc())
                    .limit(limit)
                )
            ).all()
        )

    async def attachments(
        self, db: AsyncSession, scope: AccessScope, complaint: Complaint
    ) -> builtins.list[Attachment]:
        return list(
            (
                await db.scalars(
                    select(Attachment)
                    .join(
                        Complaint,
                        (Complaint.id == Attachment.complaint_id)
                        & (Complaint.created_at == Attachment.complaint_created_at),
                    )
                    .where(
                        scope.predicate(),
                        Complaint.id == complaint.id,
                        Complaint.created_at == complaint.created_at,
                    )
                    .order_by(Attachment.created_at.desc())
                    .limit(100)
                )
            ).all()
        )
