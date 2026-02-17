from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_agents_hub.config import AppConfig
from ai_agents_hub.logging_setup import get_logger

PROMPT_KEYS: tuple[str, ...] = (
    "orchestrator",
    "general",
    "health",
    "parenting",
    "relationship",
    "homelab",
    "personal_development",
)

DEFAULT_PROMPTS: dict[str, str] = {
    "orchestrator": (
        "You are the master orchestrator agent. Decide whether specialist guidance "
        "is needed and synthesize one coherent final answer with no contradictions."
    ),
    "general": (
        "You are a reliable general assistant. Return one coherent answer with "
        "practical next steps."
    ),
    "health": (
        "You are the health specialist. Be practical and cautious. Do not provide "
        "diagnosis claims; recommend professional care for high-risk symptoms."
    ),
    "parenting": (
        "You are the parenting specialist. Give empathetic, actionable, "
        "age-appropriate guidance."
    ),
    "relationship": (
        "You are the relationship specialist. Support respectful communication, "
        "boundaries, and practical conflict resolution."
    ),
    "homelab": (
        "You are the homelab specialist. Prefer reliable, reproducible, "
        "rollback-friendly solutions."
    ),
    "personal_development": (
        "You are the personal development specialist. Help with habits, planning, "
        "accountability, and measurable progress."
    ),
}


class PromptManager:
    def __init__(self, config: AppConfig) -> None:
        self.logger = get_logger(__name__)
        self._config = config
        self._dir = config.specialists.prompts.directory
        self._files = config.specialists.prompts.files.model_dump()
        self._auto_reload = config.specialists.prompts.auto_reload
        self._prompts: dict[str, str] = {}
        self._fingerprints: dict[str, str] = {}
        self._load_all(initial=True)

    def _path_for(self, key: str) -> Path:
        filename = str(self._files.get(key, f"{key}.md")).strip()
        return self._dir / filename

    @staticmethod
    def _fingerprint(path: Path) -> str:
        if not path.exists():
            return "missing"
        try:
            stat = path.stat()
        except OSError:
            return "error"
        return f"{int(stat.st_mtime_ns)}:{stat.st_size}"

    def _read_prompt(self, key: str, path: Path) -> str:
        fallback = DEFAULT_PROMPTS[key]
        if not path.exists():
            self.logger.warning(
                "Prompt file missing for '%s': %s (using fallback prompt).",
                key,
                path,
            )
            return fallback
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            self.logger.warning(
                "Prompt file unreadable for '%s': %s (%s). Using fallback prompt.",
                key,
                path,
                exc.__class__.__name__,
            )
            return fallback
        if not text:
            self.logger.warning(
                "Prompt file empty for '%s': %s (using fallback prompt).",
                key,
                path,
            )
            return fallback
        return text

    def _load_all(self, initial: bool = False) -> None:
        loaded: dict[str, str] = {}
        fingerprints: dict[str, str] = {}
        self._dir.mkdir(parents=True, exist_ok=True)
        for key in PROMPT_KEYS:
            path = self._path_for(key)
            loaded[key] = self._read_prompt(key, path)
            fingerprints[key] = self._fingerprint(path)
        self._prompts = loaded
        self._fingerprints = fingerprints
        if initial:
            self.logger.info(
                "Prompt manager initialized (dir=%s auto_reload=%s).",
                self._dir,
                self._auto_reload,
            )
        else:
            self.logger.info("Prompt files changed; prompts reloaded from %s.", self._dir)

    def _has_changes(self) -> bool:
        for key in PROMPT_KEYS:
            current = self._fingerprint(self._path_for(key))
            if current != self._fingerprints.get(key):
                return True
        return False

    def maybe_reload(self) -> None:
        if not self._auto_reload:
            return
        if self._has_changes():
            self._load_all(initial=False)

    def get(self, key: str) -> str:
        self.maybe_reload()
        return self._prompts.get(key, DEFAULT_PROMPTS.get(key, ""))

    def resolved_prompt_files(self) -> dict[str, str]:
        return {key: str(self._path_for(key)) for key in PROMPT_KEYS}

    @property
    def auto_reload(self) -> bool:
        return self._auto_reload

    @property
    def directory(self) -> Path:
        return self._dir
