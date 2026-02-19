from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from mobius.config import StateConfig
from mobius.logging_setup import get_logger
from mobius.state.models import (
    CheckinWrite,
    JournalWrite,
    MemoryWrite,
    StateContextSnapshot,
    WriteSummaryItem,
)


def _slugify(value: str, *, fallback: str = "item") -> str:
    lowered = value.strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return normalized or fallback


def _normalize_user_key(user_key: str | None) -> str:
    key = str(user_key or "").strip()
    if key:
        return key
    return "anonymous"


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _vector_literal(values: list[float]) -> str:
    normalized = ",".join(f"{float(value):.9g}" for value in values)
    return f"[{normalized}]"


def _human_elapsed(previous: datetime | None, now: datetime) -> str:
    if previous is None:
        return "first check-in"
    seconds = max(0, int((now - previous).total_seconds()))
    if seconds < 60:
        return f"{seconds}s since previous"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m since previous"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h since previous"
    days = hours // 24
    return f"{days}d since previous"


class PostgresStore:
    def __init__(self, config: StateConfig) -> None:
        self.config = config
        self.logger = get_logger(__name__)

    @staticmethod
    def _import_psycopg() -> tuple[Any, Any]:
        try:
            import psycopg  # type: ignore[import-not-found]
            from psycopg.rows import dict_row  # type: ignore[import-not-found]
        except Exception as exc:
            raise RuntimeError(
                "psycopg is required for state storage when state.enabled=true."
            ) from exc
        return psycopg, dict_row

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        dsn = (self.config.database.dsn or "").strip()
        if not dsn:
            raise RuntimeError("state.database.dsn is empty.")
        psycopg, dict_row = self._import_psycopg()
        conn = psycopg.connect(
            dsn,
            connect_timeout=self.config.database.connect_timeout_seconds,
            row_factory=dict_row,
        )
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _find_user_id(cursor: Any, user_key: str) -> str | None:
        cursor.execute("SELECT id FROM users WHERE user_key = %s", (user_key,))
        row = cursor.fetchone()
        if not row:
            return None
        return str(row["id"])

    @staticmethod
    def _ensure_user(cursor: Any, user_key: str) -> str:
        cursor.execute(
            """
INSERT INTO users(user_key, updated_at)
VALUES (%s, NOW())
ON CONFLICT (user_key)
DO UPDATE SET updated_at = NOW()
RETURNING id
""",
            (user_key,),
        )
        row = cursor.fetchone()
        return str(row["id"])

    @staticmethod
    def _begin_write_operation(
        cursor: Any,
        *,
        user_id: str,
        turn_id: str | None,
        channel: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> tuple[str, bool]:
        cursor.execute(
            """
INSERT INTO write_operations(
    user_id,
    turn_id,
    channel,
    idempotency_key,
    status,
    payload_hash
)
VALUES (%s, %s, %s, %s, 'applied', %s)
ON CONFLICT (user_id, idempotency_key)
DO NOTHING
RETURNING id
""",
            (user_id, turn_id, channel, idempotency_key, payload_hash),
        )
        inserted = cursor.fetchone()
        if inserted:
            return str(inserted["id"]), True
        cursor.execute(
            """
SELECT id
FROM write_operations
WHERE user_id = %s AND idempotency_key = %s
""",
            (user_id, idempotency_key),
        )
        existing = cursor.fetchone()
        if not existing:
            raise RuntimeError("Failed to read existing write operation after conflict.")
        return str(existing["id"]), False

    @staticmethod
    def _finish_write_operation(
        cursor: Any,
        *,
        operation_id: str,
        status: str,
        result_ref: str | None,
        error_text: str | None = None,
    ) -> None:
        cursor.execute(
            """
UPDATE write_operations
SET status = %s,
    result_ref = %s,
    error_text = %s
WHERE id = %s
""",
            (status, result_ref, error_text, operation_id),
        )

    def fetch_context_snapshot(
        self, *, user_key: str | None, routed_domain: str
    ) -> StateContextSnapshot:
        normalized_user = _normalize_user_key(user_key)
        with self._connect() as conn:
            with conn.cursor() as cursor:
                user_id = self._find_user_id(cursor, normalized_user)
                if user_id is None:
                    conn.rollback()
                    return StateContextSnapshot()

                cursor.execute(
                    """
SELECT slug, domain, track_type, title, status, last_checkin_at, updated_at
FROM tracks
WHERE user_id = %s AND status = 'active'
ORDER BY updated_at DESC
LIMIT %s
""",
                    (user_id, self.config.retrieval.active_tracks_limit),
                )
                active_tracks = cursor.fetchall()

                cursor.execute(
                    """
SELECT
    t.slug AS track_slug,
    c.timestamp,
    c.summary,
    c.outcome,
    c.confidence
FROM checkin_events c
JOIN tracks t ON t.id = c.track_id
WHERE c.user_id = %s
ORDER BY c.timestamp DESC
LIMIT %s
""",
                    (user_id, self.config.retrieval.recent_checkins_limit),
                )
                recent_checkins = cursor.fetchall()

                cursor.execute(
                    """
SELECT entry_date, entry_ts, title, LEFT(body_md, 320) AS excerpt
FROM journal_entries
WHERE user_id = %s
ORDER BY entry_ts DESC
LIMIT %s
""",
                    (user_id, self.config.retrieval.recent_journal_entries_limit),
                )
                recent_journals = cursor.fetchall()

                cursor.execute(
                    """
SELECT domain, slug, title, summary, occurrences, last_seen
FROM memory_cards
WHERE user_id = %s
ORDER BY
    CASE WHEN domain = %s THEN 0 ELSE 1 END,
    last_seen DESC
LIMIT %s
""",
                    (user_id, routed_domain, self.config.retrieval.recent_memory_cards_limit),
                )
                recent_memories = cursor.fetchall()
                conn.rollback()
                return StateContextSnapshot(
                    active_tracks=[dict(row) for row in active_tracks],
                    recent_checkins=[dict(row) for row in recent_checkins],
                    recent_journal_entries=[dict(row) for row in recent_journals],
                    recent_memory_cards=[dict(row) for row in recent_memories],
                )

    def upsert_turn_event(
        self,
        *,
        user_key: str | None,
        session_key: str | None,
        request_hash: str,
        domain: str,
        user_text: str,
        assistant_text: str,
    ) -> tuple[str, str]:
        normalized_user = _normalize_user_key(user_key)
        with self._connect() as conn:
            try:
                with conn.cursor() as cursor:
                    user_id = self._ensure_user(cursor, normalized_user)
                    cursor.execute(
                        """
SELECT id
FROM turn_events
WHERE user_id = %s AND request_hash = %s
ORDER BY created_at DESC
LIMIT 1
""",
                        (user_id, request_hash),
                    )
                    existing = cursor.fetchone()
                    if existing:
                        turn_id = str(existing["id"])
                        cursor.execute(
                            """
UPDATE turn_events
SET assistant_text = %s,
    domain = %s
WHERE id = %s
""",
                            (assistant_text, domain, turn_id),
                        )
                        conn.commit()
                        return user_id, turn_id

                    cursor.execute(
                        """
INSERT INTO turn_events(
    user_id,
    session_key,
    request_hash,
    domain,
    user_text,
    assistant_text
)
VALUES (%s, %s, %s, %s, %s, %s)
RETURNING id
""",
                        (
                            user_id,
                            session_key,
                            request_hash,
                            domain,
                            user_text,
                            assistant_text,
                        ),
                    )
                    inserted = cursor.fetchone()
                    conn.commit()
                    return user_id, str(inserted["id"])
            except Exception:
                conn.rollback()
                raise

    def write_checkin(
        self,
        *,
        user_id: str,
        turn_id: str,
        payload: CheckinWrite,
        idempotency_key: str,
        source_model: str | None,
    ) -> WriteSummaryItem:
        title_slug = _slugify(payload.title, fallback="general-checkin")
        track_slug = (
            title_slug
            if title_slug.startswith(f"{payload.domain}-")
            else f"{payload.domain}-{title_slug}"
        )
        payload_hash = _payload_hash(
            {
                "domain": payload.domain,
                "track_type": payload.track_type,
                "title": payload.title,
                "summary": payload.summary,
                "outcome": payload.outcome,
                "confidence": payload.confidence,
                "wins": payload.wins,
                "barriers": payload.barriers,
                "next_actions": payload.next_actions,
                "tags": payload.tags,
            }
        )
        target = f"checkins/{track_slug}.md"
        now = datetime.now(timezone.utc)

        with self._connect() as conn:
            try:
                with conn.cursor() as cursor:
                    op_id, inserted = self._begin_write_operation(
                        cursor,
                        user_id=user_id,
                        turn_id=turn_id,
                        channel="checkin",
                        idempotency_key=idempotency_key,
                        payload_hash=payload_hash,
                    )
                    if not inserted:
                        conn.rollback()
                        return WriteSummaryItem(
                            channel="checkin",
                            status="skipped_duplicate",
                            target=target,
                            details="duplicate idempotency key",
                        )

                    cursor.execute(
                        """
SELECT id, last_checkin_at
FROM tracks
WHERE user_id = %s AND slug = %s
FOR UPDATE
""",
                        (user_id, track_slug),
                    )
                    existing_track = cursor.fetchone()
                    previous_last_checkin = (
                        existing_track["last_checkin_at"] if existing_track else None
                    )
                    if existing_track:
                        track_id = str(existing_track["id"])
                        cursor.execute(
                            """
UPDATE tracks
SET domain = %s,
    track_type = %s,
    title = %s,
    status = 'active',
    tags = %s::jsonb,
    updated_at = NOW(),
    source_turn_id = %s
WHERE id = %s
""",
                            (
                                payload.domain,
                                payload.track_type,
                                payload.title,
                                json.dumps(payload.tags),
                                turn_id,
                                track_id,
                            ),
                        )
                    else:
                        cursor.execute(
                            """
INSERT INTO tracks(
    user_id,
    slug,
    domain,
    track_type,
    title,
    status,
    tags,
    source_turn_id
)
VALUES (%s, %s, %s, %s, %s, 'active', %s::jsonb, %s)
RETURNING id
""",
                            (
                                user_id,
                                track_slug,
                                payload.domain,
                                payload.track_type,
                                payload.title,
                                json.dumps(payload.tags),
                                turn_id,
                            ),
                        )
                        inserted_track = cursor.fetchone()
                        track_id = str(inserted_track["id"])

                    cursor.execute(
                        """
INSERT INTO checkin_events(
    user_id,
    track_id,
    timestamp,
    outcome,
    confidence,
    summary,
    wins,
    barriers,
    next_actions,
    source_turn_id,
    source_model
)
VALUES (
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s::jsonb,
    %s::jsonb,
    %s::jsonb,
    %s,
    %s
)
RETURNING id
""",
                        (
                            user_id,
                            track_id,
                            now,
                            payload.outcome,
                            payload.confidence,
                            payload.summary,
                            json.dumps(payload.wins),
                            json.dumps(payload.barriers),
                            json.dumps(payload.next_actions),
                            turn_id,
                            source_model,
                        ),
                    )
                    checkin_row = cursor.fetchone()
                    checkin_id = str(checkin_row["id"])
                    cursor.execute(
                        """
UPDATE tracks
SET last_checkin_at = %s,
    updated_at = NOW()
WHERE id = %s
""",
                        (now, track_id),
                    )
                    self._finish_write_operation(
                        cursor,
                        operation_id=op_id,
                        status="applied",
                        result_ref=checkin_id,
                    )
                    conn.commit()
                    return WriteSummaryItem(
                        channel="checkin",
                        status="applied",
                        target=target,
                        details=_human_elapsed(previous_last_checkin, now),
                        result_ref=checkin_id,
                    )
            except Exception as exc:
                conn.rollback()
                self.logger.warning("Failed to write check-in: %s", exc.__class__.__name__)
                raise

    def write_journal(
        self,
        *,
        user_id: str,
        turn_id: str,
        payload: JournalWrite,
        idempotency_key: str,
        source_model: str | None,
    ) -> WriteSummaryItem:
        entry_date = payload.entry_ts.date().isoformat()
        target = f"journal/{entry_date}.md"
        payload_hash = _payload_hash(
            {
                "entry_ts": payload.entry_ts.isoformat(),
                "title": payload.title,
                "body_md": payload.body_md,
                "domain_hints": payload.domain_hints,
            }
        )
        with self._connect() as conn:
            try:
                with conn.cursor() as cursor:
                    op_id, inserted = self._begin_write_operation(
                        cursor,
                        user_id=user_id,
                        turn_id=turn_id,
                        channel="journal",
                        idempotency_key=idempotency_key,
                        payload_hash=payload_hash,
                    )
                    if not inserted:
                        conn.rollback()
                        return WriteSummaryItem(
                            channel="journal",
                            status="skipped_duplicate",
                            target=target,
                            details="duplicate idempotency key",
                        )

                    cursor.execute(
                        """
INSERT INTO journal_entries(
    user_id,
    entry_date,
    entry_ts,
    title,
    body_md,
    domain_hints,
    source_turn_id,
    source_model
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
RETURNING id
""",
                        (
                            user_id,
                            payload.entry_ts.date(),
                            payload.entry_ts,
                            payload.title,
                            payload.body_md,
                            payload.domain_hints,
                            turn_id,
                            source_model,
                        ),
                    )
                    inserted_row = cursor.fetchone()
                    journal_id = str(inserted_row["id"])
                    self._finish_write_operation(
                        cursor,
                        operation_id=op_id,
                        status="applied",
                        result_ref=journal_id,
                    )
                    conn.commit()
                    return WriteSummaryItem(
                        channel="journal",
                        status="applied",
                        target=target,
                        details=payload.title,
                        result_ref=journal_id,
                    )
            except Exception as exc:
                conn.rollback()
                self.logger.warning("Failed to write journal: %s", exc.__class__.__name__)
                raise

    def write_memory(
        self,
        *,
        user_id: str,
        turn_id: str,
        payload: MemoryWrite,
        idempotency_key: str,
        source_excerpt: str,
        merge_slug: str | None = None,
    ) -> WriteSummaryItem:
        memory_slug = merge_slug or _slugify(payload.title, fallback="user-memory")
        target = f"memories/{payload.domain}.md"
        payload_hash = _payload_hash(
            {
                "domain": payload.domain,
                "title": payload.title,
                "summary": payload.summary,
                "narrative": payload.narrative,
                "confidence": payload.confidence,
                "tags": payload.tags,
            }
        )
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            try:
                with conn.cursor() as cursor:
                    op_id, inserted = self._begin_write_operation(
                        cursor,
                        user_id=user_id,
                        turn_id=turn_id,
                        channel="memory",
                        idempotency_key=idempotency_key,
                        payload_hash=payload_hash,
                    )
                    if not inserted:
                        conn.rollback()
                        return WriteSummaryItem(
                            channel="memory",
                            status="skipped_duplicate",
                            target=target,
                            details="duplicate idempotency key",
                        )

                    cursor.execute(
                        """
SELECT id, occurrences, narrative
FROM memory_cards
WHERE user_id = %s AND domain = %s AND slug = %s
FOR UPDATE
""",
                        (user_id, payload.domain, memory_slug),
                    )
                    existing = cursor.fetchone()
                    if existing:
                        memory_id = str(existing["id"])
                        occurrences = int(existing["occurrences"] or 1) + 1
                        existing_narrative = str(existing["narrative"] or "").strip()
                        new_narrative_piece = payload.narrative.strip()
                        if new_narrative_piece:
                            merged_narrative = (
                                f"{existing_narrative}\n\n- {now.isoformat()}: {new_narrative_piece}"
                                if existing_narrative
                                else f"- {now.isoformat()}: {new_narrative_piece}"
                            )
                        else:
                            merged_narrative = existing_narrative
                        cursor.execute(
                            """
UPDATE memory_cards
SET title = %s,
    summary = %s,
    narrative = %s,
    last_seen = %s,
    occurrences = %s,
    confidence = %s,
    tags = %s::jsonb,
    source_turn_id = %s,
    updated_at = NOW()
WHERE id = %s
""",
                            (
                                payload.title,
                                payload.summary,
                                merged_narrative,
                                now,
                                occurrences,
                                payload.confidence,
                                json.dumps(payload.tags),
                                turn_id,
                                memory_id,
                            ),
                        )
                    else:
                        cursor.execute(
                            """
INSERT INTO memory_cards(
    user_id,
    domain,
    slug,
    title,
    summary,
    narrative,
    first_seen,
    last_seen,
    occurrences,
    confidence,
    tags,
    source_turn_id
)
VALUES (
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    %s,
    1,
    %s,
    %s::jsonb,
    %s
)
RETURNING id
""",
                            (
                                user_id,
                                payload.domain,
                                memory_slug,
                                payload.title,
                                payload.summary,
                                payload.narrative,
                                now,
                                now,
                                payload.confidence,
                                json.dumps(payload.tags),
                                turn_id,
                            ),
                        )
                        inserted_row = cursor.fetchone()
                        memory_id = str(inserted_row["id"])

                    cursor.execute(
                        """
INSERT INTO memory_evidence(
    memory_card_id,
    evidence_type,
    evidence_ref,
    excerpt
)
VALUES (%s, 'turn_event', %s, %s)
""",
                        (memory_id, turn_id, source_excerpt[:512]),
                    )
                    self._finish_write_operation(
                        cursor,
                        operation_id=op_id,
                        status="applied",
                        result_ref=memory_id,
                    )
                    conn.commit()
                    return WriteSummaryItem(
                        channel="memory",
                        status="applied",
                        target=target,
                        details=f"{payload.domain}/{memory_slug}",
                        result_ref=memory_id,
                    )
            except Exception as exc:
                conn.rollback()
                self.logger.warning("Failed to write memory: %s", exc.__class__.__name__)
                raise

    def list_memory_candidates(
        self, *, user_id: str, domain: str, limit: int
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
SELECT id, domain, slug, title, summary, narrative, occurrences, last_seen, updated_at
FROM memory_cards
WHERE user_id = %s AND domain = %s
ORDER BY last_seen DESC
LIMIT %s
""",
                    (user_id, domain, max(1, limit)),
                )
                rows = cursor.fetchall()
                conn.rollback()
                return [dict(row) for row in rows]

    def semantic_memory_candidates(
        self,
        *,
        user_id: str,
        domain: str,
        embedding: list[float],
        limit: int,
        max_distance: float,
    ) -> list[dict[str, Any]]:
        vector = _vector_literal(embedding)
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
SELECT
    m.id,
    m.domain,
    m.slug,
    m.title,
    m.summary,
    m.narrative,
    m.occurrences,
    m.last_seen,
    (s.embedding <=> %s::vector) AS distance
FROM semantic_documents s
JOIN memory_cards m
  ON m.id = s.source_id
WHERE
    s.user_id = %s
    AND s.source_type = 'memory_card'
    AND m.domain = %s
    AND (s.embedding <=> %s::vector) <= %s
ORDER BY distance ASC, m.last_seen DESC
LIMIT %s
""",
                    (
                        vector,
                        user_id,
                        domain,
                        vector,
                        max_distance,
                        max(1, limit),
                    ),
                )
                rows = cursor.fetchall()
                conn.rollback()
                return [dict(row) for row in rows]

    def get_memory_card(self, *, user_id: str, memory_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
SELECT id, user_id, domain, slug, title, summary, narrative, occurrences, updated_at
FROM memory_cards
WHERE user_id = %s AND id = %s
""",
                    (user_id, memory_id),
                )
                row = cursor.fetchone()
                conn.rollback()
                if not row:
                    return None
                return dict(row)

    def upsert_memory_embedding(
        self,
        *,
        user_id: str,
        domain: str,
        memory_id: str,
        text_content: str,
        embedding: list[float],
    ) -> None:
        vector = _vector_literal(embedding)
        with self._connect() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
INSERT INTO semantic_documents(
    user_id,
    source_type,
    source_id,
    domain,
    text_content,
    embedding,
    created_at,
    updated_at
)
VALUES (%s, 'memory_card', %s, %s, %s, %s::vector, NOW(), NOW())
ON CONFLICT (source_type, source_id)
DO UPDATE SET
    user_id = EXCLUDED.user_id,
    domain = EXCLUDED.domain,
    text_content = EXCLUDED.text_content,
    embedding = EXCLUDED.embedding,
    updated_at = NOW()
""",
                        (user_id, memory_id, domain, text_content, vector),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def list_tracks(self, *, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
SELECT id, slug, domain, track_type, title, status, tags,
       created_at, updated_at, last_checkin_at
FROM tracks
WHERE user_id = %s
ORDER BY updated_at DESC
""",
                    (user_id,),
                )
                rows = cursor.fetchall()
                conn.rollback()
                return [dict(row) for row in rows]

    def list_checkins(self, *, user_id: str, track_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
SELECT id, timestamp, outcome, confidence, summary, wins, barriers, next_actions,
       source_turn_id, source_model, created_at
FROM checkin_events
WHERE user_id = %s AND track_id = %s
ORDER BY timestamp DESC
""",
                    (user_id, track_id),
                )
                rows = cursor.fetchall()
                conn.rollback()
                return [dict(row) for row in rows]

    def list_journals(self, *, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
SELECT id, entry_date, entry_ts, title, body_md, domain_hints, created_at, updated_at
FROM journal_entries
WHERE user_id = %s
ORDER BY entry_ts DESC
""",
                    (user_id,),
                )
                rows = cursor.fetchall()
                conn.rollback()
                return [dict(row) for row in rows]

    def list_memories(self, *, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
SELECT id, domain, slug, title, summary, narrative, status, first_seen, last_seen,
       occurrences, confidence, tags, created_at, updated_at
FROM memory_cards
WHERE user_id = %s
ORDER BY domain ASC, last_seen DESC
""",
                    (user_id,),
                )
                rows = cursor.fetchall()
                conn.rollback()
                return [dict(row) for row in rows]

    def list_write_operations(self, *, user_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
SELECT channel, idempotency_key, status, payload_hash, created_at
FROM write_operations
WHERE user_id = %s
ORDER BY created_at DESC
LIMIT %s
""",
                    (user_id, max(1, limit)),
                )
                rows = cursor.fetchall()
                conn.rollback()
                return [dict(row) for row in rows]

    def upsert_projection_state(
        self,
        *,
        user_id: str,
        artifact_type: str,
        artifact_key: str,
        source_max_updated_at: datetime,
        rendered_hash: str,
        path: str,
    ) -> None:
        with self._connect() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
INSERT INTO markdown_projection_state(
    user_id,
    artifact_type,
    artifact_key,
    source_max_updated_at,
    rendered_hash,
    exported_at,
    path
)
VALUES (%s, %s, %s, %s, %s, NOW(), %s)
ON CONFLICT (user_id, artifact_type, artifact_key)
DO UPDATE SET
    source_max_updated_at = EXCLUDED.source_max_updated_at,
    rendered_hash = EXCLUDED.rendered_hash,
    exported_at = NOW(),
    path = EXCLUDED.path
""",
                        (
                            user_id,
                            artifact_type,
                            artifact_key,
                            source_max_updated_at,
                            rendered_hash,
                            path,
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
