"""Typed ORM mappings mirror immutable SQL-first migrations (not autogenerate DDL)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[type[Any], Any]] = {
        UUID: PGUUID(as_uuid=True),
        datetime: DateTime(timezone=True),
    }
