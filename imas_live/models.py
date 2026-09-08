"""Small, explicit data types used by parsing, persistence and commands."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Evidence:
    url: str
    excerpt: str
    parser: str
    quality: str = "verified"


@dataclass(slots=True)
class TicketRound:
    stable_key: str
    name: str
    ticket_scope: str
    sale_method: str
    application_start: str | None = None
    application_end: str | None = None
    result_at: str | None = None
    payment_start: str | None = None
    payment_end: str | None = None
    url: str | None = None
    seats: str | None = None
    eligibility: str | None = None
    evidence: Evidence | None = None

    def record(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence"] = asdict(self.evidence) if self.evidence else None
        return value


@dataclass(slots=True)
class Performance:
    stable_key: str
    date: str | None
    session_label: str | None
    venue: str | None
    status: str = "announced"
    precision: str = "date_only"
    evidence: Evidence | None = None


@dataclass(slots=True)
class CastAppearance:
    name: str
    role: str | None
    performance_key: str | None
    status: str = "announced"
    evidence: Evidence | None = None


@dataclass(slots=True)
class ParsedPage:
    ticket_rounds: list[TicketRound] = field(default_factory=list)
    performances: list[Performance] = field(default_factory=list)
    cast: list[CastAppearance] = field(default_factory=list)
    # Only actual roster images found inside an official CAST/出演者 section.
    cast_asset_urls: list[str] = field(default_factory=list)
    review_notes: list[str] = field(default_factory=list)
