from __future__ import annotations

from pathlib import Path

from ai_agents_hub.config import AppConfig
from ai_agents_hub.orchestration.specialists import get_specialist, normalize_domain
from ai_agents_hub.prompts.manager import PromptManager


def test_specialist_lookup_returns_general_for_unknown() -> None:
    specialist = get_specialist("not-a-domain")
    assert specialist.domain == "general"


def test_specialist_domain_normalization() -> None:
    assert normalize_domain("personal-development") == "personal_development"
    assert get_specialist("personal-development").domain == "personal_development"


def test_prompt_file_reload_and_general_prompt_override(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "orchestrator.md": "Orchestrator prompt one",
        "general.md": "General prompt one",
        "health.md": "Health prompt",
        "parenting.md": "Parenting prompt",
        "relationship.md": "Relationship prompt",
        "homelab.md": "Homelab prompt",
        "personal_development.md": "Personal development prompt",
    }
    for name, content in files.items():
        (prompt_dir / name).write_text(content, encoding="utf-8")

    config = AppConfig.model_validate(
        {
            "specialists": {
                "prompts": {
                    "directory": str(prompt_dir),
                    "auto_reload": True,
                }
            }
        }
    )
    manager = PromptManager(config)
    assert manager.get("general") == "General prompt one"

    (prompt_dir / "general.md").write_text("General prompt two", encoding="utf-8")
    assert manager.get("general") == "General prompt two"
