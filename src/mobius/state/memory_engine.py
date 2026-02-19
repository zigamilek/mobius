from __future__ import annotations

import json
import re
from typing import Any

from mobius.config import AppConfig
from mobius.logging_setup import get_logger
from mobius.providers.litellm_router import LiteLLMRouter
from mobius.state.models import MemoryWrite, WriteSummaryItem
from mobius.state.storage import PostgresStore


def _truncate(text: str, *, max_chars: int) -> str:
    return text[: max(1, max_chars)].strip()


def _memory_text(payload: MemoryWrite) -> str:
    return f"memory: {payload.memory.strip()}"


def _extract_json_payload(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = candidate[start : end + 1]
    try:
        payload = json.loads(candidate)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


class MemoryEngine:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: PostgresStore,
        llm_router: LiteLLMRouter,
    ) -> None:
        self.config = config
        self.store = store
        self.llm_router = llm_router
        self.logger = get_logger(__name__)

    async def apply(
        self,
        *,
        user_id: str,
        turn_id: str,
        payload: MemoryWrite,
        idempotency_key: str,
        source_excerpt: str,
    ) -> WriteSummaryItem:
        merge_slug = await self._resolve_semantic_merge_slug(user_id=user_id, payload=payload)
        item = self.store.write_memory(
            user_id=user_id,
            turn_id=turn_id,
            payload=payload,
            idempotency_key=idempotency_key,
            source_excerpt=source_excerpt,
            merge_slug=merge_slug,
        )
        await self._sync_memory_embedding_if_possible(user_id=user_id, payload=payload, item=item)
        self.logger.debug(
            "Memory write result status=%s target=%s details=%s",
            item.status,
            item.target,
            item.details,
        )
        return item

    async def _resolve_semantic_merge_slug(
        self, *, user_id: str, payload: MemoryWrite
    ) -> str | None:
        semantic_cfg = self.config.state.memory.semantic_merge
        if not semantic_cfg.enabled:
            return None

        candidates = self.store.list_memory_candidates(
            user_id=user_id,
            domain=payload.domain,
            limit=semantic_cfg.candidate_limit,
        )
        if not candidates:
            return None

        new_memory_text = _memory_text(payload)
        semantic_candidates: list[dict[str, Any]] = []
        try:
            _model, vector = await self.llm_router.embedding(
                primary_model=semantic_cfg.embedding_model,
                input_text=new_memory_text,
                include_fallbacks=semantic_cfg.include_fallbacks,
            )
            semantic_candidates = self.store.semantic_memory_candidates(
                user_id=user_id,
                domain=payload.domain,
                embedding=vector,
                limit=semantic_cfg.candidate_limit,
                max_distance=semantic_cfg.max_distance,
            )
        except Exception as exc:
            self.logger.warning(
                "Semantic memory candidate search failed model=%s error=%s",
                semantic_cfg.embedding_model,
                exc.__class__.__name__,
            )

        merged_candidates: dict[str, dict[str, Any]] = {}
        for row in semantic_candidates:
            slug = str(row.get("slug") or "").strip()
            if slug:
                merged_candidates[slug] = row
        for row in candidates:
            slug = str(row.get("slug") or "").strip()
            if slug and slug not in merged_candidates:
                merged_candidates[slug] = row
        if not merged_candidates:
            return None

        shortlist = list(merged_candidates.values())[: semantic_cfg.candidate_limit]
        selected_slug = await self._llm_merge_decision(
            payload=payload,
            shortlist=shortlist,
            max_candidate_chars=semantic_cfg.max_candidate_text_chars,
            max_json_retries=semantic_cfg.max_json_retries,
        )
        if not selected_slug:
            return None
        return selected_slug if selected_slug in merged_candidates else None

    async def _llm_merge_decision(
        self,
        *,
        payload: MemoryWrite,
        shortlist: list[dict[str, Any]],
        max_candidate_chars: int,
        max_json_retries: int,
    ) -> str | None:
        semantic_cfg = self.config.state.memory.semantic_merge
        model = (
            semantic_cfg.verification_model.strip() or self.config.models.orchestrator
        )
        attempts = 1 + max(0, max_json_retries)
        retry_feedback = ""

        serialized_candidates: list[str] = []
        for row in shortlist:
            slug = str(row.get("slug") or "").strip()
            memory = _truncate(str(row.get("memory") or ""), max_chars=max_candidate_chars)
            serialized_candidates.append(
                f"- slug={slug} | memory={memory}"
            )
        candidates_block = "\n".join(serialized_candidates)

        system_prompt = (
            "You decide whether a new memory should MERGE into an existing memory card.\n"
            "Output EXACTLY one JSON object and nothing else.\n"
            "Schema:\n"
            '{'
            '"action":"merge|new",'
            '"target_slug":"<slug when action=merge, else empty string>",'
            '"reason":"<short reason>",'
            '"confidence":<number 0..1>'
            '}\n'
            "Rules:\n"
            "- Use action=merge only when semantic meaning is effectively the same recurring memory.\n"
            "- If candidate is related but not equivalent, choose action=new.\n"
            "- If unsure, choose action=new.\n"
        )

        for _attempt in range(1, attempts + 1):
            user_payload = (
                f"domain={payload.domain}\n"
                f"new_memory:\n{_memory_text(payload)}\n\n"
                f"candidate_memories:\n{candidates_block}\n"
            )
            if retry_feedback.strip():
                user_payload = f"{user_payload}\nretry_feedback:\n{retry_feedback.strip()}\n"
            try:
                _used_model, raw = await self.llm_router.chat_completion(
                    primary_model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_payload},
                    ],
                    stream=False,
                    passthrough=None,
                    include_fallbacks=semantic_cfg.include_fallbacks,
                )
            except Exception as exc:
                retry_feedback = (
                    f"model failure {exc.__class__.__name__}; output strict JSON schema."
                )
                continue

            text = self._extract_text(raw)
            response = _extract_json_payload(text)
            if not response:
                retry_feedback = "previous output was not valid JSON."
                continue
            action = str(response.get("action") or "").strip().lower()
            target_slug = str(response.get("target_slug") or "").strip()
            if action == "merge" and target_slug:
                return target_slug
            if action == "new":
                return None
            retry_feedback = (
                "invalid action/target_slug. use action merge|new and target_slug for merge."
            )
        return None

    @staticmethod
    def _extract_text(raw: Any) -> str:
        if isinstance(raw, dict):
            payload = raw
        elif hasattr(raw, "model_dump"):
            payload = raw.model_dump(exclude_none=True)  # type: ignore[no-any-return]
        elif hasattr(raw, "dict"):
            payload = raw.dict()  # type: ignore[no-any-return]
        else:
            return ""
        try:
            value = payload["choices"][0]["message"]["content"]
        except Exception:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"].strip())
            return "\n".join([part for part in parts if part]).strip()
        return ""

    async def _sync_memory_embedding_if_possible(
        self,
        *,
        user_id: str,
        payload: MemoryWrite,
        item: WriteSummaryItem,
    ) -> None:
        semantic_cfg = self.config.state.memory.semantic_merge
        if not semantic_cfg.enabled:
            return
        if item.status != "applied" or not item.result_ref:
            return
        memory_id = item.result_ref
        memory = self.store.get_memory_card(user_id=user_id, memory_id=memory_id)
        if not memory:
            return
        text_content = _memory_text(payload)
        try:
            _model, embedding = await self.llm_router.embedding(
                primary_model=semantic_cfg.embedding_model,
                input_text=text_content,
                include_fallbacks=semantic_cfg.include_fallbacks,
            )
            self.store.upsert_memory_embedding(
                user_id=user_id,
                domain=str(memory.get("domain") or payload.domain),
                memory_id=memory_id,
                text_content=text_content,
                embedding=embedding,
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to sync memory embedding model=%s error=%s",
                semantic_cfg.embedding_model,
                exc.__class__.__name__,
            )
