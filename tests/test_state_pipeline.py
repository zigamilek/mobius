from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from mobius.config import AppConfig
from mobius.state.models import (
    CheckinWrite,
    JournalWrite,
    MemoryWrite,
    StateContextSnapshot,
    StateDecision,
)
from mobius.state.pipeline import StatePipeline


@dataclass
class _FakeStatus:
    ready: bool = True


@dataclass
class _FakeStateStore:
    status: _FakeStatus = field(default_factory=_FakeStatus)


class _FakeLLMRouter:
    pass


class _FakeStorage:
    def fetch_context_snapshot(
        self, *, user_key: str | None, routed_domain: str
    ) -> StateContextSnapshot:
        return StateContextSnapshot()


class _FailingDecisionEngine:
    async def decide(
        self,
        *,
        user_text: str,
        assistant_text: str,
        routed_domain: str,
        context: StateContextSnapshot,
    ) -> StateDecision:
        return StateDecision(reason="state-model-unavailable", is_failure=True)


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
                "enabled": True,
                "database": {"dsn": "postgresql://user:pass@localhost:5432/mobius"},
            },
        }
    )


def test_state_pipeline_emits_footer_warning_when_decision_fails() -> None:
    cfg = _config()
    cfg.state.decision.on_failure = "footer_warning"
    pipeline = StatePipeline(
        config=cfg,
        state_store=_FakeStateStore(),  # type: ignore[arg-type]
        llm_router=_FakeLLMRouter(),  # type: ignore[arg-type]
    )
    pipeline.storage = _FakeStorage()  # type: ignore[assignment]
    pipeline.decision_engine = _FailingDecisionEngine()  # type: ignore[assignment]

    footer = asyncio.run(
        pipeline.process_turn(
            request_user="alice",
            session_key="s1",
            routed_domain="health",
            user_text="Today update",
            assistant_text="Thanks",
            used_model="gpt-5.2",
            request_payload={
                "model": "mobius",
                "messages": [{"role": "user", "content": "Today update"}],
            },
        )
    )
    assert "*State warning:*" in footer
    assert "state-model-unavailable" in footer
    assert "state/users/alice/" in footer


def test_state_pipeline_can_silence_failure_footer() -> None:
    cfg = _config()
    cfg.state.decision.on_failure = "silent"
    pipeline = StatePipeline(
        config=cfg,
        state_store=_FakeStateStore(),  # type: ignore[arg-type]
        llm_router=_FakeLLMRouter(),  # type: ignore[arg-type]
    )
    pipeline.storage = _FakeStorage()  # type: ignore[assignment]
    pipeline.decision_engine = _FailingDecisionEngine()  # type: ignore[assignment]

    footer = asyncio.run(
        pipeline.process_turn(
            request_user="alice",
            session_key="s1",
            routed_domain="health",
            user_text="Today update",
            assistant_text="Thanks",
            used_model="gpt-5.2",
            request_payload={
                "model": "mobius",
                "messages": [{"role": "user", "content": "Today update"}],
            },
        )
    )
    assert footer == ""


def test_grounding_guard_filters_memory_without_evidence_match() -> None:
    cfg = _config()
    pipeline = StatePipeline(
        config=cfg,
        state_store=_FakeStateStore(),  # type: ignore[arg-type]
        llm_router=_FakeLLMRouter(),  # type: ignore[arg-type]
    )
    decision = StateDecision(
        memory=MemoryWrite(
            domain="health",
            memory="I am lactose intolerant.",
            evidence="This quote does not exist in user text.",
        ),
        reason="state-model",
    )
    guarded = pipeline._apply_grounding_guards(
        decision=decision,
        user_text="Today we visited the museum.",
    )
    assert guarded.memory is None
    assert "memory-filtered-missing-evidence" in guarded.reason


def test_grounding_guard_rewrites_memory_to_evidence_in_facts_only_mode() -> None:
    cfg = _config()
    pipeline = StatePipeline(
        config=cfg,
        state_store=_FakeStateStore(),  # type: ignore[arg-type]
        llm_router=_FakeLLMRouter(),  # type: ignore[arg-type]
    )
    decision = StateDecision(
        memory=MemoryWrite(
            domain="health",
            memory="User has lactose intolerance.",
            evidence="I am lactose intolerant.",
        ),
        reason="state-model",
    )
    guarded = pipeline._apply_grounding_guards(
        decision=decision,
        user_text="I am lactose intolerant.",
    )
    assert guarded.memory is not None
    assert guarded.memory.memory == "I am lactose intolerant."


def test_grounding_guard_filters_ambiguous_memory_text() -> None:
    cfg = _config()
    pipeline = StatePipeline(
        config=cfg,
        state_store=_FakeStateStore(),  # type: ignore[arg-type]
        llm_router=_FakeLLMRouter(),  # type: ignore[arg-type]
    )
    decision = StateDecision(
        memory=MemoryWrite(
            domain="general",
            memory="It broke down.",
            evidence="It broke down.",
        ),
        reason="state-model",
    )
    guarded = pipeline._apply_grounding_guards(
        decision=decision,
        user_text="It broke down.",
    )
    assert guarded.memory is None
    assert "memory-filtered-ambiguous" in guarded.reason


def test_grounding_guard_filters_all_channels_when_ungrounded() -> None:
    cfg = _config()
    pipeline = StatePipeline(
        config=cfg,
        state_store=_FakeStateStore(),  # type: ignore[arg-type]
        llm_router=_FakeLLMRouter(),  # type: ignore[arg-type]
    )
    decision = StateDecision(
        checkin=CheckinWrite(
            domain="health",
            track_type="goal",
            title="Fat loss",
            summary="Lost 2kg this week",
            outcome="partial",
            wins=["Kept calories"],
            barriers=["Ate late"],
            next_actions=["Plan meals"],
            tags=[],
            evidence="This exact quote is missing",
        ),
        journal=JournalWrite(
            entry_ts=datetime.now(timezone.utc),
            title="Day log",
            body_md="I did work and training.",
            domain_hints=["health"],
            evidence="Another missing quote",
        ),
        memory=MemoryWrite(
            domain="health",
            memory="I am lactose intolerant.",
            evidence="Missing memory quote",
        ),
        reason="state-model",
    )
    guarded = pipeline._apply_grounding_guards(
        decision=decision,
        user_text="Today I planted 3 raspberry bushes.",
    )
    assert guarded.checkin is None
    assert guarded.journal is None
    assert guarded.memory is None
