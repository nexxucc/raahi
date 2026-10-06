"""Public API contracts; these models contain no private credentials."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TrackingModeValue(StrEnum):
    SIMULATED = "simulated"
    LIVE = "live"


class ObservationSource(StrEnum):
    OFFICIAL = "official"
    CROWD = "crowd"
    SIMULATED = "simulated"


class PositionStatus(StrEnum):
    LIVE = "live"
    PROVISIONAL = "provisional"
    STALE = "stale"
    SCHEDULED_ESTIMATE = "scheduled_estimate"
    UNAVAILABLE = "unavailable"
    SIMULATED = "simulated"


class HealthResponse(StrictModel):
    status: str = "ok"
    service: str
    tracking_mode: TrackingModeValue
    database: str = "not_configured"


class SourceMetadata(StrictModel):
    source: str
    version: str | None = None
    last_updated: datetime | None = None
    retrieved_at: datetime | None = None
    attribution: str
    accuracy_note: str


class MetaResponse(StrictModel):
    service: str
    api_version: str
    tracking_mode: TrackingModeValue
    official_feed_configured: bool
    data_sources: list[SourceMetadata]
    limitations: list[str]


class RouteResponse(StrictModel):
    route_id: str = Field(min_length=1)
    short_name: str | None = None
    long_name: str | None = None
    route_type: str = "bus"
    source: str


class PositionObservation(StrictModel):
    observation_id: str
    vehicle_id: str | None = None
    route_id: str | None = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    observed_at: datetime
    received_at: datetime
    source: ObservationSource

    @field_validator("observed_at", "received_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")
        return value


class VehiclePositionResponse(StrictModel):
    vehicle_id: str
    route_id: str | None = None
    direction_id: str | None = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    observed_at: datetime
    received_at: datetime
    age_seconds: int = Field(ge=0)
    sources: list[ObservationSource]
    confidence: float = Field(ge=0, le=1)
    status: PositionStatus

    @field_validator("observed_at", "received_at")
    @classmethod
    def timestamp_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")
        return value


class CrowdReportCreate(StrictModel):
    consent: bool
    report_type: str = Field(pattern="^(position|traffic|delay|breakdown|crowding)$")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    observed_at: datetime
    contributor_token: str = Field(min_length=16, max_length=256, repr=False)
    route_id: str | None = Field(default=None, max_length=128)
    vehicle_id: str | None = Field(default=None, max_length=128)

    @field_validator("observed_at")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")
        return value


class CrowdReportResponse(StrictModel):
    report_id: str
    received_at: datetime
    expires_at: datetime
    match_status: str
    matched_route_id: str | None = None
    match_distance_m: float | None = None


class TrackedVehicleResponse(StrictModel):
    vehicle_id: str | None = None
    route_id: str | None = None
    direction_id: str | None = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    observed_at: datetime
    received_at: datetime
    age_seconds: int = Field(ge=0)
    sources: list[ObservationSource]
    confidence: float = Field(ge=0, le=1)
    status: PositionStatus
    freshness_status: str
    crowd_contributors: int = Field(ge=0)
    disagreement_distance_m: float | None = None
    route_match_status: str
    identity_kind: str

    @field_validator("observed_at", "received_at")
    @classmethod
    def tracked_timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")
        return value


class StopResponse(StrictModel):
    stop_id: str
    stop_name: str
    latitude: float
    longitude: float
    distance_m: float | None = None


class ArrivalResponse(StrictModel):
    stop_id: str
    route_id: str
    trip_id: str
    scheduled_at: datetime
    minutes_until: int
    status: PositionStatus = PositionStatus.SCHEDULED_ESTIMATE
    basis: str = "static_gtfs_schedule"
    feed_retrieved_at: datetime | None = None

    @field_validator("scheduled_at", "feed_retrieved_at")
    @classmethod
    def arrival_timestamps_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamp must include a timezone offset")
        return value
