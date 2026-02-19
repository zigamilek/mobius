from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mobius import __version__
from mobius.config import AppConfig
from mobius.prompts.manager import PromptManager
from mobius.providers.litellm_router import LiteLLMRouter
from mobius.state.store import StateStore


def health_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def readiness_payload(
    config: AppConfig, state_store: StateStore | None = None
) -> dict[str, Any]:
    openai_ready = bool(config.providers.openai.api_key)
    gemini_ready = bool(config.providers.gemini.api_key)
    state_ready = state_store.status.ready if state_store is not None else True
    if config.state.enabled and state_store is None:
        state_ready = False
    return {
        "status": "ready"
        if ((openai_ready or gemini_ready) and state_ready)
        else "degraded",
        "providers": {"openai": openai_ready, "gemini": gemini_ready},
        "state": {
            "enabled": config.state.enabled,
            "ready": state_ready,
        },
    }


def diagnostics_payload(
    config: AppConfig,
    llm_router: LiteLLMRouter,
    prompt_manager: PromptManager | None = None,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    prompt_config: dict[str, Any] = {
        "directory": str(config.specialists.prompts_directory),
        "auto_reload": config.specialists.auto_reload,
    }
    if prompt_manager is not None:
        prompt_config["files"] = prompt_manager.resolved_prompt_files()

    state_runtime = state_store.status.as_dict() if state_store is not None else None

    return {
        "service": "mobius",
        "version": __version__,
        "public_model": config.api.public_model_id,
        "models": llm_router.list_models(),
        "config": {
            "api": {
                "public_model_id": config.api.public_model_id,
                "allow_provider_model_passthrough": config.api.allow_provider_model_passthrough,
                "attribution": {
                    "enabled": config.api.attribution.enabled,
                    "include_model": config.api.attribution.include_model,
                    "include_general": config.api.attribution.include_general,
                    "template": config.api.attribution.template,
                },
            },
            "orchestrator_model": config.models.orchestrator,
            "runtime": {
                "inject_current_timestamp": config.runtime.inject_current_timestamp,
                "timezone": config.runtime.timezone,
                "include_timestamp_in_routing": config.runtime.include_timestamp_in_routing,
            },
            "state": {
                "enabled": config.state.enabled,
                "database": {
                    "auto_migrate": config.state.database.auto_migrate,
                    "min_schema_version": config.state.database.min_schema_version,
                    "max_schema_version": config.state.database.max_schema_version,
                    "connect_timeout_seconds": config.state.database.connect_timeout_seconds,
                },
                "projection": {
                    "mode": config.state.projection.mode,
                    "output_directory": str(config.state.projection.output_directory),
                },
                "user_scope": {
                    "policy": config.state.user_scope.policy,
                    "anonymous_user_key": config.state.user_scope.anonymous_user_key,
                },
                "decision": {
                    "enabled": config.state.decision.enabled,
                    "model": config.state.decision.model,
                    "include_fallbacks": config.state.decision.include_fallbacks,
                    "facts_only": config.state.decision.facts_only,
                    "strict_grounding": config.state.decision.strict_grounding,
                    "max_user_chars": config.state.decision.max_user_chars,
                    "max_assistant_chars": config.state.decision.max_assistant_chars,
                    "max_json_retries": config.state.decision.max_json_retries,
                    "on_failure": config.state.decision.on_failure,
                },
                "checkin": {
                    "enabled": config.state.checkin.enabled,
                    "max_wins": config.state.checkin.max_wins,
                    "max_barriers": config.state.checkin.max_barriers,
                    "max_next_actions": config.state.checkin.max_next_actions,
                },
                "journal": {
                    "enabled": config.state.journal.enabled,
                    "include_assistant_excerpt": config.state.journal.include_assistant_excerpt,
                    "max_assistant_excerpt_chars": config.state.journal.max_assistant_excerpt_chars,
                    "max_domain_hints": config.state.journal.max_domain_hints,
                },
                "memory": {
                    "enabled": config.state.memory.enabled,
                    "max_tags": config.state.memory.max_tags,
                    "semantic_merge": {
                        "enabled": config.state.memory.semantic_merge.enabled,
                        "embedding_model": config.state.memory.semantic_merge.embedding_model,
                        "verification_model": config.state.memory.semantic_merge.verification_model,
                        "include_fallbacks": config.state.memory.semantic_merge.include_fallbacks,
                        "candidate_limit": config.state.memory.semantic_merge.candidate_limit,
                        "max_candidate_text_chars": config.state.memory.semantic_merge.max_candidate_text_chars,
                        "max_json_retries": config.state.memory.semantic_merge.max_json_retries,
                        "max_distance": config.state.memory.semantic_merge.max_distance,
                    },
                },
                "retrieval": {
                    "active_tracks_limit": config.state.retrieval.active_tracks_limit,
                    "recent_checkins_limit": config.state.retrieval.recent_checkins_limit,
                    "recent_journal_entries_limit": config.state.retrieval.recent_journal_entries_limit,
                    "recent_memory_cards_limit": config.state.retrieval.recent_memory_cards_limit,
                },
                "runtime": state_runtime,
            },
            "prompts": prompt_config,
            "logging": {
                "level": config.logging.level,
                "output": config.logging.output,
                "directory": str(config.logging.directory),
                "filename": config.logging.filename,
            },
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
