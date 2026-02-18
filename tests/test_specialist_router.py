from __future__ import annotations

import asyncio
from typing import Any

from mobius.config import AppConfig
from mobius.orchestration.specialist_router import SpecialistRoute, SpecialistRouter


class StubLLMRouter:
    def __init__(
        self,
        outputs: list[str],
        model_name: str = "gpt-5-nano-2025-08-07",
        fail_for_models: set[str] | None = None,
    ) -> None:
        self.outputs = outputs
        self.model_name = model_name
        self.fail_for_models = fail_for_models or set()
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
        self.calls.append(
            {
                "primary_model": primary_model,
                "messages": messages,
                "stream": stream,
                "passthrough": passthrough or {},
                "include_fallbacks": include_fallbacks,
            }
        )
        if primary_model in self.fail_for_models:
            raise RuntimeError(f"forced-failure:{primary_model}")
        content = self.outputs.pop(0)
        return self.model_name, {"choices": [{"message": {"content": content}}]}


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "server": {"api_keys": []},
            "providers": {
                "openai": {"api_key": "test-openai-key"},
                "gemini": {
                    "api_key": "test-gemini-key",
                    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                },
            },
            "models": {
                "orchestrator": "gpt-5-nano-2025-08-07",
                "fallbacks": [],
            },
            "api": {
                "public_model_id": "mobius",
                "allow_provider_model_passthrough": False,
            },
            "specialists": {
                "prompts_directory": "./system_prompts",
                "orchestrator_prompt_file": "_orchestrator.md",
                "by_domain": {
                    "general": {"model": "gpt-4o-mini", "prompt_file": "general.md"},
                    "health": {"model": "gpt-4o-mini", "prompt_file": "health.md"},
                    "parenting": {
                        "model": "gpt-4o-mini",
                        "prompt_file": "parenting.md",
                    },
                    "relationships": {
                        "model": "gpt-4o-mini",
                        "prompt_file": "relationships.md",
                    },
                    "homelab": {"model": "gemini-2.5-flash", "prompt_file": "homelab.md"},
                    "personal_development": {
                        "model": "gpt-4o-mini",
                        "prompt_file": "personal_development.md",
                    },
                },
            },
        }
    )


def _print_route(query: str, route: SpecialistRoute) -> None:
    print(f"QUERY: {query}")
    print(f"ROUTED SPECIALIST: {route.domain}")
    print(f"CONFIDENCE: {route.confidence:.2f}")
    print("---")


def test_orchestrator_routes_to_health_domain() -> None:
    query = "Can you help with tennis elbow rehab?"
    llm = StubLLMRouter(
        outputs=[
            '{"specialist":"health","confidence":0.92,"reason":"rehabilitation and injury context"}'
        ]
    )
    router = SpecialistRouter(config=_config(), llm_router=llm)  # type: ignore[arg-type]
    result = asyncio.run(router.classify(query))
    _print_route(query, result)
    assert result.domain == "health"
    assert result.confidence == 0.92
    assert result.orchestrator_model == "gpt-5-nano-2025-08-07"
    assert llm.calls[0]["include_fallbacks"] is False
    assert llm.calls[0]["passthrough"] == {}


def test_orchestrator_falls_back_to_general_for_invalid_specialist() -> None:
    query = "How should I budget this month?"
    llm = StubLLMRouter(
        outputs=[
            '{"specialist":"finance","confidence":0.9,"reason":"not supported"}'
        ]
    )
    router = SpecialistRouter(config=_config(), llm_router=llm)  # type: ignore[arg-type]
    result = asyncio.run(router.classify(query))
    _print_route(query, result)
    assert result.domain == "general"
    assert result.reason == "invalid-specialist"


def test_orchestrator_falls_back_to_general_for_invalid_json() -> None:
    query = "I need advice"
    llm = StubLLMRouter(outputs=["not json"])
    router = SpecialistRouter(config=_config(), llm_router=llm)  # type: ignore[arg-type]
    result = asyncio.run(router.classify(query))
    _print_route(query, result)
    assert result.domain == "general"
    assert result.reason == "invalid-specialist"


def test_orchestrator_tries_openai_prefix_for_gpt_models() -> None:
    query = "How can I improve my Proxmox backups?"
    llm = StubLLMRouter(
        outputs=[
            '{"specialist":"homelab","confidence":0.81,"reason":"infrastructure topic"}'
        ],
        fail_for_models={"gpt-5-nano-2025-08-07"},
    )
    router = SpecialistRouter(config=_config(), llm_router=llm)  # type: ignore[arg-type]
    result = asyncio.run(router.classify(query))
    _print_route(query, result)
    assert result.domain == "homelab"
    assert [call["primary_model"] for call in llm.calls] == [
        "gpt-5-nano-2025-08-07",
        "openai/gpt-5-nano-2025-08-07",
    ]


def test_routing_does_not_include_timestamp_by_default() -> None:
    query = "Help me structure my weekly goals."
    llm = StubLLMRouter(
        outputs=[
            '{"specialist":"personal_development","confidence":0.84,"reason":"goals and planning"}'
        ]
    )
    router = SpecialistRouter(config=_config(), llm_router=llm)  # type: ignore[arg-type]
    asyncio.run(router.classify(query))
    system_prompt = str(llm.calls[0]["messages"][0]["content"])
    assert "Current timestamp:" not in system_prompt


def test_routing_can_include_timestamp_when_enabled() -> None:
    query = "Should I call my parents more often?"
    llm = StubLLMRouter(
        outputs=[
            '{"specialist":"relationships","confidence":0.78,"reason":"family relationship context"}'
        ]
    )
    config = _config()
    config.runtime.include_timestamp_in_routing = True
    router = SpecialistRouter(config=config, llm_router=llm)  # type: ignore[arg-type]
    asyncio.run(router.classify(query))
    system_prompt = str(llm.calls[0]["messages"][0]["content"])
    assert "Current timestamp:" in system_prompt
