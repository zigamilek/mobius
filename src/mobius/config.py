from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mobius.specialist_catalog import SPECIALIST_DOMAINS, normalize_domain

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional at runtime
    load_dotenv = None  # type: ignore[assignment]


ENV_REF_PATTERN = re.compile(r"^\$\{ENV:([A-Z0-9_]+)\}$")


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(StrictConfigModel):
    host: str = "0.0.0.0"
    port: int = 8080
    api_keys: list[str | None] = Field(...)


class ProviderConfig(StrictConfigModel):
    api_key: str | None = Field(...)
    base_url: str | None = None


class ProvidersConfig(StrictConfigModel):
    openai: ProviderConfig = Field(...)
    gemini: ProviderConfig = Field(...)


class ModelsConfig(StrictConfigModel):
    orchestrator: str = Field(...)
    fallbacks: list[str] = Field(default_factory=list)


class SpecialistDomainConfig(StrictConfigModel):
    model: str = Field(...)
    prompt_file: str = Field(...)
    display_name: str | None = None

    @field_validator("model", "prompt_file")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Value must not be empty.")
        return trimmed

    @field_validator("display_name")
    @classmethod
    def _display_name_non_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("display_name must not be empty when provided.")
        return trimmed


class SpecialistsConfig(StrictConfigModel):
    prompts_directory: Path = Field(...)
    auto_reload: bool = True
    orchestrator_prompt_file: str = Field(...)
    by_domain: dict[str, SpecialistDomainConfig] = Field(...)

    @field_validator("orchestrator_prompt_file")
    @classmethod
    def _validate_orchestrator_prompt_file(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("orchestrator_prompt_file must not be empty.")
        return trimmed

    @field_validator("by_domain")
    @classmethod
    def _validate_by_domain(
        cls, value: dict[str, SpecialistDomainConfig]
    ) -> dict[str, SpecialistDomainConfig]:
        normalized: dict[str, SpecialistDomainConfig] = {}
        for raw_key, config in value.items():
            key = normalize_domain(raw_key)
            if key in normalized:
                raise ValueError(
                    f"Duplicate specialist domain after normalization: '{raw_key}'."
                )
            normalized[key] = config

        required = set(SPECIALIST_DOMAINS)
        provided = set(normalized)
        missing = sorted(required - provided)
        extra = sorted(provided - required)
        if missing or extra:
            raise ValueError(
                "specialists.by_domain keys must match catalog domains. "
                f"missing={missing} extra={extra}"
            )
        return {domain: normalized[domain] for domain in SPECIALIST_DOMAINS}


class DiagnosticEndpointsConfig(StrictConfigModel):
    health: str = "/healthz"
    readiness: str = "/readyz"
    diagnostics: str = "/diagnostics"


class DiagnosticsConfig(StrictConfigModel):
    enabled: bool = True
    endpoints: DiagnosticEndpointsConfig = Field(
        default_factory=DiagnosticEndpointsConfig
    )


class ApiAttributionConfig(StrictConfigModel):
    enabled: bool = True
    include_model: bool = True
    include_general: bool = False
    template: str = (
        "Answered by {display_name} (the {domain_label} specialist){model_suffix}."
    )

    @field_validator("template")
    @classmethod
    def _template_non_empty(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("api.attribution.template must not be empty.")
        return trimmed


class ApiConfig(StrictConfigModel):
    public_model_id: str = Field(...)
    allow_provider_model_passthrough: bool = False
    attribution: ApiAttributionConfig = Field(default_factory=ApiAttributionConfig)


class LoggingConfig(StrictConfigModel):
    level: Literal["ERROR", "WARNING", "INFO", "DEBUG", "TRACE"] = "INFO"
    output: Literal["console", "file", "both"] = "console"
    directory: Path = Path("./data/logs")
    filename: str = "mobius.log"
    daily_rotation: bool = True
    retention_days: int = 14
    utc: bool = True
    include_payloads: bool = False


class RuntimeConfig(StrictConfigModel):
    inject_current_timestamp: bool = True
    timezone: str = "Europe/Ljubljana"
    include_timestamp_in_routing: bool = False

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        timezone_name = value.strip()
        if not timezone_name:
            raise ValueError("runtime.timezone must not be empty.")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"Unknown timezone '{timezone_name}'. Use an IANA timezone name."
            ) from exc
        return timezone_name


class AppConfig(StrictConfigModel):
    server: ServerConfig = Field(...)
    providers: ProvidersConfig = Field(...)
    models: ModelsConfig = Field(...)
    api: ApiConfig = Field(...)
    specialists: SpecialistsConfig = Field(...)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
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
    disabled = os.getenv("MOBIUS_DISABLE_DOTENV", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if disabled or load_dotenv is None:
        return
    dotenv_path = Path(os.getenv("MOBIUS_DOTENV_PATH", ".env"))
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path, override=False)


def load_config(config_path: str | Path | None = None) -> AppConfig:
    _maybe_load_dotenv()
    path = (
        Path(config_path)
        if config_path
        else Path(os.getenv("MOBIUS_CONFIG", "config.yaml"))
    )
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. "
            "Provide MOBIUS_CONFIG or create config.yaml."
        )
    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    if loaded is None:
        raise ValueError(f"Config file is empty: {path}")
    if not isinstance(loaded, dict):
        raise TypeError(f"Config root must be a YAML mapping/object: {path}")

    raw: dict[str, Any] = loaded

    expanded = _expand_env_refs(raw)
    if isinstance(expanded, dict):
        expanded = dict(expanded)
        expanded.pop("state", None)
    return AppConfig.model_validate(expanded)
