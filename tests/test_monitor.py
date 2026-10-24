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

    def test_cancel_on_completed_and_keep_alive_protections(self) -> None:
        from app.monitor import compute_keep_alive_protections

        rule = TrackerRule(
            tracker_id="tracker.private.org",
            enabled=True,
            cancel_on_completed=True,
            keep_top_seeded_enabled=True,
            keep_top_seeded_count=1,
            top_seeded_metric="up_speed",
            keep_largest_enabled=True,
            keep_largest_count=1,
            keep_alive_hours=48.0,  # 2 days = 172800s
        )

        t_fast = {
            "id": 1,
            "name": "Fast.Seeder",
            "status": "SEEDING",
            "up_speed": 500000,
            "uploaded": 1000000,
            "size": 1000,
            "seconds_seeding": 3600,
        }
        t_large = {
            "id": 2,
            "name": "Huge.Pack",
            "status": "SEEDING",
            "up_speed": 100,
            "uploaded": 500,
            "size": 999999999,
            "seconds_seeding": 3600,
        }
        t_normal = {
            "id": 3,
            "name": "Regular.Episode",
            "status": "SEEDING",
            "up_speed": 50,
            "uploaded": 100,
            "size": 500,
            "seconds_seeding": 10,
        }

        protections = compute_keep_alive_protections([t_fast, t_large, t_normal], rule)
        self.assertEqual(protections[1], "Top Seeded #1")
        self.assertEqual(protections[2], "Largest Transfer #1")
        self.assertNotIn(3, protections)

        # Regular transfer should cancel immediately on completion
        ev_normal = evaluate_transfer_against_rule(t_normal, rule, protections.get(3))
        self.assertTrue(ev_normal["should_cancel"])

        # Top Seeded #1 and Largest Transfer #1 are kept alive (< 48h)
        ev_fast = evaluate_transfer_against_rule(t_fast, rule, protections.get(1))
        self.assertFalse(ev_fast["should_cancel"])
        self.assertTrue(ev_fast["protection_active"])

        # Once Top Seeded #1 exceeds 48h (172800s), it is canceled
        t_fast_expired = {**t_fast, "seconds_seeding": 180000}
        ev_fast_expired = evaluate_transfer_against_rule(
            t_fast_expired, rule, protections.get(1)
        )
        self.assertTrue(ev_fast_expired["should_cancel"])

    def test_cancel_completed_on_first_scan_and_token_masking(self) -> None:
        from app.main import _mask_token
        from app.monitor import compute_keep_alive_protections

        # Verify full token anonymization (no plaintext characters leaked)
        self.assertEqual(_mask_token(""), "")
        self.assertEqual(_mask_token("secret_putio_token_12345"), "•" * 16)

        rule = TrackerRule(
            tracker_id="tracker.cached.org",
            enabled=True,
            cancel_completed_on_first_scan=True,
            use_time_rule=True,
            max_time_minutes=2880.0,  # 48h required normally
            keep_top_seeded_enabled=True,
            keep_top_seeded_count=1,
            keep_alive_hours=72.0,
        )

        # Transfer 10 was already COMPLETED on first scan (cached by another user)
        t_cached = {
            "id": 10,
            "name": "Instant.Cached.Release",
            "status": "COMPLETED",
            "up_speed": 999999,
            "uploaded": 9999999,
            "size": 5000000,
            "seconds_seeding": 0,
            "current_ratio": 0.0,
        }
        # Transfer 11 started as DOWNLOADING on first scan and is now SEEDING
        t_seeding = {
            "id": 11,
            "name": "Normal.Seeder.Release",
            "status": "SEEDING",
            "up_speed": 1000,
            "uploaded": 5000,
            "size": 5000000,
            "seconds_seeding": 60,
            "current_ratio": 0.1,
        }

        first_seen = {10: "COMPLETED", 11: "DOWNLOADING"}
        protections = compute_keep_alive_protections(
            [t_cached, t_seeding], rule, first_seen
        )
        # Cached COMPLETED transfer must not steal Top Seeded #1 slot
        self.assertNotIn(10, protections)
        self.assertEqual(protections[11], "Top Seeded #1")

        # Cached COMPLETED transfer is canceled immediately despite 48h rule & keep-alive
        ev_cached = evaluate_transfer_against_rule(
            t_cached,
            rule,
            protections.get(10),
            was_completed_on_first_scan=(first_seen[10] == "COMPLETED"),
        )
        self.assertTrue(ev_cached["should_cancel"])
        self.assertIn("Already COMPLETED on first scan", ev_cached["reason"])

        # Normal transfer that started as DOWNLOADING is NOT canceled on first scan rule
        ev_seeding = evaluate_transfer_against_rule(
            t_seeding,
            rule,
            protections.get(11),
            was_completed_on_first_scan=(first_seen[11] == "COMPLETED"),
        )
        self.assertFalse(ev_seeding["should_cancel"])

    def test_transfer_switching_to_completed_later_is_canceled(self) -> None:
        from app.monitor import compute_keep_alive_protections

        rule = TrackerRule(
            tracker_id="tracker.later.org",
            enabled=True,
            cancel_completed_on_first_scan=True,
            use_time_rule=True,
            max_time_minutes=2880.0,
            use_ratio_rule=True,
            max_ratio=2.0,
            keep_top_seeded_enabled=True,
            keep_top_seeded_count=1,
        )
        # Started as DOWNLOADING, finished soon after and is now COMPLETED (never seeded):
        # seconds_seeding and ratio stay at 0, so time/ratio limits could never trigger.
        t_done = {
            "id": 20,
            "name": "Finished.Quickly",
            "status": "COMPLETED",
            "up_speed": 0,
            "uploaded": 0,
            "size": 1000,
            "seconds_seeding": 0,
            "current_ratio": 0.0,
        }
        t_seeding = {
            "id": 21,
            "name": "Still.Seeding",
            "status": "SEEDING",
            "up_speed": 10,
            "uploaded": 10,
            "size": 1000,
            "seconds_seeding": 30,
            "current_ratio": 0.1,
        }

        protections = compute_keep_alive_protections(
            [t_done, t_seeding], rule, {20: "DOWNLOADING", 21: "DOWNLOADING"}
        )
        self.assertNotIn(20, protections)

        ev_done = evaluate_transfer_against_rule(
            t_done, rule, protections.get(20), was_completed_on_first_scan=False
        )
        self.assertTrue(ev_done["should_cancel"])
        self.assertIn("switched to COMPLETED", ev_done["reason"])

        # A transfer that is still seeding keeps following the normal time/ratio limits
        ev_seeding = evaluate_transfer_against_rule(
            t_seeding, rule, protections.get(21), was_completed_on_first_scan=False
        )
        self.assertFalse(ev_seeding["should_cancel"])

        # Without the option enabled the COMPLETED transfer is left to the regular limits
        rule_off = rule.model_copy(update={"cancel_completed_on_first_scan": False})
        ev_off = evaluate_transfer_against_rule(
            t_done, rule_off, None, was_completed_on_first_scan=False
        )
        self.assertFalse(ev_off["should_cancel"])

    def test_log_retention_prunes_old_entries(self) -> None:
        from datetime import datetime, timedelta, timezone

        from app.models import ActivityLogEntry

        def make_entry(entry_id: str, age_days: float, ts_override: str | None = None):
            ts = ts_override or (
                datetime.now(timezone.utc) - timedelta(days=age_days)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            return ActivityLogEntry(
                id=entry_id,
                timestamp=ts,
                transfer_id=1,
                transfer_name=f"t-{entry_id}",
                tracker_id="tracker.a.org",
                status_at_cancel="COMPLETED",
                seeding_seconds=0,
                ratio_at_cancel=0.0,
                rule_summary="rule",
                reason="reason",
            )

        self.assertEqual(AppConfig().log_retention_days, 7)

        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(path=Path(tmp) / "config.json")
            store.append_log(make_entry("old", 8))
            store.append_log(make_entry("fresh", 1))
            store.append_log(make_entry("edge", 6.9))
            store.append_log(make_entry("broken", 0, ts_override="not-a-date"))

            # 0 days = keep forever
            self.assertEqual(store.prune_logs(0), 0)
            self.assertEqual(len(store.get().activity_log), 4)

            self.assertEqual(store.prune_logs(7), 1)
            remaining = {e.id for e in store.get().activity_log}
            self.assertEqual(remaining, {"fresh", "edge", "broken"})

            # Persisted to disk
            reloaded = ConfigStore(path=Path(tmp) / "config.json")
            self.assertEqual({e.id for e in reloaded.get().activity_log}, remaining)

            # Shorter retention removes more
            self.assertEqual(store.prune_logs(3), 1)
            self.assertEqual(
                {e.id for e in store.get().activity_log}, {"fresh", "broken"}
            )

    def test_stalled_download_and_long_queued_detection(self) -> None:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        rule = TrackerRule(
            tracker_id="tracker.stalled.org",
            enabled=True,
            notify_stalled_download_enabled=True,
            notify_stalled_download_hours=6.0,   # Notify after 6h downloading
            cancel_stalled_download_enabled=True,
            cancel_stalled_download_hours=24.0,  # Cancel after 24h downloading
            notify_long_queued_enabled=True,
            notify_long_queued_hours=4.0,        # Notify after 4h queued
            use_time_rule=False,
            use_ratio_rule=False,
        )

        # 1. DOWNLOADING for 8h -> Notified & highlighted, but NOT canceled yet (< 24h)
        t_down_8h = {
            "id": 201,
            "name": "Stalled.Download.8h",
            "status": "DOWNLOADING",
            "created_at": (now - timedelta(hours=8)).isoformat(),
            "seconds_seeding": 0,
            "current_ratio": 0.0,
        }
        ev_down_8h = evaluate_transfer_against_rule(t_down_8h, rule)
        self.assertTrue(ev_down_8h["notification_active"])
        self.assertEqual(ev_down_8h["notification_type"], "stalled_download")
        self.assertFalse(ev_down_8h["should_cancel"])

        # 2. DOWNLOADING for 26h -> Notified AND canceled (>= 24h)
        t_down_26h = {
            "id": 202,
            "name": "Stalled.Download.26h",
            "status": "DOWNLOADING",
            "created_at": (now - timedelta(hours=26)).isoformat(),
            "seconds_seeding": 0,
            "current_ratio": 0.0,
        }
        ev_down_26h = evaluate_transfer_against_rule(t_down_26h, rule)
        self.assertTrue(ev_down_26h["notification_active"])
        self.assertTrue(ev_down_26h["should_cancel"])
        self.assertIn("Stalled Download", ev_down_26h["reason"])

        # 3. IN_QUEUE for 30h -> Must NOT be canceled by stalled download rule, but MUST be notified by long-queued rule
        t_queued_30h = {
            "id": 203,
            "name": "Long.Queued.30h",
            "status": "IN_QUEUE",
            "created_at": (now - timedelta(hours=30)).isoformat(),
            "seconds_seeding": 0,
            "current_ratio": 0.0,
        }
        ev_queued_30h = evaluate_transfer_against_rule(t_queued_30h, rule)
        self.assertFalse(ev_queued_30h["should_cancel"])
        self.assertTrue(ev_queued_30h["notification_active"])
        self.assertEqual(ev_queued_30h["notification_type"], "long_queued")
        self.assertEqual(ev_queued_30h["notification_label"], "Queued Too Long")


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
                "app.monitor.PutioClient.ensure_putwatch_oauth_app",
                new_callable=AsyncMock,
                return_value={"app_id": 9999, "token": "putwatch_app_token"},
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
        from app.main import app, store

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r_health = await client.get("/api/health")
            self.assertEqual(r_health.status_code, 200)
            self.assertEqual(r_health.json()["status"], "ok")

            r_state = await client.get("/api/state")
            self.assertEqual(r_state.status_code, 200)
            state_json = r_state.json()
            self.assertIn("trackers", state_json)
            self.assertIn("pin_transfers_header", state_json["settings"])
            self.assertIn("log_retention_days", state_json["settings"])

            r_update = await client.put(
                "/api/settings", json={"pin_transfers_header": False}
            )
            self.assertEqual(r_update.status_code, 200)
            self.assertFalse(r_update.json()["settings"]["pin_transfers_header"])

            r_restore = await client.put(
                "/api/settings", json={"pin_transfers_header": True}
            )
            self.assertEqual(r_restore.status_code, 200)
            self.assertTrue(r_restore.json()["settings"]["pin_transfers_header"])

            r_retention = await client.put(
                "/api/settings", json={"log_retention_days": 14}
            )
            self.assertEqual(r_retention.status_code, 200)
            self.assertEqual(r_retention.json()["settings"]["log_retention_days"], 14)
            r_retention_default = await client.put(
                "/api/settings", json={"log_retention_days": 7}
            )
            self.assertEqual(
                r_retention_default.json()["settings"]["log_retention_days"], 7
            )
            r_retention_bad = await client.put(
                "/api/settings", json={"log_retention_days": -1}
            )
            self.assertEqual(r_retention_bad.status_code, 422)

            r_index = await client.get("/")
            self.assertEqual(r_index.status_code, 200)
            self.assertIn("PutWatch", r_index.text)

            # Test tracker rule save (specific settings)
            r_save_tracker = await client.post(
                "/api/trackers",
                json={
                    "tracker_id": "custom.tracker.org",
                    "display_name": "Custom Tracker",
                    "enabled": True,
                    "use_time_rule": True,
                    "max_time_minutes": 180,
                    "use_ratio_rule": True,
                    "max_ratio": 2.5,
                },
            )
            self.assertEqual(r_save_tracker.status_code, 200)
            saved_trackers = {
                t["tracker_id"]: t for t in r_save_tracker.json()["trackers"]
            }
            self.assertIn("custom.tracker.org", saved_trackers)
            self.assertTrue(saved_trackers["custom.tracker.org"]["enabled"])
            self.assertEqual(saved_trackers["custom.tracker.org"]["max_ratio"], 2.5)

            # Test reverting tracker rule back to empty fallback (disabled rule)
            r_revert_tracker = await client.post(
                "/api/trackers",
                json={
                    "tracker_id": "custom.tracker.org",
                    "display_name": "Custom Tracker",
                    "enabled": False,
                    "use_time_rule": False,
                    "use_ratio_rule": False,
                },
            )
            self.assertEqual(r_revert_tracker.status_code, 200)
            reverted_trackers = {
                t["tracker_id"]: t for t in r_revert_tracker.json()["trackers"]
            }
            self.assertFalse(reverted_trackers["custom.tracker.org"]["enabled"])

            # Verify that transfers with this reverted tracker fall back to default rule when default rule is enabled
            cfg_now = store.get()
            cfg_now.default_rule.enabled = True
            store.save(cfg_now)
            matched_id, matched_r = find_matching_rule(
                [("custom.tracker.org", "udp://custom.tracker.org:1337")], cfg_now
            )
            self.assertEqual(matched_r.tracker_id, "__default__")


if __name__ == "__main__":
    unittest.main()
