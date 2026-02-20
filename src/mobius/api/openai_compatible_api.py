from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from mobius.api.schemas import ChatCompletionRequest, ModelCard, ModelListResponse
from mobius.logging_setup import get_logger

logger = get_logger(__name__)
FORWARDED_USER_NAME_HEADER = "X-OpenWebUI-User-Name"
FORWARDED_USER_ID_HEADER = "X-OpenWebUI-User-Id"


def _require_api_key(request: Request) -> None:
    config = request.app.state.services["config"]
    valid_keys = [key for key in config.server.api_keys if key]
    if not valid_keys:
        return
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip() if auth_header else ""
    if token not in valid_keys:
        logger.warning("Rejected request with invalid API key.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


def _payload_user_with_header_fallback(
    payload: ChatCompletionRequest, request: Request
) -> ChatCompletionRequest:
    payload_user = str(payload.user or "").strip()
    if payload_user:
        return payload

    header_candidates = (FORWARDED_USER_NAME_HEADER, FORWARDED_USER_ID_HEADER)
    selected_header = ""
    forwarded_user = ""
    for header_name in header_candidates:
        candidate = str(request.headers.get(header_name, "") or "").strip()
        if not candidate:
            continue
        selected_header = header_name
        forwarded_user = candidate
        break
    if not forwarded_user:
        return payload

    logger.debug(
        "Using forwarded user header '%s' for request user.",
        selected_header,
    )
    return payload.model_copy(update={"user": forwarded_user})


def create_openai_router() -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["openai-compatible-api"])

    @router.get("/models", response_model=ModelListResponse)
    async def list_models(
        request: Request,
        _: None = Depends(_require_api_key),
    ) -> ModelListResponse:
        config = request.app.state.services["config"]
        created = int(time.time())
        cards = [ModelCard(id=config.api.public_model_id, created=created)]
        logger.debug("Listing %d public model(s).", len(cards))
        return ModelListResponse(data=cards)

    @router.post("/chat/completions")
    async def chat_completions(
        payload: ChatCompletionRequest,
        request: Request,
        _: None = Depends(_require_api_key),
    ) -> Any:
        resolved_payload = _payload_user_with_header_fallback(payload, request)
        orchestrator = request.app.state.services["orchestrator"]
        app_config = request.app.state.services["config"]
        logger.info(
            "chat.completions request stream=%s requested_model=%s messages=%d",
            resolved_payload.stream,
            resolved_payload.model,
            len(resolved_payload.messages),
        )
        if app_config.logging.include_payloads:
            logger.debug("chat.completions payload: %s", resolved_payload.model_dump())
        if resolved_payload.stream:
            stream = orchestrator.stream_sse(resolved_payload)
            return StreamingResponse(stream, media_type="text/event-stream")
        response = await orchestrator.complete_non_stream(resolved_payload)
        logger.info("chat.completions completed (non-stream).")
        return JSONResponse(response)

    return router
