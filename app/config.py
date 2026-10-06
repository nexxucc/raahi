"""Environment-based application settings with safe live-mode defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    """Raised when settings are missing or inconsistent."""


class TrackingMode(StrEnum):
    SIMULATED = "simulated"
    LIVE = "live"


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "Delhi Transit Tracker API"
    tracking_mode: TrackingMode = TrackingMode.SIMULATED
    log_level: str = "INFO"
    otd_access_approved: bool = False
    otd_api_key: str | None = field(default=None, repr=False)
    otd_feed_url: str = "https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb"
    otd_request_timeout_seconds: float = 10.0
    database_path: str = "data/transit.sqlite3"
    reports_admin_token: str | None = field(default=None, repr=False)
    data_attribution: str = "Delhi Open Transit Data (OTD)"

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        mode_value = os.getenv("TRACKING_MODE", TrackingMode.SIMULATED.value).strip().lower()
        try:
            mode = TrackingMode(mode_value)
        except ValueError as exc:
            supported = ", ".join(item.value for item in TrackingMode)
            raise ConfigurationError(f"TRACKING_MODE must be one of: {supported}") from exc

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

        approved = _read_bool("OTD_ACCESS_APPROVED", False)
        api_key = os.getenv("OTD_API_KEY", "").strip() or None
        feed_url = os.getenv(
            "OTD_FEED_URL", defaults.otd_feed_url
        ).strip()
        try:
            timeout_seconds = float(os.getenv("OTD_REQUEST_TIMEOUT_SECONDS", "10"))
        except ValueError as exc:
            raise ConfigurationError("OTD_REQUEST_TIMEOUT_SECONDS must be a positive number") from exc
        parsed_feed_url = urlparse(feed_url)
        try:
            feed_port = parsed_feed_url.port
        except ValueError as exc:
            raise ConfigurationError("OTD_FEED_URL contains an invalid port") from exc
        if (
            parsed_feed_url.scheme != "https"
            or parsed_feed_url.hostname != "otd.delhi.gov.in"
            or parsed_feed_url.path != "/api/realtime/VehiclePositions.pb"
            or feed_port not in {None, 443}
            or parsed_feed_url.query
            or parsed_feed_url.username
            or parsed_feed_url.password
            or parsed_feed_url.fragment
        ):
            raise ConfigurationError(
                "OTD_FEED_URL must use the documented HTTPS OTD VehiclePositions endpoint"
            )
        if timeout_seconds <= 0:
            raise ConfigurationError("OTD_REQUEST_TIMEOUT_SECONDS must be a positive number")
        if mode is TrackingMode.LIVE:
            missing = []
            if not approved:
                missing.append("OTD_ACCESS_APPROVED=true after OTD grants access")
            if not api_key:
                missing.append("OTD_API_KEY")
            if missing:
                raise ConfigurationError(
                    "TRACKING_MODE=live requires " + " and ".join(missing)
                )

        app_name = os.getenv("APP_NAME", defaults.app_name).strip()
        attribution = os.getenv("DATA_ATTRIBUTION", defaults.data_attribution).strip()
        if not app_name:
            raise ConfigurationError("APP_NAME cannot be empty")
        if not attribution:
            raise ConfigurationError("DATA_ATTRIBUTION cannot be empty")

        return cls(
            app_name=app_name,
            tracking_mode=mode,
            log_level=log_level,
            otd_access_approved=approved,
            otd_api_key=api_key,
            otd_feed_url=feed_url,
            otd_request_timeout_seconds=timeout_seconds,
            database_path=os.getenv("DATABASE_PATH", "data/transit.sqlite3").strip(),
            reports_admin_token=os.getenv("REPORTS_ADMIN_TOKEN", "").strip() or None,
            data_attribution=attribution,
        )
