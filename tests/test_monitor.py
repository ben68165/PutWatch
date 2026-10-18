from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.models import AppConfig, TrackerRule
from app.monitor import (
    TransferMonitorService,
    evaluate_transfer_against_rule,
    find_matching_rule,
)
from app.putio_client import extract_trackers_from_transfer, normalize_tracker_url
from app.storage import ConfigStore


class TestTrackerMonitor(unittest.TestCase):
    def test_normalize_tracker_url(self) -> None:
        self.assertEqual(
            normalize_tracker_url("udp://tracker.opentrackr.org:1337/announce"),
            ("tracker.opentrackr.org", "udp://tracker.opentrackr.org:1337/announce"),
        )
        self.assertEqual(
            normalize_tracker_url("https://www.nyaa.tracker.wf:7777/announce"),
            ("nyaa.tracker.wf", "https://www.nyaa.tracker.wf:7777/announce"),
        )
        self.assertEqual(
            normalize_tracker_url("tracker.example.org"),
            ("tracker.example.org", "tracker.example.org"),
        )

    def test_extract_trackers_from_transfer(self) -> None:
        # 1. From primary `tracker` field + magnet `source`
        t1 = {
            "id": 101,
            "name": "Ubuntu ISO",
            "tracker": "udp://tracker.opentrackr.org:1337/announce",
            "source": "magnet:?xt=urn:btih:12345&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce&tr=https%3A%2F%2Ftracker.ubuntu.com%3A443%2Fannounce",
        }
        extracted = extract_trackers_from_transfer(t1)
        self.assertEqual(
            [x[0] for x in extracted],
            ["tracker.opentrackr.org", "tracker.ubuntu.com"],
        )

        # 2. Untracked transfer fallback
        t2 = {"id": 102, "name": "Direct.mp4", "source": ""}
        self.assertEqual(
            extract_trackers_from_transfer(t2),
            [("untracked", "Direct / DHT (No Tracker)")],
        )

    def test_or_vs_and_rule_evaluation(self) -> None:
        # Transfer seeding for 90 minutes (5400s) with ratio 1.8
        transfer = {
            "id": 500,
            "name": "Linux.Distro.x64",
            "status": "SEEDING",
            "seconds_seeding": 5400,  # 90 minutes
            "current_ratio": 1.8,
        }

        # Case A: OR rule (Time >= 120m OR Ratio >= 1.5) -> Should cancel because Ratio 1.8 >= 1.5
        or_rule = TrackerRule(
            tracker_id="tracker.opentrackr.org",
            enabled=True,
            use_time_rule=True,
            max_time_minutes=120.0,
            use_ratio_rule=True,
            max_ratio=1.5,
            condition_operator="OR",
        )
        ev_or = evaluate_transfer_against_rule(transfer, or_rule)
        self.assertTrue(ev_or["should_cancel"])
        self.assertIn("Ratio 1.80 >= 1.50", ev_or["reason"])

        # Case B: AND rule (Time >= 120m AND Ratio >= 1.5) -> Should NOT cancel yet because Time is 90m < 120m
        and_rule = or_rule.model_copy(update={"condition_operator": "AND"})
        ev_and = evaluate_transfer_against_rule(transfer, and_rule)
        self.assertFalse(ev_and["should_cancel"])

        # Case C: Once seeding reaches 125m (7500s), AND rule triggers!
        transfer_later = {**transfer, "seconds_seeding": 7500}
        ev_and_ready = evaluate_transfer_against_rule(transfer_later, and_rule)
        self.assertTrue(ev_and_ready["should_cancel"])
        self.assertIn("AND", ev_and_ready["reason"])


class TestMonitorServiceAsync(unittest.IsolatedAsyncioTestCase):
    async def test_auto_discovery_and_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            store = ConfigStore(path=cfg_path)
            cfg = store.get()
            cfg.putio_token = "test_token_123"
            cfg.monitoring_enabled = True
            store.save(cfg)

            service = TransferMonitorService(store=store)

            mock_transfers = [
                {
                    "id": 1001,
                    "name": "Movie.A.1080p",
                    "status": "SEEDING",
                    "seconds_seeding": 7200,  # 120 mins
                    "current_ratio": 2.1,
                    "tracker": "https://tracker.alpha.org/announce",
                },
                {
                    "id": 1002,
                    "name": "Movie.B.1080p",
                    "status": "SEEDING",
                    "seconds_seeding": 600,  # 10 mins
                    "current_ratio": 0.2,
                    "tracker": "https://tracker.beta.org/announce",
                },
            ]

            with patch(
                "app.monitor.PutioClient.list_transfers",
                new_callable=AsyncMock,
                return_value=mock_transfers,
            ), patch(
                "app.monitor.PutioClient.get_account_info",
                new_callable=AsyncMock,
                return_value={"username": "tester", "mail": "test@example.com"},
            ), patch(
                "app.monitor.PutioClient.cancel_and_remove_transfer",
                new_callable=AsyncMock,
            ) as mock_cancel:
                # 1st poll: discovers tracker.alpha.org and tracker.beta.org (disabled by default)
                res1 = await service.poll_once(execute_cancellations=True)
                self.assertTrue(res1["ok"])
                self.assertEqual(
                    set(res1["newly_discovered_trackers"]),
                    {"tracker.alpha.org", "tracker.beta.org"},
                )
                mock_cancel.assert_not_called()

                # Now user enables a rule for tracker.alpha.org: cancel after 60 mins OR ratio 1.5
                alpha_rule = store.get().trackers["tracker.alpha.org"]
                alpha_rule.enabled = True
                alpha_rule.use_time_rule = True
                alpha_rule.max_time_minutes = 60.0
                alpha_rule.use_ratio_rule = True
                alpha_rule.max_ratio = 1.5
                alpha_rule.condition_operator = "OR"
                store.upsert_tracker(alpha_rule)

                # 2nd poll: transfer 1001 matches tracker.alpha.org rule and is canceled & removed!
                res2 = await service.poll_once(execute_cancellations=True)
                self.assertEqual(len(res2["canceled"]), 1)
                self.assertEqual(res2["canceled"][0]["transfer_id"], 1001)
                mock_cancel.assert_awaited_once_with(
                    transfer_id=1001,
                    current_status="SEEDING",
                    clean_after_cancel=True,
                )

    async def test_fastapi_endpoints(self) -> None:
        import httpx
        from app.main import app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r_health = await client.get("/api/health")
            self.assertEqual(r_health.status_code, 200)
            self.assertEqual(r_health.json()["status"], "ok")

            r_state = await client.get("/api/state")
            self.assertEqual(r_state.status_code, 200)
            self.assertIn("trackers", r_state.json())

            r_index = await client.get("/")
            self.assertEqual(r_index.status_code, 200)
            self.assertIn("PutWatch", r_index.text)


if __name__ == "__main__":
    unittest.main()
