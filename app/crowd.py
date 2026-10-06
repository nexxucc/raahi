"""Consent-based crowd report storage with short retention and minimal identity data."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Request, Response

from app.matcher import match_position
from app.schemas import CrowdReportCreate, CrowdReportResponse
from app.storage import connect_database

router = APIRouter(prefix="/api/v1/crowd", tags=["crowd reports"])
RETENTION = timedelta(minutes=30)
MAX_AGE = timedelta(minutes=15)
RATE_WINDOW = timedelta(minutes=1)
RATE_LIMIT = 5


def _utc_now() -> datetime:
    return datetime.now(UTC)


@router.post("/reports", response_model=CrowdReportResponse, status_code=201)
def submit_report(body: CrowdReportCreate, request: Request) -> CrowdReportResponse:
    if not body.consent:
        raise HTTPException(400, "Explicit consent is required")
    now = _utc_now()
    observed = body.observed_at.astimezone(UTC)
    if observed < now - MAX_AGE or observed > now + timedelta(minutes=2):
        raise HTTPException(422, "observed_at must be within the last 15 minutes and no more than 2 minutes ahead")
    digest = hashlib.sha256(body.contributor_token.encode()).hexdigest()
    report_id = secrets.token_urlsafe(18)
    expires = now + RETENTION
    connection = connect_database(request.app.state.settings.database_path)
    try:
        connection.execute("DELETE FROM crowd_reports WHERE expires_at <= ?", (now.isoformat(),))
        since = (now - RATE_WINDOW).isoformat()
        recent = connection.execute(
            "SELECT COUNT(*) FROM crowd_reports WHERE contributor_digest=? AND received_at>=?",
            (digest, since),
        ).fetchone()[0]
        if recent >= RATE_LIMIT:
            raise HTTPException(429, "Report rate limit exceeded")
        if connection.execute(
            "SELECT 1 FROM crowd_reports WHERE contributor_digest=? AND received_at>=? LIMIT 1",
            (digest, (now - timedelta(seconds=10)).isoformat()),
        ).fetchone():
            raise HTTPException(409, "Duplicate report from this contributor")
        prior = connection.execute(
            "SELECT latitude, longitude, received_at FROM crowd_reports WHERE contributor_digest=? ORDER BY received_at DESC LIMIT 1",
            (digest,),
        ).fetchone()
        if prior:
            prior_at = datetime.fromisoformat(prior["received_at"])
            elapsed = max((now - prior_at).total_seconds(), 1)
            from math import asin, cos, radians, sin, sqrt
            dlat = radians(body.latitude - prior["latitude"])
            dlon = radians(body.longitude - prior["longitude"])
            value = sin(dlat / 2) ** 2 + cos(radians(prior["latitude"])) * cos(radians(body.latitude)) * sin(dlon / 2) ** 2
            distance = 6_371_000 * 2 * asin(sqrt(value))
            if distance / elapsed * 3.6 > 120:
                raise HTTPException(422, "Position jump is implausible")
        if body.route_id and connection.execute(
            "SELECT 1 FROM routes WHERE route_id=? LIMIT 1", (body.route_id,)
        ).fetchone() is None:
            raise HTTPException(422, "route_id is not present in the loaded network")
        match = match_position(connection, body.latitude, body.longitude, body.route_id)
        matched_route = match.get("route_id") if match["status"] == "matched" else None
        if body.route_id and matched_route and matched_route != body.route_id:
            raise HTTPException(422, "Reported route conflicts with the nearby route shape")
        connection.execute(
            """INSERT INTO crowd_reports
            (report_id, observed_at, received_at, latitude, longitude, report_type, route_id,
             vehicle_id, contributor_digest, expires_at, matched_route_id, matched_shape_id,
             match_distance_m, match_segment, match_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (report_id, observed.isoformat(), now.isoformat(), body.latitude, body.longitude,
             body.report_type, body.route_id, body.vehicle_id, digest, expires.isoformat(),
             matched_route, match.get("shape_id"), match.get("distance_m"), match.get("segment"),
             str(match["status"])),
        )
        connection.commit()
        return CrowdReportResponse(
            report_id=report_id, received_at=now, expires_at=expires,
            match_status=str(match["status"]), matched_route_id=matched_route,
            match_distance_m=match.get("distance_m"),
        )
    finally:
        connection.close()


@router.delete("/reports/{report_id}", status_code=204)
def delete_report(
    report_id: str,
    request: Request,
    x_reports_admin_token: str | None = Header(default=None),
) -> Response:
    expected = request.app.state.settings.reports_admin_token
    if not expected or not x_reports_admin_token or not hmac.compare_digest(expected, x_reports_admin_token):
        raise HTTPException(403, "Report deletion is restricted to authorized moderators")
    connection = connect_database(request.app.state.settings.database_path)
    try:
        connection.execute("DELETE FROM crowd_reports WHERE report_id=?", (report_id,))
        connection.commit()
    finally:
        connection.close()
    return Response(status_code=204)
