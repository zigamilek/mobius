from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mobius.config import StateConfig
from mobius.state.projection_sync import ProjectionSync


class _FakeStore:
    def list_tracks(self, *, user_id: str) -> list[dict[str, Any]]:
        return []

    def list_checkins(self, *, user_id: str, track_id: str) -> list[dict[str, Any]]:
        return []

    def list_journals(self, *, user_id: str) -> list[dict[str, Any]]:
        return []

    def list_memories(self, *, user_id: str) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        return [
            {
                "id": "mem-1",
                "domain": "health",
                "slug": "lose-fat",
                "memory": "I want to lose fat.",
                "first_seen": now,
                "last_seen": now,
                "occurrences": 3,
                "updated_at": now,
            }
        ]

    def list_write_operations(self, *, user_id: str, limit: int) -> list[dict[str, Any]]:
        return []

    def upsert_projection_state(self, **kwargs: Any) -> None:
        return None


def test_memory_projection_renders_only_fact_fields(tmp_path: Path) -> None:
    config = StateConfig.model_validate(
        {
            "projection": {
                "output_directory": str(tmp_path),
                "mode": "one_way",
            }
        }
    )
    sync = ProjectionSync(config=config, store=_FakeStore())  # type: ignore[arg-type]
    sync.export_user(user_id="u1", user_key="alice")

    content = (tmp_path / "users" / "alice" / "memories" / "health.md").read_text(
        encoding="utf-8"
    )
    assert "memory: I want to lose fat." in content
    assert "first_seen:" in content
    assert "last_seen:" in content
    assert "occurrences: 3" in content
    assert "summary:" not in content
    assert "title:" not in content
    assert "narrative:" not in content
    assert "confidence:" not in content
    assert "tags:" not in content
