from __future__ import annotations

from fastapi.testclient import TestClient

from ai_agents_hub.main import create_app


def test_models_endpoint_returns_list() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get("/v1/models")
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert isinstance(payload["data"], list)
    assert len(payload["data"]) == 1
    assert payload["data"][0]["id"] == "ai-agents-hub"


def test_diagnostics_endpoints_are_available() -> None:
    app = create_app()
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/diagnostics").status_code == 200
