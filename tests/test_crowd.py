from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from functools import wraps

import httpx

from app.config import Settings
from app.gtfs import FeedMetadata, TransitFeed
from app.main import create_app
from app.matcher import match_position
from app.storage import connect_database, replace_feed


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


def seed_network(path: str, *, second_route: bool = False) -> None:
    routes = [{"route_id": "r1", "route_type": "3"}]
    trips = [{"route_id": "r1", "service_id": "s", "trip_id": "t1", "shape_id": "sh1"}]
    shapes = [
        {"shape_id": "sh1", "shape_pt_lat": "28.6000", "shape_pt_lon": "77.2000", "shape_pt_sequence": "1"},
        {"shape_id": "sh1", "shape_pt_lat": "28.6100", "shape_pt_lon": "77.2000", "shape_pt_sequence": "2"},
    ]
    if second_route:
        routes.append({"route_id": "r2", "route_type": "3"})
        trips.append({"route_id": "r2", "service_id": "s", "trip_id": "t2", "shape_id": "sh2"})
        shapes.extend([
            {"shape_id": "sh2", "shape_pt_lat": "28.6000", "shape_pt_lon": "77.2001", "shape_pt_sequence": "1"},
            {"shape_id": "sh2", "shape_pt_lat": "28.6100", "shape_pt_lon": "77.2001", "shape_pt_sequence": "2"},
        ])
    feed = TransitFeed(
        metadata=FeedMetadata("test", None, datetime.now(UTC), "fixture", "test-feed"),
        routes=routes,
        trips=trips,
        shapes=shapes,
    )
    db = connect_database(path)
    replace_feed(db, feed)
    db.close()


def make_client(path: str, *, admin: str | None = None) -> httpx.AsyncClient:
    app = create_app(Settings(database_path=path, reports_admin_token=admin))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def payload(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "consent": True,
        "report_type": "position",
        "latitude": 28.605,
        "longitude": 77.2,
        "observed_at": datetime.now(UTC).isoformat(),
        "contributor_token": "ephemeral-test-contributor-token",
        "route_id": "r1",
    }
    data.update(overrides)
    return data


@async_test
async def test_valid_report_is_matched_and_stored_without_raw_token(tmp_path) -> None:
    path = str(tmp_path / "db.sqlite")
    seed_network(path)
    async with make_client(path) as client:
        response = await client.post("/api/v1/crowd/reports", json=payload())
    assert response.status_code == 201, response.text
    assert response.json()["match_status"] == "matched"
    assert response.json()["matched_route_id"] == "r1"
    db = connect_database(path)
    row = db.execute("SELECT contributor_digest, expires_at FROM crowd_reports").fetchone()
    assert row["contributor_digest"] != "ephemeral-test-contributor-token"
    assert datetime.fromisoformat(row["expires_at"]) - datetime.now(UTC) <= timedelta(minutes=30)
    db.close()


@async_test
async def test_report_validation_consent_timestamp_route_and_jump(tmp_path) -> None:
    path = str(tmp_path / "db.sqlite")
    seed_network(path)
    async with make_client(path) as client:
        assert (await client.post("/api/v1/crowd/reports", json=payload(consent=False))).status_code == 400
        assert (await client.post("/api/v1/crowd/reports", json=payload(observed_at=(datetime.now(UTC)-timedelta(minutes=20)).isoformat()))).status_code == 422
        assert (await client.post("/api/v1/crowd/reports", json=payload(observed_at=(datetime.now(UTC)+timedelta(minutes=3)).isoformat()))).status_code == 422
        assert (await client.post("/api/v1/crowd/reports", json=payload(latitude=91))).status_code == 422
        assert (await client.post("/api/v1/crowd/reports", json=payload(route_id="missing"))).status_code == 422
        first = await client.post("/api/v1/crowd/reports", json=payload())
        assert first.status_code == 201
        duplicate = await client.post("/api/v1/crowd/reports", json=payload())
        assert duplicate.status_code == 409
        jump = await client.post("/api/v1/crowd/reports", json=payload(
            longitude=77.4, observed_at=datetime.now(UTC).isoformat(),
            contributor_token="different-contributor-token-123",
        ))
        assert jump.status_code == 201  # a different token is a separate rider


@async_test
async def test_rate_limit_and_implausible_same_contributor_jump(tmp_path) -> None:
    path = str(tmp_path / "db.sqlite")
    seed_network(path)
    async with make_client(path) as client:
        # Reports are separated beyond the duplicate guard, but stay within the rate window.
        from app import crowd
        original = crowd._utc_now
        for offset in range(5):
            crowd._utc_now = lambda offset=offset: original() + timedelta(seconds=offset * 11)
            response = await client.post("/api/v1/crowd/reports", json=payload(
                contributor_token="rate-limit-token-12345", longitude=77.2 + offset * 0.00001
            ))
            assert response.status_code == 201, response.text
        crowd._utc_now = lambda: original() + timedelta(seconds=55)
        limited = await client.post("/api/v1/crowd/reports", json=payload(
            contributor_token="rate-limit-token-12345", longitude=77.2001
        ))
        assert limited.status_code == 429
        # Separate client DB to test a physically impossible jump without rate/duplicate interference.
        crowd._utc_now = original
    path2 = str(tmp_path / "jump.sqlite")
    seed_network(path2)
    async with make_client(path2) as client:
        from app import crowd
        original = crowd._utc_now
        assert (await client.post("/api/v1/crowd/reports", json=payload(
            contributor_token="jump-test-token-12345"
        ))).status_code == 201
        crowd._utc_now = lambda: original() + timedelta(seconds=11)
        jump = await client.post("/api/v1/crowd/reports", json=payload(
            contributor_token="jump-test-token-12345", longitude=77.4
        ))
        crowd._utc_now = original
        assert jump.status_code == 422


@async_test
async def test_expired_reports_are_cleaned_and_delete_requires_admin(tmp_path) -> None:
    path = str(tmp_path / "db.sqlite")
    seed_network(path)
    db = connect_database(path)
    db.execute("""INSERT INTO crowd_reports
      (report_id, observed_at, received_at, latitude, longitude, report_type,
       contributor_digest, expires_at) VALUES ('old', 'x', 'x', 0, 0, 'position', 'x', '2000-01-01T00:00:00+00:00')""")
    db.commit()
    db.close()
    async with make_client(path, admin="mod-secret") as client:
        assert (await client.delete("/api/v1/crowd/reports/old")).status_code == 403
        created = await client.post("/api/v1/crowd/reports", json=payload())
        assert created.status_code == 201
        assert (await client.delete(
            f"/api/v1/crowd/reports/{created.json()['report_id']}",
            headers={"x-reports-admin-token": "mod-secret"},
        )).status_code == 204
    db = connect_database(path)
    assert db.execute("SELECT COUNT(*) FROM crowd_reports WHERE report_id='old'").fetchone()[0] == 0
    db.close()


def test_shape_matcher_resolves_hint_and_keeps_parallel_routes_ambiguous(tmp_path) -> None:
    path = str(tmp_path / "db.sqlite")
    seed_network(path, second_route=True)
    db = connect_database(path)
    ambiguous = match_position(db, 28.605, 77.20005)
    hinted = match_position(db, 28.605, 77.20005, "r1")
    off_route = match_position(db, 28.605, 77.22)
    assert ambiguous["status"] == "ambiguous"
    assert hinted["status"] == "matched" and hinted["route_id"] == "r1"
    assert hinted["segment"] == 0
    assert "vehicle_id" not in hinted and "direction_id" not in hinted
    assert off_route["status"] == "off_route"
    assert match_position(db, 28.605, 77.2)["status"] == "ambiguous"
    db.close()


def test_matcher_reports_no_network(tmp_path) -> None:
    db = connect_database(str(tmp_path / "empty.sqlite"))
    assert match_position(db, 28.6, 77.2)["status"] == "no_network"
    db.close()
