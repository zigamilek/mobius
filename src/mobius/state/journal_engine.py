from __future__ import annotations

from datetime import datetime

from mobius.config import AppConfig
from mobius.logging_setup import get_logger
from mobius.state.models import JournalWrite, WriteSummaryItem
from mobius.state.storage import PostgresStore


def _format_journal_entry(
    *,
    entry_ts: datetime,
    title: str,
    user_text: str,
    generated_body: str,
    assistant_excerpt: str,
) -> str:
    timestamp = entry_ts.isoformat()
    lines = [
        f"## {timestamp} - {title}",
        "",
        generated_body.strip() or user_text.strip(),
    ]
    if assistant_excerpt.strip():
        lines.extend(
            [
                "",
                "### Coaching context",
                assistant_excerpt.strip(),
            ]
        )
    return "\n".join(lines).strip()


class JournalEngine:
    def __init__(self, *, config: AppConfig, store: PostgresStore) -> None:
        self.config = config
        self.store = store
        self.logger = get_logger(__name__)

    def apply(
        self,
        *,
        user_id: str,
        turn_id: str,
        payload: JournalWrite,
        idempotency_key: str,
        source_model: str | None,
        user_text: str,
        assistant_text: str,
    ) -> WriteSummaryItem:
        facts_only = bool(self.config.state.decision.facts_only)
        journal_body = user_text.strip() if facts_only else payload.body_md
        include_assistant_excerpt = (
            self.config.state.journal.include_assistant_excerpt and not facts_only
        )
        body = _format_journal_entry(
            entry_ts=payload.entry_ts,
            title=payload.title,
            user_text=user_text,
            generated_body=journal_body,
            assistant_excerpt=assistant_text[: self.config.state.journal.max_assistant_excerpt_chars]
            if include_assistant_excerpt
            else "",
        )
        normalized = JournalWrite(
            entry_ts=payload.entry_ts,
            title=payload.title,
            body_md=body,
            domain_hints=payload.domain_hints,
        )
        item = self.store.write_journal(
            user_id=user_id,
            turn_id=turn_id,
            payload=normalized,
            idempotency_key=idempotency_key,
            source_model=source_model,
        )
        self.logger.debug(
            "Journal write result status=%s target=%s details=%s",
            item.status,
            item.target,
            item.details,
        )
        return item
