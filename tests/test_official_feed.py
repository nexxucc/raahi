from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
import pytest
from google.transit import gtfs_realtime_pb2 as gtfs_rt

from app.config import ConfigurationError, Settings, TrackingMode
from app.official_feed import (
    FeedHealthStatus,
    OfficialFeedError,
    OtdVehicleFeedClient,
    TimestampSource,
    decode_vehicle_positions,
)

RECEIVED_AT = datetime(2026, 10, 6, 8, 30, tzinfo=UTC)


def make_feed(
    *,
    include_header_timestamp: bool = True,
    include_vehicle_timestamp: bool = True,
    latitude: float = 28.6139,
    longitude: float = 77.2090,
    vehicle_id: str = "bus-42",
    include_vehicle_descriptor_id: bool = True,
    include_position: bool = True,
) -> bytes:
    message = gtfs_rt.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    if include_header_timestamp:
        message.header.timestamp = int(RECEIVED_AT.timestamp())
    entity = message.entity.add()
    entity.id = "entity-42"
    vehicle_position = entity.vehicle
    trip = vehicle_position.trip
    trip.route_id = "route-7"
    trip.direction_id = 1
    if include_vehicle_descriptor_id:
        vehicle_position.vehicle.id = vehicle_id
    if include_position:
        vehicle_position.position.latitude = latitude
        vehicle_position.position.longitude = longitude
        vehicle_position.position.bearing = 275.0
        vehicle_position.position.speed = 6.25
    if include_vehicle_timestamp:
        vehicle_position.timestamp = int(RECEIVED_AT.timestamp()) - 12
    vehicle_position.stop_id = "stop-3"
    vehicle_position.current_status = gtfs_rt.VehiclePosition.IN_TRANSIT_TO
    return message.SerializeToString()


def live_settings(*, api_key: str = "unit-test-secret") -> Settings:
    return Settings(
        tracking_mode=TrackingMode.LIVE,
        otd_access_approved=True,
        otd_api_key=api_key,
    )


def test_decodes_vehicle_position_fields_and_provenance() -> None:
    decoded = decode_vehicle_positions(make_feed(), received_at=RECEIVED_AT)

    assert decoded.feed_version == "2.0"
    assert decoded.feed_timestamp == RECEIVED_AT
    assert len(decoded.observations) == 1
    observation = decoded.observations[0]
    assert observation.vehicle_id == "bus-42"
    assert observation.route_id == "route-7"
    assert observation.direction_id == "1"
    assert observation.latitude == pytest.approx(28.6139)
    assert observation.longitude == pytest.approx(77.2090)
    assert observation.timestamp_source is TimestampSource.VEHICLE
    assert observation.vehicle_id_source == "vehicle_descriptor_id"
    assert observation.observed_at.timestamp() == RECEIVED_AT.timestamp() - 12
    assert observation.current_stop_id == "stop-3"
    assert observation.current_status == "in_transit_to"
    assert observation.speed_mps == pytest.approx(6.25)


def test_timestamp_falls_back_to_header_then_received_time() -> None:
    header_timestamp = decode_vehicle_positions(
        make_feed(include_vehicle_timestamp=False), received_at=RECEIVED_AT
    ).observations[0]
    assert header_timestamp.timestamp_source is TimestampSource.FEED_HEADER
    assert header_timestamp.observed_at == RECEIVED_AT

    inferred = decode_vehicle_positions(
        make_feed(include_header_timestamp=False, include_vehicle_timestamp=False),
        received_at=RECEIVED_AT,
    ).observations[0]
    assert inferred.timestamp_source is TimestampSource.RECEIVED_AT_INFERRED
    assert inferred.observed_at == RECEIVED_AT


def test_uses_feed_entity_id_if_physical_vehicle_id_is_missing() -> None:
    observation = decode_vehicle_positions(
        make_feed(include_vehicle_descriptor_id=False), received_at=RECEIVED_AT
    ).observations[0]
    assert observation.vehicle_id == "entity-42"
    assert observation.vehicle_id_source == "entity_id_fallback"


def test_duplicate_vehicle_entries_keep_newest_observation() -> None:
    message = gtfs_rt.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.timestamp = int(RECEIVED_AT.timestamp())
    for entity_id, lat, seconds_ago in (
        ("old-entity", 28.60, 20),
        ("new-entity", 28.62, 5),
    ):
        entity = message.entity.add()
        entity.id = entity_id
        entity.vehicle.vehicle.id = "same-bus"
        entity.vehicle.position.latitude = lat
        entity.vehicle.position.longitude = 77.2
        entity.vehicle.timestamp = int(RECEIVED_AT.timestamp()) - seconds_ago

    decoded = decode_vehicle_positions(message.SerializeToString(), received_at=RECEIVED_AT)

    assert len(decoded.observations) == 1
    assert decoded.observations[0].latitude == pytest.approx(28.62)
    assert decoded.rejected_entities == 1


def test_rejects_bad_positions_and_missing_position_without_dropping_valid_feed() -> None:
    message = gtfs_rt.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.timestamp = int(RECEIVED_AT.timestamp())
    invalid_entity = message.entity.add()
    invalid_entity.id = "bad-coordinate"
    invalid_entity.vehicle.vehicle.id = "bus-bad"
    invalid_entity.vehicle.position.latitude = 95
    invalid_entity.vehicle.position.longitude = 77
    missing_position = message.entity.add()
    missing_position.id = "missing-position"
    missing_position.vehicle.vehicle.id = "bus-no-position"
    decoded = decode_vehicle_positions(message.SerializeToString(), received_at=RECEIVED_AT)

    assert decoded.observations == ()
    assert decoded.rejected_entities == 2


@pytest.mark.parametrize("payload", [b"", b"not protobuf"])
def test_rejects_empty_or_invalid_protobuf(payload: bytes) -> None:
    with pytest.raises(OfficialFeedError):
        decode_vehicle_positions(payload, received_at=RECEIVED_AT)


def test_rejects_naive_receive_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        decode_vehicle_positions(
            make_feed(), received_at=datetime(2026, 10, 6, 8, 30)  # noqa: DTZ001 - invalid input
        )


def test_client_refuses_fetch_without_explicit_access() -> None:
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(200, content=make_feed())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(OfficialFeedError, match="simulated mode"):
        OtdVehicleFeedClient.from_settings(Settings(), http_client=client)
    assert requested == []
    client.close()


def test_client_fetches_one_mocked_authorized_snapshot_and_tracks_health() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=make_feed())

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = OtdVehicleFeedClient.from_settings(live_settings(), http_client=http_client)
    result = client.fetch_once()

    assert len(requests) == 1
    assert requests[0].url.path == "/api/realtime/VehiclePositions.pb"
    assert requests[0].url.params["key"] == "unit-test-secret"
    assert len(result.observations) == 1
    assert client.health.status is FeedHealthStatus.OK
    assert client.health.last_observation_count == 1
    assert client.health.last_error_code is None
    http_client.close()


def test_upstream_error_is_sanitized_and_health_records_failure(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key: unit-test-secret")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = OtdVehicleFeedClient.from_settings(live_settings(), http_client=http_client)
    with caplog.at_level(logging.DEBUG), pytest.raises(OfficialFeedError) as raised:
        client.fetch_once()

    assert raised.value.code == "upstream_http_error"
    assert "unit-test-secret" not in str(raised.value)
    assert "unit-test-secret" not in caplog.text
    assert client.health.status is FeedHealthStatus.ERROR
    assert client.health.last_error_code == "upstream_http_error"
    http_client.close()


def test_timeout_is_sanitized_and_health_records_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private URL: " + str(request.url), request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = OtdVehicleFeedClient.from_settings(live_settings(), http_client=http_client)
    with pytest.raises(OfficialFeedError) as raised:
        client.fetch_once()

    assert raised.value.code == "timeout"
    assert "unit-test-secret" not in str(raised.value)
    assert client.health.status is FeedHealthStatus.ERROR
    assert client.health.last_error_code == "timeout"
    http_client.close()


def test_live_mode_settings_reject_unapproved_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACKING_MODE", "live")
    monkeypatch.setenv("OTD_ACCESS_APPROVED", "true")
    monkeypatch.delenv("OTD_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="OTD_API_KEY"):
        Settings.from_env()
