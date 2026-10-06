"""Read APIs for the imported transit network and fused bus positions."""

from __future__ import annotations

import hmac
import json
import math
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Query, Request

from app.fusion import fuse_vehicle_states
from app.official_feed import OfficialFeedError, OtdVehicleFeedClient
from app.schemas import ArrivalResponse, RouteResponse, StopResponse, TrackedVehicleResponse
from app.storage import connect_database, store_official_observations

router = APIRouter(prefix="/api/v1", tags=["transit"])
DELHI = ZoneInfo("Asia/Kolkata")


def _database(request: Request) -> sqlite3.Connection:
    return connect_database(request.app.state.settings.database_path)


def _current_feed(db: sqlite3.Connection):
    row = db.execute("SELECT feed_id FROM feed_metadata ORDER BY retrieved_at DESC LIMIT 1").fetchone()
    if row is None:
        raise HTTPException(503, "No static transit feed has been imported")
    return row["feed_id"]


def _route(row: sqlite3.Row) -> RouteResponse:
    payload = json.loads(row["payload_json"])
    return RouteResponse(
        route_id=row["route_id"], short_name=payload.get("route_short_name"),
        long_name=payload.get("route_long_name"), route_type=str(row["route_type"]),
        source=row["source_name"],
    )


def _vehicle(state: dict[str, object]) -> TrackedVehicleResponse:
    return TrackedVehicleResponse(
        **state,
    )


@router.get("/routes", response_model=list[RouteResponse])
def search_routes(
    request: Request,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[RouteResponse]:
    db = _database(request)
    try:
        feed_id = _current_feed(db)
        if q:
            like = f"%{q.strip()}%"
            rows = db.execute(
                "SELECT routes.*, feed_metadata.source_name FROM routes JOIN feed_metadata USING(feed_id) WHERE routes.feed_id=? AND (payload_json LIKE ? OR route_id LIKE ?) ORDER BY route_id LIMIT ?",
                (feed_id, like, like, limit),
            ).fetchall()
        else:
            rows = db.execute("SELECT routes.*, feed_metadata.source_name FROM routes JOIN feed_metadata USING(feed_id) WHERE routes.feed_id=? ORDER BY route_id LIMIT ?", (feed_id, limit)).fetchall()
        return [_route(row) for row in rows]
    finally:
        db.close()


@router.get("/routes/{route_id}", response_model=RouteResponse)
def route_detail(route_id: str, request: Request) -> RouteResponse:
    db = _database(request)
    try:
        feed_id = _current_feed(db)
        row = db.execute("SELECT routes.*, feed_metadata.source_name FROM routes JOIN feed_metadata USING(feed_id) WHERE routes.feed_id=? AND route_id=?", (feed_id, route_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Route not found")
        return _route(row)
    finally:
        db.close()


@router.get("/stops", response_model=list[StopResponse])
def search_stops(
    request: Request,
    q: str | None = Query(default=None, max_length=100),
    latitude: float | None = Query(default=None, ge=-90, le=90),
    longitude: float | None = Query(default=None, ge=-180, le=180),
    radius_m: float = Query(default=2000, gt=0, le=50000),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[StopResponse]:
    if (latitude is None) != (longitude is None):
        raise HTTPException(422, "latitude and longitude must be provided together")
    db = _database(request)
    try:
        feed_id = _current_feed(db)
        if latitude is not None and longitude is not None:
            rows = db.execute("SELECT * FROM stops WHERE feed_id=?", (feed_id,)).fetchall()
            candidates = []
            for row in rows:
                dy = math.radians(row["stop_lat"] - latitude) * 6_371_000
                dx = math.radians(row["stop_lon"] - longitude) * 6_371_000 * math.cos(math.radians(latitude))
                distance = math.hypot(dx, dy)
                if distance <= radius_m:
                    candidates.append((distance, row))
            candidates.sort(key=lambda item: item[0])
            return [StopResponse(stop_id=row["stop_id"], stop_name=row["stop_name"], latitude=row["stop_lat"], longitude=row["stop_lon"], distance_m=round(distance, 1)) for distance, row in candidates[:limit]]
        if q:
            rows = db.execute("SELECT * FROM stops WHERE feed_id=? AND stop_name LIKE ? ORDER BY stop_name LIMIT ?", (feed_id, f"%{q.strip()}%", limit)).fetchall()
        else:
            rows = db.execute("SELECT * FROM stops WHERE feed_id=? ORDER BY stop_name LIMIT ?", (feed_id, limit)).fetchall()
        return [StopResponse(stop_id=row["stop_id"], stop_name=row["stop_name"], latitude=row["stop_lat"], longitude=row["stop_lon"]) for row in rows]
    finally:
        db.close()


@router.get("/stops/{stop_id}", response_model=StopResponse)
def stop_detail(stop_id: str, request: Request) -> StopResponse:
    db = _database(request)
    try:
        feed_id = _current_feed(db)
        row = db.execute("SELECT * FROM stops WHERE feed_id=? AND stop_id=?", (feed_id, stop_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Stop not found")
        return StopResponse(stop_id=row["stop_id"], stop_name=row["stop_name"], latitude=row["stop_lat"], longitude=row["stop_lon"])
    finally:
        db.close()


@router.get("/routes/{route_id}/vehicles", response_model=list[TrackedVehicleResponse])
def route_vehicles(route_id: str, request: Request) -> list[TrackedVehicleResponse]:
    db = _database(request)
    try:
        feed_id = _current_feed(db)
        exists = db.execute("SELECT 1 FROM routes WHERE routes.feed_id=? AND route_id=?", (feed_id, route_id)).fetchone()
        if exists is None:
            raise HTTPException(404, "Route not found")
        return [_vehicle(state) for state in fuse_vehicle_states(db) if state["route_id"] == route_id]
    finally:
        db.close()


@router.get("/vehicles/{vehicle_id}", response_model=TrackedVehicleResponse)
def vehicle_detail(vehicle_id: str, request: Request) -> TrackedVehicleResponse:
    db = _database(request)
    try:
        for state in fuse_vehicle_states(db):
            if state["vehicle_id"] == vehicle_id:
                return _vehicle(state)
        raise HTTPException(404, "Vehicle not found or its observation has expired")
    finally:
        db.close()


@router.post("/admin/official-feed/fetch", tags=["admin"])
def fetch_official_feed_once(
    request: Request,
    x_reports_admin_token: str | None = Header(default=None),
) -> dict[str, object]:
    """Explicitly trigger one authorized OTD request; never called by a timer."""
    expected = request.app.state.settings.reports_admin_token
    if not expected or not x_reports_admin_token or not hmac.compare_digest(expected, x_reports_admin_token):
        raise HTTPException(403, "Feed fetch is restricted to authorized operators")
    try:
        client = OtdVehicleFeedClient.from_settings(request.app.state.settings)
        try:
            feed = client.fetch_once()
        finally:
            client.close()
    except OfficialFeedError as exc:
        raise HTTPException(503, str(exc)) from None
    db = _database(request)
    try:
        stored = store_official_observations(db, list(feed.observations))
    finally:
        db.close()
    return {
        "status": "ok", "feed_version": feed.feed_version,
        "feed_timestamp": feed.feed_timestamp, "received_at": feed.received_at,
        "stored_observations": stored, "rejected_entities": feed.rejected_entities,
        "polling": "one_shot_only",
    }


def _active_services(db: sqlite3.Connection, feed_id: str, service_date: date) -> set[str]:
    weekday = service_date.strftime("%A").lower()
    services: set[str] = set()
    calendars = db.execute("SELECT service_id, payload_json FROM calendars WHERE feed_id=?", (feed_id,)).fetchall()
    for row in calendars:
        item = json.loads(row["payload_json"])
        if item.get("start_date", "00000000") <= service_date.strftime("%Y%m%d") <= item.get("end_date", "99999999") and item.get(weekday) == "1":
            services.add(row["service_id"])
    overrides = db.execute("SELECT service_id, exception_type FROM calendar_dates WHERE feed_id=? AND service_date=?", (feed_id, service_date.strftime("%Y%m%d"))).fetchall()
    for row in overrides:
        if row["exception_type"] == 1:
            services.add(row["service_id"])
        elif row["exception_type"] == 2:
            services.discard(row["service_id"])
    return services


@router.get("/stops/{stop_id}/arrivals", response_model=list[ArrivalResponse])
def stop_arrivals(
    stop_id: str,
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
) -> list[ArrivalResponse]:
    db = _database(request)
    try:
        feed_id = _current_feed(db)
        if db.execute("SELECT 1 FROM stops WHERE feed_id=? AND stop_id=?", (feed_id, stop_id)).fetchone() is None:
            raise HTTPException(404, "Stop not found")
        now = datetime.now(DELHI)
        metadata = db.execute("SELECT retrieved_at FROM feed_metadata WHERE feed_id=?", (feed_id,)).fetchone()
        retrieved = datetime.fromisoformat(metadata["retrieved_at"])
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=UTC)
        arrivals: list[ArrivalResponse] = []
        for service_date in (now.date(), now.date() + timedelta(days=1)):
            active = _active_services(db, feed_id, service_date)
            if not active:
                continue
            rows = db.execute(
                """SELECT st.trip_id, st.arrival_seconds, t.route_id FROM stop_times st
                   JOIN trips t ON t.feed_id=st.feed_id AND t.trip_id=st.trip_id
                   WHERE st.feed_id=? AND st.stop_id=?""",
                (feed_id, stop_id),
            ).fetchall()
            midnight = datetime.combine(service_date, time.min, tzinfo=DELHI)
            for row in rows:
                trip = db.execute("SELECT service_id FROM trips WHERE feed_id=? AND trip_id=?", (feed_id, row["trip_id"])).fetchone()
                if trip["service_id"] not in active:
                    continue
                scheduled = midnight + timedelta(seconds=row["arrival_seconds"])
                minutes = int((scheduled - now).total_seconds() // 60)
                if 0 <= minutes <= 24 * 60:
                    arrivals.append(ArrivalResponse(stop_id=stop_id, route_id=row["route_id"], trip_id=row["trip_id"], scheduled_at=scheduled, minutes_until=minutes, feed_retrieved_at=retrieved))
        arrivals.sort(key=lambda item: item.scheduled_at)
        return arrivals[:limit]
    finally:
        db.close()
