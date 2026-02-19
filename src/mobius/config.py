from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mobius.specialist_catalog import SPECIALIST_DOMAINS, normalize_domain

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional at runtime
    load_dotenv = None  # type: ignore[assignment]


ENV_REF_PATTERN = re.compile(r"^\$\{ENV:([A-Z0-9_]+)\}$")
SCHEMA_VERSION_PATTERN = re.compile(r"^\d{4}$")


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


class StateDatabaseConfig(StrictConfigModel):
    dsn: str | None = None
    auto_migrate: bool = True
    min_schema_version: str = "0002"
    max_schema_version: str = "0002"
    connect_timeout_seconds: int = 5

    @field_validator("dsn")
    @classmethod
    def _dsn_non_empty_if_set(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None

    @field_validator("min_schema_version", "max_schema_version")
    @classmethod
    def _schema_version_format(cls, value: str) -> str:
        trimmed = value.strip()
        if not SCHEMA_VERSION_PATTERN.match(trimmed):
            raise ValueError("Schema versions must match 'NNNN' format (example: 0001).")
        return trimmed

    @field_validator("connect_timeout_seconds")
    @classmethod
    def _positive_timeout(cls, value: int) -> int:
        if value < 1:
            raise ValueError("connect_timeout_seconds must be >= 1.")
        return value


class StateProjectionConfig(StrictConfigModel):
    mode: Literal["one_way", "hybrid_bidirectional"] = "one_way"
    output_directory: Path = Path("./data/state")


class StateUserScopeConfig(StrictConfigModel):
    policy: Literal["by_user", "fallback_anonymous"] = "by_user"
    anonymous_user_key: str = "anonymous"

    @field_validator("anonymous_user_key")
    @classmethod
    def _non_empty_anonymous_user_key(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("state.user_scope.anonymous_user_key must not be empty.")
        return trimmed


class StateDecisionConfig(StrictConfigModel):
    enabled: bool = True
    model: str = ""
    include_fallbacks: bool = False
    facts_only: bool = True
    strict_grounding: bool = True
    max_user_chars: int = 3000
    max_assistant_chars: int = 3000
    max_json_retries: int = 1
    on_failure: Literal["silent", "footer_warning"] = "footer_warning"

    @field_validator("max_user_chars", "max_assistant_chars")
    @classmethod
    def _positive_max_chars(cls, value: int) -> int:
        if value < 1:
            raise ValueError("State decision max char limits must be >= 1.")
        return value

    @field_validator("max_json_retries")
    @classmethod
    def _non_negative_json_retries(cls, value: int) -> int:
        if value < 0:
            raise ValueError("state.decision.max_json_retries must be >= 0.")
        return value


class StateCheckinConfig(StrictConfigModel):
    enabled: bool = True
    max_wins: int = 3
    max_barriers: int = 3
    max_next_actions: int = 3

    @field_validator("max_wins", "max_barriers", "max_next_actions")
    @classmethod
    def _positive_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("state.checkin limits must be >= 1.")
        return value


class StateJournalConfig(StrictConfigModel):
    enabled: bool = True
    include_assistant_excerpt: bool = False
    max_assistant_excerpt_chars: int = 320
    max_domain_hints: int = 4

    @field_validator("max_assistant_excerpt_chars", "max_domain_hints")
    @classmethod
    def _positive_journal_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("state.journal limits must be >= 1.")
        return value


class StateMemorySemanticMergeConfig(StrictConfigModel):
    enabled: bool = True
    embedding_model: str = "text-embedding-3-small"
    verification_model: str = ""
    include_fallbacks: bool = False
    candidate_limit: int = 8
    max_candidate_text_chars: int = 280
    max_json_retries: int = 1
    max_distance: float = 0.42

    @field_validator("candidate_limit", "max_candidate_text_chars")
    @classmethod
    def _positive_semantic_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("state.memory.semantic_merge limits must be >= 1.")
        return value

    @field_validator("max_json_retries")
    @classmethod
    def _non_negative_semantic_json_retries(cls, value: int) -> int:
        if value < 0:
            raise ValueError(
                "state.memory.semantic_merge.max_json_retries must be >= 0."
            )
        return value

    @field_validator("max_distance")
    @classmethod
    def _valid_max_distance(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError("state.memory.semantic_merge.max_distance must be >= 0.")
        return value


class StateMemoryConfig(StrictConfigModel):
    enabled: bool = True
    max_tags: int = 8
    semantic_merge: StateMemorySemanticMergeConfig = Field(
        default_factory=StateMemorySemanticMergeConfig
    )

    @field_validator("max_tags")
    @classmethod
    def _positive_max_tags(cls, value: int) -> int:
        if value < 1:
            raise ValueError("state.memory.max_tags must be >= 1.")
        return value


class StateRetrievalConfig(StrictConfigModel):
    active_tracks_limit: int = 5
    recent_checkins_limit: int = 5
    recent_journal_entries_limit: int = 3
    recent_memory_cards_limit: int = 5

    @field_validator(
        "active_tracks_limit",
        "recent_checkins_limit",
        "recent_journal_entries_limit",
        "recent_memory_cards_limit",
    )
    @classmethod
    def _positive_retrieval_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("state.retrieval limits must be >= 1.")
        return value


class StateConfig(StrictConfigModel):
    enabled: bool = False
    database: StateDatabaseConfig = Field(default_factory=StateDatabaseConfig)
    projection: StateProjectionConfig = Field(default_factory=StateProjectionConfig)
    user_scope: StateUserScopeConfig = Field(default_factory=StateUserScopeConfig)
    decision: StateDecisionConfig = Field(default_factory=StateDecisionConfig)
    checkin: StateCheckinConfig = Field(default_factory=StateCheckinConfig)
    journal: StateJournalConfig = Field(default_factory=StateJournalConfig)
    memory: StateMemoryConfig = Field(default_factory=StateMemoryConfig)
    retrieval: StateRetrievalConfig = Field(default_factory=StateRetrievalConfig)

    @model_validator(mode="after")
    def _validate_enabled_dsn(self) -> "StateConfig":
        if not self.enabled:
            return self
        if not self.database.dsn:
            raise ValueError("state.database.dsn must be set when state.enabled is true.")
        if self.database.min_schema_version > self.database.max_schema_version:
            raise ValueError(
                "state.database.min_schema_version must be <= max_schema_version."
            )
        return self


class AppConfig(StrictConfigModel):
    server: ServerConfig = Field(...)
    providers: ProvidersConfig = Field(...)
    models: ModelsConfig = Field(...)
    api: ApiConfig = Field(...)
    specialists: SpecialistsConfig = Field(...)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    state: StateConfig = Field(default_factory=StateConfig)
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
    config = AppConfig.model_validate(expanded)

    # Optional runtime overrides for quick debugging without editing YAML.
    if os.getenv("MOBIUS_LOG_LEVEL"):
        config.logging.level = os.getenv("MOBIUS_LOG_LEVEL", config.logging.level)  # type: ignore[assignment]
    if os.getenv("MOBIUS_LOG_OUTPUT"):
        config.logging.output = os.getenv("MOBIUS_LOG_OUTPUT", config.logging.output)  # type: ignore[assignment]
    if os.getenv("MOBIUS_LOG_DIR"):
        config.logging.directory = Path(
            os.getenv("MOBIUS_LOG_DIR", str(config.logging.directory))
        )
    if os.getenv("MOBIUS_LOG_INCLUDE_PAYLOADS"):
        config.logging.include_payloads = (
            os.getenv("MOBIUS_LOG_INCLUDE_PAYLOADS", "false").lower()
            in {"1", "true", "yes", "on"}
        )
    if os.getenv("MOBIUS_PROMPTS_DIR"):
        config.specialists.prompts_directory = Path(
            os.getenv(
                "MOBIUS_PROMPTS_DIR",
                str(config.specialists.prompts_directory),
            )
        )
    if os.getenv("MOBIUS_PROMPTS_AUTO_RELOAD"):
        config.specialists.auto_reload = (
            os.getenv("MOBIUS_PROMPTS_AUTO_RELOAD", "true").lower()
            in {"1", "true", "yes", "on"}
        )
    if os.getenv("MOBIUS_STATE_ENABLED"):
        config.state.enabled = (
            os.getenv("MOBIUS_STATE_ENABLED", "false").lower()
            in {"1", "true", "yes", "on"}
        )
    if os.getenv("MOBIUS_STATE_DSN"):
        dsn = os.getenv("MOBIUS_STATE_DSN", "").strip()
        config.state.database.dsn = dsn or None
    if os.getenv("MOBIUS_STATE_CONNECT_TIMEOUT_SECONDS"):
        try:
            config.state.database.connect_timeout_seconds = int(
                os.getenv("MOBIUS_STATE_CONNECT_TIMEOUT_SECONDS", "5")
            )
        except Exception:
            pass
    if os.getenv("MOBIUS_STATE_PROJECTION_DIR"):
        config.state.projection.output_directory = Path(
            os.getenv(
                "MOBIUS_STATE_PROJECTION_DIR",
                str(config.state.projection.output_directory),
            )
        )
    if os.getenv("MOBIUS_STATE_PROJECTION_MODE"):
        mode = os.getenv("MOBIUS_STATE_PROJECTION_MODE", "").strip().lower()
        if mode in {"one_way", "hybrid_bidirectional"}:
            config.state.projection.mode = mode  # type: ignore[assignment]
    if os.getenv("MOBIUS_STATE_USER_SCOPE_POLICY"):
        policy = os.getenv("MOBIUS_STATE_USER_SCOPE_POLICY", "").strip().lower()
        if policy in {"by_user", "fallback_anonymous"}:
            config.state.user_scope.policy = policy  # type: ignore[assignment]
    if os.getenv("MOBIUS_STATE_ANONYMOUS_USER_KEY"):
        config.state.user_scope.anonymous_user_key = os.getenv(
            "MOBIUS_STATE_ANONYMOUS_USER_KEY",
            config.state.user_scope.anonymous_user_key,
        )
    if os.getenv("MOBIUS_STATE_DECISION_MODEL"):
        config.state.decision.model = os.getenv(
            "MOBIUS_STATE_DECISION_MODEL",
            config.state.decision.model,
        )
    if os.getenv("MOBIUS_STATE_DECISION_ENABLED"):
        config.state.decision.enabled = (
            os.getenv("MOBIUS_STATE_DECISION_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
    if os.getenv("MOBIUS_STATE_DECISION_FACTS_ONLY"):
        config.state.decision.facts_only = (
            os.getenv("MOBIUS_STATE_DECISION_FACTS_ONLY", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
    if os.getenv("MOBIUS_STATE_DECISION_STRICT_GROUNDING"):
        config.state.decision.strict_grounding = (
            os.getenv("MOBIUS_STATE_DECISION_STRICT_GROUNDING", "true")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
    if os.getenv("MOBIUS_STATE_DECISION_MAX_JSON_RETRIES"):
        try:
            config.state.decision.max_json_retries = int(
                os.getenv("MOBIUS_STATE_DECISION_MAX_JSON_RETRIES", "1")
            )
        except Exception:
            pass
    if os.getenv("MOBIUS_STATE_DECISION_ON_FAILURE"):
        mode = os.getenv("MOBIUS_STATE_DECISION_ON_FAILURE", "").strip().lower()
        if mode in {"silent", "footer_warning"}:
            config.state.decision.on_failure = mode  # type: ignore[assignment]
    if os.getenv("MOBIUS_STATE_MEMORY_SEMANTIC_ENABLED"):
        config.state.memory.semantic_merge.enabled = (
            os.getenv("MOBIUS_STATE_MEMORY_SEMANTIC_ENABLED", "true")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
    if os.getenv("MOBIUS_STATE_MEMORY_EMBEDDING_MODEL"):
        config.state.memory.semantic_merge.embedding_model = os.getenv(
            "MOBIUS_STATE_MEMORY_EMBEDDING_MODEL",
            config.state.memory.semantic_merge.embedding_model,
        )
    if os.getenv("MOBIUS_STATE_MEMORY_VERIFY_MODEL"):
        config.state.memory.semantic_merge.verification_model = os.getenv(
            "MOBIUS_STATE_MEMORY_VERIFY_MODEL",
            config.state.memory.semantic_merge.verification_model,
        )

    # Re-validate after env overrides to enforce cross-field invariants.
    return AppConfig.model_validate(config.model_dump())
