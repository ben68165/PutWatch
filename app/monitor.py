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
    hours, rem = divmod(secs, 3600)
    minutes, s = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    if minutes > 0:
        return f"{minutes}m {s:02d}s"
    return f"{s}s"


def describe_rule(rule: TrackerRule) -> str:
    if not rule.enabled:
        return "Disabled"
    parts: List[str] = []
    if rule.use_time_rule:
        metric_label = "Seeding" if rule.time_metric == "seeding_time" else "Age"
        parts.append(f"{metric_label} >= {format_duration(rule.max_time_minutes * 60)}")
    if rule.use_ratio_rule:
        parts.append(f"Ratio >= {rule.max_ratio:.2f}")
    if not parts:
        return "No thresholds selected"
    joiner = f" {rule.condition_operator} "
    return joiner.join(parts)


def find_matching_rule(
    transfer_trackers: List[Tuple[str, str]], config: AppConfig
) -> Tuple[str, Optional[TrackerRule]]:
    """
    Given a transfer's extracted [(tracker_id, raw_url), ...], find the best matching TrackerRule.
    Priority:
      1. Exact match in config.trackers with enabled=True
      2. Substring match in config.trackers with enabled=True
      3. Exact match in config.trackers (even if disabled, for UI visibility)
      4. Default fallback rule if enabled
    Returns (primary_tracker_id, matched_rule_or_none).
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

    # 4. Exact match even if disabled (so UI shows which tracker config corresponds to it)
    for t_id, _ in transfer_trackers:
        if t_id in config.trackers:
            return t_id, config.trackers[t_id]

    return primary_id, None


def evaluate_transfer_against_rule(
    transfer: Dict[str, Any], rule: Optional[TrackerRule]
) -> Dict[str, Any]:
    """
    Evaluate a single Put.io transfer dict against a TrackerRule.
    Returns evaluation metadata including progress percentages and whether it should be canceled.
    """
    status = str(transfer.get("status") or "UNKNOWN").upper()
    seeding_seconds = int(transfer.get("seconds_seeding") or 0)
    current_ratio = float(transfer.get("current_ratio") or 0.0)

    # Calculate total age in seconds if created_at is present
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
            "time_progress_pct": 0.0,
            "ratio_progress_pct": 0.0,
            "remaining_seconds": None,
            "evaluated_seconds": seeding_seconds,
            "target_seconds": None,
            "target_ratio": None,
        }

    # Check if transfer status is eligible for rule evaluation
    seeding_or_done = status in ("SEEDING", "COMPLETED", "COMPLETING")
    eligible_status = seeding_or_done or bool(rule.include_downloading)

    evaluated_seconds = (
        seeding_seconds if rule.time_metric == "seeding_time" else age_seconds
    )
    target_seconds = max(0.0, float(rule.max_time_minutes) * 60.0)
    target_ratio = max(0.0, float(rule.max_ratio))

    time_reached = False
    time_progress_pct = 0.0
    remaining_seconds: Optional[int] = None

    if rule.use_time_rule:
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

    should_cancel = False
    reasons: List[str] = []
    metric_label = "Seeding time" if rule.time_metric == "seeding_time" else "Transfer age"

    if eligible_status:
        if rule.use_time_rule and rule.use_ratio_rule:
            if rule.condition_operator == "AND":
                should_cancel = time_reached and ratio_reached
                if should_cancel:
                    reasons.append(
                        f"{metric_label} {format_duration(evaluated_seconds)} >= {format_duration(target_seconds)} "
                        f"AND Ratio {current_ratio:.2f} >= {target_ratio:.2f}"
                    )
            else:  # OR
                should_cancel = time_reached or ratio_reached
                if time_reached:
                    reasons.append(
                        f"{metric_label} {format_duration(evaluated_seconds)} >= {format_duration(target_seconds)}"
                    )
                if ratio_reached:
                    reasons.append(
                        f"Ratio {current_ratio:.2f} >= {target_ratio:.2f}"
                    )
        elif rule.use_time_rule:
            should_cancel = time_reached
            if time_reached:
                reasons.append(
                    f"{metric_label} {format_duration(evaluated_seconds)} >= {format_duration(target_seconds)}"
                )
        elif rule.use_ratio_rule:
            should_cancel = ratio_reached
            if ratio_reached:
                reasons.append(f"Ratio {current_ratio:.2f} >= {target_ratio:.2f}")

    return {
        "rule_enabled": True,
        "rule_id": rule.tracker_id,
        "rule_summary": describe_rule(rule),
        "condition_operator": rule.condition_operator,
        "use_time_rule": rule.use_time_rule,
        "use_ratio_rule": rule.use_ratio_rule,
        "eligible_status": eligible_status,
        "should_cancel": should_cancel,
        "reason": " | ".join(reasons) if reasons else None,
        "time_progress_pct": time_progress_pct,
        "ratio_progress_pct": ratio_progress_pct,
        "remaining_seconds": remaining_seconds,
        "evaluated_seconds": evaluated_seconds,
        "target_seconds": int(target_seconds) if rule.use_time_rule else None,
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
        self.is_polling: bool = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run_loop())
            logger.info("Started Put.io TransferMonitorService background loop.")

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("Stopped Put.io TransferMonitorService background loop.")

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

            interval = max(10, int(self.store.get().poll_interval_seconds))
            self._wake_event.clear()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=float(interval))
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
                raw_transfers = await client.list_transfers()

                # Refresh account info occasionally if missing
                if self.account_info is None:
                    try:
                        self.account_info = await client.get_account_info()
                    except PutioApiError:
                        pass

                now_iso = _utc_now_iso()
                self.last_poll_at = now_iso
                self.last_error = None

                newly_discovered: List[str] = []
                config_modified = False

                # Count active transfers per tracker for this poll
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

                # Update current active transfer counts on tracker records
                for tracker_id, rule_obj in cfg.trackers.items():
                    new_count = active_counts.get(tracker_id, 0)
                    if rule_obj.matched_transfers_count != new_count:
                        rule_obj.matched_transfers_count = new_count
                        config_modified = True

                if config_modified:
                    cfg = self.store.save(cfg)

                # Second pass: evaluate rules and optionally cancel & remove matching transfers
                enriched_transfers: List[Dict[str, Any]] = []
                canceled_Actions: List[ActivityLogEntry] = []
                is_dry_run = cfg.dry_run if force_dry_run is None else force_dry_run

                for t in raw_transfers:
                    tid = int(t.get("id") or 0)
                    t_name = str(t.get("name") or f"Transfer #{tid}")
                    t_status = str(t.get("status") or "UNKNOWN").upper()
                    extracted = transfer_tracker_map.get(
                        tid, [("untracked", "Direct / DHT (No Tracker)")]
                    )
                    matched_tracker_id, matched_rule = find_matching_rule(
                        extracted, cfg
                    )
                    evaluation = evaluate_transfer_against_rule(t, matched_rule)

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
                                # Increment counter on tracker rule
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
                        canceled_Actions.append(entry)

                    enriched_transfers.append(enriched)

                # If we actually canceled any transfers (non-dry-run), filter them out or re-fetch
                if any(a.success and not a.dry_run for a in canceled_Actions):
                    canceled_ids = {
                        a.transfer_id
                        for a in canceled_Actions
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
                    "canceled": [a.model_dump(mode="json") for a in canceled_Actions],
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
