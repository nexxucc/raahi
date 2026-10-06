import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from app.config import ConfigurationError, Settings, TrackingMode
from app.main import create_app
from app.schemas import ObservationSource, PositionObservation


async def request_paths(*paths: str) -> list[httpx.Response]:
    transport = httpx.ASGITransport(app=create_app(Settings()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return [await client.get(path) for path in paths]


def test_health_and_meta_in_simulated_mode() -> None:
    health, meta = asyncio.run(request_paths("/api/v1/health", "/api/v1/meta"))

    assert health.status_code == 200
    assert health.json()["tracking_mode"] == "simulated"
    assert meta.status_code == 200
    assert meta.json()["official_feed_configured"] is False
    assert "labelled simulated" in meta.json()["data_sources"][1]["accuracy_note"]
    assert health.json()["database"] == "ok"


def test_health_and_meta_routes_are_in_openapi() -> None:
    response = asyncio.run(request_paths("/openapi.json"))[0]
    paths = response.json()["paths"]

    assert "/api/v1/health" in paths
    assert "/api/v1/meta" in paths


def test_meta_reports_live_access_configuration_without_claiming_feed_connection() -> None:
    settings = Settings(
        tracking_mode=TrackingMode.LIVE,
        otd_access_approved=True,
        otd_api_key="test-key",
    )
    meta = asyncio.run(request_paths_for_settings(settings, "/api/v1/meta"))[0].json()

    assert meta["official_feed_configured"] is True
    assert "No background polling" in meta["data_sources"][1]["accuracy_note"]


async def request_paths_for_settings(settings: Settings, *paths: str) -> list[httpx.Response]:
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return [await client.get(path) for path in paths]


def test_live_mode_requires_explicit_authorization_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACKING_MODE", "live")
    monkeypatch.delenv("OTD_ACCESS_APPROVED", raising=False)
    monkeypatch.delenv("OTD_API_KEY", raising=False)

    with pytest.raises(ConfigurationError, match="OTD_ACCESS_APPROVED=true"):
        Settings.from_env()


def test_observation_requires_timezone_aware_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        PositionObservation(
            observation_id="obs-1",
            vehicle_id="bus-1",
            route_id="route-1",
            latitude=28.6139,
            longitude=77.2090,
            observed_at=datetime(2026, 10, 5),  # noqa: DTZ001 - intentionally naive for validation
            received_at=datetime(2026, 10, 5, tzinfo=UTC),
            source=ObservationSource.CROWD,
        )
