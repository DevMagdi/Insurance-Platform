"""PDPL (Egypt Law 151/2020) consent and data-subject request contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ConsentPurpose(StrEnum):
    complaint_processing = "complaint_processing"
    notifications = "notifications"
    voice_followup = "voice_followup"
    ai_processing = "ai_processing"
    marketing = "marketing"


class PrivacyRequestKind(StrEnum):
    access = "access"
    erasure = "erasure"
    rectification = "rectification"
    objection = "objection"


class PrivacyDecision(StrEnum):
    in_progress = "in_progress"
    completed = "completed"
    rejected = "rejected"


class ConsentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: ConsentPurpose
    granted: bool


class PrivacyRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: PrivacyRequestKind
    details: str | None = Field(default=None, max_length=2000)


class PrivacyRequestDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PrivacyDecision
    resolution: str | None = Field(default=None, max_length=2000)
