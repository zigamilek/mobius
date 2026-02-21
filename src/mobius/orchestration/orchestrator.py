from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, AsyncIterator
from uuid import uuid4

from mobius.api.schemas import ChatCompletionRequest, OpenAIMessage, latest_user_text
from mobius.config import AppConfig
from mobius.logging_setup import get_logger
from mobius.orchestration.session_store import StickySessionStore
from mobius.orchestration.specialist_router import SpecialistRouter
from mobius.orchestration.specialists import SpecialistProfile, get_specialist
from mobius.prompts.manager import PromptManager
from mobius.providers.litellm_router import LiteLLMRouter
from mobius.runtime_context import timestamp_context_line


@dataclass
class RoutingDecision:
    selected: list[SpecialistProfile]
    domain: str
    confidence: float
    route_model: str
    response_model: str
    orchestrator_model: str | None


SESSION_ID_FIELDS: tuple[str, ...] = (
    "session_id",
    "conversation_id",
    "chat_id",
    "thread_id",
    "session",
    "conversation",
)


def _normalize_md_line(line: str) -> str:
    return line.strip().strip("*_ ").strip().lower()


def _is_state_block_header(line: str) -> bool:
    normalized = _normalize_md_line(line)
    return normalized in {"state detection:", "state writes:", "state warning:"}


def _is_answered_by_header(line: str) -> bool:
    normalized = _normalize_md_line(line)
    return normalized.startswith("answered by ")


def _sanitize_assistant_text(text: str) -> str:
    if not text.strip():
        return text
    lines = text.splitlines()
    cleaned: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if _is_state_block_header(line):
            idx += 1
            while idx < len(lines):
                stripped = lines[idx].strip()
                if not stripped:
                    idx += 1
                    continue
                if stripped.startswith("- "):
                    idx += 1
                    continue
                break
            while idx < len(lines) and not lines[idx].strip():
                idx += 1
            continue
        if _is_answered_by_header(line):
            idx += 1
            while idx < len(lines) and not lines[idx].strip():
                idx += 1
            continue
        cleaned.append(line)
        idx += 1
    rendered = "\n".join(cleaned).strip()
    if not rendered:
        return ""
    return re.sub(r"\n{3,}", "\n\n", rendered)


def _message_to_dict(message: OpenAIMessage) -> dict[str, Any]:
    payload = message.model_dump(exclude_none=True)
    if payload.get("role") != "assistant":
        return payload

    content = payload.get("content")
    if isinstance(content, str):
        payload["content"] = _sanitize_assistant_text(content)
        return payload

    if isinstance(content, list):
        sanitized_parts: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            part = dict(item)
            if item_type in {"text", "input_text"} and isinstance(item.get("text"), str):
                part["text"] = _sanitize_assistant_text(item["text"])
            sanitized_parts.append(part)
        payload["content"] = sanitized_parts
    return payload


def _chunk_to_dict(chunk: Any) -> dict[str, Any]:
    if isinstance(chunk, dict):
        return chunk
    if hasattr(chunk, "model_dump"):
        return chunk.model_dump(exclude_none=True)  # type: ignore[no-any-return]
    if hasattr(chunk, "dict"):
        return chunk.dict()  # type: ignore[no-any-return]
    raise TypeError(f"Unsupported stream chunk type: {type(chunk)}")


class Orchestrator:
    def __init__(
        self,
        *,
        config: AppConfig,
        llm_router: LiteLLMRouter,
        specialist_router: SpecialistRouter,
        prompt_manager: PromptManager,
        session_store: StickySessionStore | None = None,
    ) -> None:
        self.config = config
        self.llm_router = llm_router
        self.specialist_router = specialist_router
        self.prompt_manager = prompt_manager
        self.session_store = session_store or StickySessionStore(history_size=3)
        self.logger = get_logger(__name__)
        self.public_model_id = self.config.api.public_model_id
        self.allow_provider_model_passthrough = (
            self.config.api.allow_provider_model_passthrough
        )
        self.provider_model_ids = set(self.llm_router.list_models())

    def _timestamp_context_line(self) -> str:
        return timestamp_context_line(self.config.runtime.timezone)

    @staticmethod
    def _first_user_text(messages: list[OpenAIMessage]) -> str:
        for message in messages:
            if message.role != "user":
                continue
            text = message.text_content().strip()
            if text:
                return text
        return ""

    @staticmethod
    def _is_first_user_prompt(messages: list[OpenAIMessage]) -> bool:
        user_count = 0
        assistant_count = 0
        for message in messages:
            if message.role == "user":
                user_count += 1
            elif message.role == "assistant":
                assistant_count += 1
        return user_count == 1 and assistant_count == 0

    def _session_key_for_request(self, request: ChatCompletionRequest) -> str | None:
        extras = request.model_extra if isinstance(request.model_extra, dict) else {}
        for field in SESSION_ID_FIELDS:
            raw = extras.get(field)
            if raw is None:
                continue
            value = str(raw).strip()
            if value:
                return f"{field}:{value}"

        first_user = self._first_user_text(request.messages)
        if not first_user:
            return None

        digest = hashlib.sha256(first_user.encode("utf-8")).hexdigest()[:16]
        user_id = (request.user or "").strip()
        if user_id:
            return f"user:{user_id}:first:{digest}"
        return f"first:{digest}"

    async def _decide_routing(
        self,
        messages: list[OpenAIMessage],
        requested_model: str | None,
        session_key: str | None,
    ) -> RoutingDecision:
        user_text = latest_user_text(messages)
        recent_domains = (
            self.session_store.recent_domains(session_key) if session_key else []
        )
        current_domain = recent_domains[-1] if recent_domains else None
        route = await self.specialist_router.classify(
            user_text,
            current_domain=current_domain,
            recent_domains=recent_domains,
        )
        domain = route.domain
        confidence = route.confidence
        orchestrator_model = route.orchestrator_model
        if current_domain:
            if domain == current_domain:
                self.logger.info(
                    "Routing kept domain=%s using session context session=%s.",
                    domain,
                    session_key,
                )
            else:
                self.logger.info(
                    "Routing switched domain=%s -> %s using session context session=%s.",
                    current_domain,
                    domain,
                    session_key,
                )

        selected: list[SpecialistProfile] = []
        if domain != "general":
            selected = [get_specialist(domain)]

        requested = (requested_model or "").strip()
        passthrough = (
            self.allow_provider_model_passthrough
            and bool(requested)
            and requested in self.provider_model_ids
            and requested != self.public_model_id
        )
        if passthrough:
            route_model = requested
            response_model = requested
        else:
            specialist_config = self.config.specialists.by_domain.get(domain)
            route_model = (
                specialist_config.model
                if specialist_config is not None
                else self.config.models.orchestrator
            )
            response_model = self.public_model_id
            if requested and requested != self.public_model_id:
                self.logger.info(
                    "Requested model '%s' is not exposed; using public model '%s'.",
                    requested,
                    self.public_model_id,
                )

        decision = RoutingDecision(
            selected=selected,
            domain=domain,
            confidence=confidence,
            route_model=route_model,
            response_model=response_model,
            orchestrator_model=orchestrator_model,
        )
        self.logger.debug(
            "Routing decision domain=%s confidence=%.2f specialists=%s route_model=%s response_model=%s orchestrator_model=%s requested_model=%s passthrough=%s",
            decision.domain,
            decision.confidence,
            [item.domain for item in decision.selected],
            decision.route_model,
            decision.response_model,
            decision.orchestrator_model,
            requested_model,
            passthrough,
        )
        return decision

    def _build_system_prompt(self, selected: list[SpecialistProfile]) -> str:
        if not selected:
            prompt = self.prompt_manager.get("general")
        else:
            lines = [self.prompt_manager.get("orchestrator"), "", "Specialist instructions:"]
            for specialist in selected:
                lines.append(f"- {specialist.label} ({specialist.domain}):")
                lines.append(self.prompt_manager.get(specialist.domain))
            prompt = "\n".join(lines)

        if not self.config.runtime.inject_current_timestamp:
            return prompt

        return f"{self._timestamp_context_line()}\n\n{prompt}"

    @staticmethod
    def _default_display_name_for_domain(domain: str) -> str:
        label = get_specialist(domain).label.strip()
        suffix = " specialist"
        if label.lower().endswith(suffix):
            return label[: -len(suffix)].strip()
        return label

    def _answered_by_prefix(self, domain: str, used_model: str | None) -> str:
        attribution = self.config.api.attribution
        if not attribution.enabled:
            return ""
        if domain == "general" and not attribution.include_general:
            return ""
        specialist_cfg = self.config.specialists.by_domain.get(domain)
        configured_display_name = (
            specialist_cfg.display_name.strip()
            if specialist_cfg is not None and specialist_cfg.display_name
            else ""
        )
        display_name = configured_display_name or self._default_display_name_for_domain(
            domain
        )
        model_name = (used_model or "").strip()
        if not model_name and specialist_cfg is not None:
            model_name = specialist_cfg.model
        if not model_name:
            model_name = self.config.models.orchestrator
        domain_label = domain.replace("_", " ")
        model_suffix = f" using {model_name} model" if attribution.include_model else ""
        try:
            rendered = attribution.template.format(
                display_name=display_name,
                domain=domain,
                domain_label=domain_label,
                model=model_name,
                model_suffix=model_suffix,
            )
        except Exception as exc:
            self.logger.warning(
                "Invalid api.attribution.template; using default template error=%s",
                exc.__class__.__name__,
            )
            rendered = (
                f"Answered by {display_name} (the {domain_label} specialist)"
                f"{model_suffix}."
            )
        return f"*{rendered}*\n\n"

    def _build_orchestrated_messages(
        self,
        request: ChatCompletionRequest,
        decision: RoutingDecision,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        system_prompt = self._build_system_prompt(decision.selected)
        messages.append({"role": "system", "content": system_prompt})
        for message in request.messages:
            serialized = _message_to_dict(message)
            role = str(serialized.get("role") or "")
            content = serialized.get("content")
            if role == "assistant" and isinstance(content, str) and not content.strip():
                continue
            messages.append(serialized)
        return messages

    @staticmethod
    def _extract_assistant_text(response: dict[str, Any]) -> str:
        try:
            return str(response["choices"][0]["message"]["content"] or "")
        except Exception:
            return ""

    async def complete_non_stream(self, request: ChatCompletionRequest) -> dict[str, Any]:
        started_at = perf_counter()
        user_text = latest_user_text(request.messages)
        self.logger.info(
            "Non-stream completion started model=%s message_count=%d",
            request.model,
            len(request.messages),
        )
        if self.config.logging.include_payloads:
            self.logger.debug("Non-stream user_text=%s", user_text)

        session_key = self._session_key_for_request(request)
        if session_key and self._is_first_user_prompt(request.messages):
            self.session_store.reset(session_key)
        decision = await self._decide_routing(request.messages, request.model, session_key)
        messages = self._build_orchestrated_messages(request, decision)

        passthrough = request.model_dump(
            exclude={"messages", "model", "stream"},
            exclude_none=True,
        )
        used_model, raw_response = await self.llm_router.chat_completion(
            primary_model=decision.route_model,
            messages=messages,
            stream=False,
            passthrough=passthrough,
        )
        response = _chunk_to_dict(raw_response)
        response["model"] = decision.response_model
        assistant_text = self._extract_assistant_text(response)
        augmented = self._answered_by_prefix(decision.domain, used_model) + assistant_text
        try:
            response["choices"][0]["message"]["content"] = augmented
        except Exception:
            pass
        if session_key:
            self.session_store.remember_domain(session_key, decision.domain)
        self.logger.info(
            "Non-stream completion finished public_model=%s internal_model=%s elapsed_ms=%d",
            decision.response_model,
            used_model,
            int((perf_counter() - started_at) * 1000),
        )
        return response

    async def stream_sse(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        started_at = perf_counter()
        user_text = latest_user_text(request.messages)
        self.logger.info(
            "Stream completion started model=%s message_count=%d",
            request.model,
            len(request.messages),
        )
        if self.config.logging.include_payloads:
            self.logger.debug("Stream user_text=%s", user_text)

        session_key = self._session_key_for_request(request)
        if session_key and self._is_first_user_prompt(request.messages):
            self.session_store.reset(session_key)
        decision = await self._decide_routing(request.messages, request.model, session_key)
        messages = self._build_orchestrated_messages(request, decision)
        passthrough = request.model_dump(
            exclude={"messages", "model", "stream"},
            exclude_none=True,
        )
        used_model, stream = await self.llm_router.chat_completion(
            primary_model=decision.route_model,
            messages=messages,
            stream=True,
            passthrough=passthrough,
        )
        if session_key:
            self.session_store.remember_domain(session_key, decision.domain)

        stream_id: str | None = None
        chunk_count = 0
        prefix = self._answered_by_prefix(decision.domain, used_model)
        prefix_pending = bool(prefix)
        collected_assistant_chunks: list[str] = []
        async for chunk in stream:
            as_dict = _chunk_to_dict(chunk)
            stream_id = stream_id or as_dict.get("id")
            chunk_count += 1
            try:
                raw_delta = as_dict["choices"][0].get("delta", {})
                raw_piece = raw_delta.get("content")
                if isinstance(raw_piece, str):
                    collected_assistant_chunks.append(raw_piece)
            except Exception:
                pass
            if prefix_pending:
                try:
                    delta = as_dict["choices"][0].setdefault("delta", {})
                    content_piece = delta.get("content")
                    if isinstance(content_piece, str):
                        delta["content"] = prefix + content_piece
                    else:
                        delta["content"] = prefix
                except Exception:
                    fallback_prefix_chunk = {
                        "id": stream_id or f"chatcmpl-{uuid4().hex}",
                        "object": "chat.completion.chunk",
                        "created": int(datetime.now(timezone.utc).timestamp()),
                        "model": decision.response_model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": prefix},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(fallback_prefix_chunk)}\n\n".encode("utf-8")
                prefix_pending = False
            as_dict["model"] = decision.response_model
            yield f"data: {json.dumps(as_dict)}\n\n".encode("utf-8")

        yield b"data: [DONE]\n\n"
        self.logger.info(
            "Stream completion finished public_model=%s internal_model=%s chunks=%d elapsed_ms=%d",
            decision.response_model,
            used_model,
            chunk_count,
            int((perf_counter() - started_at) * 1000),
        )
