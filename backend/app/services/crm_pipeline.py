"""Pure pipeline rules for the unified CRM module (`/api/crm`).

Default stage set (seeded by migration 039, carried into
``crm_pipeline_stages`` by migration 041; tenants customise at runtime),
terminal-stage semantics that drive ``closed_at``/conversion metrics, and
the client-slug helper used by one-click Lead → corporate client
conversion. Database-free so it is unit-testable offline.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Spec pipeline, in order. "won"/"lost" are the two closing outcomes and
# "converted" follows a win when the lead becomes a corporate client.
DEFAULT_STAGES: tuple[tuple[str, str, str], ...] = (
    ("new", "جديد", "New"),
    ("contacted", "تم التواصل", "Contacted"),
    ("meeting", "في ميتنج", "Meeting"),
    ("needs_info", "يحتاج معلومات / عرض سعر", "Needs info / quotation"),
    ("quote_sent", "عرض مرسل", "Quote sent"),
    ("negotiation", "تفاوض", "Negotiation"),
    ("won", "ربح", "Won"),
    ("lost", "خسارة", "Lost"),
    ("converted", "محوّل لعميل", "Converted to client"),
)

DEFAULT_STAGE_CODE = "new"


@dataclass(frozen=True)
class StageFlags:
    """The three mutually exclusive terminal flags a stage may carry."""

    is_won: bool = False
    is_lost: bool = False
    is_converted: bool = False

    @property
    def is_terminal(self) -> bool:
        """Won/lost/converted close the lead (``closed_at`` is stamped)."""
        return self.is_won or self.is_lost or self.is_converted

    @property
    def counts_as_won(self) -> bool:
        """Converted clients were won first; both feed win metrics."""
        return self.is_won or self.is_converted


def slugify_client_name(name: str, fallback: str) -> str:
    """ASCII slug for a converted corporate client (Arabic → fallback)."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:60]
    return slug or f"client-{fallback[:12]}"
