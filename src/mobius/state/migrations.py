from __future__ import annotations

Migration = tuple[str, str]

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS turn_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    session_key TEXT NULL,
    request_hash TEXT NOT NULL,
    domain TEXT NOT NULL,
    user_text TEXT NOT NULL,
    assistant_text TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tracks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    slug TEXT NOT NULL,
    domain TEXT NOT NULL,
    track_type TEXT NOT NULL CHECK (track_type IN ('goal', 'habit', 'system')),
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'completed', 'archived')),
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checkin_at TIMESTAMPTZ NULL,
    source_turn_id UUID NULL REFERENCES turn_events(id),
    UNIQUE (user_id, slug)
);

CREATE TABLE IF NOT EXISTS checkin_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    track_id UUID NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    timestamp TIMESTAMPTZ NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('win', 'partial', 'miss', 'note')),
    confidence NUMERIC(4,3) NULL,
    summary TEXT NOT NULL,
    wins JSONB NOT NULL DEFAULT '[]'::jsonb,
    barriers JSONB NOT NULL DEFAULT '[]'::jsonb,
    next_actions JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_turn_id UUID NULL REFERENCES turn_events(id),
    source_model TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    entry_date DATE NOT NULL,
    entry_ts TIMESTAMPTZ NOT NULL,
    title TEXT NULL,
    body_md TEXT NOT NULL,
    domain_hints TEXT[] NOT NULL DEFAULT '{}'::text[],
    source_turn_id UUID NULL REFERENCES turn_events(id),
    source_model TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS memory_cards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    domain TEXT NOT NULL,
    slug TEXT NOT NULL,
    memory TEXT NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    source_turn_id UUID NULL REFERENCES turn_events(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, domain, slug)
);

CREATE TABLE IF NOT EXISTS memory_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_card_id UUID NOT NULL REFERENCES memory_cards(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL CHECK (evidence_type IN ('checkin_event', 'journal_entry', 'turn_event', 'manual_note')),
    evidence_ref UUID NULL,
    excerpt TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS semantic_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    source_type TEXT NOT NULL CHECK (source_type IN ('memory_card', 'journal_entry')),
    source_id UUID NOT NULL,
    domain TEXT NULL,
    text_content TEXT NOT NULL,
    embedding VECTOR(1536) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_type, source_id)
);

CREATE TABLE IF NOT EXISTS write_operations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    turn_id UUID NULL REFERENCES turn_events(id),
    channel TEXT NOT NULL CHECK (channel IN ('checkin', 'journal', 'memory', 'projection')),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('applied', 'skipped_duplicate', 'failed')),
    payload_hash TEXT NOT NULL,
    result_ref UUID NULL,
    error_text TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS markdown_projection_state (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    artifact_type TEXT NOT NULL CHECK (artifact_type IN ('tracks', 'checkin_file', 'journal_file', 'memory_file')),
    artifact_key TEXT NOT NULL,
    source_max_updated_at TIMESTAMPTZ NOT NULL,
    rendered_hash TEXT NOT NULL,
    exported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    path TEXT NOT NULL,
    UNIQUE (user_id, artifact_type, artifact_key)
);

CREATE INDEX IF NOT EXISTS idx_turn_events_user_created_desc
    ON turn_events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_turn_events_user_session_created_desc
    ON turn_events(user_id, session_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tracks_user_status_updated_desc
    ON tracks(user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_checkin_events_track_ts_desc
    ON checkin_events(track_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_checkin_events_user_ts_desc
    ON checkin_events(user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_journal_entries_user_date_ts_desc
    ON journal_entries(user_id, entry_date DESC, entry_ts DESC);
CREATE INDEX IF NOT EXISTS idx_memory_cards_user_domain_last_seen_desc
    ON memory_cards(user_id, domain, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_memory_evidence_card_created_desc
    ON memory_evidence(memory_card_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_write_ops_user_created_desc
    ON write_operations(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_projection_state_user_type_exported_desc
    ON markdown_projection_state(user_id, artifact_type, exported_at DESC);
"""

RESET_STATE_SQL = """
DROP TABLE IF EXISTS markdown_projection_state CASCADE;
DROP TABLE IF EXISTS write_operations CASCADE;
DROP TABLE IF EXISTS semantic_documents CASCADE;
DROP TABLE IF EXISTS memory_evidence CASCADE;
DROP TABLE IF EXISTS checkin_events CASCADE;
DROP TABLE IF EXISTS journal_entries CASCADE;
DROP TABLE IF EXISTS tracks CASCADE;
DROP TABLE IF EXISTS turn_events CASCADE;
DROP TABLE IF EXISTS memory_cards CASCADE;
DROP TABLE IF EXISTS users CASCADE;
"""

MIGRATIONS: tuple[Migration, ...] = (
    (
        "0001",
        SCHEMA_SQL,
    ),
    (
        "0002",
        RESET_STATE_SQL + "\n" + SCHEMA_SQL,
    ),
)


def migration_versions() -> list[str]:
    return [version for version, _ in MIGRATIONS]


def migration_sql(version: str) -> str:
    for candidate, sql in MIGRATIONS:
        if candidate == version:
            return sql
    raise KeyError(f"Unknown migration version: {version}")
