from __future__ import annotations

import os
from typing import Any

from fastapi.testclient import TestClient

os.environ.setdefault("MOBIUS_CONFIG", "config.local.yaml")

from mobius import __version__
from mobius.main import create_app


def test_models_endpoint_returns_list() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get("/v1/models", headers={"Authorization": "Bearer dev-local-key"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert isinstance(payload["data"], list)
    assert len(payload["data"]) == 1
    assert payload["data"][0]["id"] == "mobius"


def test_diagnostics_endpoints_are_available() -> None:
    app = create_app()
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    diagnostics = client.get("/diagnostics")
    assert diagnostics.status_code == 200
    payload = diagnostics.json()
    assert payload["service"] == "mobius"
    assert payload["version"] == __version__
    assert payload["config"]["api"]["public_model_id"] == "mobius"
    assert payload["config"]["api"]["attribution"]["enabled"] is True
    assert payload["config"]["state"]["enabled"] is False
    assert payload["config"]["state"]["projection"]["mode"] == "one_way"
    assert payload["config"]["state"]["user_scope"]["policy"] == "by_user"
    assert payload["config"]["state"]["decision"]["enabled"] is True
    assert payload["config"]["state"]["decision"]["facts_only"] is True
    assert payload["config"]["state"]["decision"]["strict_grounding"] is True
    assert payload["config"]["state"]["decision"]["max_json_retries"] == 1
    assert payload["config"]["state"]["decision"]["on_failure"] == "footer_warning"
    assert payload["config"]["state"]["memory"]["semantic_merge"]["enabled"] is True


class _StubOrchestrator:
    def __init__(self) -> None:
        self.last_user: str | None = None

    async def complete_non_stream(self, payload: Any) -> dict[str, Any]:
        self.last_user = getattr(payload, "user", None)
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "mobius",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }

    async def stream_sse(self, payload: Any):  # pragma: no cover - not used here
        self.last_user = getattr(payload, "user", None)
        yield b"data: [DONE]\n\n"


def test_chat_completion_uses_forwarded_user_id_header_when_payload_user_missing() -> None:
    app = create_app()
    stub = _StubOrchestrator()
    app.state.services["orchestrator"] = stub
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer dev-local-key",
            "X-OpenWebUI-User-Id": "ziga",
        },
        json={
            "model": "mobius",
            "messages": [{"role": "user", "content": "test"}],
            "stream": False,
        },
    )
    assert response.status_code == 200
    assert stub.last_user == "ziga"


def test_chat_completion_prefers_forwarded_user_name_header_over_user_id() -> None:
    app = create_app()
    stub = _StubOrchestrator()
    app.state.services["orchestrator"] = stub
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer dev-local-key",
            "X-OpenWebUI-User-Name": "ziga",
            "X-OpenWebUI-User-Id": "6aedbd6f-3a09-4781-9980-2bb8114ba497",
        },
        json={
            "model": "mobius",
            "messages": [{"role": "user", "content": "test"}],
            "stream": False,
        },
    )
    assert response.status_code == 200
    assert stub.last_user == "ziga"


def test_chat_completion_prefers_payload_user_over_forwarded_header() -> None:
    app = create_app()
    stub = _StubOrchestrator()
    app.state.services["orchestrator"] = stub
    client = TestClient(app)
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer dev-local-key",
            "X-OpenWebUI-User-Id": "header-user",
        },
        json={
            "model": "mobius",
            "messages": [{"role": "user", "content": "test"}],
            "user": "payload-user",
            "stream": False,
        },
    )
    assert response.status_code == 200
    assert stub.last_user == "payload-user"
