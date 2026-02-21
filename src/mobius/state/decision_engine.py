from __future__ import annotations

import json
import re
from typing import Any

from mobius.config import AppConfig
from mobius.logging_setup import get_logger
from mobius.providers.litellm_router import LiteLLMRouter
from mobius.state.models import (
    CheckinWrite,
    MemoryWrite,
    StateDecision,
    StateContextSnapshot,
)

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def _response_to_dict(chunk: Any) -> dict[str, Any]:
    if isinstance(chunk, dict):
        return chunk
    if hasattr(chunk, "model_dump"):
        return chunk.model_dump(exclude_none=True)  # type: ignore[no-any-return]
    if hasattr(chunk, "dict"):
        return chunk.dict()  # type: ignore[no-any-return]
    return {}


def _extract_text(response: dict[str, Any]) -> str:
    try:
        value = response["choices"][0]["message"]["content"]
    except Exception:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"].strip())
        return "\n".join([part for part in parts if part]).strip()
    return ""


def _extract_json_payload(text: str) -> dict[str, Any]:
    candidate = text.strip()
    match = JSON_BLOCK_RE.search(candidate)
    if match:
        candidate = match.group(1).strip()
    else:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = candidate[start : end + 1]
    try:
        loaded = json.loads(candidate)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _payload_has_required_shape(payload: dict[str, Any]) -> bool:
    required_top_level = ("checkin", "memory", "reason")
    for key in required_top_level:
        if key not in payload:
            return False
    if not isinstance(payload.get("checkin"), dict):
        return False
    if not isinstance(payload.get("memory"), dict):
        return False
    reason = payload.get("reason")
    if not isinstance(reason, str):
        return False
    for block_name in ("checkin", "memory"):
        block = payload.get(block_name)
        if not isinstance(block, dict):
            return False
        write_value = block.get("write")
        if not isinstance(write_value, bool):
            return False
        block_reason = block.get("reason")
        if not isinstance(block_reason, str):
            return False
        if not block_reason.strip():
            return False
        if not write_value:
            continue
        if block_name == "checkin":
            required = ("domain", "track_type", "title", "summary", "outcome", "evidence")
        else:
            required = ("domain", "memory", "evidence")
        for key in required:
            if not isinstance(block.get(key), str):
                return False
    return True


def _normalize_items(values: Any, *, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        normalized.append(text)
        if len(normalized) >= limit:
            break
    return normalized


def _slug_words(text: str, *, max_words: int = 8) -> str:
    raw = text.strip()
    if not raw:
        return ""
    words = re.findall(r"[A-Za-z0-9_]+", raw)
    return " ".join(words[:max_words]).strip()


def _default_title_from_text(user_text: str) -> str:
    first_sentence = re.split(r"[.!?\n]", user_text.strip(), maxsplit=1)[0].strip()
    return _slug_words(first_sentence) or "User note"


def _normalize_track_type(raw: str) -> str:
    candidate = raw.strip().lower()
    if candidate in {"goal", "habit", "system"}:
        return candidate
    return "goal"


def _compact_reason(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


class StateDecisionEngine:
    def __init__(self, *, config: AppConfig, llm_router: LiteLLMRouter) -> None:
        self.config = config
        self.llm_router = llm_router
        self.logger = get_logger(__name__)

    @property
    def _decision_model(self) -> str:
        configured = self.config.state.decision.model.strip()
        return configured or self.config.models.orchestrator

    async def decide(
        self,
        *,
        user_text: str,
        assistant_text: str,
        routed_domain: str,
        context: StateContextSnapshot,
    ) -> StateDecision:
        trimmed_user = user_text.strip()
        if not trimmed_user:
            return StateDecision(
                reason="empty-user-text",
                checkin_reason="empty user text",
                memory_reason="empty user text",
            )

        llm_decision = await self._decide_with_model(
            user_text=trimmed_user,
            assistant_text=assistant_text.strip(),
            routed_domain=routed_domain,
            context=context,
        )
        if llm_decision is not None:
            return llm_decision
        if not self.config.state.decision.enabled:
            return StateDecision(
                reason="state-decision-disabled",
                checkin_reason="state decision disabled",
                memory_reason="state decision disabled",
            )
        return StateDecision(
            reason="state-model-unavailable",
            checkin_reason="state decision model unavailable",
            memory_reason="state decision model unavailable",
            is_failure=True,
        )

    async def _decide_with_model(
        self,
        *,
        user_text: str,
        assistant_text: str,
        routed_domain: str,
        context: StateContextSnapshot,
    ) -> StateDecision | None:
        if not self.config.state.decision.enabled:
            return None

        trimmed_user = user_text[: self.config.state.decision.max_user_chars]
        trimmed_assistant = assistant_text[: self.config.state.decision.max_assistant_chars]
        context_block = self._render_context(context)
        max_attempts = 1 + self.config.state.decision.max_json_retries
        retry_feedback = ""

        system_prompt = self._system_prompt()
        for attempt in range(1, max_attempts + 1):
            user_payload = self._user_payload(
                routed_domain=routed_domain,
                trimmed_user=trimmed_user,
                trimmed_assistant=trimmed_assistant,
                context_block=context_block,
                retry_feedback=retry_feedback,
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ]

            try:
                used_model, raw = await self.llm_router.chat_completion(
                    primary_model=self._decision_model,
                    messages=messages,
                    stream=False,
                    passthrough=None,
                    include_fallbacks=self.config.state.decision.include_fallbacks,
                )
            except Exception as exc:
                self.logger.warning(
                    "State decision model call failed model=%s attempt=%d/%d error=%s",
                    self._decision_model,
                    attempt,
                    max_attempts,
                    exc.__class__.__name__,
                )
                retry_feedback = (
                    f"Model call failed with {exc.__class__.__name__}. "
                    "Return ONLY strict JSON matching the schema."
                )
                continue

            parsed = _response_to_dict(raw)
            text = _extract_text(parsed)
            payload = _extract_json_payload(text)
            if not payload:
                retry_feedback = (
                    "Previous output was not parseable as a JSON object. "
                    "Return ONLY valid JSON and no markdown/code fences."
                )
                continue
            if not _payload_has_required_shape(payload):
                retry_feedback = (
                    "Previous JSON did not match required schema keys/types. "
                    "Include top-level keys checkin, memory, reason; "
                    "and each channel must include boolean write."
                )
                continue

            decision = self._decision_from_payload(
                payload=payload,
                routed_domain=routed_domain,
                source_user_text=user_text,
                source_model=used_model,
            )
            if decision is not None:
                return decision
            retry_feedback = "Previous JSON could not be normalized. Return stricter schema."

        self.logger.warning(
            "State decision model did not produce valid decision after %d attempt(s).",
            max_attempts,
        )
        return None

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are Mobius State Decision Engine.\n"
            "Task: Decide whether to write check-in and/or memory for the current user turn.\n"
            "You must be conservative, facts-only, and grounded in user text.\n"
            "Output requirements (MANDATORY):\n"
            "- Output EXACTLY one JSON object.\n"
            "- No markdown. No code fences. No commentary.\n"
            "- Do not add extra top-level keys.\n"
            "Top-level schema (all keys required):\n"
            "{\n"
            '  "checkin": {\n'
            '    "write": boolean,\n'
            '    "domain": string,\n'
            '    "track_type": "goal|habit|system",\n'
            '    "title": string,\n'
            '    "summary": string,\n'
            '    "outcome": "win|partial|miss|note",\n'
            '    "wins": string[],\n'
            '    "barriers": string[],\n'
            '    "next_actions": string[],\n'
            '    "tags": string[],\n'
            '    "evidence": string,\n'
            '    "reason": string\n'
            "  },\n"
            '  "memory": {\n'
            '    "write": boolean,\n'
            '    "domain": string,\n'
            '    "memory": string,\n'
            '    "evidence": string,\n'
            '    "reason": string\n'
            "  },\n"
            '  "reason": string\n'
            "}\n"
            "Policy:\n"
            "- One message may trigger 0-2 writes.\n"
            "- Facts only: never invent details that are not in user text.\n"
            "- Never persist assistant advice as fact unless user explicitly confirms it.\n"
            "- For each write=true block, evidence must be an exact quote from user_text.\n"
            "- If uncertain, set write=false (especially for memory).\n"
            "- Memory text must be self-contained and explicit; no vague pronouns.\n"
            "- If latest user text conflicts with an existing durable memory, produce updated memory text (do not add contradictory fact).\n"
            "- Ignore sarcasm/jokes/non-literal claims for memory unless user explicitly confirms literal intent.\n"
            "- For EACH channel, include a short reason (<=12 words) for why write is true/false.\n"
            "Triage ladder:\n"
            "1) Memory: durable preferences, recurring patterns, long-term facts/commitments.\n"
            "2) Check-in: ongoing goal/habit/system plus progress/barrier/accountability/coaching signal.\n"
            "Canonical positive examples:\n"
            "- 'I am lactose intolerant.' -> memory only.\n"
            "- 'Fat-loss check-in: this week I trained 4 times ... keep me on the plan.' -> check-in only.\n"
            "- 'For months I have been eating sweets late at night; track this weekly.' -> memory + check-in.\n"
            "- 'Today I decided to quit smoking for good; day 1, I want daily coaching.' -> memory + check-in.\n"
            "Canonical negative examples:\n"
            "- 'Today I planted 3 raspberry bushes, 2 currant bushes, and a cherry tree.' -> no state writes.\n"
            "- 'How should I prune currant bushes?' -> no state writes.\n"
            "- If channel is not justified, set write=false and use empty strings/lists.\n"
            "- Keep titles concise and stable.\n"
            "- Keep reason short and specific.\n"
        )

    @staticmethod
    def _user_payload(
        *,
        routed_domain: str,
        trimmed_user: str,
        trimmed_assistant: str,
        context_block: str,
        retry_feedback: str,
    ) -> str:
        sections = [
            f"routed_domain={routed_domain}",
            "user_text:",
            trimmed_user,
            "",
            "assistant_text:",
            trimmed_assistant,
            "",
            "context:",
            context_block,
        ]
        if retry_feedback.strip():
            sections.extend(
                [
                    "",
                    "retry_feedback:",
                    retry_feedback.strip(),
                ]
            )
        return "\n".join(sections).strip() + "\n"

    def _decision_from_payload(
        self,
        *,
        payload: dict[str, Any],
        routed_domain: str,
        source_user_text: str,
        source_model: str | None,
    ) -> StateDecision | None:
        max_wins = self.config.state.checkin.max_wins
        max_barriers = self.config.state.checkin.max_barriers
        max_next_actions = self.config.state.checkin.max_next_actions
        max_tags = 8

        checkin_block = payload.get("checkin")
        checkin_write: CheckinWrite | None = None
        checkin_reason = (
            _compact_reason(checkin_block.get("reason"))
            if isinstance(checkin_block, dict)
            else ""
        )
        if self.config.state.checkin.enabled and isinstance(checkin_block, dict):
            if bool(checkin_block.get("write")):
                checkin_domain = str(checkin_block.get("domain") or routed_domain).strip()
                checkin_title = str(checkin_block.get("title") or "").strip()
                if not checkin_title:
                    checkin_title = _default_title_from_text(source_user_text)
                checkin_summary = str(checkin_block.get("summary") or "").strip()
                if not checkin_summary:
                    checkin_summary = _slug_words(source_user_text, max_words=14) or "Check-in update."
                outcome = str(checkin_block.get("outcome") or "note").strip().lower()
                if outcome not in {"win", "partial", "miss", "note"}:
                    outcome = "note"
                confidence_raw = checkin_block.get("confidence")
                confidence: float | None = None
                if confidence_raw is not None:
                    try:
                        confidence = max(0.0, min(1.0, float(confidence_raw)))
                    except Exception:
                        confidence = None
                track_type = _normalize_track_type(str(checkin_block.get("track_type") or ""))
                evidence = str(checkin_block.get("evidence") or "").strip()
                checkin_write = CheckinWrite(
                    domain=checkin_domain or routed_domain,
                    track_type=track_type,
                    title=checkin_title,
                    summary=checkin_summary,
                    outcome=outcome,
                    confidence=confidence,
                    wins=_normalize_items(checkin_block.get("wins"), limit=max_wins),
                    barriers=_normalize_items(
                        checkin_block.get("barriers"), limit=max_barriers
                    ),
                    next_actions=_normalize_items(
                        checkin_block.get("next_actions"), limit=max_next_actions
                    ),
                    tags=_normalize_items(checkin_block.get("tags"), limit=max_tags),
                    evidence=evidence,
                )
        elif not self.config.state.checkin.enabled:
            checkin_reason = "check-in channel disabled by config"
        if not checkin_reason:
            checkin_reason = "missing check-in reason from state decision model"

        memory_block = payload.get("memory")
        memory_write: MemoryWrite | None = None
        memory_reason = (
            _compact_reason(memory_block.get("reason"))
            if isinstance(memory_block, dict)
            else ""
        )
        if self.config.state.memory.enabled and isinstance(memory_block, dict):
            if bool(memory_block.get("write")):
                memory_domain = str(memory_block.get("domain") or routed_domain).strip()
                memory_text = str(memory_block.get("memory") or "").strip()
                if not memory_text:
                    memory_text = _slug_words(source_user_text, max_words=16) or source_user_text
                evidence = str(memory_block.get("evidence") or "").strip()
                memory_write = MemoryWrite(
                    domain=memory_domain or routed_domain,
                    memory=memory_text,
                    evidence=evidence,
                )
        elif not self.config.state.memory.enabled:
            memory_reason = "memory channel disabled by config"
        if not memory_reason:
            memory_reason = "missing memory reason from state decision model"

        reason = _compact_reason(payload.get("reason"))
        return StateDecision(
            checkin=checkin_write,
            memory=memory_write,
            checkin_reason=checkin_reason,
            memory_reason=memory_reason,
            reason=reason or "state-model",
            source_model=source_model,
        )

    @staticmethod
    def _render_context(context: StateContextSnapshot) -> str:
        lines: list[str] = []
        if context.active_tracks:
            lines.append("active_tracks:")
            for row in context.active_tracks:
                lines.append(
                    f"- {row.get('slug')}: {row.get('title')} "
                    f"(domain={row.get('domain')} status={row.get('status')})"
                )
        if context.recent_checkins:
            lines.append("recent_checkins:")
            for row in context.recent_checkins:
                lines.append(
                    f"- {row.get('track_slug')} @ {row.get('timestamp')}: {row.get('summary')}"
                )
        if context.recent_memory_cards:
            lines.append("recent_memory_cards:")
            for row in context.recent_memory_cards:
                lines.append(
                    f"- {row.get('domain')}/{row.get('slug')}: {row.get('memory')}"
                )
        return "\n".join(lines) if lines else "none"
