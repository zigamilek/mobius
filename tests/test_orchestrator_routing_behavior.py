from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from mobius.api.schemas import ChatCompletionRequest
from mobius.config import AppConfig
from mobius.orchestration.orchestrator import Orchestrator
from mobius.orchestration.specialist_router import SpecialistRoute


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
    latest_seen_current_domain: str | None = None
    latest_seen_recent_domains: list[str] = field(default_factory=list)
    classify_calls: int = 0

    async def classify(
        self,
        latest_user_text: str,
        *,
        current_domain: str | None = None,
        recent_domains: list[str] | None = None,
    ) -> SpecialistRoute:
        self.classify_calls += 1
        self.latest_seen_text = latest_user_text
        self.latest_seen_current_domain = current_domain
        self.latest_seen_recent_domains = list(recent_domains or [])
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
            "relationships": "relationships prompt",
            "homelab": "homelab prompt",
            "personal_development": "personal development prompt",
        }
        return prompts.get(key, f"{key} prompt")


class StubStatePipeline:
    def __init__(
        self,
        *,
        context_text: str = "",
        footer_text: str = "",
    ) -> None:
        self.context_text = context_text
        self.footer_text = footer_text
        self.context_calls: list[dict[str, Any]] = []
        self.process_calls: list[dict[str, Any]] = []

    def context_for_prompt(self, *, user_key: str | None, routed_domain: str) -> str:
        self.context_calls.append({"user_key": user_key, "routed_domain": routed_domain})
        return self.context_text

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
        self.process_calls.append(
            {
                "request_user": request_user,
                "session_key": session_key,
                "routed_domain": routed_domain,
                "user_text": user_text,
                "assistant_text": assistant_text,
                "used_model": used_model,
                "request_payload": request_payload,
            }
        )
        return self.footer_text


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
                    "general": {
                        "model": "gpt-4o-mini",
                        "prompt_file": "general.md",
                        "display_name": "The Generalist",
                    },
                    "health": {
                        "model": "gpt-4o-mini",
                        "prompt_file": "health.md",
                        "display_name": "The Healer",
                    },
                    "parenting": {
                        "model": "gpt-4o-mini",
                        "prompt_file": "parenting.md",
                        "display_name": "The Coach",
                    },
                    "relationships": {
                        "model": "gpt-4o-mini",
                        "prompt_file": "relationships.md",
                        "display_name": "The Mediator",
                    },
                    "homelab": {
                        "model": "gemini-2.5-flash",
                        "prompt_file": "homelab.md",
                        "display_name": "The Builder",
                    },
                    "personal_development": {
                        "model": "gpt-4o-mini",
                        "prompt_file": "personal_development.md",
                        "display_name": "The Mentor",
                    },
                },
            },
        }
    )


def _request(
    messages: list[dict[str, Any]],
    *,
    user: str | None = None,
    session_id: str | None = None,
) -> ChatCompletionRequest:
    payload: dict[str, Any] = {
        "model": "mobius",
        "messages": messages,
        "stream": False,
    }
    if user is not None:
        payload["user"] = user
    if session_id is not None:
        payload["session_id"] = session_id
    return ChatCompletionRequest.model_validate(payload)


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
    assert content.startswith(
        "*Answered by The Healer (the health specialist) using gpt-4o-mini model.*\n\n"
    )
    assert "Do wrist extensor isometrics daily." in content
    assert llm_router.calls[0]["primary_model"] == "gpt-4o-mini"
    system_prompt = str(llm_router.calls[0]["messages"][0]["content"])
    assert "Current timestamp:" in system_prompt


def test_general_response_has_no_specialist_prefix() -> None:
    orchestrator, llm_router, _specialist_router = _build_orchestrator(
        domain="general",
        answer_text="Let's make a weekly plan.",
    )
    request = _request([{"role": "user", "content": "Help me plan my week."}])
    response = asyncio.run(orchestrator.complete_non_stream(request))
    content = response["choices"][0]["message"]["content"]
    assert not content.startswith("*Answered by ")
    assert content.startswith("Let's make a weekly plan.")
    assert llm_router.calls[0]["primary_model"] == "gpt-4o-mini"
    system_prompt = str(llm_router.calls[0]["messages"][0]["content"])
    assert "Current timestamp:" in system_prompt


def test_attribution_can_disable_model_suffix() -> None:
    cfg = _config()
    cfg.api.attribution.include_model = False
    llm_router = StubLLMRouter(answer_text="Do rehab exercises.")
    specialist_router = StubSpecialistRouter(domain="health")
    orchestrator = Orchestrator(
        config=cfg,
        llm_router=llm_router,  # type: ignore[arg-type]
        specialist_router=specialist_router,  # type: ignore[arg-type]
        prompt_manager=StubPromptManager(),  # type: ignore[arg-type]
    )
    request = _request(
        [{"role": "user", "content": "Can you help with tennis elbow rehab?"}]
    )
    response = asyncio.run(orchestrator.complete_non_stream(request))
    content = str(response["choices"][0]["message"]["content"] or "")
    assert content.startswith("*Answered by The Healer (the health specialist).*\n\n")
    assert "using gpt-4o-mini model" not in content


def test_attribution_can_be_disabled() -> None:
    cfg = _config()
    cfg.api.attribution.enabled = False
    llm_router = StubLLMRouter(answer_text="Do rehab exercises.")
    specialist_router = StubSpecialistRouter(domain="health")
    orchestrator = Orchestrator(
        config=cfg,
        llm_router=llm_router,  # type: ignore[arg-type]
        specialist_router=specialist_router,  # type: ignore[arg-type]
        prompt_manager=StubPromptManager(),  # type: ignore[arg-type]
    )
    request = _request(
        [{"role": "user", "content": "Can you help with tennis elbow rehab?"}]
    )
    response = asyncio.run(orchestrator.complete_non_stream(request))
    content = str(response["choices"][0]["message"]["content"] or "")
    assert not content.startswith("*Answered by ")
    assert content.startswith("Do rehab exercises.")


def test_timestamp_can_be_disabled_for_orchestrated_response_prompt() -> None:
    cfg = _config()
    cfg.runtime.inject_current_timestamp = False
    llm_router = StubLLMRouter(answer_text="ok")
    specialist_router = StubSpecialistRouter(domain="general")
    orchestrator = Orchestrator(
        config=cfg,
        llm_router=llm_router,  # type: ignore[arg-type]
        specialist_router=specialist_router,  # type: ignore[arg-type]
        prompt_manager=StubPromptManager(),  # type: ignore[arg-type]
    )
    request = _request([{"role": "user", "content": "Help me plan my day."}])
    asyncio.run(orchestrator.complete_non_stream(request))
    system_prompt = str(llm_router.calls[0]["messages"][0]["content"])
    assert "Current timestamp:" not in system_prompt


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


def test_routing_passes_session_domain_history_to_classifier() -> None:
    orchestrator, llm_router, specialist_router = _build_orchestrator(
        domain="homelab",
        answer_text="Use VLAN segmentation and nightly backups.",
    )
    first_request = _request(
        [{"role": "user", "content": "Can you help with my homelab network?"}],
        session_id="chat-1",
    )
    asyncio.run(orchestrator.complete_non_stream(first_request))
    assert specialist_router.classify_calls == 1
    assert specialist_router.latest_seen_current_domain is None
    assert specialist_router.latest_seen_recent_domains == []

    # Follow-up turn should still call classifier, but with continuity context.
    specialist_router.domain = "homelab"
    followup_request = _request(
        [
            {"role": "user", "content": "Can you help with my homelab network?"},
            {"role": "assistant", "content": "Previous answer from Mobius."},
            {"role": "user", "content": "What should I improve next?"},
        ],
        session_id="chat-1",
    )
    response = asyncio.run(orchestrator.complete_non_stream(followup_request))
    content = str(response["choices"][0]["message"]["content"] or "")
    assert content.startswith(
        "*Answered by The Builder (the homelab specialist) using gemini-2.5-flash model.*\n\n"
    )
    assert llm_router.calls[-1]["primary_model"] == "gemini-2.5-flash"
    assert specialist_router.classify_calls == 2
    assert specialist_router.latest_seen_current_domain == "homelab"
    assert specialist_router.latest_seen_recent_domains == ["homelab"]


def test_sticky_session_resets_when_request_is_first_user_prompt() -> None:
    orchestrator, llm_router, specialist_router = _build_orchestrator(
        domain="health",
        answer_text="Use snapshots before patch windows.",
    )
    first_request = _request(
        [{"role": "user", "content": "My elbow hurts after tennis."}],
        session_id="chat-2",
    )
    asyncio.run(orchestrator.complete_non_stream(first_request))
    assert specialist_router.classify_calls == 1

    # New session turn (single user prompt) must reset sticky state.
    specialist_router.domain = "homelab"
    reset_request = _request(
        [{"role": "user", "content": "Now help with Proxmox backups."}],
        session_id="chat-2",
    )
    response = asyncio.run(orchestrator.complete_non_stream(reset_request))
    content = str(response["choices"][0]["message"]["content"] or "")
    assert content.startswith(
        "*Answered by The Builder (the homelab specialist) using gemini-2.5-flash model.*\n\n"
    )
    assert llm_router.calls[-1]["primary_model"] == "gemini-2.5-flash"
    assert specialist_router.latest_seen_text == "Now help with Proxmox backups."
    assert specialist_router.latest_seen_current_domain is None
    assert specialist_router.latest_seen_recent_domains == []
    assert specialist_router.classify_calls == 2


def test_state_context_and_footer_are_injected_when_pipeline_is_present() -> None:
    cfg = _config()
    llm_router = StubLLMRouter(answer_text="Core specialist answer.")
    specialist_router = StubSpecialistRouter(domain="health")
    state_pipeline = StubStatePipeline(
        context_text="Active tracks:\n- Lose fat [health] status=active",
        footer_text=(
            "*State writes:*\n"
            "- check-in: `state/users/alex/checkins/health-lose-fat.md` (applied)"
        ),
    )
    orchestrator = Orchestrator(
        config=cfg,
        llm_router=llm_router,  # type: ignore[arg-type]
        specialist_router=specialist_router,  # type: ignore[arg-type]
        prompt_manager=StubPromptManager(),  # type: ignore[arg-type]
        state_pipeline=state_pipeline,  # type: ignore[arg-type]
    )
    request = _request(
        [{"role": "user", "content": "Today I decided I'll finally lose fat."}],
        user="alex",
    )
    response = asyncio.run(orchestrator.complete_non_stream(request))
    content = str(response["choices"][0]["message"]["content"] or "")
    assert "Core specialist answer." in content
    assert "*State writes:*" in content
    assert "state/users/alex/checkins/health-lose-fat.md" in content
    system_prompt = str(llm_router.calls[0]["messages"][0]["content"])
    assert "User state context (deterministic snapshot):" in system_prompt
    assert "Active tracks:" in system_prompt
    assert len(state_pipeline.context_calls) == 1
    assert len(state_pipeline.process_calls) == 1


def test_build_system_prompt_tolerates_none_state_context() -> None:
    orchestrator, _llm_router, _specialist_router = _build_orchestrator(
        domain="general",
        answer_text="ok",
    )
    prompt = orchestrator._build_system_prompt([], None)  # type: ignore[arg-type]
    assert isinstance(prompt, str)
    assert "general prompt" in prompt
