from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, AsyncIterator
from uuid import uuid4

from ai_agents_hub.api.schemas import ChatCompletionRequest, OpenAIMessage, latest_user_text
from ai_agents_hub.config import AppConfig
from ai_agents_hub.journal.obsidian_writer import ObsidianJournalWriter
from ai_agents_hub.logging_setup import get_logger
from ai_agents_hub.memory.store import MemoryRecord, MemoryStore
from ai_agents_hub.orchestration.specialists import (
    SpecialistProfile,
    rank_specialists,
)
from ai_agents_hub.prompts.manager import PromptManager
from ai_agents_hub.providers.litellm_router import LiteLLMRouter
from ai_agents_hub.tools.runner import ToolRunner
from ai_agents_hub.tools.web_search import SearchSource


@dataclass
class RoutingDecision:
    selected: list[SpecialistProfile]
    domain: str
    confidence: float
    route_model: str
    response_model: str


def _now_unix() -> int:
    return int(time.time())


def _message_to_dict(message: OpenAIMessage) -> dict[str, Any]:
    return message.model_dump(exclude_none=True)


def _response_from_text(model: str, text: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion",
        "created": _now_unix(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _chunk_to_dict(chunk: Any) -> dict[str, Any]:
    if isinstance(chunk, dict):
        return chunk
    if hasattr(chunk, "model_dump"):
        return chunk.model_dump(exclude_none=True)  # type: ignore[no-any-return]
    if hasattr(chunk, "dict"):
        return chunk.dict()  # type: ignore[no-any-return]
    raise TypeError(f"Unsupported stream chunk type: {type(chunk)}")


class Supervisor:
    def __init__(
        self,
        *,
        config: AppConfig,
        llm_router: LiteLLMRouter,
        memory_store: MemoryStore,
        tool_runner: ToolRunner,
        prompt_manager: PromptManager,
        journal_writer: ObsidianJournalWriter | None,
    ) -> None:
        self.config = config
        self.llm_router = llm_router
        self.memory_store = memory_store
        self.tool_runner = tool_runner
        self.prompt_manager = prompt_manager
        self.journal_writer = journal_writer
        self.logger = get_logger(__name__)
        self.public_model_id = self.config.openai_compat.master_model_id
        self.allow_provider_model_passthrough = (
            self.config.openai_compat.allow_provider_model_passthrough
        )
        self.provider_model_ids = set(self.llm_router.list_models())

    def _decide_routing(self, user_text: str, requested_model: str | None) -> RoutingDecision:
        ranked = rank_specialists(user_text)
        selected: list[SpecialistProfile] = []
        confidence = 0.0
        domain = "general"
        threshold = self.config.router.specialist_selection.min_confidence
        delta = self.config.router.specialist_selection.dual_specialist_delta
        max_specialists = self.config.router.specialist_selection.max_specialists_per_turn

        if ranked:
            top_spec, top_score = ranked[0]
            if top_score >= threshold:
                selected.append(top_spec)
                confidence = top_score
                domain = top_spec.domain
            if (
                len(ranked) > 1
                and len(selected) < max_specialists
                and (top_score - ranked[1][1]) <= delta
                and ranked[1][1] >= threshold
            ):
                selected.append(ranked[1][0])

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
            route_model = (
                self.config.models.routing.by_domain(domain)
                if domain != "general"
                else self.config.models.routing.general
            ) or self.config.models.default_chat
            response_model = self.public_model_id
            if requested and requested != self.public_model_id:
                self.logger.info(
                    "Requested model '%s' is not exposed; using master model '%s'.",
                    requested,
                    self.public_model_id,
                )

        decision = RoutingDecision(
            selected=selected,
            domain=domain,
            confidence=confidence,
            route_model=route_model,
            response_model=response_model,
        )
        self.logger.debug(
            "Routing decision domain=%s confidence=%.2f specialists=%s route_model=%s response_model=%s requested_model=%s passthrough=%s",
            decision.domain,
            decision.confidence,
            [item.domain for item in decision.selected],
            decision.route_model,
            decision.response_model,
            requested_model,
            passthrough,
        )
        return decision

    def _build_system_prompt(self, selected: list[SpecialistProfile]) -> str:
        if not selected:
            return self.prompt_manager.get("general")
        lines = [self.prompt_manager.get("supervisor"), "", "Specialist instructions:"]
        for specialist in selected:
            lines.append(f"- {specialist.label} ({specialist.domain}):")
            lines.append(self.prompt_manager.get(specialist.domain))
        return "\n".join(lines)

    async def _maybe_handle_control_command(self, user_text: str) -> dict[str, Any] | None:
        normalized = user_text.strip()
        if normalized.startswith("/undo-memory "):
            memory_id = normalized.split(maxsplit=1)[1].strip()
            self.logger.info("Received memory undo command id=%s", memory_id)
            success = self.memory_store.undo_memory(memory_id)
            message = (
                f"Memory `{memory_id}` marked as tombstone."
                if success
                else f"Memory `{memory_id}` not found."
            )
            return _response_from_text(self.public_model_id, message)

        if normalized.startswith("/edit-memory "):
            parts = normalized.split(maxsplit=2)
            if len(parts) < 3:
                return _response_from_text(
                    self.public_model_id,
                    "Usage: /edit-memory <memory_id> <instructions>",
                )
            memory_id = parts[1]
            instructions = parts[2]
            self.logger.info("Received memory edit command id=%s", memory_id)
            success = self.memory_store.edit_memory(memory_id, instructions)
            message = (
                f"Memory `{memory_id}` updated with your edit note."
                if success
                else f"Memory `{memory_id}` not found."
            )
            return _response_from_text(self.public_model_id, message)
        return None

    @staticmethod
    def _sources_block(sources: list[SearchSource]) -> str:
        if not sources:
            return ""
        lines = ["\nSources:"]
        for idx, source in enumerate(sources, start=1):
            lines.append(f"- [S{idx}] {source.title} - {source.url}")
        return "\n".join(lines)

    def _notification_block(self, record: MemoryRecord | None) -> str:
        if not record or not self.config.memory.notify_on_write:
            return ""
        return (
            "\n\nMemory written:\n"
            f"- id: `{record.memory_id}`\n"
            f"- file: `{record.path}`\n"
            "Actions:\n"
            f"- undo: `/undo-memory {record.memory_id}`\n"
            f"- edit: `/edit-memory {record.memory_id} <instructions>`"
        )

    def _build_orchestrated_messages(
        self,
        request: ChatCompletionRequest,
        decision: RoutingDecision,
        sources: list[SearchSource],
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        system_prompt = self._build_system_prompt(decision.selected)
        messages.append({"role": "system", "content": system_prompt})
        if sources:
            messages.append(
                {
                    "role": "system",
                    "content": self.tool_runner.sources_context_block(sources),
                }
            )
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "When web sources are used, include explicit source citations "
                        "like [S1], [S2] in the response."
                    ),
                }
            )
        messages.extend(_message_to_dict(msg) for msg in request.messages)
        return messages

    def _persist_side_effects(
        self,
        *,
        user_text: str,
        assistant_text: str,
        decision: RoutingDecision,
    ) -> tuple[MemoryRecord | None, str | None]:
        record: MemoryRecord | None = None
        journal_path: str | None = None
        if assistant_text.strip() and self.config.memory.auto_write:
            summary = user_text.strip().split("\n")[0][:120] or "Conversation memory"
            body = (
                "### User\n"
                f"{user_text.strip()}\n\n"
                "### Assistant\n"
                f"{assistant_text.strip()}\n"
            )
            record = self.memory_store.write_memory(
                domain=decision.domain if decision.domain != "general" else "general",
                summary=summary,
                body=body,
                confidence=max(decision.confidence, 0.5),
                tags=[decision.domain],
                created_by_agent="supervisor",
            )
            self.logger.debug("Memory side effect persisted id=%s", record.memory_id)

        if self.journal_writer and self.config.journal.enabled and assistant_text.strip():
            journal_file = self.journal_writer.append_entry(
                heading=f"{decision.domain} conversation",
                content=(
                    f"User: {user_text.strip()}\n\nAssistant: {assistant_text.strip()}"
                ),
            )
            journal_path = str(journal_file)
            self.logger.debug("Journal side effect persisted path=%s", journal_path)
        return record, journal_path

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
        control = await self._maybe_handle_control_command(user_text)
        if control:
            self.logger.debug("Control command handled in non-stream mode.")
            return control

        decision = self._decide_routing(user_text, request.model)
        sources = await self.tool_runner.maybe_search(user_text)
        self.logger.debug("Source count=%d", len(sources))
        messages = self._build_orchestrated_messages(request, decision, sources)

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

        record, journal_path = self._persist_side_effects(
            user_text=user_text,
            assistant_text=assistant_text,
            decision=decision,
        )
        augmented = assistant_text + self._sources_block(sources) + self._notification_block(
            record
        )
        if journal_path:
            augmented += f"\n- journal: `{journal_path}`"
        try:
            response["choices"][0]["message"]["content"] = augmented
        except Exception:
            pass
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
        control = await self._maybe_handle_control_command(user_text)
        if control:
            yield f"data: {json.dumps(control)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
            self.logger.debug("Control command handled in stream mode.")
            return

        decision = self._decide_routing(user_text, request.model)
        sources = await self.tool_runner.maybe_search(user_text)
        messages = self._build_orchestrated_messages(request, decision, sources)
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

        collected: list[str] = []
        stream_id: str | None = None
        chunk_count = 0
        async for chunk in stream:
            as_dict = _chunk_to_dict(chunk)
            stream_id = stream_id or as_dict.get("id")
            chunk_count += 1
            try:
                delta = as_dict["choices"][0].get("delta", {})
                piece = delta.get("content")
                if isinstance(piece, str):
                    collected.append(piece)
            except Exception:
                pass
            as_dict["model"] = decision.response_model
            yield f"data: {json.dumps(as_dict)}\n\n".encode("utf-8")

        assistant_text = "".join(collected).strip()
        record, journal_path = self._persist_side_effects(
            user_text=user_text,
            assistant_text=assistant_text,
            decision=decision,
        )
        tail = self._sources_block(sources) + self._notification_block(record)
        if journal_path:
            tail += f"\n- journal: `{journal_path}`"
        if tail.strip():
            synthetic_chunk = {
                "id": stream_id or f"chatcmpl-{uuid4().hex}",
                "object": "chat.completion.chunk",
                "created": int(datetime.now(timezone.utc).timestamp()),
                "model": decision.response_model,
                "choices": [{"index": 0, "delta": {"content": tail}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(synthetic_chunk)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
        self.logger.info(
            "Stream completion finished public_model=%s internal_model=%s chunks=%d elapsed_ms=%d",
            decision.response_model,
            used_model,
            chunk_count,
            int((perf_counter() - started_at) * 1000),
        )
