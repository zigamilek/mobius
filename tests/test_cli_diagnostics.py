from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import yaml

import mobius.__main__ as cli


def _write_config(path: Path) -> None:
    payload = {
        "server": {
            "host": "0.0.0.0",
            "port": 8080,
            "api_keys": ["${ENV:MOBIUS_API_KEY}"],
        },
        "providers": {
            "openai": {"api_key": "${ENV:OPENAI_API_KEY}"},
            "gemini": {
                "api_key": "${ENV:GEMINI_API_KEY}",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            },
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
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_parser_accepts_diagnostics_env_file_flag() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(["diagnostics", "--config", "cfg.yaml", "--env-file", "env"])
    assert args.command == "diagnostics"
    assert args.config_path == "cfg.yaml"
    assert args.env_file == "env"


def test_diagnostics_loads_env_file_for_config_validation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    cfg_path = tmp_path / "config.yaml"
    env_path = tmp_path / "mobius.env"
    _write_config(cfg_path)
    env_path.write_text("OPENAI_API_KEY=test-key\n")

    monkeypatch.setenv("MOBIUS_DISABLE_DOTENV", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_detect_local_ip", lambda: "192.168.111.33")

    rc = cli._cmd_diagnostics(
        Namespace(
            config_path=str(cfg_path),
            env_file=str(env_path),
        )
    )
    output = capsys.readouterr().out
    assert rc == 0
    assert "ValidationError" not in output
    assert "Config load note:" not in output
    assert "Configured port: 8080" in output
