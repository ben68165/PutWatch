from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.models import ActivityLogEntry, AppConfig, TrackerRule
from app.putio_client import (
    PutioApiError,
    PutioClient,
    extract_trackers_from_transfer,
)
from app.storage import ConfigStore

logger = logging.getLogger("putio_monitor.monitor")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_putio_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str or not isinstance(dt_str, str):
        return None
    cleaned = dt_str.strip()
    try:
        if cleaned.endswith("Z"):
            return datetime.fromisoformat(cleaned[:-1]).replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def format_duration(seconds: float) -> str:
    secs = max(0, int(round(seconds)))
    days, rem_d = divmod(secs, 86400)
    hours, rem = divmod(rem_d, 3600)
    minutes, s = divmod(rem, 60)
    if days > 0:
        if minutes > 0 and days < 3:
            return f"{days}d {hours:02d}h {minutes:02d}m"
        return f"{days}d {hours:02d}h"
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    if minutes > 0:
        return f"{minutes}m {s:02d}s"
    return f"{s}s"


def describe_rule(rule: TrackerRule) -> str:
    if not rule.enabled:
        return "Disabled"
    parts: List[str] = []
    if rule.cancel_on_completed:
        parts.append("Cancel on Download Complete")
    else:
        if rule.use_time_rule:
            metric_label = "Seeding" if rule.time_metric == "seeding_time" else "Age"
            parts.append(f"{metric_label} >= {format_duration(rule.max_time_minutes * 60)}")
        if rule.use_ratio_rule:
            parts.append(f"Ratio >= {rule.max_ratio:.2f}")

    joiner = f" {rule.condition_operator} "
    base_str = joiner.join(parts) if parts else "Active Rule"

    if rule.cancel_completed_on_first_scan and not rule.cancel_on_completed:
        base_str += " + Instant COMPLETED"

    keep_parts: List[str] = []
    if rule.keep_top_seeded_enabled:
        keep_parts.append(f"Top {rule.keep_top_seeded_count} Seeded")
    if rule.keep_largest_enabled:
        keep_parts.append(f"Top {rule.keep_largest_count} Largest")
    if keep_parts:
        keep_dur = format_duration(rule.keep_alive_hours * 3600)
        base_str += f" (Keep {' + '.join(keep_parts)} for {keep_dur})"

    return base_str


def find_matching_rule(
    transfer_trackers: List[Tuple[str, str]], config: AppConfig
) -> Tuple[str, Optional[TrackerRule]]:
    """
    Given a transfer's extracted [(tracker_id, raw_url), ...], find the best matching TrackerRule.
    """
    primary_id = transfer_trackers[0][0] if transfer_trackers else "untracked"

    # 1. Exact match (enabled)
    for t_id, _ in transfer_trackers:
        rule = config.trackers.get(t_id)
        if rule and rule.enabled:
            return t_id, rule

    # 2. Substring match against configured enabled rules
    for cfg_id, rule in config.trackers.items():
        if not rule.enabled:
            continue
        needle = cfg_id.lower()
        for t_id, raw_url in transfer_trackers:
            if needle in t_id.lower() or needle in raw_url.lower():
                return t_id, rule

    # 3. Default rule if enabled
    if config.default_rule and config.default_rule.enabled:
        return primary_id, config.default_rule

    # 4. Exact match even if disabled (for UI visibility)
    for t_id, _ in transfer_trackers:
        if t_id in config.trackers:
            return t_id, config.trackers[t_id]

    return primary_id, None


def compute_keep_alive_protections(
    transfers_for_rule: List[Dict[str, Any]],
    rule: TrackerRule,
    first_seen_status_map: Optional[Dict[int, str]] = None,
) -> Dict[int, str]:
    """
    For all active SEEDING/COMPLETED transfers matched to `rule`, determines which transfer IDs
    qualify for extended keep-alive protection:
      1. Top N Seeded transfers (`keep_top_seeded_enabled`, `keep_top_seeded_count`, `top_seeded_metric`)
      2. Additionally, Top M Largest transfers (`keep_largest_enabled`, `keep_largest_count`)
    Returns a map of {transfer_id: protection_label}.
    """
    protections: Dict[int, str] = {}
    if not rule or not rule.enabled:
        return protections
    if not rule.keep_top_seeded_enabled and not rule.keep_largest_enabled:
        return protections

    seeding_pool: List[Dict[str, Any]] = []
    for t in transfers_for_rule:
        tid = int(t.get("id") or 0)
        st = str(t.get("status") or "").upper()
        if st not in ("SEEDING", "COMPLETED", "COMPLETING"):
            continue
        # If cancel_completed_on_first_scan is active and this transfer was already COMPLETED on first scan,
        # do not protect it so it can be canceled immediately.
        first_st = (
            first_seen_status_map.get(tid, st) if first_seen_status_map else st
        )
        if rule.cancel_completed_on_first_scan and st == "COMPLETED" and first_st == "COMPLETED":
            continue
        seeding_pool.append(t)

    if not seeding_pool:
        return protections

    # 1. Top N Seeded transfers
    if rule.keep_top_seeded_enabled and rule.keep_top_seeded_count > 0:
        metric = rule.top_seeded_metric or "up_speed"

        def _seed_sort_key(t: Dict[str, Any]) -> Tuple[float, float, float, float, int]:
            up_spd = float(t.get("up_speed") or 0.0)
            uploaded = float(t.get("uploaded") or 0.0)
            ratio = float(t.get("current_ratio") or 0.0)
            size = float(t.get("size") or 0.0)
            tid = int(t.get("id") or 0)
            if metric == "uploaded":
                return (uploaded, up_spd, ratio, size, -tid)
            if metric == "ratio":
                return (ratio, uploaded, up_spd, size, -tid)
            return (up_spd, uploaded, ratio, size, -tid)

        sorted_by_seed = sorted(seeding_pool, key=_seed_sort_key, reverse=True)
        for idx, t in enumerate(sorted_by_seed[: rule.keep_top_seeded_count], start=1):
            tid = int(t.get("id") or 0)
            if tid:
                protections[tid] = f"Top Seeded #{idx}"

    # 2. Additionally keep Top M Largest transfers (among those not already in Top Seeded)
    if rule.keep_largest_enabled and rule.keep_largest_count > 0:
        remaining_pool = [
            t for t in seeding_pool if int(t.get("id") or 0) not in protections
        ]
        sorted_by_size = sorted(
            remaining_pool,
            key=lambda t: (
                float(t.get("size") or 0.0),
                float(t.get("uploaded") or 0.0),
                float(t.get("up_speed") or 0.0),
                -int(t.get("id") or 0),
            ),
            reverse=True,
        )
        for idx, t in enumerate(sorted_by_size[: rule.keep_largest_count], start=1):
            tid = int(t.get("id") or 0)
            if tid:
                protections[tid] = f"Largest Transfer #{idx}"

    return protections


def evaluate_transfer_against_rule(
    transfer: Dict[str, Any],
    rule: Optional[TrackerRule],
    protection_label: Optional[str] = None,
    was_completed_on_first_scan: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Evaluate a single Put.io transfer dict against a TrackerRule, factoring in:
      - Immediate cancellation if already COMPLETED on first scan (`cancel_completed_on_first_scan`)
      - Immediate cancellation upon download completion (`cancel_on_completed`)
      - Time threshold (`use_time_rule`, `max_time_minutes`)
      - Ratio threshold (`use_ratio_rule`, `max_ratio`)
      - Combination logic (`OR` vs `AND`)
      - Extended Keep-Alive protection for Top Seeded & Largest transfers (`protection_label`, `keep_alive_hours`)
    """
    status = str(transfer.get("status") or "UNKNOWN").upper()
    seeding_seconds = int(transfer.get("seconds_seeding") or 0)
    current_ratio = float(transfer.get("current_ratio") or 0.0)

    if was_completed_on_first_scan is None:
        was_completed_on_first_scan = status == "COMPLETED"

    created_dt = _parse_putio_datetime(transfer.get("created_at"))
    now_dt = datetime.now(timezone.utc)
    age_seconds = (
        max(0, int((now_dt - created_dt).total_seconds()))
        if created_dt
        else seeding_seconds
    )

    if not rule or not rule.enabled:
        return {
            "rule_enabled": False,
            "rule_id": rule.tracker_id if rule else None,
            "rule_summary": describe_rule(rule) if rule else "No active rule",
            "eligible_status": False,
            "should_cancel": False,
            "reason": None,
            "protection_label": None,
            "protection_active": False,
            "time_progress_pct": 0.0,
            "ratio_progress_pct": 0.0,
            "remaining_seconds": None,
            "evaluated_seconds": seeding_seconds,
            "target_seconds": None,
            "target_ratio": None,
        }

    # Check if this transfer was already COMPLETED when first scanned (instant/cached transfer on Put.io)
    if (
        rule.cancel_completed_on_first_scan
        and status == "COMPLETED"
        and was_completed_on_first_scan
    ):
        return {
            "rule_enabled": True,
            "rule_id": rule.tracker_id,
            "rule_summary": describe_rule(rule),
            "condition_operator": rule.condition_operator,
            "cancel_on_completed": rule.cancel_on_completed,
            "cancel_completed_on_first_scan": True,
            "use_time_rule": True,
            "use_ratio_rule": False,
            "eligible_status": True,
            "should_cancel": True,
            "reason": "Already COMPLETED on first scan (instant/cached transfer on Put.io)",
            "protection_label": None,
            "protection_active": False,
            "keep_alive_target_seconds": None,
            "time_progress_pct": 100.0,
            "ratio_progress_pct": 100.0,
            "remaining_seconds": 0,
            "evaluated_seconds": seeding_seconds,
            "target_seconds": 0,
            "target_ratio": None,
        }

    seeding_or_done = status in ("SEEDING", "COMPLETED", "COMPLETING")
    eligible_status = seeding_or_done or bool(rule.include_downloading)

    evaluated_seconds = (
        seeding_seconds if rule.time_metric == "seeding_time" else age_seconds
    )
    target_seconds = max(0.0, float(rule.max_time_minutes) * 60.0)
    target_ratio = max(0.0, float(rule.max_ratio))
    keep_alive_target_seconds = max(0.0, float(rule.keep_alive_hours) * 3600.0)

    time_reached = False
    time_progress_pct = 0.0
    remaining_seconds: Optional[int] = None

    if rule.cancel_on_completed:
        time_reached = seeding_or_done
        time_progress_pct = 100.0 if seeding_or_done else 0.0
        remaining_seconds = 0 if seeding_or_done else None
    elif rule.use_time_rule:
        if target_seconds <= 0:
            time_reached = True
            time_progress_pct = 100.0
            remaining_seconds = 0
        else:
            time_reached = evaluated_seconds >= target_seconds
            time_progress_pct = min(
                100.0, round((evaluated_seconds / target_seconds) * 100.0, 1)
            )
            remaining_seconds = max(0, int(target_seconds - evaluated_seconds))

    ratio_reached = False
    ratio_progress_pct = 0.0
    if rule.use_ratio_rule:
        if target_ratio <= 0:
            ratio_reached = True
            ratio_progress_pct = 100.0
        else:
            ratio_reached = current_ratio >= target_ratio
            ratio_progress_pct = min(
                100.0, round((current_ratio / target_ratio) * 100.0, 1)
            )

    base_should_cancel = False
    reasons: List[str] = []
    metric_label = "Seeding time" if rule.time_metric == "seeding_time" else "Transfer age"

    if eligible_status:
        if rule.cancel_on_completed and seeding_or_done:
            base_should_cancel = True
            reasons.append("Files downloaded to Put.io (Immediate Cancel Rule)")
        elif rule.use_time_rule and rule.use_ratio_rule:
            if rule.condition_operator == "AND":
                base_should_cancel = time_reached and ratio_reached
                if base_should_cancel:
                    reasons.append(
                        f"{metric_label} {format_duration(evaluated_seconds)} >= {format_duration(target_seconds)} "
                        f"AND Ratio {current_ratio:.2f} >= {target_ratio:.2f}"
                    )
            else:  # OR
                base_should_cancel = time_reached or ratio_reached
                if time_reached:
                    reasons.append(
                        f"{metric_label} {format_duration(evaluated_seconds)} >= {format_duration(target_seconds)}"
                    )
                if ratio_reached:
                    reasons.append(
                        f"Ratio {current_ratio:.2f} >= {target_ratio:.2f}"
                    )
        elif rule.use_time_rule:
            base_should_cancel = time_reached
            if time_reached:
                reasons.append(
                    f"{metric_label} {format_duration(evaluated_seconds)} >= {format_duration(target_seconds)}"
                )
        elif rule.use_ratio_rule:
            base_should_cancel = ratio_reached
            if ratio_reached:
                reasons.append(f"Ratio {current_ratio:.2f} >= {target_ratio:.2f}")

    # Apply Extended Keep-Alive Protection if transfer qualifies as Top Seeded or Largest
    protection_active = False
    should_cancel = base_should_cancel

    if protection_label and seeding_or_done:
        if seeding_seconds < keep_alive_target_seconds:
            protection_active = True
            should_cancel = False
            reasons = []
            target_seconds = keep_alive_target_seconds
            evaluated_seconds = seeding_seconds
            time_progress_pct = (
                min(
                    100.0,
                    round((seeding_seconds / keep_alive_target_seconds) * 100.0, 1),
                )
                if keep_alive_target_seconds > 0
                else 100.0
            )
            remaining_seconds = max(0, int(keep_alive_target_seconds - seeding_seconds))
        else:
            should_cancel = True
            reasons.append(
                f"{protection_label} keep-alive limit reached ({format_duration(seeding_seconds)} >= {format_duration(keep_alive_target_seconds)})"
            )

    effective_use_time = bool(
        rule.use_time_rule or rule.cancel_on_completed or protection_active
    )

    return {
        "rule_enabled": True,
        "rule_id": rule.tracker_id,
        "rule_summary": describe_rule(rule),
        "condition_operator": rule.condition_operator,
        "cancel_on_completed": rule.cancel_on_completed,
        "cancel_completed_on_first_scan": rule.cancel_completed_on_first_scan,
        "use_time_rule": effective_use_time,
        "use_ratio_rule": rule.use_ratio_rule,
        "eligible_status": eligible_status,
        "should_cancel": should_cancel,
        "reason": " | ".join(reasons) if reasons else None,
        "protection_label": protection_label,
        "protection_active": protection_active,
        "keep_alive_target_seconds": int(keep_alive_target_seconds) if protection_label else None,
        "time_progress_pct": time_progress_pct,
        "ratio_progress_pct": ratio_progress_pct,
        "remaining_seconds": remaining_seconds,
        "evaluated_seconds": evaluated_seconds,
        "target_seconds": int(target_seconds) if effective_use_time else None,
        "target_ratio": target_ratio if rule.use_ratio_rule else None,
    }


class TransferMonitorService:
    """
    24/7 background monitor service that polls Put.io transfers,
    discovers trackers from active transfers, and cancels/removes transfers matching user rules.
    """

    def __init__(self, store: ConfigStore) -> None:
        self.store = store
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._poll_lock = asyncio.Lock()

        # Live runtime state for UI
        self.last_poll_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.account_info: Optional[Dict[str, Any]] = None
        self.cached_transfers: List[Dict[str, Any]] = []
        self.first_seen_status: Dict[int, str] = {}
        self.is_polling: bool = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run_loop())
            logger.info("Started PutWatch TransferMonitorService background loop.")

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("Stopped PutWatch TransferMonitorService background loop.")

    def trigger_immediate_poll(self) -> None:
        self._wake_event.set()

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            cfg = self.store.get()
            if cfg.putio_token:
                try:
                    await self.poll_once(execute_cancellations=cfg.monitoring_enabled)
                except Exception as exc:
                    logger.exception("Unexpected error in monitor poll loop: %s", exc)
                    self.last_error = str(exc)

            interval_minutes = max(1, int(self.store.get().poll_interval_minutes))
            interval_seconds = interval_minutes * 60
            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=float(interval_seconds))
            except asyncio.TimeoutError:
                pass

    async def poll_once(
        self,
        *,
        execute_cancellations: bool = True,
        force_dry_run: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Fetches active transfers from Put.io, auto-discovers trackers, evaluates rules,
        and cancels/removes matching transfers if execute_cancellations is True.
        """
        async with self._poll_lock:
            self.is_polling = True
            try:
                cfg = self.store.get()
                if not cfg.putio_token:
                    self.last_error = "Put.io API token not configured."
                    self.cached_transfers = []
                    return {
                        "ok": False,
                        "error": self.last_error,
                        "transfers": [],
                        "newly_discovered_trackers": [],
                        "canceled": [],
                    }

                client = PutioClient(token=cfg.putio_token)

                if not cfg.putio_app_id:
                    app_reg = await client.ensure_putwatch_oauth_app()
                    if app_reg and app_reg.get("app_id"):
                        cfg.putio_app_id = int(app_reg["app_id"])
                        if app_reg.get("token"):
                            cfg.putio_token = str(app_reg["token"])
                            client = PutioClient(token=cfg.putio_token)
                        cfg = self.store.save(cfg)

                raw_transfers = await client.list_transfers()

                if self.account_info is None:
                    try:
                        self.account_info = await client.get_account_info()
                    except PutioApiError:
                        pass

                now_iso = _utc_now_iso()
                self.last_poll_at = now_iso
                self.last_error = None

                # Record first-seen status for each transfer ID
                current_ids = set()
                for t in raw_transfers:
                    tid = int(t.get("id") or 0)
                    if tid:
                        current_ids.add(tid)
                        if tid not in self.first_seen_status:
                            self.first_seen_status[tid] = str(
                                t.get("status") or "UNKNOWN"
                            ).upper()

                # Prune first_seen_status for transfers no longer in Put.io list
                for old_tid in list(self.first_seen_status.keys()):
                    if old_tid not in current_ids:
                        del self.first_seen_status[old_tid]

                newly_discovered: List[str] = []
                config_modified = False
                active_counts: Dict[str, int] = {}

                # First pass: extract trackers & auto-discover
                transfer_tracker_map: Dict[int, List[Tuple[str, str]]] = {}
                for t in raw_transfers:
                    tid = int(t.get("id") or 0)
                    extracted = extract_trackers_from_transfer(t)
                    transfer_tracker_map[tid] = extracted

                    for tracker_id, sample_url in extracted:
                        active_counts[tracker_id] = active_counts.get(tracker_id, 0) + 1
                        if tracker_id in cfg.trackers:
                            existing = cfg.trackers[tracker_id]
                            existing.last_seen_at = now_iso
                            if not existing.sample_announce_url and sample_url:
                                existing.sample_announce_url = sample_url
                            config_modified = True
                        elif cfg.auto_discover_trackers:
                            cfg.trackers[tracker_id] = TrackerRule(
                                tracker_id=tracker_id,
                                display_name=(
                                    "Direct / DHT (No Tracker)"
                                    if tracker_id == "untracked"
                                    else tracker_id
                                ),
                                enabled=False,
                                use_time_rule=True,
                                max_time_minutes=120.0,
                                time_metric="seeding_time",
                                use_ratio_rule=False,
                                max_ratio=1.0,
                                condition_operator="OR",
                                discovered_from_transfers=True,
                                sample_announce_url=sample_url,
                                first_seen_at=now_iso,
                                last_seen_at=now_iso,
                                matched_transfers_count=1,
                            )
                            newly_discovered.append(tracker_id)
                            config_modified = True

                for tracker_id, rule_obj in cfg.trackers.items():
                    new_count = active_counts.get(tracker_id, 0)
                    if rule_obj.matched_transfers_count != new_count:
                        rule_obj.matched_transfers_count = new_count
                        config_modified = True

                if config_modified:
                    cfg = self.store.save(cfg)

                # Group transfers by matched rule to compute Top Seeded & Largest keep-alive rankings per tracker
                transfers_by_rule_id: Dict[str, List[Dict[str, Any]]] = {}
                rules_by_id: Dict[str, TrackerRule] = {}
                matched_for_transfer: Dict[int, Tuple[str, Optional[TrackerRule]]] = {}

                for t in raw_transfers:
                    tid = int(t.get("id") or 0)
                    extracted = transfer_tracker_map.get(
                        tid, [("untracked", "Direct / DHT (No Tracker)")]
                    )
                    matched_tracker_id, matched_rule = find_matching_rule(
                        extracted, cfg
                    )
                    matched_for_transfer[tid] = (matched_tracker_id, matched_rule)
                    if matched_rule and matched_rule.enabled:
                        rid = matched_rule.tracker_id
                        rules_by_id[rid] = matched_rule
                        transfers_by_rule_id.setdefault(rid, []).append(t)

                # Compute protected transfer IDs per rule
                protections_by_transfer_id: Dict[int, str] = {}
                for rid, rule_transfers in transfers_by_rule_id.items():
                    rule_protections = compute_keep_alive_protections(
                        rule_transfers,
                        rules_by_id[rid],
                        first_seen_status_map=self.first_seen_status,
                    )
                    protections_by_transfer_id.update(rule_protections)

                # Second pass: evaluate rules and optionally cancel & remove matching transfers
                enriched_transfers: List[Dict[str, Any]] = []
                canceled_actions: List[ActivityLogEntry] = []
                is_dry_run = cfg.dry_run if force_dry_run is None else force_dry_run

                for t in raw_transfers:
                    tid = int(t.get("id") or 0)
                    t_name = str(t.get("name") or f"Transfer #{tid}")
                    t_status = str(t.get("status") or "UNKNOWN").upper()
                    extracted = transfer_tracker_map.get(
                        tid, [("untracked", "Direct / DHT (No Tracker)")]
                    )
                    matched_tracker_id, matched_rule = matched_for_transfer.get(
                        tid, ("untracked", None)
                    )
                    protection_label = protections_by_transfer_id.get(tid)
                    was_completed_first = (
                        t_status == "COMPLETED"
                        and self.first_seen_status.get(tid, t_status) == "COMPLETED"
                    )
                    evaluation = evaluate_transfer_against_rule(
                        t,
                        matched_rule,
                        protection_label=protection_label,
                        was_completed_on_first_scan=was_completed_first,
                    )

                    enriched = {
                        **t,
                        "primary_tracker": matched_tracker_id,
                        "trackers_list": [
                            {"tracker_id": tr_id, "announce_url": tr_url}
                            for tr_id, tr_url in extracted
                        ],
                        "evaluation": evaluation,
                    }

                    if execute_cancellations and evaluation["should_cancel"]:
                        entry = ActivityLogEntry(
                            id=str(uuid.uuid4())[:8],
                            timestamp=_utc_now_iso(),
                            transfer_id=tid,
                            transfer_name=t_name,
                            tracker_id=matched_tracker_id,
                            status_at_cancel=t_status,
                            seeding_seconds=int(t.get("seconds_seeding") or 0),
                            ratio_at_cancel=float(t.get("current_ratio") or 0.0),
                            rule_summary=evaluation["rule_summary"],
                            reason=evaluation["reason"] or "Rule matched",
                            dry_run=is_dry_run,
                            success=True,
                        )

                        if not is_dry_run:
                            try:
                                await client.cancel_and_remove_transfer(
                                    transfer_id=tid,
                                    current_status=t_status,
                                    clean_after_cancel=cfg.clean_completed_after_cancel,
                                )
                                enriched["canceled_now"] = True
                                latest_cfg = self.store.get()
                                if (
                                    matched_rule
                                    and matched_rule.tracker_id in latest_cfg.trackers
                                ):
                                    latest_cfg.trackers[
                                        matched_rule.tracker_id
                                    ].canceled_transfers_count += 1
                                    self.store.save(latest_cfg)
                            except Exception as exc:
                                logger.error(
                                    "Failed to cancel transfer %s (%s): %s",
                                    tid,
                                    t_name,
                                    exc,
                                )
                                entry.success = False
                                entry.error_message = str(exc)

                        self.store.append_log(entry)
                        canceled_actions.append(entry)

                    enriched_transfers.append(enriched)

                if any(a.success and not a.dry_run for a in canceled_actions):
                    canceled_ids = {
                        a.transfer_id
                        for a in canceled_actions
                        if a.success and not a.dry_run
                    }
                    enriched_transfers = [
                        et
                        for et in enriched_transfers
                        if int(et.get("id") or 0) not in canceled_ids
                    ]

                self.cached_transfers = enriched_transfers
                return {
                    "ok": True,
                    "last_poll_at": self.last_poll_at,
                    "transfers": enriched_transfers,
                    "newly_discovered_trackers": newly_discovered,
                    "canceled": [a.model_dump(mode="json") for a in canceled_actions],
                }
            except PutioApiError as exc:
                self.last_error = f"Put.io API Error: {exc}"
                logger.warning("Put.io API error during poll: %s", exc)
                return {
                    "ok": False,
                    "error": self.last_error,
                    "transfers": self.cached_transfers,
                    "newly_discovered_trackers": [],
                    "canceled": [],
                }
            finally:
                self.is_polling = False
