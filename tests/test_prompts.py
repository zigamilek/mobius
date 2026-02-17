from __future__ import annotations

from pathlib import Path

from ai_agents_hub.config import AppConfig
from ai_agents_hub.prompts.manager import DEFAULT_PROMPTS, PromptManager


def test_missing_prompt_file_uses_fallback(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "general.md").write_text("General from file", encoding="utf-8")

    config = AppConfig.model_validate(
        {"specialists": {"prompts": {"directory": str(prompt_dir), "auto_reload": True}}}
    )
    manager = PromptManager(config)

    assert manager.get("general") == "General from file"
    assert manager.get("health") == DEFAULT_PROMPTS["health"]


def test_prompt_auto_reload_on_change(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    for key in (
        "orchestrator",
        "general",
        "health",
        "parenting",
        "relationship",
        "homelab",
        "personal_development",
    ):
        (prompt_dir / f"{key}.md").write_text(f"{key} v1", encoding="utf-8")

    config = AppConfig.model_validate(
        {"specialists": {"prompts": {"directory": str(prompt_dir), "auto_reload": True}}}
    )
    manager = PromptManager(config)
    assert manager.get("orchestrator") == "orchestrator v1"

    (prompt_dir / "orchestrator.md").write_text("orchestrator v2", encoding="utf-8")
    assert manager.get("orchestrator") == "orchestrator v2"
