from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from app.config import Settings, TrackingMode
from app.gtfs import FeedMetadata, TransitFeed
from app.main import create_app
from app.storage import connect_database, replace_feed


def seeded_db(path: str) -> None:
    now = datetime.now(UTC)
    feed = TransitFeed(
        metadata=FeedMetadata("fixture", None, now, "test source", "api-fixture", "v1"),
        routes=[{"route_id": "r1", "route_type": "3", "route_short_name": "10", "route_long_name": "North South"}],
        stops=[
            {"stop_id": "s1", "stop_name": "Central Stop", "stop_lat": "28.6000", "stop_lon": "77.2000"},
            {"stop_id": "s2", "stop_name": "Far Stop", "stop_lat": "28.9000", "stop_lon": "77.2000"},
        ],
        trips=[{"route_id": "r1", "service_id": "daily", "trip_id": "t1"}],
        calendars=[{"service_id": "daily", "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1", "friday": "1", "saturday": "1", "sunday": "1", "start_date": "20200101", "end_date": "20991231"}],
        stop_times=[{"trip_id": "t1", "stop_id": "s1", "stop_sequence": "1", "arrival_time": "23:59:00", "departure_time": "23:59:00"}],
    )
    db = connect_database(path)
    replace_feed(db, feed)
    db.execute("""INSERT INTO official_observations
        (observation_id, vehicle_id, route_id, direction_id, latitude, longitude, observed_at, received_at, expires_at)
        VALUES ('obs1', 'bus-1', 'r1', '0', 28.6, 77.2, ?, ?, ?)""",
        (now.isoformat(), now.isoformat(), (now + timedelta(minutes=30)).isoformat()))
    db.commit()
    db.close()


def call_api(settings: Settings, method: str, path: str, **kwargs):
    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(call())


def test_network_search_nearby_tracking_and_schedule_fallback(tmp_path):
    path = str(tmp_path / "api.sqlite")
    seeded_db(path)
    settings = Settings(database_path=path)
    routes = call_api(settings, "GET", "/api/v1/routes?q=North")
    stops = call_api(settings, "GET", "/api/v1/stops", params={"latitude": 28.6, "longitude": 77.2, "radius_m": 1000})
    vehicles = call_api(settings, "GET", "/api/v1/routes/r1/vehicles")
    vehicle = call_api(settings, "GET", "/api/v1/vehicles/bus-1")
    arrivals = call_api(settings, "GET", "/api/v1/stops/s1/arrivals")
    assert routes.status_code == 200 and routes.json()[0]["route_id"] == "r1"
    assert stops.status_code == 200 and [item["stop_id"] for item in stops.json()] == ["s1"]
    assert vehicles.status_code == 200 and vehicles.json()[0]["vehicle_id"] == "bus-1"
    assert vehicle.status_code == 200 and vehicle.json()["status"] == "live"
    assert arrivals.status_code == 200
    assert all(item["status"] == "scheduled_estimate" and item["basis"] == "static_gtfs_schedule" for item in arrivals.json())


def test_unknown_ids_missing_feed_and_bad_nearby_coordinate_pair(tmp_path):
    path = str(tmp_path / "api.sqlite")
    settings = Settings(database_path=path)
    assert call_api(settings, "GET", "/api/v1/routes").status_code == 503
    seeded_db(path)
    assert call_api(settings, "GET", "/api/v1/vehicles/nope").status_code == 404
    assert call_api(settings, "GET", "/api/v1/routes/nope").status_code == 404
    assert call_api(settings, "GET", "/api/v1/stops", params={"latitude": 28.6}).status_code == 422


def test_arrivals_returns_empty_for_no_active_service(tmp_path):
    path = str(tmp_path / "no-service.sqlite")
    seeded_db(path)
    db = connect_database(path)
    db.execute("UPDATE calendars SET payload_json=replace(payload_json, '\"1\"', '\"0\"')")
    db.commit()
    db.close()
    response = call_api(Settings(database_path=path), "GET", "/api/v1/stops/s1/arrivals")
    assert response.status_code == 200
    assert response.json() == []


def test_empty_route_positions_and_moderated_one_shot_feed_route(tmp_path):
    path = str(tmp_path / "api.sqlite")
    seeded_db(path)
    settings = Settings(database_path=path, reports_admin_token="mod-token")
    # Endpoint is guarded and does not fetch without OTD live authorization.
    denied = call_api(settings, "POST", "/api/v1/admin/official-feed/fetch")
    assert denied.status_code == 403
    bad_auth = call_api(settings, "POST", "/api/v1/admin/official-feed/fetch", headers={"X-Reports-Admin-Token": "mod-token"})
    assert bad_auth.status_code == 503
    live = Settings(database_path=path, reports_admin_token="mod-token", tracking_mode=TrackingMode.LIVE,
                    otd_access_approved=True, otd_api_key="fake-test-key")
    assert call_api(live, "POST", "/api/v1/admin/official-feed/fetch", headers={"X-Reports-Admin-Token": "wrong"}).status_code == 403


def test_moderated_one_shot_feed_stores_mocked_authorized_snapshot(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from app.official_feed import OfficialVehicleObservation, TimestampSource
    path = str(tmp_path / "api.sqlite")
    seeded_db(path)
    observation = OfficialVehicleObservation(
        observation_id="mock-observation", vehicle_id="bus-2", route_id="r1",
        direction_id=None, latitude=28.61, longitude=77.2,
        observed_at=datetime.now(UTC), received_at=datetime.now(UTC),
        timestamp_source=TimestampSource.VEHICLE, vehicle_id_source="test",
    )

    class FakeClient:
        def fetch_once(self):
            return type("Feed", (), {"feed_version": "2.0", "feed_timestamp": None,
                                      "received_at": datetime.now(UTC),
                                      "observations": (observation,), "rejected_entities": 0})()

        def close(self):
            pass

    monkeypatch.setattr("app.tracking.OtdVehicleFeedClient.from_settings", lambda settings: FakeClient())
    settings = Settings(database_path=path, reports_admin_token="mod-token", tracking_mode=TrackingMode.LIVE,
                        otd_access_approved=True, otd_api_key="test-only")
    response = call_api(settings, "POST", "/api/v1/admin/official-feed/fetch", headers={"X-Reports-Admin-Token": "mod-token"})
    assert response.status_code == 200
    assert response.json()["stored_observations"] == 1
    db = connect_database(path)
    source = db.execute("SELECT source FROM official_observations WHERE vehicle_id='bus-2'").fetchone()[0]
    assert source == "official"
    db.close()
