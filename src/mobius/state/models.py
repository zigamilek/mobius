from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class CheckinWrite:
    domain: str
    track_type: str
    title: str
    summary: str
    outcome: str = "note"
    confidence: float | None = None
    wins: list[str] = field(default_factory=list)
    barriers: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    evidence: str = ""


@dataclass(frozen=True)
class JournalWrite:
    entry_ts: datetime
    title: str
    body_md: str
    domain_hints: list[str] = field(default_factory=list)
    evidence: str = ""


@dataclass(frozen=True)
class MemoryWrite:
    domain: str
    memory: str
    evidence: str = ""


@dataclass(frozen=True)
class StateDecision:
    checkin: CheckinWrite | None = None
    journal: JournalWrite | None = None
    memory: MemoryWrite | None = None
    reason: str = ""
    source_model: str | None = None
    is_failure: bool = False

    def has_writes(self) -> bool:
        return bool(self.checkin or self.journal or self.memory)


@dataclass(frozen=True)
class WriteSummaryItem:
    channel: str
    status: str
    target: str
    details: str = ""
    result_ref: str | None = None


@dataclass(frozen=True)
class StateContextSnapshot:
    active_tracks: list[dict[str, object]] = field(default_factory=list)
    recent_checkins: list[dict[str, object]] = field(default_factory=list)
    recent_journal_entries: list[dict[str, object]] = field(default_factory=list)
    recent_memory_cards: list[dict[str, object]] = field(default_factory=list)
