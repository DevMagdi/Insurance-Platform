"""Explicit access scope included in EVERY complaint repository query.

PostgreSQL RLS is an independent second check on the same transaction. Empty
scopes are deliberately false, never interpreted as unrestricted access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, false, true

from app.db import Actor
from app.domain.enums import InsuranceType, OwnershipLevel
from app.models.tickets import Complaint


@dataclass(frozen=True)
class AccessScope:
    actor: Actor
    all_companies: bool = False
    company_ids: frozenset[UUID] = frozenset()
    insurance_types: frozenset[InsuranceType] = frozenset()
    ownership: OwnershipLevel | None = None
    team_ids: frozenset[UUID] = frozenset()

    @classmethod
    def for_customer(cls, actor: Actor) -> AccessScope:
        if actor.scope != "customer":
            raise ValueError("Expected customer actor")
        return cls(actor)

    @classmethod
    def for_staff(cls, actor: Actor, snapshot: dict[str, Any]) -> AccessScope:
        if actor.scope != "staff" or snapshot.get("id") != str(actor.id):
            raise ValueError("Access snapshot does not belong to the authenticated staff member")
        ownership = snapshot.get("ownership")
        return cls(
            actor=actor,
            all_companies=bool(snapshot.get("all_companies")),
            company_ids=frozenset(UUID(s) for s in snapshot.get("company_ids", [])),
            insurance_types=frozenset(
                InsuranceType(s) for s in snapshot.get("insurance_types", [])
            ),
            ownership=OwnershipLevel(ownership) if ownership else None,
            team_ids=frozenset(UUID(s) for s in snapshot.get("team_ids", [])),
        )

    def predicate(self) -> ColumnElement[bool]:
        """Same explicit condition for list, detail, count, search and export."""
        if self.actor.scope == "customer":
            return Complaint.customer_id == self.actor.id
        if not (self.all_companies or self.company_ids) or self.ownership is None:
            return false()
        companies = true() if self.all_companies else Complaint.company_id.in_(self.company_ids)
        kinds = (
            Complaint.insurance_type.in_(self.insurance_types) if self.insurance_types else true()
        )
        if self.ownership == OwnershipLevel.ALL:
            owners: ColumnElement[bool] = true()
        elif self.ownership == OwnershipLevel.OWN:
            owners = Complaint.assigned_agent_id == self.actor.id
        else:
            owners = Complaint.team_id.in_(self.team_ids) if self.team_ids else false()
        return and_(companies, kinds, owners)
