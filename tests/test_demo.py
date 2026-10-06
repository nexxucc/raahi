from __future__ import annotations

import asyncio

import httpx

from app.config import Settings
from app.main import create_app
from app.replay import SCENARIOS, seed_replay_database


def test_demo_scenario_switching_exposes_distinct_tracking_states(tmp_path):
    database = str(tmp_path / "demo.sqlite3")
    seed_replay_database(database)
    app = create_app(Settings(database_path=database))

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            presets = (await client.get("/api/v1/demo/scenarios")).json()
            assert presets["active_scenario"] == "weekday-rush"
            assert {item["id"] for item in presets["scenarios"]} == {
                item["id"] for item in SCENARIOS
            }
            result = {}
            for scenario in SCENARIOS:
                switched = await client.post(f"/api/v1/demo/scenarios/{scenario['id']}")
                assert switched.status_code == 200
                states = (
                    await client.get("/api/v1/routes/DEMO-REPLAY-1/vehicles")
                ).json()
                result[scenario["id"]] = states
            assert len(result["weekday-rush"]) == 3
            assert any(item["freshness_status"] == "stale" for item in result["weekday-rush"])
            assert any(item["crowd_contributors"] >= 2 for item in result["weekday-rush"])
            assert result["crowd-consensus"][0]["identity_kind"] == "unidentified_crowd_cluster"
            assert result["sparse-coverage"][0]["freshness_status"] == "stale"
            assert any(
                item["disagreement_distance_m"] is not None
                for item in result["route-disagreement"]
            )
            return await client.post("/api/v1/demo/scenarios/not-a-scenario")

    assert asyncio.run(exercise()).status_code == 404
