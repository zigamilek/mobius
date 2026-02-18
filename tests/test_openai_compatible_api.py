from __future__ import annotations

import os

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
