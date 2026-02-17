from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, AsyncIterator
from uuid import uuid4

from ai_agents_hub.api.schemas import ChatCompletionRequest, OpenAIMessage, latest_user_text
from ai_agents_hub.config import AppConfig
from ai_agents_hub.logging_setup import get_logger
from ai_agents_hub.orchestration.specialist_router import SpecialistRouter
from ai_agents_hub.orchestration.specialists import SpecialistProfile, get_specialist
from ai_agents_hub.prompts.manager import PromptManager
from ai_agents_hub.providers.litellm_router import LiteLLMRouter


@dataclass
class RoutingDecision:
    selected: list[SpecialistProfile]
    domain: str
    confidence: float
    route_model: str
    response_model: str
    classifier_model: str | None


def _message_to_dict(message: OpenAIMessage) -> dict[str, Any]:
    return message.model_dump(exclude_none=True)


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
    ) -> None:
        self.config = config
        self.llm_router = llm_router
        self.specialist_router = specialist_router
        self.prompt_manager = prompt_manager
        self.logger = get_logger(__name__)
        self.public_model_id = self.config.openai_compatibility.public_model_id
        self.allow_provider_model_passthrough = (
            self.config.openai_compatibility.allow_provider_model_passthrough
        )
        self.provider_model_ids = set(self.llm_router.list_models())

    async def _decide_routing(
        self, user_text: str, requested_model: str | None
    ) -> RoutingDecision:
        route = await self.specialist_router.classify(user_text)
        domain = route.domain
        confidence = route.confidence
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
            route_model = (
                self.config.models.specialists.by_domain(domain)
                if domain != "general"
                else self.config.models.specialists.general
            ) or self.config.models.orchestrator
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
            classifier_model=route.classifier_model,
        )
        self.logger.debug(
            "Routing decision domain=%s confidence=%.2f specialists=%s route_model=%s response_model=%s classifier_model=%s requested_model=%s passthrough=%s",
            decision.domain,
            decision.confidence,
            [item.domain for item in decision.selected],
            decision.route_model,
            decision.response_model,
            decision.classifier_model,
            requested_model,
            passthrough,
        )
        return decision

    def _build_system_prompt(self, selected: list[SpecialistProfile]) -> str:
        if not selected:
            return self.prompt_manager.get("general")
        lines = [self.prompt_manager.get("orchestrator"), "", "Specialist instructions:"]
        for specialist in selected:
            lines.append(f"- {specialist.label} ({specialist.domain}):")
            lines.append(self.prompt_manager.get(specialist.domain))
        return "\n".join(lines)

    @staticmethod
    def _answered_by_prefix(domain: str) -> str:
        if domain == "general":
            return ""
        label = domain.replace("_", " ")
        return f"Answered by the {label} specialist.\n\n"

    def _build_orchestrated_messages(
        self,
        request: ChatCompletionRequest,
        decision: RoutingDecision,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        system_prompt = self._build_system_prompt(decision.selected)
        messages.append({"role": "system", "content": system_prompt})
        messages.extend(_message_to_dict(msg) for msg in request.messages)
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

        decision = await self._decide_routing(user_text, request.model)
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
        augmented = self._answered_by_prefix(decision.domain) + assistant_text
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

        decision = await self._decide_routing(user_text, request.model)
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

        stream_id: str | None = None
        chunk_count = 0
        prefix = self._answered_by_prefix(decision.domain)
        prefix_pending = bool(prefix)
        async for chunk in stream:
            as_dict = _chunk_to_dict(chunk)
            stream_id = stream_id or as_dict.get("id")
            chunk_count += 1
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
