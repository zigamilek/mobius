from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ai_agents_hub.api.openai_compat import create_openai_router
from ai_agents_hub.config import AppConfig, load_config
from ai_agents_hub.diagnostics import diagnostics_payload, health_payload, readiness_payload
from ai_agents_hub.logging_setup import configure_logging, get_logger
from ai_agents_hub.orchestration.orchestrator import Orchestrator
from ai_agents_hub.orchestration.specialist_router import SpecialistRouter
from ai_agents_hub.prompts.manager import PromptManager
from ai_agents_hub.providers.litellm_router import LiteLLMRouter


def _ensure_runtime_dirs(config: AppConfig) -> None:
    config.specialists.prompts.directory.mkdir(parents=True, exist_ok=True)
    if config.logging.output in {"file", "both"}:
        config.logging.directory.mkdir(parents=True, exist_ok=True)


def _build_services(config: AppConfig) -> dict[str, Any]:
    _ensure_runtime_dirs(config)
    llm_router = LiteLLMRouter(config)
    specialist_router = SpecialistRouter(config=config, llm_router=llm_router)
    prompt_manager = PromptManager(config)
    orchestrator = Orchestrator(
        config=config,
        llm_router=llm_router,
        specialist_router=specialist_router,
        prompt_manager=prompt_manager,
    )
    return {
        "config": config,
        "specialist_router": specialist_router,
        "llm_router": llm_router,
        "prompt_manager": prompt_manager,
        "orchestrator": orchestrator,
    }


def create_app(config_path: str | Path | None = None) -> FastAPI:
    config = load_config(config_path)
    configure_logging(config.logging)
    logger = get_logger(__name__)
    logger.info("Initializing AI Agents Hub app...")

    services = _build_services(config)
    logger.info(
        "Services initialized (orchestrator_model=%s, prompts_dir=%s)",
        config.models.orchestrator,
        config.specialists.prompts.directory,
    )

    app = FastAPI(title="AI Agents Hub", version="0.1.0")
    app.state.services = services
    app.include_router(create_openai_router())

    endpoints = config.diagnostics.endpoints

    @app.get(endpoints.health, tags=["diagnostics"])
    async def healthz() -> dict[str, Any]:
        return health_payload()

    @app.get(endpoints.readiness, tags=["diagnostics"])
    async def readyz() -> dict[str, Any]:
        return readiness_payload(config)

    @app.get(endpoints.diagnostics, tags=["diagnostics"])
    async def diagnostics() -> dict[str, Any]:
        return diagnostics_payload(
            config=config,
            llm_router=services["llm_router"],
            prompt_manager=services["prompt_manager"],
        )

    logger.info(
        "Diagnostics routes active (%s, %s, %s)",
        endpoints.health,
        endpoints.readiness,
        endpoints.diagnostics,
    )

    return app


app = create_app()
