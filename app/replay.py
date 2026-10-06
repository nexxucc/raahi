"""Seed a clearly synthetic transit network and vehicle snapshot for demos."""

from __future__ import annotations

import argparse
import hashlib
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.gtfs import FeedMetadata, TransitFeed
from app.official_feed import OfficialVehicleObservation, TimestampSource
from app.storage import connect_database, replace_feed, store_official_observations

ROUTE_ID = "DEMO-REPLAY-1"
SCENARIOS = [
    {"id": "baseline", "label": "Baseline", "description": "One simulated bus with no corroborating rider reports."},
    {"id": "weekday-rush", "label": "Weekday rush", "description": "Several buses; one is corroborated and one has gone stale."},
    {"id": "crowd-consensus", "label": "Crowd consensus", "description": "An unidentified bus location backed by independent riders."},
    {"id": "sparse-coverage", "label": "Sparse coverage", "description": "One stale bus and an isolated report that is not enough to track."},
    {"id": "route-disagreement", "label": "Conflicting reports", "description": "A fresh bus position disagrees with a separate crowd cluster."},
]
SCENARIO_IDS = {scenario["id"] for scenario in SCENARIOS}


def _point_on_demo_shape(progress: float) -> tuple[float, float]:
    start = (28.6139, 77.2090)
    end = (28.6200, 77.2150)
    fraction = max(0.0, min(1.0, progress))
    return (start[0] + (end[0] - start[0]) * fraction,
            start[1] + (end[1] - start[1]) * fraction)


def seed_replay_database(database_path: str, scenario_id: str = "weekday-rush") -> None:
    now = datetime.now(UTC)
    feed = TransitFeed(
        metadata=FeedMetadata(
            source_name="Synthetic Delhi demo network", source_url=None,
            retrieved_at=now, attribution="Synthetic demo data; not operator data",
            archive_sha256=hashlib.sha256(b"synthetic-delhi-replay-v1").hexdigest(),
            feed_version="demo-v1",
        ),
        routes=[{"route_id": ROUTE_ID, "route_type": "3", "route_short_name": "DEMO", "route_long_name": "Synthetic Central Delhi Loop"}],
        stops=[
            {"stop_id": "DEMO-STOP-A", "stop_name": "Demo Stop A", "stop_lat": "28.6139", "stop_lon": "77.2090"},
            {"stop_id": "DEMO-STOP-B", "stop_name": "Demo Stop B", "stop_lat": "28.6200", "stop_lon": "77.2150"},
        ],
        trips=[{"route_id": ROUTE_ID, "service_id": "demo-daily", "trip_id": "DEMO-TRIP-1", "shape_id": "DEMO-SHAPE-1"}],
        shapes=[
            {"shape_id": "DEMO-SHAPE-1", "shape_pt_lat": "28.6139", "shape_pt_lon": "77.2090", "shape_pt_sequence": "1"},
            {"shape_id": "DEMO-SHAPE-1", "shape_pt_lat": "28.6200", "shape_pt_lon": "77.2150", "shape_pt_sequence": "2"},
        ],
        calendars=[{"service_id": "demo-daily", "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1", "friday": "1", "saturday": "1", "sunday": "1", "start_date": "20200101", "end_date": "20991231"}],
        stop_times=[{"trip_id": "DEMO-TRIP-1", "stop_id": "DEMO-STOP-A", "stop_sequence": "1", "arrival_time": "08:00:00", "departure_time": "08:00:00"}],
    )
    db = connect_database(database_path)
    try:
        replace_feed(db, feed)
        db.commit()
    finally:
        db.close()
    apply_demo_scenario(database_path, scenario_id)


def apply_demo_scenario(database_path: str, scenario_id: str) -> None:
    """Replace only synthetic observations on the known demo network."""
    if scenario_id not in SCENARIO_IDS:
        raise ValueError("Unknown demo scenario")
    now = datetime.now(UTC)
    db = connect_database(database_path)
    try:
        feed = db.execute(
            "SELECT source_name FROM feed_metadata ORDER BY retrieved_at DESC LIMIT 1"
        ).fetchone()
        if feed is None or feed["source_name"] != "Synthetic Delhi demo network":
            raise ValueError("Scenario switching is available only with the synthetic demo network")
        db.execute("DELETE FROM crowd_reports WHERE synthetic=1 AND route_id=?", (ROUTE_ID,))
        db.execute("DELETE FROM official_observations WHERE source='simulated' AND route_id=?", (ROUTE_ID,))

        positions: list[tuple[str, float, int]]
        crowd_groups: list[tuple[float, int, int]]
        if scenario_id == "baseline":
            positions, crowd_groups = [("DEMO-REPLAY-BUS", 0.47, 30)], []
        elif scenario_id == "weekday-rush":
            positions = [("DEMO-REPLAY-BUS", 0.47, 18), ("DEMO-BUS-12", 0.12, 42), ("DEMO-BUS-23", 0.82, 245)]
            crowd_groups = [(0.47, 2, 15), (0.12, 1, 20)]
        elif scenario_id == "crowd-consensus":
            positions = []
            crowd_groups = [(0.64, 4, 20), (0.28, 1, 35)]
        elif scenario_id == "sparse-coverage":
            positions = [("DEMO-BUS-31", 0.35, 520)]
            crowd_groups = [(0.70, 1, 40)]
        else:  # route-disagreement
            positions = [("DEMO-BUS-44", 0.18, 20)]
            crowd_groups = [(0.83, 4, 18)]

        observations = []
        for vehicle_id, progress, age_seconds in positions:
            lat, lon = _point_on_demo_shape(progress)
            observed = now - timedelta(seconds=age_seconds)
            observations.append(OfficialVehicleObservation(
                observation_id=f"{scenario_id}-{vehicle_id}", vehicle_id=vehicle_id,
                route_id=ROUTE_ID, direction_id=None, latitude=lat, longitude=lon,
                observed_at=observed, received_at=now,
                timestamp_source=TimestampSource.VEHICLE, vehicle_id_source="synthetic_fixture",
            ))
        store_official_observations(db, observations, source="simulated")

        report_index = 0
        for progress, contributor_count, age_seconds in crowd_groups:
            lat, lon = _point_on_demo_shape(progress)
            for contributor_index in range(contributor_count):
                report_index += 1
                # A few metres of deterministic jitter makes the centroid meaningful.
                report_lat = lat + contributor_index * 0.000008
                report_lon = lon - contributor_index * 0.000006
                observed = now - timedelta(seconds=age_seconds)
                digest = hashlib.sha256(
                    f"synthetic:{scenario_id}:rider:{report_index}".encode()
                ).hexdigest()
                db.execute(
                    """INSERT INTO crowd_reports
                    (report_id, observed_at, received_at, latitude, longitude, report_type,
                     route_id, contributor_digest, expires_at, matched_route_id,
                     match_distance_m, match_status, synthetic)
                    VALUES (?, ?, ?, ?, ?, 'position', ?, ?, ?, ?, 0, 'matched', 1)""",
                    (f"synthetic-{scenario_id}-{report_index}", observed.isoformat(),
                     now.isoformat(), report_lat, report_lon, ROUTE_ID, digest,
                     (now + timedelta(minutes=30)).isoformat(), ROUTE_ID),
                )
        db.execute(
            """INSERT INTO demo_state (state_id, scenario_id, changed_at) VALUES (1, ?, ?)
            ON CONFLICT(state_id) DO UPDATE SET scenario_id=excluded.scenario_id,
              changed_at=excluded.changed_at""",
            (scenario_id, now.isoformat()),
        )
        db.commit()
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=Settings.from_env().database_path)
    parser.add_argument("--scenario", choices=sorted(SCENARIO_IDS), default="weekday-rush")
    args = parser.parse_args()
    seed_replay_database(args.database, args.scenario)
    print(f"Seeded synthetic-only {args.scenario} scenario into {args.database}")
    print("Simulated vehicle states are labelled simulated; no operator feed was used.")


if __name__ == "__main__":
    main()
