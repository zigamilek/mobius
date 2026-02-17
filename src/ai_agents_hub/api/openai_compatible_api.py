from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from ai_agents_hub.api.schemas import ChatCompletionRequest, ModelCard, ModelListResponse
from ai_agents_hub.logging_setup import get_logger

logger = get_logger(__name__)


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
        orchestrator = request.app.state.services["orchestrator"]
        app_config = request.app.state.services["config"]
        logger.info(
            "chat.completions request stream=%s requested_model=%s messages=%d",
            payload.stream,
            payload.model,
            len(payload.messages),
        )
        if app_config.logging.include_payloads:
            logger.debug("chat.completions payload: %s", payload.model_dump())
        if payload.stream:
            stream = orchestrator.stream_sse(payload)
            return StreamingResponse(stream, media_type="text/event-stream")
        response = await orchestrator.complete_non_stream(payload)
        logger.info("chat.completions completed (non-stream).")
        return JSONResponse(response)

    return router
