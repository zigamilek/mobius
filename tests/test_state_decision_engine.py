from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from mobius.config import AppConfig
from mobius.state.decision_engine import StateDecisionEngine
from mobius.state.models import StateContextSnapshot


class FailingLLMRouter:
    async def chat_completion(
        self,
        *,
        primary_model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        passthrough: dict[str, Any] | None = None,
        include_fallbacks: bool = True,
    ) -> tuple[str, Any]:
        raise RuntimeError("forced failure for fallback test")


class StubLLMRouter:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def chat_completion(
        self,
        *,
        primary_model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        passthrough: dict[str, Any] | None = None,
        include_fallbacks: bool = True,
    ) -> tuple[str, Any]:
        return primary_model, {"choices": [{"message": {"content": json.dumps(self.payload)}}]}


class SequencedLLMRouter:
    def __init__(self, payloads: list[str]) -> None:
        self.payloads = payloads
        self.calls = 0

    async def chat_completion(
        self,
        *,
        primary_model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        passthrough: dict[str, Any] | None = None,
        include_fallbacks: bool = True,
    ) -> tuple[str, Any]:
        idx = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        return primary_model, {"choices": [{"message": {"content": self.payloads[idx]}}]}


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "server": {"api_keys": ["dev-key"]},
            "providers": {
                "openai": {"api_key": "test-openai-key"},
                "gemini": {"api_key": "test-gemini-key"},
            },
            "models": {"orchestrator": "gpt-5-nano-2025-08-07", "fallbacks": []},
            "api": {"public_model_id": "mobius"},
            "specialists": {
                "prompts_directory": "./system_prompts",
                "orchestrator_prompt_file": "_orchestrator.md",
                "by_domain": {
                    "general": {"model": "gpt-5.2", "prompt_file": "general.md"},
                    "health": {"model": "gpt-5.2", "prompt_file": "health.md"},
                    "parenting": {"model": "gpt-5.2", "prompt_file": "parenting.md"},
                    "relationships": {"model": "gpt-5.2", "prompt_file": "relationships.md"},
                    "homelab": {"model": "gpt-5.2", "prompt_file": "homelab.md"},
                    "personal_development": {
                        "model": "gpt-5.2",
                        "prompt_file": "personal_development.md",
                    },
                },
            },
            "state": {
                "enabled": False,
                "decision": {"enabled": True},
                "checkin": {"enabled": True},
                "memory": {"enabled": True},
            },
        }
    )


def test_model_failure_returns_no_writes() -> None:
    cfg = _config()
    engine = StateDecisionEngine(
        config=cfg,
        llm_router=FailingLLMRouter(),  # type: ignore[arg-type]
    )
    decision = asyncio.run(
        engine.decide(
            user_text="Today I decided I'll finally lose fat.",
            assistant_text="Great, let's define a plan.",
            routed_domain="health",
            context=StateContextSnapshot(),
        )
    )
    assert decision.checkin is None
    assert decision.memory is None
    assert decision.reason == "state-model-unavailable"
    assert decision.is_failure is True


def test_model_json_can_trigger_checkin_and_memory_writes() -> None:
    cfg = _config()
    payload = {
        "checkin": {
            "write": True,
            "domain": "health",
            "track_type": "goal",
            "title": "Lose fat",
            "summary": "Started focused fat-loss plan.",
            "outcome": "partial",
            "wins": ["Committed to meal prep"],
            "barriers": ["Late-night snacking"],
            "next_actions": ["Prepare tomorrow meals in advance"],
            "tags": ["fat_loss"],
            "evidence": "Today I decided I'll finally lose fat.",
            "reason": "explicit ongoing goal with accountability intent",
        },
        "memory": {
            "write": True,
            "domain": "health",
            "memory": "I want to lose fat.",
            "evidence": "Today I decided I'll finally lose fat.",
            "reason": "durable long-term commitment",
        },
        "reason": "explicit_goal_signal",
    }
    engine = StateDecisionEngine(
        config=cfg,
        llm_router=StubLLMRouter(payload),  # type: ignore[arg-type]
    )
    decision = asyncio.run(
        engine.decide(
            user_text="Today I decided I'll finally lose fat.",
            assistant_text="Great, let's define a plan.",
            routed_domain="health",
            context=StateContextSnapshot(),
        )
    )
    assert decision.checkin is not None
    assert decision.memory is not None
    assert decision.reason == "explicit_goal_signal"
    assert decision.checkin_reason == "explicit ongoing goal with accountability intent"
    assert decision.memory_reason == "durable long-term commitment"


def test_invalid_json_is_retried_and_second_attempt_succeeds() -> None:
    cfg = _config()
    cfg.state.decision.max_json_retries = 1
    valid_payload = {
        "checkin": {
            "write": True,
            "domain": "health",
            "track_type": "goal",
            "title": "Lose fat",
            "summary": "Progressing.",
            "outcome": "partial",
            "wins": [],
            "barriers": [],
            "next_actions": [],
            "tags": [],
            "evidence": "fat-loss progress",
            "reason": "ongoing progress update",
        },
        "memory": {
            "write": False,
            "domain": "",
            "memory": "",
            "evidence": "",
            "reason": "not durable",
        },
        "reason": "checkin_only",
    }
    router = SequencedLLMRouter(["not json", json.dumps(valid_payload)])
    engine = StateDecisionEngine(
        config=cfg,
        llm_router=router,  # type: ignore[arg-type]
    )
    decision = asyncio.run(
        engine.decide(
            user_text="Quick update on my fat-loss progress.",
            assistant_text="Thanks.",
            routed_domain="health",
            context=StateContextSnapshot(),
        )
    )
    assert router.calls == 2
    assert decision.checkin is not None
    assert decision.memory is None


def _base_decision_payload() -> dict[str, Any]:
    return {
        "checkin": {
            "write": False,
            "domain": "",
            "track_type": "goal",
            "title": "",
            "summary": "",
            "outcome": "note",
            "wins": [],
            "barriers": [],
            "next_actions": [],
            "tags": [],
            "evidence": "",
            "reason": "no ongoing coaching signal",
        },
        "memory": {
            "write": False,
            "domain": "",
            "memory": "",
            "evidence": "",
            "reason": "no durable memory signal",
        },
        "reason": "matrix",
    }


@pytest.mark.parametrize(
    ("enabled_channels", "query"),
    [
        ({"memory"}, "I am lactose intolerant."),
        (
            {"checkin"},
            "Fat-loss check-in: this week I trained 4 times and broke nutrition twice.",
        ),
        (
            {"memory", "checkin"},
            "Today I decided to quit smoking for good; this is day 1 and I want coaching.",
        ),
    ],
)
def test_positive_write_combination_matrix(
    enabled_channels: set[str], query: str
) -> None:
    cfg = _config()
    payload = _base_decision_payload()
    if "checkin" in enabled_channels:
        payload["checkin"] = {
            "write": True,
            "domain": "health",
            "track_type": "goal",
            "title": "Health check-in",
            "summary": query,
            "outcome": "partial",
            "wins": [],
            "barriers": [],
            "next_actions": [],
            "tags": [],
            "evidence": query,
            "reason": "check-in signal present",
        }
    if "memory" in enabled_channels:
        payload["memory"] = {
            "write": True,
            "domain": "health",
            "memory": query,
            "evidence": query,
            "reason": "durable preference or commitment",
        }
    engine = StateDecisionEngine(
        config=cfg,
        llm_router=StubLLMRouter(payload),  # type: ignore[arg-type]
    )
    decision = asyncio.run(
        engine.decide(
            user_text=query,
            assistant_text="Thanks for sharing.",
            routed_domain="health",
            context=StateContextSnapshot(),
        )
    )
    assert (decision.checkin is not None) is ("checkin" in enabled_channels)
    assert (decision.memory is not None) is ("memory" in enabled_channels)


def test_negative_matrix_generic_qna_can_return_no_writes() -> None:
    cfg = _config()
    payload = _base_decision_payload()
    payload["reason"] = "generic_qna_no_state"
    engine = StateDecisionEngine(
        config=cfg,
        llm_router=StubLLMRouter(payload),  # type: ignore[arg-type]
    )
    decision = asyncio.run(
        engine.decide(
            user_text="How should I prune currant bushes?",
            assistant_text="I can help with pruning steps.",
            routed_domain="health",
            context=StateContextSnapshot(),
        )
    )
    assert decision.checkin is None
    assert decision.memory is None


def test_prompt_contains_examples() -> None:
    text = StateDecisionEngine._system_prompt()
    assert "Canonical positive examples:" in text
    assert "Canonical negative examples:" in text
    assert "Today I planted 3 raspberry bushes" in text
