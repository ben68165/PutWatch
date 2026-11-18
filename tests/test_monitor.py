from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.models import AppConfig, SlotRetentionConfig, TrackerRule
from app.monitor import (
    TransferMonitorService,
    apply_slot_retention,
    compute_unified_retention_scores,
    detect_dead_or_finished_transfer,
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

    def test_slot_retention_weighted_score_and_eviction(self) -> None:
        import json

        # Verify backward compatibility when loading legacy config.json without slot_retention
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "monitoring_enabled": True,
                        "trackers": {
                            "tracker.keep.org": {
                                "tracker_id": "tracker.keep.org",
                                "enabled": True,
                                "use_time_rule": True,
                                "max_time_minutes": 60.0,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            loaded_store = ConfigStore(path=cfg_path)
            loaded_cfg = loaded_store.get()
            self.assertFalse(loaded_cfg.slot_retention.enabled)
            self.assertEqual(loaded_cfg.slot_retention.max_transfer_slots, 50)
            self.assertFalse(loaded_cfg.trackers["tracker.keep.org"].slot_retention_enabled)

        rule_keep = TrackerRule(
            tracker_id="tracker.keep.org",
            enabled=True,
            use_time_rule=True,
            max_time_minutes=60.0,
            slot_retention_enabled=True,
        )
        rule_drop = TrackerRule(
            tracker_id="tracker.drop.org",
            enabled=True,
            use_time_rule=True,
            max_time_minutes=60.0,
            slot_retention_enabled=False,
        )
        cfg = AppConfig(
            slot_retention=SlotRetentionConfig(
                enabled=True,
                max_transfer_slots=6,
                slot_buffer=2,  # target_max_active = 4
                popularity_metric="combined",
                popularity_weight=50.0,
                size_weight=50.0,
                cancel_dead_or_completed=True,
                max_retention_enabled=True,
                max_retention_hours=336.0,
            ),
            trackers={
                "tracker.keep.org": rule_keep,
                "tracker.drop.org": rule_drop,
            },
        )

        # Base active downloading transfer (1 slot)
        t_dl = {"id": 1, "name": "Active.DL", "status": "DOWNLOADING", "size": 1000}
        # Unselected tracker overtime (should cancel normally)
        t_drop = {
            "id": 2,
            "name": "Drop.Me",
            "status": "SEEDING",
            "seconds_seeding": 7200,
            "size": 10_000_000_000,
            "uploaded": 50_000_000_000,
            "up_speed": 500_000,
            "current_ratio": 5.0,
        }
        # Selected tracker overtime candidates: high, mid, low weighted scores
        t_high = {
            "id": 3,
            "name": "High.Score",
            "status": "SEEDING",
            "seconds_seeding": 7200,
            "size": 50_000_000_000,
            "uploaded": 100_000_000_000,
            "up_speed": 2_000_000,
            "current_ratio": 2.0,
        }
        t_mid = {
            "id": 4,
            "name": "Mid.Score",
            "status": "SEEDING",
            "seconds_seeding": 7200,
            "size": 20_000_000_000,
            "uploaded": 30_000_000_000,
            "up_speed": 500_000,
            "current_ratio": 1.5,
        }
        t_low = {
            "id": 5,
            "name": "Low.Score",
            "status": "SEEDING",
            "seconds_seeding": 7200,
            "size": 500_000_000,
            "uploaded": 100_000_000,
            "up_speed": 1_000,
            "current_ratio": 0.2,
        }

        transfers = [t_dl, t_drop, t_high, t_mid, t_low]
        matched = {
            1: ("tracker.keep.org", rule_keep),
            2: ("tracker.drop.org", rule_drop),
            3: ("tracker.keep.org", rule_keep),
            4: ("tracker.keep.org", rule_keep),
            5: ("tracker.keep.org", rule_keep),
        }
        evals = {
            tid: evaluate_transfer_against_rule(t, matched[tid][1])
            for t, tid in ((x, x["id"]) for x in transfers)
        }

        status = apply_slot_retention(transfers, evals, matched, cfg)
        self.assertEqual(status["available_overtime_slots"], 3)
        self.assertEqual(status["retained_count"], 3)
        self.assertEqual(status["evicted_count"], 0)
        # Unselected tracker still cancels
        self.assertTrue(evals[2]["should_cancel"])
        self.assertFalse(evals[2]["slot_retained"])
        # All 3 selected transfers are retained and ranked High -> Mid -> Low
        for tid in (3, 4, 5):
            self.assertFalse(evals[tid]["should_cancel"])
            self.assertTrue(evals[tid]["slot_retained"])
        self.assertEqual(evals[3]["slot_rank"], 1)
        self.assertEqual(evals[4]["slot_rank"], 2)
        self.assertEqual(evals[5]["slot_rank"], 3)

        # Now add 2 IN_QUEUE transfers: available_overtime_slots becomes 4 - 1 - 2 = 1!
        t_q1 = {"id": 6, "name": "Queued.1", "status": "IN_QUEUE", "size": 1000}
        t_q2 = {"id": 7, "name": "Queued.2", "status": "WAITING", "size": 1000}
        transfers_queued = transfers + [t_q1, t_q2]
        matched_q = {
            **matched,
            6: ("tracker.keep.org", rule_keep),
            7: ("tracker.keep.org", rule_keep),
        }
        evals_q = {
            t["id"]: evaluate_transfer_against_rule(t, matched_q[t["id"]][1])
            for t in transfers_queued
        }

        status_q = apply_slot_retention(transfers_queued, evals_q, matched_q, cfg)
        self.assertEqual(status_q["queued_count"], 2)
        self.assertEqual(status_q["available_overtime_slots"], 1)
        self.assertEqual(status_q["retained_count"], 1)
        self.assertEqual(status_q["evicted_count"], 2)
        # Only Rank #1 (t_high) stays retained; t_mid and t_low are evicted!
        self.assertFalse(evals_q[3]["should_cancel"])
        self.assertTrue(evals_q[3]["slot_retained"])
        self.assertTrue(evals_q[4]["should_cancel"])
        self.assertFalse(evals_q[4]["slot_retained"])
        self.assertIn("Slot limit reached", evals_q[4]["reason"])
        self.assertTrue(evals_q[5]["should_cancel"])
        self.assertIn("Slot limit reached", evals_q[5]["reason"])

        # Verify score weights can prioritize size vs popularity
        size_heavy_cfg = cfg.slot_retention.model_copy(
            update={"popularity_weight": 0.0, "size_weight": 100.0}
        )
        scores_size = compute_unified_retention_scores([t_high, t_low], size_heavy_cfg)
        self.assertGreater(scores_size[3]["score"], scores_size[5]["score"])

    def test_slot_retention_dead_tracker_and_completed_cleanup(self) -> None:
        rule_keep = TrackerRule(
            tracker_id="tracker.keep.org",
            enabled=True,
            use_time_rule=True,
            max_time_minutes=60.0,
            slot_retention_enabled=True,
        )
        cfg = AppConfig(
            slot_retention=SlotRetentionConfig(
                enabled=True,
                max_transfer_slots=50,
                slot_buffer=2,
                cancel_dead_or_completed=True,
            ),
            trackers={"tracker.keep.org": rule_keep},
        )

        transfers = [
            {"id": 10, "name": "Completed.One", "status": "COMPLETED", "seconds_seeding": 0, "size": 5_000_000},
            {"id": 11, "name": "Finished.One", "status": "FINISHED", "seconds_seeding": 0, "size": 5_000_000},
            {"id": 12, "name": "Error.One", "status": "ERROR", "seconds_seeding": 0, "size": 5_000_000},
            {
                "id": 13,
                "name": "Unregistered.One",
                "status": "SEEDING",
                "seconds_seeding": 7200,
                "size": 99_000_000_000,
                "uploaded": 99_000_000_000,
                "up_speed": 9_000_000,
                "tracker_message": "Unregistered torrent",
            },
            {
                "id": 14,
                "name": "NotFound.One",
                "status": "SEEDING",
                "seconds_seeding": 7200,
                "size": 50_000_000_000,
                "error_message": "Torrent not found on tracker",
            },
            {
                "id": 15,
                "name": "Healthy.Seeder",
                "status": "SEEDING",
                "seconds_seeding": 7200,
                "size": 10_000_000,
                "up_speed": 1000,
            },
        ]
        matched = {t["id"]: ("tracker.keep.org", rule_keep) for t in transfers}
        evals = {
            t["id"]: evaluate_transfer_against_rule(t, rule_keep) for t in transfers
        }

        status = apply_slot_retention(transfers, evals, matched, cfg)
        self.assertEqual(status["retained_count"], 1)
        for dead_id in (10, 11, 12, 13, 14):
            self.assertTrue(evals[dead_id]["should_cancel"])
            self.assertFalse(evals[dead_id]["slot_retained"])
            self.assertTrue(detect_dead_or_finished_transfer(next(t for t in transfers if t["id"] == dead_id))[0])
        self.assertIn("Unregistered torrent", evals[13]["reason"])
        self.assertIn("Torrent not found", evals[14]["reason"])

        self.assertFalse(evals[15]["should_cancel"])
        self.assertTrue(evals[15]["slot_retained"])

    def test_slot_retention_max_duration_cap(self) -> None:
        rule_keep = TrackerRule(
            tracker_id="tracker.keep.org",
            enabled=True,
            use_time_rule=True,
            max_time_minutes=60.0,
            slot_retention_enabled=True,
        )
        cfg = AppConfig(
            slot_retention=SlotRetentionConfig(
                enabled=True,
                max_transfer_slots=50,
                slot_buffer=2,
                max_retention_enabled=True,
                max_retention_hours=336.0,  # 14 days
            ),
            trackers={"tracker.keep.org": rule_keep},
        )
        t_10d = {
            "id": 20,
            "name": "Within.14d",
            "status": "SEEDING",
            "seconds_seeding": 10 * 86400,
            "size": 10_000_000,
        }
        t_15d = {
            "id": 21,
            "name": "Over.14d",
            "status": "SEEDING",
            "seconds_seeding": 15 * 86400,
            "size": 99_000_000,
        }
        matched = {20: ("tracker.keep.org", rule_keep), 21: ("tracker.keep.org", rule_keep)}
        evals = {
            20: evaluate_transfer_against_rule(t_10d, rule_keep),
            21: evaluate_transfer_against_rule(t_15d, rule_keep),
        }

        status = apply_slot_retention([t_10d, t_15d], evals, matched, cfg)
        self.assertEqual(status["retained_count"], 1)
        self.assertFalse(evals[20]["should_cancel"])
        self.assertTrue(evals[20]["slot_retained"])
        self.assertEqual(evals[20]["target_seconds"], int(336 * 3600))

        self.assertTrue(evals[21]["should_cancel"])
        self.assertFalse(evals[21]["slot_retained"])
        self.assertIn("Slot retention max cap reached", evals[21]["reason"])

        # When max cap is disabled, the 15-day transfer is retained
        cfg_unlimited = cfg.model_copy(
            update={
                "slot_retention": cfg.slot_retention.model_copy(
                    update={"max_retention_enabled": False}
                )
            }
        )
        evals_u = {
            20: evaluate_transfer_against_rule(t_10d, rule_keep),
            21: evaluate_transfer_against_rule(t_15d, rule_keep),
        }
        status_u = apply_slot_retention([t_10d, t_15d], evals_u, matched, cfg_unlimited)
        self.assertEqual(status_u["retained_count"], 2)
        self.assertFalse(evals_u[21]["should_cancel"])
        self.assertTrue(evals_u[21]["slot_retained"])
        self.assertIsNone(evals_u[21]["target_seconds"])


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
            ) as mock_ensure_oauth, patch(
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
                # Verify that polling transfers does NOT invoke ensure_putwatch_oauth_app
                mock_ensure_oauth.assert_not_called()

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

    async def test_slot_retention_poll_once_integration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            store = ConfigStore(path=cfg_path)
            cfg = store.get()
            cfg.putio_token = "test_token_123"
            cfg.monitoring_enabled = True
            cfg.slot_retention = SlotRetentionConfig(
                enabled=True,
                max_transfer_slots=4,
                slot_buffer=1,  # target_max_active = 3
                popularity_metric="combined",
                popularity_weight=50.0,
                size_weight=50.0,
                cancel_dead_or_completed=True,
                max_retention_enabled=True,
                max_retention_hours=336.0,
            )
            cfg.trackers["tracker.keep.org"] = TrackerRule(
                tracker_id="tracker.keep.org",
                enabled=True,
                use_time_rule=True,
                max_time_minutes=60.0,
                slot_retention_enabled=True,
            )
            cfg.trackers["tracker.drop.org"] = TrackerRule(
                tracker_id="tracker.drop.org",
                enabled=True,
                use_time_rule=True,
                max_time_minutes=60.0,
                slot_retention_enabled=False,
            )
            store.save(cfg)

            service = TransferMonitorService(store=store)

            poll1_transfers = [
                {
                    "id": 2001,
                    "name": "Keep.High",
                    "status": "SEEDING",
                    "seconds_seeding": 7200,
                    "size": 50_000_000_000,
                    "uploaded": 50_000_000_000,
                    "up_speed": 1_000_000,
                    "current_ratio": 1.0,
                    "tracker": "https://tracker.keep.org/announce",
                },
                {
                    "id": 2002,
                    "name": "Keep.Low",
                    "status": "SEEDING",
                    "seconds_seeding": 7200,
                    "size": 500_000_000,
                    "uploaded": 100_000_000,
                    "up_speed": 500,
                    "current_ratio": 0.2,
                    "tracker": "https://tracker.keep.org/announce",
                },
                {
                    "id": 2003,
                    "name": "Keep.Dead",
                    "status": "SEEDING",
                    "seconds_seeding": 7200,
                    "size": 90_000_000_000,
                    "tracker_message": "Unregistered torrent",
                    "tracker": "https://tracker.keep.org/announce",
                },
                {
                    "id": 2004,
                    "name": "Drop.Unselected",
                    "status": "SEEDING",
                    "seconds_seeding": 7200,
                    "size": 90_000_000_000,
                    "tracker": "https://tracker.drop.org/announce",
                },
            ]

            with patch(
                "app.monitor.PutioClient.list_transfers",
                new_callable=AsyncMock,
                return_value=poll1_transfers,
            ), patch(
                "app.monitor.PutioClient.get_account_info",
                new_callable=AsyncMock,
                return_value={"username": "tester"},
            ), patch(
                "app.monitor.PutioClient.cancel_and_remove_transfer",
                new_callable=AsyncMock,
            ) as mock_cancel:
                res1 = await service.poll_once(execute_cancellations=True)
                self.assertTrue(res1["ok"])
                canceled_ids_1 = {c["transfer_id"] for c in res1["canceled"]}
                # 2003 (dead tracker) and 2004 (unselected tracker) canceled; 2001 & 2002 retained!
                self.assertEqual(canceled_ids_1, {2003, 2004})
                self.assertEqual(res1["slot_status"]["retained_count"], 2)
                self.assertEqual(mock_cancel.await_count, 2)

            # Next poll: 2001 & 2002 remain, plus 2 IN_QUEUE transfers appear -> only 3 - 0 - 2 = 1 slot left!
            poll2_transfers = [
                poll1_transfers[0],
                poll1_transfers[1],
                {
                    "id": 2005,
                    "name": "New.Queued.1",
                    "status": "IN_QUEUE",
                    "size": 1_000_000,
                    "tracker": "https://tracker.keep.org/announce",
                },
                {
                    "id": 2006,
                    "name": "New.Queued.2",
                    "status": "WAITING",
                    "size": 1_000_000,
                    "tracker": "https://tracker.keep.org/announce",
                },
            ]
            with patch(
                "app.monitor.PutioClient.list_transfers",
                new_callable=AsyncMock,
                return_value=poll2_transfers,
            ), patch(
                "app.monitor.PutioClient.cancel_and_remove_transfer",
                new_callable=AsyncMock,
            ) as mock_cancel2:
                res2 = await service.poll_once(execute_cancellations=True)
                self.assertTrue(res2["ok"])
                canceled_ids_2 = {c["transfer_id"] for c in res2["canceled"]}
                # Lowest-scored retained transfer (2002) is evicted to make room; 2001 remains retained!
                self.assertEqual(canceled_ids_2, {2002})
                self.assertEqual(res2["slot_status"]["retained_count"], 1)
                self.assertEqual(res2["slot_status"]["evicted_count"], 1)
                mock_cancel2.assert_awaited_once()

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
            self.assertIn("slot_retention", state_json)
            self.assertIn("slot_status", state_json)
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

            # Test slot_retention_enabled on /api/trackers (allowed as sole condition when enabled)
            r_slot_tracker = await client.post(
                "/api/trackers",
                json={
                    "tracker_id": "slotonly.tracker.org",
                    "display_name": "Slot Only Tracker",
                    "enabled": True,
                    "use_time_rule": False,
                    "use_ratio_rule": False,
                    "slot_retention_enabled": True,
                },
            )
            self.assertEqual(r_slot_tracker.status_code, 200)
            slot_trackers = {
                t["tracker_id"]: t for t in r_slot_tracker.json()["trackers"]
            }
            self.assertTrue(slot_trackers["slotonly.tracker.org"]["slot_retention_enabled"])

            # Verify PUT /api/slot-retention updates config and bulk-selects trackers
            r_sr = await client.put(
                "/api/slot-retention",
                json={
                    "enabled": True,
                    "max_transfer_slots": 50,
                    "slot_buffer": 3,
                    "popularity_metric": "up_speed",
                    "popularity_weight": 60.0,
                    "size_weight": 40.0,
                    "cancel_dead_or_completed": True,
                    "max_retention_enabled": True,
                    "max_retention_hours": 336.0,
                    "selected_tracker_ids": ["custom.tracker.org", "__default__"],
                },
            )
            self.assertEqual(r_sr.status_code, 200)
            sr_json = r_sr.json()
            self.assertTrue(sr_json["slot_retention"]["enabled"])
            self.assertEqual(sr_json["slot_retention"]["slot_buffer"], 3)
            self.assertEqual(sr_json["slot_retention"]["popularity_metric"], "up_speed")
            self.assertTrue(sr_json["default_rule"]["slot_retention_enabled"])
            sr_trackers = {t["tracker_id"]: t for t in sr_json["trackers"]}
            self.assertTrue(sr_trackers["custom.tracker.org"]["slot_retention_enabled"])
            self.assertFalse(sr_trackers["slotonly.tracker.org"]["slot_retention_enabled"])

            # Restore clean state
            await client.put(
                "/api/slot-retention",
                json={
                    "enabled": False,
                    "slot_buffer": 2,
                    "selected_tracker_ids": [],
                },
            )
            await client.delete("/api/trackers/slotonly.tracker.org")


if __name__ == "__main__":
    unittest.main()
