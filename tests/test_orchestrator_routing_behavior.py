from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ai_agents_hub.api.schemas import ChatCompletionRequest
from ai_agents_hub.config import AppConfig
from ai_agents_hub.orchestration.orchestrator import Orchestrator
from ai_agents_hub.orchestration.specialist_router import SpecialistRoute


class StubLLMRouter:
    def __init__(self, answer_text: str = "Specialist answer.") -> None:
        self.answer_text = answer_text
        self.calls: list[dict[str, Any]] = []

    def list_models(self) -> list[str]:
        return [
            "gpt-5-nano-2025-08-07",
            "gpt-4o-mini",
            "gemini-2.5-flash",
        ]

    async def chat_completion(
        self,
        *,
        primary_model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        passthrough: dict[str, Any] | None = None,
        include_fallbacks: bool = True,
    ) -> tuple[str, Any]:
        self.calls.append(
            {
                "primary_model": primary_model,
                "messages": messages,
                "stream": stream,
                "passthrough": passthrough or {},
                "include_fallbacks": include_fallbacks,
            }
        )
        return primary_model, {"choices": [{"message": {"content": self.answer_text}}]}


@dataclass
class StubSpecialistRouter:
    domain: str
    confidence: float = 0.91
    reason: str = "test"
    orchestrator_model: str = "gpt-5-nano-2025-08-07"
    latest_seen_text: str = ""

    async def classify(self, latest_user_text: str) -> SpecialistRoute:
        self.latest_seen_text = latest_user_text
        return SpecialistRoute(
            domain=self.domain,
            confidence=self.confidence,
            reason=self.reason,
            orchestrator_model=self.orchestrator_model,
        )


class StubPromptManager:
    def get(self, key: str) -> str:
        prompts = {
            "orchestrator": "orchestrator prompt",
            "general": "general prompt",
            "health": "health prompt",
            "parenting": "parenting prompt",
            "relationship": "relationship prompt",
            "homelab": "homelab prompt",
            "personal_development": "personal development prompt",
        }
        return prompts.get(key, f"{key} prompt")


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": {
                "orchestrator": "gpt-5-nano-2025-08-07",
                "specialists": {
                    "general": "gpt-4o-mini",
                    "health": "gpt-4o-mini",
                    "parenting": "gpt-4o-mini",
                    "relationship": "gpt-4o-mini",
                    "homelab": "gemini-2.5-flash",
                    "personal_development": "gpt-4o-mini",
                },
            },
        }
    )


def _request(messages: list[dict[str, Any]]) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {
            "model": "ai-agents-hub",
            "messages": messages,
            "stream": False,
        }
    )


def _build_orchestrator(
    *,
    domain: str,
    answer_text: str = "Specialist answer.",
) -> tuple[Orchestrator, StubLLMRouter, StubSpecialistRouter]:
    cfg = _config()
    llm_router = StubLLMRouter(answer_text=answer_text)
    specialist_router = StubSpecialistRouter(domain=domain)
    orchestrator = Orchestrator(
        config=cfg,
        llm_router=llm_router,  # type: ignore[arg-type]
        specialist_router=specialist_router,  # type: ignore[arg-type]
        prompt_manager=StubPromptManager(),  # type: ignore[arg-type]
    )
    return orchestrator, llm_router, specialist_router


def test_non_general_response_has_specialist_prefix_and_uses_domain_model() -> None:
    orchestrator, llm_router, _specialist_router = _build_orchestrator(
        domain="health",
        answer_text="Do wrist extensor isometrics daily.",
    )
    request = _request(
        [{"role": "user", "content": "Can you help with tennis elbow rehab?"}]
    )
    response = asyncio.run(orchestrator.complete_non_stream(request))
    content = response["choices"][0]["message"]["content"]
    assert content.startswith("Answered by the health specialist.\n\n")
    assert "Do wrist extensor isometrics daily." in content
    assert llm_router.calls[0]["primary_model"] == "gpt-4o-mini"


def test_general_response_has_no_specialist_prefix() -> None:
    orchestrator, llm_router, _specialist_router = _build_orchestrator(
        domain="general",
        answer_text="Let's make a weekly plan.",
    )
    request = _request([{"role": "user", "content": "Help me plan my week."}])
    response = asyncio.run(orchestrator.complete_non_stream(request))
    content = response["choices"][0]["message"]["content"]
    assert not content.startswith("Answered by the")
    assert content.startswith("Let's make a weekly plan.")
    assert llm_router.calls[0]["primary_model"] == "gpt-4o-mini"


def test_routing_uses_latest_user_message_only() -> None:
    orchestrator, _llm_router, specialist_router = _build_orchestrator(
        domain="parenting",
        answer_text="Use calm boundaries and consistency.",
    )
    request = _request(
        [
            {"role": "user", "content": "I need infrastructure advice."},
            {"role": "assistant", "content": "Sure, tell me more."},
            {"role": "user", "content": "Actually, my son ignores instructions."},
        ]
    )
    asyncio.run(orchestrator.complete_non_stream(request))
    assert specialist_router.latest_seen_text == "Actually, my son ignores instructions."
