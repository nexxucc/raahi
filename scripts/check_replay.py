"""Exercise report intake and tracking against a running local demo server."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import UTC, datetime

import httpx

ROUTE_ID = "DEMO-REPLAY-1"
VEHICLE_ID = "DEMO-REPLAY-BUS"


def run(base_url: str, timeout: float = 5.0) -> dict[str, object]:
    client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
    try:
        # Verify the service is reachable and a deliberately invalid coordinate is rejected.
        health = client.get("/api/v1/health")
        health.raise_for_status()
        bad = client.post("/api/v1/crowd/reports", json={
            "consent": True, "report_type": "position", "latitude": 91,
            "longitude": 77.2118, "observed_at": datetime.now(UTC).isoformat(),
            "contributor_token": secrets.token_urlsafe(24), "route_id": ROUTE_ID,
        })
        if bad.status_code != 422:
            raise RuntimeError(f"invalid report expected HTTP 422, got {bad.status_code}")

        reports = []
        for _ in range(2):
            response = client.post("/api/v1/crowd/reports", json={
                "consent": True, "report_type": "position", "latitude": 28.6167,
                "longitude": 77.2118, "observed_at": datetime.now(UTC).isoformat(),
                "contributor_token": secrets.token_urlsafe(24), "route_id": ROUTE_ID,
                "vehicle_id": VEHICLE_ID,
            })
            response.raise_for_status()
            reports.append(response.json())

        response = client.get(f"/api/v1/routes/{ROUTE_ID}/vehicles")
        response.raise_for_status()
        vehicles = response.json()
        target = next((item for item in vehicles if item.get("vehicle_id") == VEHICLE_ID), None)
        if target is None:
            raise RuntimeError("demo vehicle was not returned by the route tracking endpoint")
        if target["status"] != "simulated" or "simulated" not in target["sources"]:
            raise RuntimeError("demo vehicle was not clearly labelled simulated")
        if target["crowd_contributors"] < 2:
            raise RuntimeError("independent reports did not corroborate the demo vehicle")
        detail = client.get(f"/api/v1/vehicles/{VEHICLE_ID}")
        detail.raise_for_status()
        if detail.json()["status"] != "simulated":
            raise RuntimeError("vehicle detail response lost the simulated label")
        return {
            "result": "passed", "health": health.json(), "invalid_report_status": bad.status_code,
            "accepted_reports": len(reports), "tracking_status": target["status"],
            "sources": target["sources"], "crowd_contributors": target["crowd_contributors"],
        }
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.base_url, args.timeout), indent=2))
    except (httpx.HTTPError, RuntimeError) as exc:
        print(json.dumps({"result": "failed", "error": str(exc)}, indent=2), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
