"""Conservative fusion of official positions and independent crowd evidence."""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime


def _distance_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    lat_mid = math.radians((a_lat + b_lat) / 2)
    dx = math.radians(b_lon - a_lon) * 6_371_000 * math.cos(lat_mid)
    dy = math.radians(b_lat - a_lat) * 6_371_000
    return math.hypot(dx, dy)


def fuse_vehicle_states(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    minimum_crowd_contributors: int = 2,
    official_stale_after_seconds: int = 120,
    crowd_fresh_seconds: int = 300,
    corroboration_radius_m: float = 250,
) -> list[dict[str, object]]:
    """Build visible states; crowds corroborate but never override an official anchor."""
    current = now or datetime.now(UTC)
    current = current.astimezone(UTC)
    db = connection
    db.execute("DELETE FROM official_observations WHERE expires_at <= ?", (current.isoformat(),))
    db.execute("DELETE FROM crowd_reports WHERE expires_at <= ?", (current.isoformat(),))
    db.commit()
    officials = db.execute(
        "SELECT * FROM official_observations ORDER BY observed_at DESC"
    ).fetchall()
    reports = db.execute(
        """SELECT report_id, route_id, matched_route_id, latitude, longitude, observed_at,
                  received_at, contributor_digest, match_status
           FROM crowd_reports WHERE expires_at>? AND report_type='position'
           ORDER BY observed_at DESC""",
        (current.isoformat(),),
    ).fetchall()
    recent_reports = []
    for report in reports:
        observed = datetime.fromisoformat(report["observed_at"]).astimezone(UTC)
        age = (current - observed).total_seconds()
        if -120 <= age <= crowd_fresh_seconds and report["match_status"] == "matched":
            recent_reports.append((report, observed, age))

    states: list[dict[str, object]] = []
    used_reports: set[str] = set()
    for row in officials:
        observed = datetime.fromisoformat(row["observed_at"]).astimezone(UTC)
        age = max(0, int((current - observed).total_seconds()))
        if age > 1800:
            continue
        nearby = []
        disagreement: float | None = None
        for report, when, report_age in recent_reports:
            report_route = report["matched_route_id"] or report["route_id"]
            # Spatial proximity alone cannot tie a crowd report to a particular bus.
            if not row["route_id"] or not report_route or row["route_id"] != report_route:
                continue
            if abs((observed - when).total_seconds()) > 180:
                continue
            distance = _distance_m(row["latitude"], row["longitude"], report["latitude"], report["longitude"])
            if distance <= corroboration_radius_m:
                nearby.append((report, distance))
            else:
                disagreement = distance if disagreement is None else min(disagreement, distance)
        independent = {item[0]["contributor_digest"] for item in nearby}
        for report, _distance in nearby:
            used_reports.add(report["report_id"])
        corroborated = len(independent) >= minimum_crowd_contributors
        simulated = row["source"] == "simulated"
        freshness = "stale" if age > official_stale_after_seconds else "fresh"
        status = "simulated" if simulated else ("live" if age <= official_stale_after_seconds else "stale")
        states.append({
            "vehicle_id": row["vehicle_id"], "route_id": row["route_id"],
            "direction_id": row["direction_id"], "latitude": row["latitude"],
            "longitude": row["longitude"], "observed_at": observed,
            "received_at": datetime.fromisoformat(row["received_at"]).astimezone(UTC),
            "age_seconds": age,
            "sources": (["simulated"] if simulated else ["official"]) + (["crowd"] if corroborated else []),
            "confidence": (0.5 if freshness == "fresh" else 0.25) if simulated else ((0.96 if corroborated else 0.82) if status == "live" else 0.25),
            "status": status, "freshness_status": freshness, "crowd_contributors": len(independent),
            "disagreement_distance_m": round(disagreement, 1) if disagreement is not None else None,
            "route_match_status": "route_agreement" if corroborated else ("route_id_available" if row["route_id"] else "unknown"),
            "identity_kind": "simulated_vehicle" if simulated else "official_vehicle",
        })

    # Greedy spatial-temporal clusters. Only independently tokenized reporters count;
    # no synthetic physical vehicle ID is claimed for a crowd-only cluster.
    candidates = [entry for entry in recent_reports if entry[0]["report_id"] not in used_reports]
    clusters: list[list[tuple[sqlite3.Row, datetime, float]]] = []
    for item in candidates:
        report, observed, _age = item
        route = report["matched_route_id"] or report["route_id"]
        selected = None
        for cluster in clusters:
            anchor, anchor_time, _ = cluster[0]
            anchor_route = anchor["matched_route_id"] or anchor["route_id"]
            if route != anchor_route or abs((observed - anchor_time).total_seconds()) > 120:
                continue
            if _distance_m(report["latitude"], report["longitude"], anchor["latitude"], anchor["longitude"]) <= corroboration_radius_m:
                selected = cluster
                break
        if selected is None:
            clusters.append([item])
        else:
            selected.append(item)

    for cluster in clusters:
        unique: dict[str, tuple[sqlite3.Row, datetime, float]] = {}
        for entry in cluster:
            unique.setdefault(entry[0]["contributor_digest"], entry)
        if len(unique) < minimum_crowd_contributors:
            continue
        entries = list(unique.values())
        lat = sum(item[0]["latitude"] for item in entries) / len(entries)
        lon = sum(item[0]["longitude"] for item in entries) / len(entries)
        observed = max(item[1] for item in entries)
        age = max(0, int((current - observed).total_seconds()))
        route_id = entries[0][0]["matched_route_id"] or entries[0][0]["route_id"]
        states.append({
            "vehicle_id": None, "route_id": route_id, "direction_id": None,
            "latitude": lat, "longitude": lon, "observed_at": observed,
            "received_at": max(item[0]["received_at"] for item in entries),
            "age_seconds": age, "sources": ["crowd"], "confidence": min(0.7, 0.45 + 0.05 * len(entries)),
            "status": "provisional", "crowd_contributors": len(entries),
            "freshness_status": "fresh",
            "disagreement_distance_m": None, "identity_kind": "unidentified_crowd_cluster",
            "route_match_status": "matched_to_gtfs_shape",
        })
    states.sort(key=lambda state: (state["status"] == "stale", -state["confidence"], state["age_seconds"]))
    return states
