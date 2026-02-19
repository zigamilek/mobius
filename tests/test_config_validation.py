from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError
import yaml

from mobius.config import AppConfig, load_config


def _valid_config() -> dict:
    return {
        "server": {"host": "0.0.0.0", "port": 8080, "api_keys": ["dev-key"]},
        "providers": {
            "openai": {"api_key": "openai-key"},
            "gemini": {
                "api_key": "gemini-key",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            },
        },
        "models": {
            "orchestrator": "gpt-5-nano-2025-08-07",
            "fallbacks": ["gemini-2.5-flash"],
        },
        "api": {
            "public_model_id": "mobius",
            "allow_provider_model_passthrough": False,
        },
        "specialists": {
            "prompts_directory": "./system_prompts",
            "auto_reload": True,
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
    }


def test_load_config_requires_existing_file(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing-config.yaml"
    with pytest.raises(FileNotFoundError):
        load_config(missing_path)


def test_specialists_config_rejects_missing_domain() -> None:
    payload = deepcopy(_valid_config())
    payload["specialists"]["by_domain"].pop("health")
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_specialists_config_rejects_extra_domain() -> None:
    payload = deepcopy(_valid_config())
    payload["specialists"]["by_domain"]["finance"] = {
        "model": "gpt-5.2",
        "prompt_file": "finance.md",
    }
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_config_forbids_unknown_keys() -> None:
    payload = deepcopy(_valid_config())
    payload["models"]["unknown"] = "value"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_runtime_rejects_invalid_timezone_name() -> None:
    payload = deepcopy(_valid_config())
    payload["runtime"] = {"timezone": "Mars/OlympusMons"}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_specialist_display_name_rejects_empty_string() -> None:
    payload = deepcopy(_valid_config())
    payload["specialists"]["by_domain"]["health"]["display_name"] = "   "
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_api_attribution_template_rejects_empty_string() -> None:
    payload = deepcopy(_valid_config())
    payload["api"]["attribution"] = {"template": "   "}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_state_enabled_requires_database_dsn() -> None:
    payload = deepcopy(_valid_config())
    payload["state"] = {"enabled": True, "database": {"dsn": None}}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_state_schema_version_rejects_invalid_format() -> None:
    payload = deepcopy(_valid_config())
    payload["state"] = {
        "database": {
            "min_schema_version": "1",
            "max_schema_version": "0001",
        }
    }
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_state_schema_version_rejects_inverted_range() -> None:
    payload = deepcopy(_valid_config())
    payload["state"] = {
        "enabled": True,
        "database": {
            "dsn": "postgresql://user:pass@localhost:5432/mobius",
            "min_schema_version": "0002",
            "max_schema_version": "0001",
        },
    }
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_state_user_scope_rejects_empty_anonymous_user_key() -> None:
    payload = deepcopy(_valid_config())
    payload["state"] = {
        "user_scope": {"policy": "fallback_anonymous", "anonymous_user_key": "   "}
    }
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_state_retrieval_limits_reject_zero() -> None:
    payload = deepcopy(_valid_config())
    payload["state"] = {"retrieval": {"recent_memory_cards_limit": 0}}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_state_checkin_limits_reject_zero() -> None:
    payload = deepcopy(_valid_config())
    payload["state"] = {"checkin": {"max_wins": 0}}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_state_decision_json_retries_reject_negative() -> None:
    payload = deepcopy(_valid_config())
    payload["state"] = {"decision": {"max_json_retries": -1}}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_state_memory_semantic_merge_limits_reject_zero() -> None:
    payload = deepcopy(_valid_config())
    payload["state"] = {"memory": {"semantic_merge": {"candidate_limit": 0}}}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_state_decision_on_failure_rejects_invalid_value() -> None:
    payload = deepcopy(_valid_config())
    payload["state"] = {"decision": {"on_failure": "explode"}}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_load_config_ignores_runtime_behavior_env_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    payload = deepcopy(_valid_config())
    payload["state"] = {"enabled": False, "database": {"dsn": None}}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    monkeypatch.setenv("MOBIUS_STATE_ENABLED", "true")
    monkeypatch.setenv("MOBIUS_STATE_DECISION_FACTS_ONLY", "false")
    monkeypatch.setenv("MOBIUS_STATE_DECISION_STRICT_GROUNDING", "false")
    monkeypatch.setenv("MOBIUS_LOG_LEVEL", "TRACE")
    loaded = load_config(cfg_path)

    assert loaded.state.enabled is False
    assert loaded.state.decision.facts_only is True
    assert loaded.state.decision.strict_grounding is True
    assert loaded.logging.level == "INFO"
