from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.services.health_service import mark_runtime_health


def test_fastapi_health_endpoint_reports_database_component():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["components"]["database"]["status"] == "ok"
    assert "telegram" in body["components"]


def test_provider_health_endpoint_does_not_require_network_probe():
    with TestClient(app) as client:
        response = client.get("/health/providers")
    assert response.status_code == 200
    assert response.json()["providers"]["status"] == "unknown"


def test_scheduler_health_endpoint_exposes_last_scans():
    mark_runtime_health("scheduler", "running")
    mark_runtime_health("alarm_scan", "ok")
    mark_runtime_health("close_scan", "ok")
    with TestClient(app) as client:
        response = client.get("/health/scheduler")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert body["last_successful_alarm_scan"]
    assert body["last_successful_close_scan"]


def test_data_health_endpoint_exposes_cache_and_last_fetch():
    mark_runtime_health("data_fetch", "ok")
    with TestClient(app) as client:
        response = client.get("/health/data")
    assert response.status_code == 200
    body = response.json()
    assert "cache" in body
    assert body["last_successful_data_fetch"]

