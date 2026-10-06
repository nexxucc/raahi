from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.fusion import fuse_vehicle_states
from app.storage import connect_database


def add_report(db, *, report_id, token, lat=28.6, lon=77.2, route="r1", at=None):
    when = at or datetime.now(UTC)
    db.execute(
        """INSERT INTO crowd_reports
        (report_id, observed_at, received_at, latitude, longitude, report_type, route_id,
         contributor_digest, expires_at, matched_route_id, match_status)
        VALUES (?, ?, ?, ?, ?, 'position', ?, ?, ?, ?, 'matched')""",
        (report_id, when.isoformat(), when.isoformat(), lat, lon, route, token,
         (when + timedelta(minutes=30)).isoformat(), route),
    )


def add_official(db, *, vehicle="bus-1", lat=28.6, lon=77.2, at=None):
    when = at or datetime.now(UTC)
    db.execute(
        """INSERT INTO official_observations
        (observation_id, vehicle_id, route_id, latitude, longitude, observed_at, received_at, expires_at)
        VALUES (?, ?, 'r1', ?, ?, ?, ?, ?)""",
        ("obs-" + vehicle, vehicle, lat, lon, when.isoformat(), when.isoformat(),
         (when + timedelta(minutes=30)).isoformat()),
    )


def test_official_anchor_corroborated_only_by_independent_nearby_reports(tmp_path):
    db = connect_database(str(tmp_path / "f.sqlite"))
    now = datetime.now(UTC)
    add_official(db, at=now)
    add_report(db, report_id="a", token="contributor-a", lat=28.6001, at=now)
    add_report(db, report_id="b", token="contributor-b", lat=28.6002, at=now)
    add_report(db, report_id="repeat", token="contributor-b", lat=28.6, at=now)
    states = fuse_vehicle_states(db, now=now)
    official = next(state for state in states if state["vehicle_id"] == "bus-1")
    assert official["status"] == "live"
    assert official["sources"] == ["official", "crowd"]
    assert official["crowd_contributors"] == 2
    assert official["latitude"] == 28.6  # official location remains the anchor
    db.close()


def test_stale_official_and_single_report_do_not_look_live_or_corroborated(tmp_path):
    db = connect_database(str(tmp_path / "f.sqlite"))
    now = datetime.now(UTC)
    add_official(db, at=now - timedelta(minutes=8))
    add_report(db, report_id="only", token="one-rider", lat=28.6, at=now)
    state = fuse_vehicle_states(db, now=now)[0]
    assert state["status"] == "stale"
    assert state["sources"] == ["official"]
    assert state["confidence"] == 0.25
    db.close()


def test_crowd_only_cluster_needs_independent_agreement_and_has_no_vehicle_identity(tmp_path):
    db = connect_database(str(tmp_path / "f.sqlite"))
    now = datetime.now(UTC)
    add_report(db, report_id="one", token="same-rider", lat=28.6, at=now)
    add_report(db, report_id="two", token="same-rider", lat=28.6001, at=now)
    assert fuse_vehicle_states(db, now=now) == []
    add_report(db, report_id="three", token="other-rider", lat=28.6001, at=now)
    state = fuse_vehicle_states(db, now=now)[0]
    assert state["status"] == "provisional"
    assert state["vehicle_id"] is None
    assert state["crowd_contributors"] == 2
    db.close()


def test_conflicting_crowd_majority_is_reported_as_disagreement_not_override(tmp_path):
    db = connect_database(str(tmp_path / "f.sqlite"))
    now = datetime.now(UTC)
    add_official(db, at=now)
    for number in range(4):
        add_report(db, report_id=f"far-{number}", token=f"rider-{number}", lat=28.61, at=now)
    state = fuse_vehicle_states(db, now=now)[0]
    assert state["latitude"] == 28.6
    assert state["sources"] == ["official"]
    assert state["disagreement_distance_m"] > 1000
    db.close()


def test_old_crowd_reports_and_expired_official_observations_are_ignored(tmp_path):
    db = connect_database(str(tmp_path / "f.sqlite"))
    now = datetime.now(UTC)
    add_official(db, at=now - timedelta(hours=1))
    add_report(db, report_id="old-a", token="old-a", at=now - timedelta(minutes=10))
    add_report(db, report_id="old-b", token="old-b", at=now - timedelta(minutes=10))
    assert fuse_vehicle_states(db, now=now) == []
    assert db.execute("SELECT COUNT(*) FROM official_observations").fetchone()[0] == 0
    db.close()


def test_simulated_observations_remain_labelled_simulated(tmp_path):
    db = connect_database(str(tmp_path / "sim.sqlite"))
    now = datetime.now(UTC)
    db.execute(
        """INSERT INTO official_observations
        (observation_id, vehicle_id, route_id, latitude, longitude, source, observed_at, received_at, expires_at)
        VALUES ('sim-obs', 'demo-bus', 'r1', 28.6, 77.2, 'simulated', ?, ?, ?)""",
        (now.isoformat(), now.isoformat(), (now + timedelta(minutes=30)).isoformat()),
    )
    state = fuse_vehicle_states(db, now=now)[0]
    assert state["status"] == "simulated"
    assert state["sources"] == ["simulated"]
    db.close()
