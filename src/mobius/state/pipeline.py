from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from mobius.config import AppConfig
from mobius.logging_setup import get_logger
from mobius.providers.litellm_router import LiteLLMRouter
from mobius.state.checkin_engine import CheckinEngine
from mobius.state.decision_engine import StateDecisionEngine
from mobius.state.journal_engine import JournalEngine
from mobius.state.memory_engine import MemoryEngine
from mobius.state.models import (
    CheckinWrite,
    JournalWrite,
    MemoryWrite,
    StateContextSnapshot,
    StateDecision,
    WriteSummaryItem,
)
from mobius.state.projection_sync import ProjectionSync
from mobius.state.storage import PostgresStore
from mobius.state.store import StateStore


def _request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_user_path(user_key: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", user_key.strip()).strip("-")
    return normalized or "anonymous"


class StatePipeline:
    def __init__(
        self,
        *,
        config: AppConfig,
        state_store: StateStore,
        llm_router: LiteLLMRouter,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.llm_router = llm_router
        self.logger = get_logger(__name__)
        self.enabled = bool(config.state.enabled and state_store.status.ready)

        self.storage = PostgresStore(config.state)
        self.decision_engine = StateDecisionEngine(config=config, llm_router=llm_router)
        self.checkin_engine = CheckinEngine(store=self.storage)
        self.journal_engine = JournalEngine(config=config, store=self.storage)
        self.memory_engine = MemoryEngine(
            config=config,
            store=self.storage,
            llm_router=llm_router,
        )
        self.projection_sync = ProjectionSync(config=config.state, store=self.storage)

    @staticmethod
    def _fallback_user_key(config: AppConfig) -> str:
        return config.state.user_scope.anonymous_user_key

    @classmethod
    def resolve_user_key(cls, request_user: str | None, config: AppConfig) -> str:
        user = str(request_user or "").strip()
        if user:
            return user
        if config.state.user_scope.policy == "fallback_anonymous":
            return cls._fallback_user_key(config)
        # by_user policy still needs a deterministic fallback when OpenAI request.user is absent.
        return cls._fallback_user_key(config)

    def context_for_prompt(self, *, user_key: str | None, routed_domain: str) -> str:
        if not self.enabled:
            return ""
        try:
            normalized_user_key = self.resolve_user_key(user_key, self.config)
            snapshot = self.storage.fetch_context_snapshot(
                user_key=normalized_user_key,
                routed_domain=routed_domain,
            )
            return self._format_context(snapshot)
        except Exception as exc:
            self.logger.warning(
                "State context load failed domain=%s error=%s",
                routed_domain,
                exc.__class__.__name__,
            )
            return ""

    @staticmethod
    def _contains_evidence(*, user_text: str, evidence: str) -> bool:
        user = re.sub(r"\s+", " ", user_text.strip()).lower()
        quote = re.sub(r"\s+", " ", evidence.strip()).lower()
        if not user or not quote:
            return False
        return quote in user

    @staticmethod
    def _looks_ambiguous_memory(text: str) -> bool:
        normalized = text.strip().lower()
        if not normalized:
            return True
        ambiguous_prefixes = (
            "it ",
            "this ",
            "that ",
            "these ",
            "those ",
            "they ",
            "he ",
            "she ",
            "there ",
            "here ",
        )
        return normalized.startswith(ambiguous_prefixes)

    def _apply_grounding_guards(self, *, decision: StateDecision, user_text: str) -> StateDecision:
        strict_grounding = bool(self.config.state.decision.strict_grounding)
        facts_only = bool(self.config.state.decision.facts_only)
        if not strict_grounding and not facts_only:
            return decision

        reason_parts: list[str] = []
        checkin = decision.checkin
        journal = decision.journal
        memory = decision.memory

        if checkin is not None:
            evidence = checkin.evidence.strip()
            if strict_grounding and not self._contains_evidence(user_text=user_text, evidence=evidence):
                checkin = None
                reason_parts.append("check-in-filtered-missing-evidence")
            elif facts_only:
                checkin = CheckinWrite(
                    domain=checkin.domain,
                    track_type=checkin.track_type,
                    title=checkin.title,
                    summary=evidence or checkin.summary,
                    outcome=checkin.outcome,
                    confidence=checkin.confidence,
                    wins=[
                        item
                        for item in checkin.wins
                        if self._contains_evidence(user_text=user_text, evidence=item)
                    ],
                    barriers=[
                        item
                        for item in checkin.barriers
                        if self._contains_evidence(user_text=user_text, evidence=item)
                    ],
                    next_actions=[
                        item
                        for item in checkin.next_actions
                        if self._contains_evidence(user_text=user_text, evidence=item)
                    ],
                    tags=[],
                    evidence=evidence,
                )

        if journal is not None:
            evidence = journal.evidence.strip()
            if strict_grounding and not self._contains_evidence(user_text=user_text, evidence=evidence):
                journal = None
                reason_parts.append("journal-filtered-missing-evidence")
            elif facts_only:
                journal = JournalWrite(
                    entry_ts=journal.entry_ts,
                    title=journal.title,
                    body_md=user_text.strip(),
                    domain_hints=journal.domain_hints,
                    evidence=evidence,
                )

        if memory is not None:
            evidence = memory.evidence.strip()
            if strict_grounding and not self._contains_evidence(user_text=user_text, evidence=evidence):
                memory = None
                reason_parts.append("memory-filtered-missing-evidence")
            else:
                memory_text = evidence or memory.memory
                if self._looks_ambiguous_memory(memory_text):
                    memory = None
                    reason_parts.append("memory-filtered-ambiguous")
                elif facts_only:
                    memory = MemoryWrite(
                        domain=memory.domain,
                        memory=memory_text.strip(),
                        evidence=evidence,
                    )

        reason = decision.reason.strip()
        if reason_parts:
            suffix = ",".join(reason_parts)
            reason = f"{reason}|{suffix}" if reason else suffix
        return StateDecision(
            checkin=checkin,
            journal=journal,
            memory=memory,
            reason=reason,
            source_model=decision.source_model,
            is_failure=decision.is_failure,
        )

    async def process_turn(
        self,
        *,
        request_user: str | None,
        session_key: str | None,
        routed_domain: str,
        user_text: str,
        assistant_text: str,
        used_model: str | None,
        request_payload: dict[str, Any],
    ) -> str:
        if not self.enabled:
            return ""

        normalized_user_key = self.resolve_user_key(request_user, self.config)
        request_hash = _request_hash(request_payload)
        try:
            snapshot = self.storage.fetch_context_snapshot(
                user_key=normalized_user_key,
                routed_domain=routed_domain,
            )
            decision = await self.decision_engine.decide(
                user_text=user_text,
                assistant_text=assistant_text,
                routed_domain=routed_domain,
                context=snapshot,
            )
            decision = self._apply_grounding_guards(decision=decision, user_text=user_text)
            if not decision.has_writes():
                return self._decision_failure_footer(
                    decision=decision,
                    user_key=normalized_user_key,
                )

            user_id, turn_id = self.storage.upsert_turn_event(
                user_key=normalized_user_key,
                session_key=session_key,
                request_hash=request_hash,
                domain=routed_domain,
                user_text=user_text,
                assistant_text=assistant_text,
            )

            summary_items: list[WriteSummaryItem] = []
            if decision.checkin is not None:
                summary_items.append(
                    self.checkin_engine.apply(
                        user_id=user_id,
                        turn_id=turn_id,
                        payload=decision.checkin,
                        idempotency_key=f"{request_hash}:checkin",
                        source_model=decision.source_model or used_model,
                    )
                )
            if decision.journal is not None:
                summary_items.append(
                    self.journal_engine.apply(
                        user_id=user_id,
                        turn_id=turn_id,
                        payload=decision.journal,
                        idempotency_key=f"{request_hash}:journal",
                        source_model=decision.source_model or used_model,
                        user_text=user_text,
                        assistant_text=assistant_text,
                    )
                )
            if decision.memory is not None:
                summary_items.append(
                    await self.memory_engine.apply(
                        user_id=user_id,
                        turn_id=turn_id,
                        payload=decision.memory,
                        idempotency_key=f"{request_hash}:memory",
                        source_excerpt=user_text,
                    )
                )

            if any(item.status == "applied" for item in summary_items):
                summary_items.extend(
                    self.projection_sync.export_user(
                        user_id=user_id,
                        user_key=normalized_user_key,
                    )
                )
            return self._format_footer(summary_items, user_key=normalized_user_key)
        except Exception as exc:
            self.logger.warning(
                "State pipeline failed domain=%s error=%s",
                routed_domain,
                exc.__class__.__name__,
            )
            self.logger.debug("State pipeline details: %s", exc)
            return ""

    def _format_context(self, snapshot: StateContextSnapshot) -> str:
        lines: list[str] = []
        if snapshot.active_tracks:
            lines.append("Active tracks:")
            for row in snapshot.active_tracks:
                lines.append(
                    f"- {row.get('title')} [{row.get('domain')}] status={row.get('status')} "
                    f"last_checkin={row.get('last_checkin_at')}"
                )
        if snapshot.recent_checkins:
            lines.append("Recent check-ins:")
            for row in snapshot.recent_checkins:
                lines.append(
                    f"- {row.get('track_slug')}: {row.get('summary')} ({row.get('timestamp')})"
                )
        if snapshot.recent_journal_entries:
            lines.append("Recent journal entries:")
            for row in snapshot.recent_journal_entries:
                lines.append(f"- {row.get('entry_date')}: {row.get('title')}")
        if snapshot.recent_memory_cards:
            lines.append("Recent memories:")
            for row in snapshot.recent_memory_cards:
                lines.append(
                    f"- {row.get('domain')}/{row.get('slug')} "
                    f"(occurrences={row.get('occurrences')})"
                )
        return "\n".join(lines).strip()

    def _decision_failure_footer(self, *, decision: StateDecision, user_key: str) -> str:
        if not decision.is_failure:
            return ""
        if self.config.state.decision.on_failure != "footer_warning":
            return ""
        safe_user = _safe_user_path(user_key)
        reason = decision.reason or "state-decision-failure"
        return "\n".join(
            [
                "*State warning:*",
                f"- decision engine failed for this turn (`{reason}`), so state writes were skipped.",
                f"- state path scope: `state/users/{safe_user}/`",
            ]
        )

    @staticmethod
    def _format_footer(summary_items: list[WriteSummaryItem], *, user_key: str) -> str:
        if not summary_items:
            return ""
        safe_user = _safe_user_path(user_key)
        lines = ["*State writes:*"]
        for item in summary_items:
            if item.channel == "projection":
                target = item.target
            else:
                target = f"state/users/{safe_user}/{item.target}"
            details = f" - {item.details}" if item.details else ""
            channel_label = "check-in" if item.channel == "checkin" else item.channel
            lines.append(f"- {channel_label}: `{target}` ({item.status}){details}")
        return "\n".join(lines)
