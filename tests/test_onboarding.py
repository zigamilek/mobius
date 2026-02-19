from __future__ import annotations

from pathlib import Path

import mobius.onboarding as onboarding


def _write_min_config(path: Path) -> None:
    path.write_text(
        "server:\n  host: 0.0.0.0\n  port: 8080\n  api_keys:\n    - ${ENV:MOBIUS_API_KEY}\n",
        encoding="utf-8",
    )


def test_onboarding_keep_mode_continues_with_existing_values(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    cfg_path = tmp_path / "config.yaml"
    env_path = tmp_path / "mobius.env"
    _write_min_config(cfg_path)
    env_path.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=existing-openai",
                "GEMINI_API_KEY=existing-gemini",
                "MOBIUS_API_KEY=existing-mobius-key",
                "MOBIUS_STATE_DSN=postgresql://user:pass@localhost:5432/mobius",
            ]
        ),
        encoding="utf-8",
    )

    input_values = iter(
        [
            "k",  # existing data mode -> keep
            "",  # service host
            "",  # service port
            "",  # prompts dir
            "y",  # enable stateful pipeline
        ]
    )
    secret_values = iter(
        [
            "",  # openai key (keep existing)
            "",  # gemini key (keep existing)
            "",  # mobius api key (keep existing)
            "",  # state dsn (keep existing)
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(input_values))
    monkeypatch.setattr(onboarding.getpass, "getpass", lambda _prompt="": next(secret_values))

    onboarding.run_onboarding(config_path=cfg_path, env_file=env_path, force=False)

    output = capsys.readouterr().out
    assert "Onboarding complete." in output
    assert "cancelled" not in output.lower()
    env_text = env_path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=existing-openai" in env_text
    assert "GEMINI_API_KEY=existing-gemini" in env_text
    assert "MOBIUS_API_KEY=existing-mobius-key" in env_text
    assert "MOBIUS_STATE_DSN=postgresql://user:pass@localhost:5432/mobius" in env_text
    assert "MOBIUS_STATE_ENABLED=" not in env_text


def test_onboarding_cancel_mode_stops_without_writing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    cfg_path = tmp_path / "config.yaml"
    env_path = tmp_path / "mobius.env"
    _write_min_config(cfg_path)
    env_original = "\n".join(
        [
            "OPENAI_API_KEY=existing-openai",
            "GEMINI_API_KEY=existing-gemini",
            "MOBIUS_API_KEY=existing-mobius-key",
        ]
    )
    env_path.write_text(env_original, encoding="utf-8")
    cfg_original = cfg_path.read_text(encoding="utf-8")

    input_values = iter(["c"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(input_values))

    onboarding.run_onboarding(config_path=cfg_path, env_file=env_path, force=False)

    output = capsys.readouterr().out
    assert "Onboarding cancelled. No files were modified." in output
    assert env_path.read_text(encoding="utf-8") == env_original
    assert cfg_path.read_text(encoding="utf-8") == cfg_original
