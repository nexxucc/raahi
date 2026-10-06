"""FastAPI app factory and initial health/metadata endpoints."""

from __future__ import annotations

import logging
import logging.config
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from app.config import ConfigurationError, Settings
from app.crowd import router as crowd_router
from app.fusion import fuse_vehicle_states
from app.replay import SCENARIOS, apply_demo_scenario
from app.schemas import (
    HealthResponse,
    MetaResponse,
    SourceMetadata,
    TrackingModeValue,
)
from app.storage import connect_database
from app.tracking import router as tracking_router


def configure_logging(level: str) -> None:
    """Configure concise stdout logging without exposing settings or secrets."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"level": level, "handlers": ["console"]},
        }
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the application; settings can be injected for tests."""
    resolved = settings or Settings.from_env()
    configure_logging(resolved.log_level)

    application = FastAPI(
        title=resolved.app_name,
        version="0.1.0",
        description=(
            "Delhi bus-tracking API with explicit evidence and freshness labels. Live mode requires authorized OTD access; "
            "simulated mode never represents real vehicle positions."
        ),
    )
    application.state.settings = resolved
    application.include_router(crowd_router)
    application.include_router(tracking_router)

    def demo_network_ready() -> sqlite3.Row | None:
        db = connect_database(resolved.database_path)
        try:
            feed = db.execute(
                "SELECT source_name FROM feed_metadata ORDER BY retrieved_at DESC LIMIT 1"
            ).fetchone()
            if feed is None or feed["source_name"] != "Synthetic Delhi demo network":
                return None
            return db.execute("SELECT scenario_id, changed_at FROM demo_state WHERE state_id=1").fetchone()
        finally:
            db.close()

    @application.get("/api/v1/demo/scenarios", tags=["demo"])
    def demo_scenarios() -> dict[str, object]:
        state = demo_network_ready()
        if resolved.tracking_mode.value != "simulated" or state is None:
            raise HTTPException(404, "Demo scenarios are available only on the synthetic demo network")
        return {"active_scenario": state["scenario_id"], "scenarios": SCENARIOS}

    @application.post("/api/v1/demo/scenarios/{scenario_id}", tags=["demo"])
    def switch_demo_scenario(scenario_id: str) -> dict[str, object]:
        if resolved.tracking_mode.value != "simulated" or demo_network_ready() is None:
            raise HTTPException(404, "Demo scenarios are available only on the synthetic demo network")
        try:
            apply_demo_scenario(resolved.database_path, scenario_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        db = connect_database(resolved.database_path)
        try:
            count = len(fuse_vehicle_states(db))
            state = db.execute("SELECT changed_at FROM demo_state WHERE state_id=1").fetchone()
        finally:
            db.close()
        scenario = next(item for item in SCENARIOS if item["id"] == scenario_id)
        return {**scenario, "active_scenario": scenario_id, "changed_at": state["changed_at"], "positions": count}

    @application.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        mode = TrackingModeValue(resolved.tracking_mode.value)
        database_status = "ok"
        try:
            db = connect_database(resolved.database_path)
            try:
                db.execute("SELECT 1")
            finally:
                db.close()
        except (sqlite3.Error, OSError):
            database_status = "error"
        return HealthResponse(
            service=resolved.app_name, tracking_mode=mode,
            status="ok" if database_status == "ok" else "degraded",
            database=database_status,
        )

    @application.get("/api/v1/meta", response_model=MetaResponse, tags=["system"])
    def metadata() -> MetaResponse:
        now = datetime.now(UTC)
        live_mode = resolved.tracking_mode.value == "live"
        live_credentials_configured = (
            live_mode and resolved.otd_access_approved and bool(resolved.otd_api_key)
        )
        db = connect_database(resolved.database_path)
        try:
            feed = db.execute("SELECT * FROM feed_metadata ORDER BY retrieved_at DESC LIMIT 1").fetchone()
        finally:
            db.close()
        has_feed = feed is not None
        return MetaResponse(
            service=resolved.app_name,
            api_version=application.version,
            tracking_mode=TrackingModeValue(resolved.tracking_mode.value),
            official_feed_configured=live_credentials_configured,
            data_sources=[
                SourceMetadata(
                    source=feed["source_name"] if has_feed else "Static GTFS (not imported)",
                    version=feed["feed_version"] if has_feed else None,
                    last_updated=None,
                    retrieved_at=datetime.fromisoformat(feed["retrieved_at"]) if has_feed else None,
                    attribution=feed["attribution"] if has_feed else resolved.data_attribution,
                    accuracy_note=(
                        "Static bus stop times are approximate schedule estimates, "
                        "not live arrivals."
                    ),
                ),
                SourceMetadata(
                    source="OTD authorized vehicle positions" if live_mode else "simulated GPS replay",
                    last_updated=None,
                    retrieved_at=now if resolved.tracking_mode.value == "simulated" else None,
                    attribution=resolved.data_attribution if live_mode else "Synthetic demo data; not operator data",
                    accuracy_note=(
                        "Authorized positions can be fetched explicitly by an operator and queried with freshness and evidence. No background polling is enabled; use remains subject to OTD terms."
                        if live_mode
                        else "Synthetic vehicle observations are explicitly labelled simulated. Crowd-only clusters remain provisional and unidentified."
                    ),
                ),
            ],
            limitations=[
                (
                    "OTD polling is disabled; live access, schema, retention and redistribution require operator approval and confirmation."
                    if live_mode
                    else "Official live positions are disabled in simulated mode; demo vehicle observations are synthetic."
                ),
                "Crowd reports are short-lived and opt-in; live fusion requires fresh official observations or independent crowd agreement.",
                "Static timetable times are approximate and must not be treated as live ETAs.",
            ],
        )

    web_dist = Path(__file__).resolve().parents[1] / "web" / "dist"
    if web_dist.is_dir():
        application.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return application


try:
    app = create_app()
except ConfigurationError as exc:
    # Fail early with actionable configuration errors during startup.
    raise RuntimeError(f"Invalid application configuration: {exc}") from exc
