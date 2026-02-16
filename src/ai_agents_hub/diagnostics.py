from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ai_agents_hub.config import AppConfig
from ai_agents_hub.memory.store import MemoryStore
from ai_agents_hub.prompts.manager import PromptManager
from ai_agents_hub.providers.litellm_router import LiteLLMRouter


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
    memory_store: MemoryStore,
    llm_router: LiteLLMRouter,
    prompt_manager: PromptManager | None = None,
) -> dict[str, Any]:
    prompt_config: dict[str, Any] = {
        "directory": str(config.specialists.prompts.directory),
        "auto_reload": config.specialists.prompts.auto_reload,
    }
    if prompt_manager is not None:
        prompt_config["files"] = prompt_manager.resolved_prompt_files()

    return {
        "service": "ai-agents-hub",
        "public_model": config.openai_compat.master_model_id,
        "models": llm_router.list_models(),
        "memory": memory_store.index.stats(),
        "config": {
            "memory_root": str(config.memory.root_path),
            "journal_vault": str(config.journal.obsidian_vault_path),
            "web_search_enabled": config.tools.web_search,
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
