from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from ai_agents_hub.api.schemas import ChatCompletionRequest
from ai_agents_hub.config import AppConfig, load_config
from ai_agents_hub.orchestration.orchestrator import Orchestrator
from ai_agents_hub.orchestration.specialist_router import SpecialistRouter
from ai_agents_hub.prompts.manager import PromptManager
from ai_agents_hub.providers.litellm_router import LiteLLMRouter

PREFIX_RE = re.compile(r"^Answered by the (?P<label>.+?) specialist\.$")


def _live_enabled() -> bool:
    return os.getenv("AI_AGENTS_HUB_LIVE_TESTS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _resolve_config_path() -> Path:
    env_path = os.getenv("AI_AGENTS_HUB_CONFIG", "").strip()
    if env_path:
        return Path(env_path)
    local = Path("config.local.yaml")
    if local.exists():
        return local
    return Path("config.yaml")


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


def _response_to_dict(chunk: Any) -> dict[str, Any]:
    if isinstance(chunk, dict):
        return chunk
    if hasattr(chunk, "model_dump"):
        return chunk.model_dump(exclude_none=True)  # type: ignore[no-any-return]
    if hasattr(chunk, "dict"):
        return chunk.dict()  # type: ignore[no-any-return]
    return {}


def _extract_json_payload(text: str) -> dict[str, Any]:
    candidate = text.strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = candidate[start : end + 1]
    try:
        loaded = json.loads(candidate)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _extract_confidence(calls: list[dict[str, Any]]) -> float | None:
    orchestrator_calls = [c for c in calls if c["include_fallbacks_requested"] is False]
    for call in reversed(orchestrator_calls):
        text = str(call.get("response_text") or "").strip()
        if not text:
            continue
        payload = _extract_json_payload(text)
        raw_confidence = payload.get("confidence")
        try:
            confidence = float(raw_confidence)
        except Exception:
            continue
        return max(0.0, min(1.0, confidence))
    return None


def _extract_reason(calls: list[dict[str, Any]]) -> str | None:
    orchestrator_calls = [c for c in calls if c["include_fallbacks_requested"] is False]
    for call in reversed(orchestrator_calls):
        text = str(call.get("response_text") or "").strip()
        if not text:
            continue
        payload = _extract_json_payload(text)
        reason = str(payload.get("reason") or "").strip()
        if reason:
            return reason
    return None


class SpyLiteLLMRouter(LiteLLMRouter):
    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self.calls: list[dict[str, Any]] = []

    async def chat_completion(
        self,
        *,
        primary_model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        passthrough: dict[str, Any] | None = None,
        include_fallbacks: bool = True,
    ) -> tuple[str, Any]:
        requested_include_fallbacks = include_fallbacks
        call_record: dict[str, Any] = {
            "primary_model": primary_model,
            "stream": stream,
            "include_fallbacks_requested": requested_include_fallbacks,
        }

        # Live probe should not cascade into unrelated provider fallbacks,
        # otherwise one model issue can mask the actual routing behavior.
        include_fallbacks = False
        call_record["include_fallbacks_executed"] = include_fallbacks
        self.calls.append(call_record)
        try:
            used_model, raw = await super().chat_completion(
                primary_model=primary_model,
                messages=messages,
                stream=stream,
                passthrough=passthrough,
                include_fallbacks=include_fallbacks,
            )
            call_record["used_model"] = used_model
            raw_dict = _response_to_dict(raw)
            if raw_dict:
                call_record["response_text"] = _extract_text(raw_dict)
            return used_model, raw
        except Exception as exc:
            call_record["error"] = exc.__class__.__name__
            raise


def _parse_specialist_from_response(content: str) -> str:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return "unknown"
    match = PREFIX_RE.match(lines[0])
    if not match:
        return "general"
    return match.group("label").strip().replace(" ", "_")


@pytest.mark.live
@pytest.mark.skipif(
    not _live_enabled(),
    reason=(
        "Set AI_AGENTS_HUB_LIVE_TESTS=1 to run live routing behavior tests "
        "(calls external model providers)."
    ),
)
def test_live_openwebui_like_routing_flow() -> None:
    cfg = load_config(_resolve_config_path())

    default_model = cfg.models.orchestrator.lower()
    needs_openai = default_model.startswith("gpt") or default_model.startswith("openai/")
    needs_gemini = default_model.startswith("gemini")
    if needs_openai and not cfg.providers.openai.api_key:
        pytest.skip("OPENAI_API_KEY is required for live orchestrator model.")
    if needs_gemini and not cfg.providers.gemini.api_key:
        pytest.skip("GEMINI_API_KEY is required for live orchestrator model.")

    # Keep probe focused on orchestrator + specialist routing behavior.
    cfg.models.fallbacks = []
    cfg.specialists.prompts.directory = Path("prompts/specialists").resolve()
    if not cfg.providers.gemini.api_key:
        cfg.models.specialists.homelab = cfg.models.specialists.general

    llm_router = SpyLiteLLMRouter(cfg)
    specialist_router = SpecialistRouter(config=cfg, llm_router=llm_router)
    prompt_manager = PromptManager(cfg)
    orchestrator = Orchestrator(
        config=cfg,
        llm_router=llm_router,
        specialist_router=specialist_router,
        prompt_manager=prompt_manager,
    )

    queries = [
        "Can you help me construct a rehabilitation program for tennis elbow?",
        "How can I make my son obey my instructions?",
        "Help me plan my week better.",
        "Design a cryptocurrency trading strategy for me.",
        "Wife and I are having a fight, how can we resolve it?",
        "I'm trying to build a home lab, can you help me with that?",
        "Join me in a game of chess.",
        "Please play the piano for me.",
        "Suggest a new hobby for me.",
        "I'm a senior data engineer, suggest a side hustle for me.",
        "I'd like to start saving for retirement, can you help me with that?",
        "I'd like to move to the country and start a slow living lifestyle, do you have any tips?",
    ]

    for query in queries:
        start_idx = len(llm_router.calls)
        request = ChatCompletionRequest.model_validate(
            {
                "model": cfg.openai_compatibility.public_model_id,
                "messages": [{"role": "user", "content": query}],
                "stream": False,
            }
        )
        response = asyncio.run(orchestrator.complete_non_stream(request))
        content = str(response["choices"][0]["message"]["content"] or "").strip()
        routed_specialist = _parse_specialist_from_response(content)

        calls = llm_router.calls[start_idx:]
        orchestrator_models = [
            call["primary_model"]
            for call in calls
            if call["include_fallbacks_requested"] is False
        ]
        specialist_models = [
            call["primary_model"]
            for call in calls
            if call["include_fallbacks_requested"] is True
        ]
        confidence = _extract_confidence(calls)
        reason = _extract_reason(calls)

        print(f"QUERY: {query}")
        print(f"ROUTED SPECIALIST: {routed_specialist}")
        print(
            f"ROUTING CONFIDENCE: {confidence:.2f}"
            if confidence is not None
            else "ROUTING CONFIDENCE: n/a"
        )
        print(f"ROUTING REASON: {reason or 'n/a'}")
        print(f"ORCHESTRATOR MODEL CALLS: {orchestrator_models}")
        print(f"SPECIALIST MODEL CALLS: {specialist_models}")
        print("---")

        assert content
        assert orchestrator_models, "Expected at least one orchestrator model call."
        assert specialist_models, "Expected at least one specialist model call."
