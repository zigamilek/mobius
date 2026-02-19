from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mobius.config import StateConfig
from mobius.logging_setup import get_logger
from mobius.state.models import WriteSummaryItem
from mobius.state.storage import PostgresStore


def _safe_path_part(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return normalized or "anonymous"


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.isoformat()
    return str(value or "")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _max_updated(rows: list[dict[str, Any]], *keys: str) -> datetime:
    latest: datetime | None = None
    for row in rows:
        for key in keys:
            value = row.get(key)
            if isinstance(value, datetime):
                latest = value if latest is None or value > latest else latest
    return latest or datetime.now(timezone.utc)


class ProjectionSync:
    def __init__(self, *, config: StateConfig, store: PostgresStore) -> None:
        self.config = config
        self.store = store
        self.logger = get_logger(__name__)

    def export_user(self, *, user_id: str, user_key: str) -> list[WriteSummaryItem]:
        root = self.config.projection.output_directory / "users" / _safe_path_part(user_key)
        checkins_dir = root / "checkins"
        journal_dir = root / "journal"
        memories_dir = root / "memories"
        root.mkdir(parents=True, exist_ok=True)
        checkins_dir.mkdir(parents=True, exist_ok=True)
        journal_dir.mkdir(parents=True, exist_ok=True)
        memories_dir.mkdir(parents=True, exist_ok=True)

        tracks = self.store.list_tracks(user_id=user_id)
        checkins_by_track: dict[str, list[dict[str, Any]]] = {}
        for track in tracks:
            track_id = str(track.get("id"))
            checkins_by_track[track_id] = self.store.list_checkins(
                user_id=user_id,
                track_id=track_id,
            )
        journals = self.store.list_journals(user_id=user_id)
        memories = self.store.list_memories(user_id=user_id)
        operations = self.store.list_write_operations(user_id=user_id, limit=500)

        self._render_tracks(root=root, user_id=user_id, tracks=tracks)
        self._render_checkins(
            checkins_dir=checkins_dir,
            user_id=user_id,
            tracks=tracks,
            checkins_by_track=checkins_by_track,
        )
        self._render_journal(journal_dir=journal_dir, user_id=user_id, journals=journals)
        self._render_memories(
            memories_dir=memories_dir,
            user_id=user_id,
            memories=memories,
        )
        self._render_ops_log(root=root, operations=operations)

        return [
            WriteSummaryItem(
                channel="projection",
                status="applied",
                target=f"state/users/{_safe_path_part(user_key)}",
                details="one-way markdown projection",
            )
        ]

    def _render_tracks(
        self,
        *,
        root: Path,
        user_id: str,
        tracks: list[dict[str, Any]],
    ) -> None:
        lines = [
            "---",
            "schema_version: 1",
            "generated_by: mobius",
            f"updated_at: {datetime.now(timezone.utc).isoformat()}",
            "---",
            "",
            "# Tracks",
            "",
        ]
        for track in tracks:
            slug = str(track.get("slug") or "")
            tags = track.get("tags") or []
            tag_text = ", ".join(str(tag) for tag in tags) if isinstance(tags, list) else ""
            lines.extend(
                [
                    f"<!-- track:{track.get('id')} -->",
                    f"id: {track.get('id')}",
                    f"slug: {slug}",
                    f"domain: {track.get('domain')}",
                    f"type: {track.get('track_type')}",
                    f"title: {track.get('title')}",
                    f"status: {track.get('status')}",
                    f"created_at: {_iso(track.get('created_at'))}",
                    f"updated_at: {_iso(track.get('updated_at'))}",
                    f"last_checkin_at: {_iso(track.get('last_checkin_at'))}",
                    f"checkins_file: checkins/{slug}.md",
                    f"tags: [{tag_text}]",
                    f"<!-- /track:{track.get('id')} -->",
                    "",
                ]
            )
        content = "\n".join(lines).rstrip() + "\n"
        file_path = root / "tracks.md"
        file_path.write_text(content, encoding="utf-8")
        self.store.upsert_projection_state(
            user_id=user_id,
            artifact_type="tracks",
            artifact_key="tracks",
            source_max_updated_at=_max_updated(tracks, "updated_at", "last_checkin_at"),
            rendered_hash=_content_hash(content),
            path=str(file_path),
        )

    def _render_checkins(
        self,
        *,
        checkins_dir: Path,
        user_id: str,
        tracks: list[dict[str, Any]],
        checkins_by_track: dict[str, list[dict[str, Any]]],
    ) -> None:
        now = datetime.now(timezone.utc)
        for track in tracks:
            track_id = str(track.get("id"))
            slug = str(track.get("slug") or "")
            events = checkins_by_track.get(track_id, [])
            last_checkin = track.get("last_checkin_at")
            since_text = "n/a"
            if isinstance(last_checkin, datetime):
                seconds = max(0, int((now - last_checkin).total_seconds()))
                hours = seconds // 3600
                days = hours // 24
                since_text = f"{days}d" if days > 0 else f"{hours}h"

            lines = [
                "---",
                "schema_version: 1",
                "generated_by: mobius",
                f"track_id: {track_id}",
                f"track_slug: {slug}",
                f"domain: {track.get('domain')}",
                f"type: {track.get('track_type')}",
                f"title: {track.get('title')}",
                f"status: {track.get('status')}",
                f"created_at: {_iso(track.get('created_at'))}",
                f"updated_at: {_iso(track.get('updated_at'))}",
                f"last_checkin_at: {_iso(last_checkin)}",
                "---",
                "",
                f"# Track: {track.get('title')}",
                "",
                "## Snapshot",
                f"- Current status: {track.get('status')}",
                f"- Last check-in: {_iso(last_checkin)}",
                f"- Time since last check-in: {since_text}",
                "",
                "## Check-in Events",
                "",
            ]

            for event in events:
                lines.extend(
                    [
                        f"<!-- checkin:{event.get('id')} -->",
                        f"id: {event.get('id')}",
                        f"timestamp: {_iso(event.get('timestamp'))}",
                        f"outcome: {event.get('outcome')}",
                        f"confidence: {event.get('confidence')}",
                        f"summary: {event.get('summary')}",
                        "wins:",
                    ]
                )
                for win in event.get("wins") or []:
                    lines.append(f"  - {win}")
                lines.append("barriers:")
                for barrier in event.get("barriers") or []:
                    lines.append(f"  - {barrier}")
                lines.append("next_actions:")
                for action in event.get("next_actions") or []:
                    lines.append(f"  - {action}")
                lines.extend(
                    [
                        "source:",
                        f"  turn_id: {event.get('source_turn_id')}",
                        f"  model: {event.get('source_model')}",
                        f"<!-- /checkin:{event.get('id')} -->",
                        "",
                    ]
                )

            content = "\n".join(lines).rstrip() + "\n"
            file_path = checkins_dir / f"{slug}.md"
            file_path.write_text(content, encoding="utf-8")
            source_rows = [track, *events]
            self.store.upsert_projection_state(
                user_id=user_id,
                artifact_type="checkin_file",
                artifact_key=slug,
                source_max_updated_at=_max_updated(
                    source_rows,
                    "updated_at",
                    "timestamp",
                    "created_at",
                ),
                rendered_hash=_content_hash(content),
                path=str(file_path),
            )

    def _render_journal(
        self,
        *,
        journal_dir: Path,
        user_id: str,
        journals: list[dict[str, Any]],
    ) -> None:
        by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in journals:
            by_date[str(row.get("entry_date"))].append(row)

        for entry_date, rows in by_date.items():
            sorted_rows = sorted(
                rows,
                key=lambda row: row.get("entry_ts") or datetime.now(timezone.utc),
            )
            lines = [
                "---",
                "schema_version: 1",
                "generated_by: mobius",
                f"entry_date: {entry_date}",
                f"updated_at: {datetime.now(timezone.utc).isoformat()}",
                "---",
                "",
                f"# Journal - {entry_date}",
                "",
            ]
            for row in sorted_rows:
                lines.extend(
                    [
                        f"<!-- journal:{row.get('id')} -->",
                        str(row.get("body_md") or "").strip(),
                        f"<!-- /journal:{row.get('id')} -->",
                        "",
                    ]
                )
            content = "\n".join(lines).rstrip() + "\n"
            file_path = journal_dir / f"{entry_date}.md"
            file_path.write_text(content, encoding="utf-8")
            self.store.upsert_projection_state(
                user_id=user_id,
                artifact_type="journal_file",
                artifact_key=entry_date,
                source_max_updated_at=_max_updated(sorted_rows, "updated_at", "entry_ts"),
                rendered_hash=_content_hash(content),
                path=str(file_path),
            )

    def _render_memories(
        self,
        *,
        memories_dir: Path,
        user_id: str,
        memories: list[dict[str, Any]],
    ) -> None:
        by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in memories:
            by_domain[str(row.get("domain"))].append(row)

        for domain, rows in by_domain.items():
            lines = [
                f"# Memories - {domain}",
                "",
            ]
            for row in rows:
                memory_text = str(row.get("memory") or "").strip() or "-"
                lines.extend(
                    [
                        f"memory: {memory_text}",
                        f"first_seen: {_iso(row.get('first_seen'))}",
                        f"last_seen: {_iso(row.get('last_seen'))}",
                        f"occurrences: {row.get('occurrences')}",
                        "",
                    ]
                )
            content = "\n".join(lines).rstrip() + "\n"
            file_path = memories_dir / f"{domain}.md"
            file_path.write_text(content, encoding="utf-8")
            self.store.upsert_projection_state(
                user_id=user_id,
                artifact_type="memory_file",
                artifact_key=domain,
                source_max_updated_at=_max_updated(rows, "updated_at", "last_seen"),
                rendered_hash=_content_hash(content),
                path=str(file_path),
            )

    @staticmethod
    def _render_ops_log(*, root: Path, operations: list[dict[str, Any]]) -> None:
        lines = [
            "# Mobius write operations",
            "",
        ]
        for row in operations:
            lines.append(
                f"{_iso(row.get('created_at'))} | {row.get('channel')} | "
                f"{row.get('status')} | {row.get('idempotency_key')}"
            )
        content = "\n".join(lines).rstrip() + "\n"
        (root / "ops.log").write_text(content, encoding="utf-8")
