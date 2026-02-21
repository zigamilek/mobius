from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mobius import __version__
from mobius.config import AppConfig
from mobius.prompts.manager import PromptManager
from mobius.providers.litellm_router import LiteLLMRouter


def health_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def readiness_payload(config: AppConfig) -> dict[str, Any]:
    openai_ready = bool(config.providers.openai.api_key)
    gemini_ready = bool(config.providers.gemini.api_key)
    return {
        "status": "ready" if (openai_ready or gemini_ready) else "degraded",
        "providers": {"openai": openai_ready, "gemini": gemini_ready},
    }


def diagnostics_payload(
    config: AppConfig,
    llm_router: LiteLLMRouter,
    prompt_manager: PromptManager | None = None,
) -> dict[str, Any]:
    prompt_config: dict[str, Any] = {
        "directory": str(config.specialists.prompts_directory),
        "auto_reload": config.specialists.auto_reload,
    }
    if prompt_manager is not None:
        prompt_config["files"] = prompt_manager.resolved_prompt_files()

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
