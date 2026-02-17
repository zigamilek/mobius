from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional at runtime
    load_dotenv = None  # type: ignore[assignment]


ENV_REF_PATTERN = re.compile(r"^\$\{ENV:([A-Z0-9_]+)\}$")


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    api_keys: list[str | None] = Field(default_factory=list)


class ProviderConfig(BaseModel):
    api_key: str | None = None
    base_url: str | None = None


class ProvidersConfig(BaseModel):
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(
        default_factory=lambda: ProviderConfig(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
        )
    )


class SpecialistModels(BaseModel):
    general: str = "gpt-4o-mini"
    health: str = "gpt-4o-mini"
    parenting: str = "gpt-4o-mini"
    relationship: str = "gpt-4o-mini"
    homelab: str = "gemini-2.5-flash"
    personal_development: str = "gpt-4o-mini"

    def by_domain(self, domain: str) -> str:
        normalized = domain.strip().lower().replace("-", "_")
        return getattr(self, normalized, self.general)


class ModelsConfig(BaseModel):
    orchestrator: str = "gpt-5-nano-2025-08-07"
    specialists: SpecialistModels = Field(default_factory=SpecialistModels)
    fallbacks: list[str] = Field(default_factory=list)

class SpecialistPromptFilesConfig(BaseModel):
    orchestrator: str = "orchestrator.md"
    general: str = "general.md"
    health: str = "health.md"
    parenting: str = "parenting.md"
    relationship: str = "relationship.md"
    homelab: str = "homelab.md"
    personal_development: str = "personal_development.md"


class SpecialistPromptsConfig(BaseModel):
    directory: Path = Path("/etc/ai-agents-hub/prompts/specialists")
    auto_reload: bool = True
    files: SpecialistPromptFilesConfig = Field(default_factory=SpecialistPromptFilesConfig)


class SpecialistsConfig(BaseModel):
    prompts: SpecialistPromptsConfig = Field(default_factory=SpecialistPromptsConfig)


class DiagnosticEndpointsConfig(BaseModel):
    health: str = "/healthz"
    readiness: str = "/readyz"
    diagnostics: str = "/diagnostics"


class DiagnosticsConfig(BaseModel):
    enabled: bool = True
    endpoints: DiagnosticEndpointsConfig = Field(
        default_factory=DiagnosticEndpointsConfig
    )


class OpenAICompatibilityConfig(BaseModel):
    public_model_id: str = "ai-agents-hub"
    allow_provider_model_passthrough: bool = False


class LoggingConfig(BaseModel):
    level: Literal["ERROR", "WARNING", "INFO", "DEBUG", "TRACE"] = "INFO"
    output: Literal["console", "file", "both"] = "console"
    directory: Path = Path("./data/logs")
    filename: str = "ai-agents-hub.log"
    daily_rotation: bool = True
    retention_days: int = 14
    utc: bool = True
    include_payloads: bool = False


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    openai_compatibility: OpenAICompatibilityConfig = Field(
        default_factory=OpenAICompatibilityConfig
    )
    specialists: SpecialistsConfig = Field(default_factory=SpecialistsConfig)
    diagnostics: DiagnosticsConfig = Field(default_factory=DiagnosticsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def _expand_env_refs(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _expand_env_refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_refs(v) for v in value]
    if isinstance(value, str):
        match = ENV_REF_PATTERN.match(value.strip())
        if match:
            return os.getenv(match.group(1))
    return value


def _maybe_load_dotenv() -> None:
    disabled = os.getenv("AI_AGENTS_HUB_DISABLE_DOTENV", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if disabled or load_dotenv is None:
        return
    dotenv_path = Path(os.getenv("AI_AGENTS_HUB_DOTENV_PATH", ".env"))
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path, override=False)


def load_config(config_path: str | Path | None = None) -> AppConfig:
    _maybe_load_dotenv()
    path = (
        Path(config_path)
        if config_path
        else Path(os.getenv("AI_AGENTS_HUB_CONFIG", "config.yaml"))
    )
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                raw = loaded
    expanded = _expand_env_refs(raw)
    config = AppConfig.model_validate(expanded)

    # Optional runtime overrides for quick debugging without editing YAML.
    if os.getenv("AI_AGENTS_HUB_LOG_LEVEL"):
        config.logging.level = os.getenv("AI_AGENTS_HUB_LOG_LEVEL", config.logging.level)  # type: ignore[assignment]
    if os.getenv("AI_AGENTS_HUB_LOG_OUTPUT"):
        config.logging.output = os.getenv("AI_AGENTS_HUB_LOG_OUTPUT", config.logging.output)  # type: ignore[assignment]
    if os.getenv("AI_AGENTS_HUB_LOG_DIR"):
        config.logging.directory = Path(
            os.getenv("AI_AGENTS_HUB_LOG_DIR", str(config.logging.directory))
        )
    if os.getenv("AI_AGENTS_HUB_LOG_INCLUDE_PAYLOADS"):
        config.logging.include_payloads = (
            os.getenv("AI_AGENTS_HUB_LOG_INCLUDE_PAYLOADS", "false").lower()
            in {"1", "true", "yes", "on"}
        )
    if os.getenv("AI_AGENTS_HUB_PROMPTS_DIR"):
        config.specialists.prompts.directory = Path(
            os.getenv("AI_AGENTS_HUB_PROMPTS_DIR", str(config.specialists.prompts.directory))
        )
    if os.getenv("AI_AGENTS_HUB_PROMPTS_AUTO_RELOAD"):
        config.specialists.prompts.auto_reload = (
            os.getenv("AI_AGENTS_HUB_PROMPTS_AUTO_RELOAD", "true").lower()
            in {"1", "true", "yes", "on"}
        )

    return config
