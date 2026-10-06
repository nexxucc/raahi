"""Transactional SQLite storage for validated GTFS snapshots."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.gtfs import TransitFeed, parse_gtfs_time

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS feed_metadata (
    feed_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_url TEXT,
    retrieved_at TEXT NOT NULL,
    attribution TEXT NOT NULL,
    archive_sha256 TEXT NOT NULL,
    feed_version TEXT,
    feed_start_date TEXT,
    feed_end_date TEXT
);
CREATE TABLE IF NOT EXISTS agencies (
    feed_id TEXT NOT NULL REFERENCES feed_metadata(feed_id) ON DELETE CASCADE,
    agency_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (feed_id, agency_id)
);
CREATE TABLE IF NOT EXISTS routes (
    feed_id TEXT NOT NULL REFERENCES feed_metadata(feed_id) ON DELETE CASCADE,
    route_id TEXT NOT NULL,
    agency_id TEXT,
    route_type INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (feed_id, route_id)
);
CREATE INDEX IF NOT EXISTS idx_routes_route_type ON routes(route_type);
CREATE TABLE IF NOT EXISTS stops (
    feed_id TEXT NOT NULL REFERENCES feed_metadata(feed_id) ON DELETE CASCADE,
    stop_id TEXT NOT NULL,
    parent_station TEXT,
    stop_name TEXT NOT NULL,
    stop_lat REAL NOT NULL,
    stop_lon REAL NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (feed_id, stop_id)
);
CREATE INDEX IF NOT EXISTS idx_stops_name ON stops(stop_name);
CREATE TABLE IF NOT EXISTS shape_points (
    feed_id TEXT NOT NULL REFERENCES feed_metadata(feed_id) ON DELETE CASCADE,
    shape_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (feed_id, shape_id, sequence)
);
CREATE TABLE IF NOT EXISTS trips (
    feed_id TEXT NOT NULL REFERENCES feed_metadata(feed_id) ON DELETE CASCADE,
    trip_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    service_id TEXT NOT NULL,
    shape_id TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (feed_id, trip_id)
);
CREATE INDEX IF NOT EXISTS idx_trips_route ON trips(feed_id, route_id);
CREATE TABLE IF NOT EXISTS calendars (
    feed_id TEXT NOT NULL REFERENCES feed_metadata(feed_id) ON DELETE CASCADE,
    service_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (feed_id, service_id)
);
CREATE TABLE IF NOT EXISTS calendar_dates (
    feed_id TEXT NOT NULL REFERENCES feed_metadata(feed_id) ON DELETE CASCADE,
    service_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    exception_type INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (feed_id, service_id, service_date)
);
CREATE TABLE IF NOT EXISTS stop_times (
    feed_id TEXT NOT NULL REFERENCES feed_metadata(feed_id) ON DELETE CASCADE,
    trip_id TEXT NOT NULL,
    stop_sequence INTEGER NOT NULL,
    stop_id TEXT NOT NULL,
    arrival_seconds INTEGER NOT NULL,
    departure_seconds INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (feed_id, trip_id, stop_sequence)
);
CREATE INDEX IF NOT EXISTS idx_stop_times_stop ON stop_times(feed_id, stop_id, arrival_seconds);
CREATE TABLE IF NOT EXISTS crowd_reports (
    report_id TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    report_type TEXT NOT NULL,
    route_id TEXT,
    vehicle_id TEXT,
    contributor_digest TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    matched_route_id TEXT,
    matched_shape_id TEXT,
    match_distance_m REAL,
    match_segment INTEGER,
    match_status TEXT NOT NULL DEFAULT 'unmatched',
    synthetic INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_crowd_reports_expiry ON crowd_reports(expires_at);
CREATE INDEX IF NOT EXISTS idx_crowd_reports_contributor ON crowd_reports(contributor_digest, received_at);
CREATE TABLE IF NOT EXISTS official_observations (
    observation_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    route_id TEXT,
    direction_id TEXT,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'official',
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_official_route_time ON official_observations(route_id, observed_at);
CREATE TABLE IF NOT EXISTS demo_state (
    state_id INTEGER PRIMARY KEY CHECK(state_id=1),
    scenario_id TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
"""


def _json(row: dict[str, str]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def connect_database(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    official_columns = {row["name"] for row in connection.execute("PRAGMA table_info(official_observations)")}
    if "source" not in official_columns:
        connection.execute("ALTER TABLE official_observations ADD COLUMN source TEXT NOT NULL DEFAULT 'official'")
        connection.commit()
    crowd_columns = {row["name"] for row in connection.execute("PRAGMA table_info(crowd_reports)")}
    if "synthetic" not in crowd_columns:
        connection.execute("ALTER TABLE crowd_reports ADD COLUMN synthetic INTEGER NOT NULL DEFAULT 0")
        connection.commit()
    return connection


def replace_feed(connection: sqlite3.Connection, feed: TransitFeed) -> str:
    """Replace one content-addressed feed atomically; prior feed survives failure."""
    feed_id = feed.metadata.archive_sha256
    metadata = feed.metadata
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM feed_metadata WHERE feed_id = ?", (feed_id,))
        connection.execute(
            """INSERT INTO feed_metadata
            (feed_id, source_name, source_url, retrieved_at, attribution, archive_sha256,
             feed_version, feed_start_date, feed_end_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                feed_id,
                metadata.source_name,
                metadata.source_url,
                metadata.retrieved_at.isoformat(),
                metadata.attribution,
                metadata.archive_sha256,
                metadata.feed_version,
                metadata.feed_start_date,
                metadata.feed_end_date,
            ),
        )
        connection.executemany(
            "INSERT INTO agencies VALUES (?, ?, ?)",
            [
                (feed_id, row.get("agency_id") or "default", _json(row))
                for row in feed.agencies
            ],
        )
        connection.executemany(
            "INSERT INTO routes VALUES (?, ?, ?, ?, ?)",
            [
                (feed_id, row["route_id"], row.get("agency_id") or None,
                 int(row["route_type"]), _json(row))
                for row in feed.routes
            ],
        )
        connection.executemany(
            "INSERT INTO stops VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (feed_id, row["stop_id"], row.get("parent_station") or None,
                 row["stop_name"], float(row["stop_lat"]), float(row["stop_lon"]), _json(row))
                for row in feed.stops
            ],
        )
        connection.executemany(
            "INSERT INTO shape_points VALUES (?, ?, ?, ?, ?, ?)",
            [
                (feed_id, row["shape_id"], int(row["shape_pt_sequence"]),
                 float(row["shape_pt_lat"]), float(row["shape_pt_lon"]), _json(row))
                for row in feed.shapes
            ],
        )
        connection.executemany(
            "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)",
            [
                (feed_id, row["trip_id"], row["route_id"], row["service_id"],
                 row.get("shape_id") or None, _json(row))
                for row in feed.trips
            ],
        )
        connection.executemany(
            "INSERT INTO calendars VALUES (?, ?, ?)",
            [(feed_id, row["service_id"], _json(row)) for row in feed.calendars],
        )
        connection.executemany(
            "INSERT INTO calendar_dates VALUES (?, ?, ?, ?, ?)",
            [
                (feed_id, row["service_id"], row["date"], int(row["exception_type"]), _json(row))
                for row in feed.calendar_dates
            ],
        )
        connection.executemany(
            "INSERT INTO stop_times VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (feed_id, row["trip_id"], int(row["stop_sequence"]), row["stop_id"],
                 parse_gtfs_time(row["arrival_time"]), parse_gtfs_time(row["departure_time"]), _json(row))
                for row in feed.stop_times
            ],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return feed_id


def ingest_feed(feed: TransitFeed, database_path: str | Path) -> str:
    connection = connect_database(database_path)
    try:
        return replace_feed(connection, feed)
    finally:
        connection.close()


def store_official_observations(
    connection: sqlite3.Connection,
    observations: list[object],
    *,
    source: str = "official",
) -> int:
    """Upsert normalized official observations with a bounded 30-minute lifetime."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    rows = []
    for item in observations:
        rows.append((
            item.observation_id, item.vehicle_id, item.route_id, item.direction_id,
            item.latitude, item.longitude, source, item.observed_at.isoformat(),
            item.received_at.isoformat(), (now + timedelta(minutes=30)).isoformat(),
        ))
    connection.executemany(
        """INSERT INTO official_observations
        (observation_id, vehicle_id, route_id, direction_id, latitude, longitude,
         source, observed_at, received_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(observation_id) DO UPDATE SET vehicle_id=excluded.vehicle_id,
          route_id=excluded.route_id, direction_id=excluded.direction_id,
          latitude=excluded.latitude, longitude=excluded.longitude, source=excluded.source,
          observed_at=excluded.observed_at, received_at=excluded.received_at,
          expires_at=excluded.expires_at""",
        rows,
    )
    connection.commit()
    return len(rows)
