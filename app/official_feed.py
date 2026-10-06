"""One-shot OTD GTFS-Realtime VehiclePositions adapter.

The adapter deliberately has no built-in polling cadence. OTD has not published
one; callers must use a cadence explicitly allowed by OTD before scheduling calls.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from urllib.parse import urlparse

import httpx
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

from app.config import Settings, TrackingMode
from app.schemas import ObservationSource

LOGGER = logging.getLogger(__name__)
MAX_FEED_BYTES = 20 * 1024 * 1024
_HTTPX_LOGGER = logging.getLogger("httpx")
_SECRET_QUERY_PATTERN = re.compile(r"(?i)([?&]key=)[^&\s\"']+")


class _QueryCredentialRedactionFilter(logging.Filter):
    """Redact OTD's query-string credential from HTTPX request log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _SECRET_QUERY_PATTERN.sub(r"\1[REDACTED]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


if not any(isinstance(item, _QueryCredentialRedactionFilter) for item in _HTTPX_LOGGER.filters):
    _HTTPX_LOGGER.addFilter(_QueryCredentialRedactionFilter())


class OfficialFeedError(RuntimeError):
    """Sanitized adapter error; never embeds the credential-bearing URL."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class TimestampSource(StrEnum):
    VEHICLE = "vehicle"
    FEED_HEADER = "feed_header"
    RECEIVED_AT_INFERRED = "received_at_inferred"


class FeedHealthStatus(StrEnum):
    NEVER_POLLED = "never_polled"
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class OfficialVehicleObservation:
    observation_id: str
    vehicle_id: str
    route_id: str | None
    direction_id: str | None
    latitude: float
    longitude: float
    observed_at: datetime
    received_at: datetime
    timestamp_source: TimestampSource
    vehicle_id_source: str
    source: ObservationSource = ObservationSource.OFFICIAL
    bearing_degrees: float | None = None
    speed_mps: float | None = None
    current_stop_id: str | None = None
    current_status: str | None = None


@dataclass(frozen=True, slots=True)
class DecodedVehicleFeed:
    feed_version: str
    feed_timestamp: datetime | None
    received_at: datetime
    observations: tuple[OfficialVehicleObservation, ...]
    rejected_entities: int


@dataclass(slots=True)
class OfficialFeedHealth:
    status: FeedHealthStatus = FeedHealthStatus.NEVER_POLLED
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_feed_timestamp: datetime | None = None
    last_feed_version: str | None = None
    last_observation_count: int = 0
    last_rejected_entity_count: int = 0
    last_error_code: str | None = None


def _unix_datetime(timestamp: int) -> datetime:
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise OfficialFeedError("invalid_timestamp", "Feed contains an invalid timestamp") from exc


def decode_vehicle_positions(
    payload: bytes,
    *,
    received_at: datetime | None = None,
) -> DecodedVehicleFeed:
    """Decode a GTFS-Realtime FeedMessage and normalize valid vehicle positions."""
    received = received_at or datetime.now(UTC)
    if received.tzinfo is None or received.utcoffset() is None:
        raise ValueError("received_at must be timezone-aware")
    if not payload:
        raise OfficialFeedError("empty_feed", "OTD returned an empty vehicle feed")
    if len(payload) > MAX_FEED_BYTES:
        raise OfficialFeedError("feed_too_large", "Vehicle feed exceeded the configured size limit")

    message = gtfs_realtime_pb2.FeedMessage()
    try:
        message.ParseFromString(payload)
    except DecodeError as exc:
        raise OfficialFeedError("invalid_protobuf", "OTD returned an invalid protobuf feed") from exc
    if not message.IsInitialized():
        missing = ", ".join(message.FindInitializationErrors())
        raise OfficialFeedError(
            "incomplete_feed",
            f"GTFS-Realtime feed is missing required fields: {missing}",
        )

    header = message.header
    if not header.gtfs_realtime_version.strip():
        raise OfficialFeedError("missing_feed_version", "GTFS-Realtime header has no version")
    feed_timestamp = (
        _unix_datetime(header.timestamp)
        if header.HasField("timestamp")
        else None
    )
    observations: list[OfficialVehicleObservation] = []
    rejected = 0

    for entity in message.entity:
        if not entity.HasField("vehicle"):
            continue
        vehicle = entity.vehicle
        if not vehicle.HasField("position"):
            rejected += 1
            continue

        position = vehicle.position
        latitude = float(position.latitude)
        longitude = float(position.longitude)
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            rejected += 1
            continue

        descriptor = vehicle.vehicle if vehicle.HasField("vehicle") else None
        trip = vehicle.trip if vehicle.HasField("trip") else None
        vehicle_id = None
        vehicle_id_source = "entity_id_fallback"
        if descriptor is not None:
            if descriptor.id:
                vehicle_id = descriptor.id
                vehicle_id_source = "vehicle_descriptor_id"
            elif descriptor.label:
                vehicle_id = descriptor.label
                vehicle_id_source = "vehicle_descriptor_label"
        # Entity IDs are feed-unique and provide a safe fallback when the agency
        # omits a physical vehicle identifier. Preserve no license-plate data.
        vehicle_id = vehicle_id or entity.id or None
        if not vehicle_id:
            rejected += 1
            continue

        if vehicle.HasField("timestamp"):
            observed_at = _unix_datetime(vehicle.timestamp)
            timestamp_source = TimestampSource.VEHICLE
        elif feed_timestamp is not None:
            observed_at = feed_timestamp
            timestamp_source = TimestampSource.FEED_HEADER
        else:
            observed_at = received
            timestamp_source = TimestampSource.RECEIVED_AT_INFERRED

        direction_id = None
        route_id = None
        if trip is not None:
            route_id = trip.route_id or None
            if trip.HasField("direction_id"):
                direction_id = str(trip.direction_id)

        current_status = None
        if vehicle.HasField("current_status"):
            try:
                current_status = gtfs_realtime_pb2.VehiclePosition.VehicleStopStatus.Name(
                    vehicle.current_status
                ).lower()
            except ValueError:
                current_status = "unknown"

        bearing = float(position.bearing) if position.HasField("bearing") else None
        speed = float(position.speed) if position.HasField("speed") else None
        if bearing is not None and not math.isfinite(bearing):
            bearing = None
        if speed is not None and (not math.isfinite(speed) or speed < 0):
            speed = None

        observations.append(
            OfficialVehicleObservation(
                observation_id=entity.id,
                vehicle_id=vehicle_id,
                route_id=route_id,
                direction_id=direction_id,
                latitude=latitude,
                longitude=longitude,
                observed_at=observed_at,
                received_at=received,
                timestamp_source=timestamp_source,
                vehicle_id_source=vehicle_id_source,
                bearing_degrees=bearing,
                speed_mps=speed,
                current_stop_id=vehicle.stop_id or None,
                current_status=current_status,
            )
        )

    # A GTFS-Realtime snapshot should have one active position per physical
    # vehicle. If duplicates exist, keep the freshest and count discarded rows.
    by_vehicle: dict[str, OfficialVehicleObservation] = {}
    for observation in observations:
        previous = by_vehicle.get(observation.vehicle_id)
        if previous is None:
            by_vehicle[observation.vehicle_id] = observation
        else:
            rejected += 1
            if observation.observed_at > previous.observed_at:
                by_vehicle[observation.vehicle_id] = observation

    return DecodedVehicleFeed(
        feed_version=header.gtfs_realtime_version,
        feed_timestamp=feed_timestamp,
        received_at=received,
        observations=tuple(by_vehicle.values()),
        rejected_entities=rejected,
    )


class OtdVehicleFeedClient:
    """Fetch and decode one OTD VehiclePositions snapshot at a time."""

    def __init__(
        self,
        *,
        api_key: str,
        feed_url: str,
        timeout_seconds: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise OfficialFeedError("missing_credentials", "An OTD API key is required")
        parsed_url = urlparse(feed_url)
        try:
            feed_port = parsed_url.port
        except ValueError:
            feed_port = -1
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "otd.delhi.gov.in"
            or parsed_url.path != "/api/realtime/VehiclePositions.pb"
            or feed_port not in {None, 443}
            or parsed_url.query
            or parsed_url.fragment
            or parsed_url.username
            or parsed_url.password
        ):
            raise OfficialFeedError("invalid_endpoint", "Only the documented OTD feed endpoint is supported")
        self._api_key = api_key
        self._feed_url = feed_url
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client or httpx.Client()
        self._owns_http_client = http_client is None
        self.health = OfficialFeedHealth()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        http_client: httpx.Client | None = None,
    ) -> OtdVehicleFeedClient:
        if settings.tracking_mode is not TrackingMode.LIVE:
            raise OfficialFeedError("live_mode_required", "Live feed is disabled in simulated mode")
        if not settings.otd_access_approved or not settings.otd_api_key:
            raise OfficialFeedError(
                "authorization_required",
                "OTD access approval and an issued API key are required",
            )
        return cls(
            api_key=settings.otd_api_key,
            feed_url=settings.otd_feed_url,
            timeout_seconds=settings.otd_request_timeout_seconds,
            http_client=http_client,
        )

    def fetch_once(self) -> DecodedVehicleFeed:
        """Make one authorized request. Does not schedule or retry polling."""
        attempted_at = datetime.now(UTC)
        self.health.last_attempt_at = attempted_at
        payload = bytearray()
        try:
            with self._http_client.stream(
                "GET",
                self._feed_url,
                params={"key": self._api_key},
                headers={"Accept": "application/x-protobuf, application/octet-stream"},
                timeout=self._timeout_seconds,
            ) as response:
                if response.status_code != 200:
                    self._mark_error("upstream_http_error")
                    raise OfficialFeedError(
                        "upstream_http_error",
                        f"OTD vehicle feed returned HTTP {response.status_code}",
                    ) from None
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_FEED_BYTES:
                        self._mark_error("feed_too_large")
                        raise OfficialFeedError(
                            "feed_too_large",
                            "Vehicle feed exceeded the configured size limit",
                        ) from None
        except httpx.TimeoutException:
            self._mark_error("timeout")
            raise OfficialFeedError("timeout", "Timed out while requesting the OTD vehicle feed") from None
        except httpx.RequestError:
            self._mark_error("request_failed")
            raise OfficialFeedError("request_failed", "Could not request the OTD vehicle feed") from None
        try:
            feed = decode_vehicle_positions(bytes(payload))
        except OfficialFeedError as exc:
            self._mark_error(exc.code)
            raise

        self.health.status = FeedHealthStatus.OK
        self.health.last_success_at = feed.received_at
        self.health.last_feed_timestamp = feed.feed_timestamp
        self.health.last_feed_version = feed.feed_version
        self.health.last_observation_count = len(feed.observations)
        self.health.last_rejected_entity_count = feed.rejected_entities
        self.health.last_error_code = None
        LOGGER.info(
            "OTD vehicle feed received version=%s observations=%d rejected=%d",
            feed.feed_version,
            len(feed.observations),
            feed.rejected_entities,
        )
        return feed

    def _mark_error(self, code: str) -> None:
        self.health.status = FeedHealthStatus.ERROR
        self.health.last_error_code = code

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
