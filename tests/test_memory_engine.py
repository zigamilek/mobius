from __future__ import annotations

import asyncio
import json
from typing import Any

from mobius.config import AppConfig
from mobius.state.memory_engine import MemoryEngine
from mobius.state.models import MemoryWrite, WriteSummaryItem


class FakeStore:
    def __init__(self) -> None:
        self.last_merge_slug: str | None = None
        self.embedding_upserts: list[dict[str, Any]] = []

    def list_memory_candidates(
        self, *, user_id: str, domain: str, limit: int
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "mem-existing-1",
                "domain": domain,
                "slug": "lose-fat",
                "memory": "I want to lose fat.",
                "occurrences": 3,
                "last_seen": "2026-02-15T10:00:00+00:00",
            }
        ]

    def semantic_memory_candidates(
        self,
        *,
        user_id: str,
        domain: str,
        embedding: list[float],
        limit: int,
        max_distance: float,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "mem-existing-1",
                "domain": domain,
                "slug": "lose-fat",
                "memory": "I want to lose fat.",
                "distance": 0.12,
            }
        ]

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
        self.last_merge_slug = merge_slug
        return WriteSummaryItem(
            channel="memory",
            status="applied",
            target=f"memories/{payload.domain}.md",
            details=f"{payload.domain}/{merge_slug or 'new-slug'}",
            result_ref="mem-existing-1",
        )

    def get_memory_card(self, *, user_id: str, memory_id: str) -> dict[str, Any] | None:
        return {
            "id": memory_id,
            "domain": "health",
            "slug": "lose-fat",
            "memory": "I want to lose fat.",
        }

    def upsert_memory_embedding(
        self,
        *,
        user_id: str,
        domain: str,
        memory_id: str,
        text_content: str,
        embedding: list[float],
    ) -> None:
        self.embedding_upserts.append(
            {
                "user_id": user_id,
                "domain": domain,
                "memory_id": memory_id,
                "text_content": text_content,
                "embedding_len": len(embedding),
            }
        )


class FakeRouter:
    def __init__(self) -> None:
        self.chat_calls = 0
        self.embedding_calls = 0

    async def embedding(
        self,
        *,
        primary_model: str,
        input_text: str,
        include_fallbacks: bool = False,
    ) -> tuple[str, list[float]]:
        self.embedding_calls += 1
        return primary_model, [0.1] * 8

    async def chat_completion(
        self,
        *,
        primary_model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        passthrough: dict[str, Any] | None = None,
        include_fallbacks: bool = True,
    ) -> tuple[str, Any]:
        self.chat_calls += 1
        payload = {
            "action": "merge",
            "target_slug": "lose-fat",
            "reason": "same recurring long-term goal",
            "confidence": 0.88,
        }
        return primary_model, {"choices": [{"message": {"content": json.dumps(payload)}}]}


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "server": {"api_keys": ["dev-key"]},
            "providers": {
                "openai": {"api_key": "test-openai-key"},
                "gemini": {"api_key": "test-gemini-key"},
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
            "state": {
                "memory": {
                    "enabled": True,
                    "semantic_merge": {
                        "enabled": True,
                        "embedding_model": "text-embedding-3-small",
                        "verification_model": "gpt-5.2",
                        "candidate_limit": 8,
                        "max_candidate_text_chars": 200,
                        "max_json_retries": 1,
                        "max_distance": 0.42,
                    },
                }
            },
        }
    )


def test_memory_engine_semantic_merge_reuses_existing_slug_and_syncs_embedding() -> None:
    cfg = _config()
    store = FakeStore()
    router = FakeRouter()
    engine = MemoryEngine(
        config=cfg,
        store=store,  # type: ignore[arg-type]
        llm_router=router,  # type: ignore[arg-type]
    )
    payload = MemoryWrite(
        domain="health",
        memory="I want to lose body fat.",
        evidence="Today I decided I'll finally lose fat.",
    )
    item = asyncio.run(
        engine.apply(
            user_id="user-1",
            turn_id="turn-1",
            payload=payload,
            idempotency_key="req:memory",
            source_excerpt="Today I decided I'll finally lose fat.",
        )
    )
    assert item.status == "applied"
    assert store.last_merge_slug == "lose-fat"
    assert router.chat_calls == 1
    assert router.embedding_calls >= 2  # shortlist + post-write semantic sync
    assert len(store.embedding_upserts) == 1
