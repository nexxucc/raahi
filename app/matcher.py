"""Small-feed route-shape matcher; uses a local metric approximation for Delhi."""

from __future__ import annotations

import math
import sqlite3
from itertools import pairwise


def _segment_distance_m(lat: float, lon: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    scale_y = 111_320.0
    scale_x = scale_y * math.cos(math.radians(lat))
    ax, ay = (a[1] - lon) * scale_x, (a[0] - lat) * scale_y
    bx, by = (b[1] - lon) * scale_x, (b[0] - lat) * scale_y
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    t = 0.0 if denom == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / denom))
    return math.hypot(ax + t * dx, ay + t * dy)


def match_position(
    connection: sqlite3.Connection,
    latitude: float,
    longitude: float,
    route_hint: str | None = None,
    *,
    max_distance_m: float = 150.0,
    ambiguity_margin_m: float = 25.0,
) -> dict[str, object]:
    """Return nearest route segment, preserving route ambiguity rather than guessing."""
    feed = connection.execute("SELECT feed_id FROM feed_metadata ORDER BY retrieved_at DESC LIMIT 1").fetchone()
    if feed is None:
        return {"status": "no_network"}
    feed_id = feed["feed_id"]
    rows = connection.execute(
        """SELECT t.route_id, s.shape_id, s.sequence, s.latitude, s.longitude
           FROM shape_points s JOIN trips t ON t.feed_id=s.feed_id AND t.shape_id=s.shape_id
           WHERE s.feed_id=? AND (? IS NULL OR t.route_id=?)
           ORDER BY t.route_id, s.shape_id, s.sequence""",
        (feed_id, route_hint, route_hint),
    ).fetchall()
    grouped: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for row in rows:
        grouped.setdefault((row["route_id"], row["shape_id"]), []).append(
            (row["latitude"], row["longitude"])
        )
    candidates: list[tuple[float, str, str, int]] = []
    for (route_id, shape_id), points in grouped.items():
        if len(points) < 2:
            continue
        for index, (a, b) in enumerate(pairwise(points)):
            candidates.append((_segment_distance_m(latitude, longitude, a, b), route_id, shape_id, index))
    candidates.sort()
    if not candidates or candidates[0][0] > max_distance_m:
        return {"status": "off_route" if candidates else "no_shapes"}
    best = candidates[0]
    close_routes = {item[1] for item in candidates if item[0] <= best[0] + ambiguity_margin_m}
    if len(close_routes) > 1:
        return {"status": "ambiguous", "distance_m": round(best[0], 1)}
    return {
        "status": "matched",
        "route_id": best[1],
        "shape_id": best[2],
        "distance_m": round(best[0], 1),
        "segment": best[3],
    }
