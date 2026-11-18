from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.models import ActivityLogEntry, AppConfig, SlotRetentionConfig, TrackerRule
from app.putio_client import (
    PutioApiError,
    PutioClient,
    extract_trackers_from_transfer,
)
from app.storage import ConfigStore

logger = logging.getLogger("putio_monitor.monitor")

DEAD_TRACKER_PATTERNS: Tuple[str, ...] = (
    "unregistered",
    "torrent not found",
    "not found",
    "deleted",
    "does not exist",
    "not exist",
    "not authorized",
    "info_hash not found",
    "info hash not found",
    "banned",
    "removed from tracker",
    "trumped",
)


def detect_dead_or_finished_transfer(
    transfer: Dict[str, Any],
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Detect whether a transfer is finished (COMPLETED / FINISHED / ERROR)
    or no longer available/registered on the tracker.
    Returns (is_dead_or_finished, kind, human_readable_reason).
    """
    for field_name in ("tracker_message", "error_message", "status_message"):
        raw_msg = transfer.get(field_name)
        if isinstance(raw_msg, str) and raw_msg.strip():
            cleaned_msg = raw_msg.strip()
            lowered = cleaned_msg.lower()
            for pattern in DEAD_TRACKER_PATTERNS:
                if pattern in lowered:
                    snippet = cleaned_msg[:160]
                    return (
                        True,
                        "dead_tracker",
                        f"Tracker unavailable/removed ({snippet})",
                    )

    status = str(transfer.get("status") or "UNKNOWN").upper()
    if status in ("COMPLETED", "FINISHED", "ERROR"):
        return (
            True,
            "finished_status",
            f"Transfer status is {status} (no longer seeding)",
        )

    return False, None, None


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
        return "Disabled [Slot Retention]" if rule.slot_retention_enabled else "Disabled"
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
        base_str += " + Cancel COMPLETED"
    if rule.cancel_stalled_download_enabled:
        base_str += f" + Stalled DL >= {format_duration(rule.cancel_stalled_download_hours * 3600)}"
    if rule.notify_stalled_download_enabled:
        base_str += f" + Notify Stalled DL >= {format_duration(rule.notify_stalled_download_hours * 3600)}"
    if rule.notify_long_queued_enabled:
        base_str += f" + Notify Queued >= {format_duration(rule.notify_long_queued_hours * 3600)}"

    keep_parts: List[str] = []
    if rule.keep_top_seeded_enabled:
        keep_parts.append(f"Top {rule.keep_top_seeded_count} Seeded")
    if rule.keep_largest_enabled:
        keep_parts.append(f"Top {rule.keep_largest_count} Largest")
    if keep_parts:
        keep_dur = format_duration(rule.keep_alive_hours * 3600)
        base_str += f" (Keep {' + '.join(keep_parts)} for {keep_dur})"

    if rule.slot_retention_enabled:
        base_str += " [Slot Retention]"

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
        # If cancel_completed_on_first_scan is active, every COMPLETED transfer (instantly completed or
        # switched to COMPLETED later) is canceled immediately, so it must never claim a keep-alive slot.
        if rule.cancel_completed_on_first_scan and st == "COMPLETED":
            continue
        if detect_dead_or_finished_transfer(t)[0]:
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
      - Immediate cancellation of any COMPLETED transfer, whether it was already COMPLETED on first scan or
        switched to COMPLETED later (`cancel_completed_on_first_scan`)
      - Immediate cancellation upon download completion (`cancel_on_completed`)
      - Stalled download notification & auto-cancellation (`notify_stalled_download_enabled`, `cancel_stalled_download_enabled`) for DOWNLOADING transfers only
      - Long-queued transfer notification (`notify_long_queued_enabled`) for IN_QUEUE / WAITING transfers
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

    created_dt = _parse_putio_datetime(
        transfer.get("created_at") or transfer.get("started_at")
    )
    now_dt = datetime.now(timezone.utc)
    if created_dt:
        age_seconds = max(0, int((now_dt - created_dt).total_seconds()))
    elif transfer.get("age_seconds") is not None:
        age_seconds = max(0, int(transfer.get("age_seconds") or 0))
    else:
        age_seconds = seeding_seconds

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
            "slot_retained": False,
            "slot_rank": None,
            "slot_score": None,
            "notification_active": False,
            "notification_type": None,
            "notification_label": None,
            "notification_reason": None,
            "time_progress_pct": 0.0,
            "ratio_progress_pct": 0.0,
            "remaining_seconds": None,
            "evaluated_seconds": seeding_seconds,
            "target_seconds": None,
            "target_ratio": None,
        }

    # COMPLETED transfers never seed (seconds_seeding / ratio stay at 0), so time and ratio rules could
    # never fire for them. Cancel them right away, both when they were already COMPLETED on the first scan
    # (instant/cached transfer on Put.io) and when they switched to COMPLETED afterwards.
    if rule.cancel_completed_on_first_scan and status == "COMPLETED":
        completed_reason = (
            "Already COMPLETED on first scan (instant/cached transfer on Put.io)"
            if was_completed_on_first_scan
            else "Status switched to COMPLETED (download finished, not seeding)"
        )
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
            "reason": completed_reason,
            "protection_label": None,
            "protection_active": False,
            "slot_retained": False,
            "slot_rank": None,
            "slot_score": None,
            "notification_active": False,
            "notification_type": None,
            "notification_label": None,
            "notification_reason": None,
            "keep_alive_target_seconds": None,
            "time_progress_pct": 100.0,
            "ratio_progress_pct": 100.0,
            "remaining_seconds": 0,
            "evaluated_seconds": seeding_seconds,
            "target_seconds": 0,
            "target_ratio": None,
        }

    is_downloading = status == "DOWNLOADING"
    is_queued = status in ("IN_QUEUE", "WAITING", "QUEUED")
    seeding_or_done = status in ("SEEDING", "COMPLETED", "COMPLETING")

    # Evaluate Notification / Highlight status (Stalled DOWNLOADING or Long-Queued IN_QUEUE/WAITING)
    notification_active = False
    notification_type: Optional[str] = None
    notification_label: Optional[str] = None
    notification_reason: Optional[str] = None

    if is_downloading and rule.notify_stalled_download_enabled:
        stalled_notify_secs = max(
            0.0, float(rule.notify_stalled_download_hours) * 3600.0
        )
        if age_seconds >= stalled_notify_secs:
            notification_active = True
            notification_type = "stalled_download"
            notification_label = "Stalled Download"
            notification_reason = (
                f"Downloading for {format_duration(age_seconds)} "
                f"(>= {format_duration(stalled_notify_secs)})"
            )
    elif is_queued and rule.notify_long_queued_enabled:
        queued_notify_secs = max(
            0.0, float(rule.notify_long_queued_hours) * 3600.0
        )
        if age_seconds >= queued_notify_secs:
            notification_active = True
            notification_type = "long_queued"
            notification_label = "Queued Too Long"
            notification_reason = (
                f"Queued for {format_duration(age_seconds)} "
                f"(>= {format_duration(queued_notify_secs)})"
            )

    eligible_status = (
        seeding_or_done
        or (is_downloading and bool(rule.cancel_stalled_download_enabled))
        or bool(rule.include_downloading)
    )

    evaluated_seconds = (
        seeding_seconds if rule.time_metric == "seeding_time" else age_seconds
    )
    target_seconds = max(0.0, float(rule.max_time_minutes) * 60.0)
    target_ratio = max(0.0, float(rule.max_ratio))
    keep_alive_target_seconds = max(0.0, float(rule.keep_alive_hours) * 3600.0)

    time_reached = False
    time_progress_pct = 0.0
    remaining_seconds: Optional[int] = None

    # Stalled Download Cancellation applies strictly to DOWNLOADING transfers (never queued transfers)
    if is_downloading and rule.cancel_stalled_download_enabled:
        stalled_cancel_secs = max(
            0.0, float(rule.cancel_stalled_download_hours) * 3600.0
        )
        evaluated_seconds = age_seconds
        target_seconds = stalled_cancel_secs
        if stalled_cancel_secs <= 0:
            time_reached = True
            time_progress_pct = 100.0
            remaining_seconds = 0
        else:
            time_reached = age_seconds >= stalled_cancel_secs
            time_progress_pct = min(
                100.0, round((age_seconds / stalled_cancel_secs) * 100.0, 1)
            )
            remaining_seconds = max(0, int(stalled_cancel_secs - age_seconds))
    elif rule.cancel_on_completed:
        time_reached = seeding_or_done
        time_progress_pct = 100.0 if seeding_or_done else 0.0
        remaining_seconds = 0 if seeding_or_done else None
    elif rule.use_time_rule and seeding_or_done:
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
    if rule.use_ratio_rule and seeding_or_done:
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

    if is_downloading and rule.cancel_stalled_download_enabled:
        if time_reached:
            base_should_cancel = True
            reasons.append(
                f"Stalled Download: Downloading for {format_duration(age_seconds)} >= {format_duration(target_seconds)}"
            )
    elif seeding_or_done or bool(rule.include_downloading):
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
        (rule.use_time_rule and seeding_or_done)
        or rule.cancel_on_completed
        or protection_active
        or (is_downloading and rule.cancel_stalled_download_enabled)
    )

    return {
        "rule_enabled": True,
        "rule_id": rule.tracker_id,
        "rule_summary": describe_rule(rule),
        "condition_operator": rule.condition_operator,
        "cancel_on_completed": rule.cancel_on_completed,
        "cancel_completed_on_first_scan": rule.cancel_completed_on_first_scan,
        "cancel_stalled_download_enabled": rule.cancel_stalled_download_enabled,
        "use_time_rule": effective_use_time,
        "use_ratio_rule": bool(rule.use_ratio_rule and seeding_or_done),
        "eligible_status": eligible_status,
        "should_cancel": should_cancel,
        "reason": " | ".join(reasons) if reasons else None,
        "protection_label": protection_label,
        "protection_active": protection_active,
        "slot_retained": False,
        "slot_rank": None,
        "slot_score": None,
        "notification_active": notification_active,
        "notification_type": notification_type,
        "notification_label": notification_label,
        "notification_reason": notification_reason,
        "keep_alive_target_seconds": int(keep_alive_target_seconds) if protection_label else None,
        "time_progress_pct": time_progress_pct,
        "ratio_progress_pct": ratio_progress_pct,
        "remaining_seconds": remaining_seconds,
        "evaluated_seconds": evaluated_seconds,
        "target_seconds": int(target_seconds) if effective_use_time else None,
        "target_ratio": target_ratio if rule.use_ratio_rule else None,
    }


def _normalize_metric_scores(values_by_tid: Dict[int, float]) -> Dict[int, float]:
    """
    Normalize a non-negative metric into [0.0, 1.0] combining rank percentile and value ratio.
    Zero or negative values receive 0.0; tied values receive identical scores.
    """
    n = len(values_by_tid)
    if n == 0:
        return {}
    max_val = max(values_by_tid.values(), default=0.0)
    if max_val <= 0.0:
        return {tid: 0.0 for tid in values_by_tid}

    sorted_vals = sorted(values_by_tid.values())
    rank_by_val: Dict[float, int] = {}
    for idx, val in enumerate(sorted_vals, start=1):
        rank_by_val[val] = idx

    normalized: Dict[int, float] = {}
    for tid, val in values_by_tid.items():
        if val <= 0.0:
            normalized[tid] = 0.0
        else:
            rank_pct = rank_by_val[val] / float(n)
            val_pct = val / max_val
            normalized[tid] = 0.5 * rank_pct + 0.5 * val_pct
    return normalized


def compute_unified_retention_scores(
    candidates: List[Dict[str, Any]],
    slot_cfg: SlotRetentionConfig,
) -> Dict[int, Dict[str, float]]:
    """
    Compute unified weighted score (0-100) combining popularity and file size
    across all candidate transfers for global slot-based retention.
    """
    if not candidates:
        return {}

    size_vals: Dict[int, float] = {}
    up_speed_vals: Dict[int, float] = {}
    uploaded_vals: Dict[int, float] = {}
    ratio_vals: Dict[int, float] = {}

    for t in candidates:
        tid = int(t.get("id") or 0)
        size_vals[tid] = max(0.0, float(t.get("size") or 0.0))
        up_speed_vals[tid] = max(0.0, float(t.get("up_speed") or 0.0))
        uploaded_vals[tid] = max(0.0, float(t.get("uploaded") or 0.0))
        ratio_vals[tid] = max(0.0, float(t.get("current_ratio") or 0.0))

    size_norm = _normalize_metric_scores(size_vals)
    up_speed_norm = _normalize_metric_scores(up_speed_vals)
    uploaded_norm = _normalize_metric_scores(uploaded_vals)
    ratio_norm = _normalize_metric_scores(ratio_vals)

    metric = slot_cfg.popularity_metric or "combined"
    w_pop = max(0.0, float(slot_cfg.popularity_weight))
    w_size = max(0.0, float(slot_cfg.size_weight))
    if w_pop + w_size <= 0.0:
        w_pop = 50.0
        w_size = 50.0
    total_w = w_pop + w_size

    scores: Dict[int, Dict[str, float]] = {}
    for tid in size_vals:
        if metric == "up_speed":
            pop_val = 0.7 * up_speed_norm[tid] + 0.3 * uploaded_norm[tid]
        elif metric == "uploaded":
            pop_val = uploaded_norm[tid]
        elif metric == "ratio":
            pop_val = ratio_norm[tid]
        else:
            pop_val = (
                0.4 * uploaded_norm[tid]
                + 0.35 * up_speed_norm[tid]
                + 0.25 * ratio_norm[tid]
            )

        size_val = size_norm[tid]
        combined = ((w_pop * pop_val + w_size * size_val) / total_w) * 100.0
        scores[tid] = {
            "score": round(combined, 1),
            "pop_score": round(pop_val * 100.0, 1),
            "size_score": round(size_val * 100.0, 1),
        }
    return scores


def _is_transfer_slot_retention_eligible(
    matched_tracker_id: str,
    matched_rule: Optional[TrackerRule],
    extracted_trackers: List[Tuple[str, str]],
    cfg: AppConfig,
) -> bool:
    """
    Determine whether a transfer belongs to a tracker opted into global slot-based retention.
    """
    if matched_rule is not None and matched_rule.tracker_id != "__default__":
        if matched_rule.slot_retention_enabled:
            return True
        exact = cfg.trackers.get(matched_tracker_id)
        return bool(exact and exact.slot_retention_enabled)

    if matched_rule is not None and matched_rule.slot_retention_enabled:
        return True

    exact = cfg.trackers.get(matched_tracker_id)
    if exact and exact.slot_retention_enabled:
        return True

    for tr_id, _ in extracted_trackers:
        tr_rule = cfg.trackers.get(tr_id)
        if tr_rule and tr_rule.slot_retention_enabled:
            return True

    return False


def apply_slot_retention(
    raw_transfers: List[Dict[str, Any]],
    evaluations_by_tid: Dict[int, Dict[str, Any]],
    matched_for_transfer: Dict[int, Tuple[str, Optional[TrackerRule]]],
    cfg: AppConfig,
    transfer_tracker_map: Optional[Dict[int, List[Tuple[str, str]]]] = None,
) -> Dict[str, Any]:
    """
    Evaluate global Put.io slot capacity across all transfers and retain top-scoring
    overtime seeding transfers on selected trackers while respecting buffer and queued transfers.
    Mutates evaluation dicts in `evaluations_by_tid` in place and returns a slot status summary.
    """
    slot_cfg = cfg.slot_retention
    queued_statuses = ("IN_QUEUE", "WAITING", "QUEUED")
    target_max_active = max(
        0, int(slot_cfg.max_transfer_slots) - int(slot_cfg.slot_buffer)
    )

    participating_by_tid: Dict[int, bool] = {}
    for t in raw_transfers:
        tid = int(t.get("id") or 0)
        matched_tracker_id, matched_rule = matched_for_transfer.get(
            tid, ("untracked", None)
        )
        extracted = (transfer_tracker_map or {}).get(tid, [])
        is_part = _is_transfer_slot_retention_eligible(
            matched_tracker_id, matched_rule, extracted, cfg
        )
        participating_by_tid[tid] = is_part

        # Step 1: Remove dead/completed/finished transfers on selected trackers
        if slot_cfg.enabled and slot_cfg.cancel_dead_or_completed and is_part:
            ev = evaluations_by_tid.get(tid)
            if ev is not None:
                is_dead, _kind, dead_reason = detect_dead_or_finished_transfer(t)
                if is_dead:
                    ev["should_cancel"] = True
                    ev["protection_active"] = False
                    ev["protection_label"] = None
                    ev["slot_retained"] = False
                    ev["reason"] = dead_reason
                    if not ev.get("rule_enabled"):
                        ev["rule_enabled"] = True
                        ev["rule_summary"] = "Slot Retention (Dead/Completed Cleanup)"

    queued_count = 0
    base_active_count = 0
    overtime_candidates: List[Dict[str, Any]] = []

    for t in raw_transfers:
        tid = int(t.get("id") or 0)
        st = str(t.get("status") or "UNKNOWN").upper()
        ev = evaluations_by_tid.get(tid, {})
        should_cancel = bool(ev.get("should_cancel"))

        if st in queued_statuses:
            if not should_cancel:
                queued_count += 1
            continue

        is_overtime_candidate = (
            slot_cfg.enabled
            and participating_by_tid.get(tid, False)
            and st == "SEEDING"
            and should_cancel
            and not detect_dead_or_finished_transfer(t)[0]
        )
        if is_overtime_candidate:
            overtime_candidates.append(t)
        elif not should_cancel:
            base_active_count += 1

    available_overtime_slots = max(
        0, target_max_active - base_active_count - queued_count
    )

    if not slot_cfg.enabled:
        return {
            "enabled": False,
            "max_transfer_slots": int(slot_cfg.max_transfer_slots),
            "slot_buffer": int(slot_cfg.slot_buffer),
            "target_max_active": target_max_active,
            "total_transfers": len(raw_transfers),
            "base_active_count": base_active_count,
            "queued_count": queued_count,
            "available_overtime_slots": available_overtime_slots,
            "retained_count": 0,
            "evicted_count": 0,
            "candidate_count": 0,
        }

    # Filter out candidates exceeding optional max retention cap
    eligible_candidates: List[Dict[str, Any]] = []
    max_ret_secs = (
        max(0.0, float(slot_cfg.max_retention_hours) * 3600.0)
        if slot_cfg.max_retention_enabled
        else None
    )
    expired_count = 0

    for t in overtime_candidates:
        tid = int(t.get("id") or 0)
        ev = evaluations_by_tid[tid]
        seeding_seconds = int(t.get("seconds_seeding") or 0)
        if max_ret_secs is not None and seeding_seconds >= max_ret_secs:
            expired_count += 1
            cap_msg = (
                f"Slot retention max cap reached "
                f"({format_duration(seeding_seconds)} >= {format_duration(max_ret_secs)})"
            )
            ev["reason"] = f"{ev['reason']} | {cap_msg}" if ev.get("reason") else cap_msg
            ev["slot_retained"] = False
            ev["protection_active"] = False
            ev["protection_label"] = None
            continue
        eligible_candidates.append(t)

    scores = compute_unified_retention_scores(eligible_candidates, slot_cfg)
    ranked_candidates = sorted(
        eligible_candidates,
        key=lambda t: (
            scores[int(t.get("id") or 0)]["score"],
            float(t.get("up_speed") or 0.0),
            float(t.get("uploaded") or 0.0),
            float(t.get("size") or 0.0),
            float(t.get("current_ratio") or 0.0),
            -int(t.get("id") or 0),
        ),
        reverse=True,
    )

    retained_count = 0
    evicted_count = expired_count

    for rank, t in enumerate(ranked_candidates, start=1):
        tid = int(t.get("id") or 0)
        ev = evaluations_by_tid[tid]
        score_info = scores[tid]
        seeding_seconds = int(t.get("seconds_seeding") or 0)

        ev["slot_rank"] = rank
        ev["slot_score"] = score_info["score"]

        if rank <= available_overtime_slots:
            retained_count += 1
            ev["should_cancel"] = False
            ev["reason"] = None
            ev["slot_retained"] = True
            ev["protection_active"] = True
            ev["protection_label"] = (
                f"Slot Retained #{rank} (Score {score_info['score']:.0f})"
            )
            if max_ret_secs is not None:
                ev["use_time_rule"] = True
                ev["target_seconds"] = int(max_ret_secs)
                ev["evaluated_seconds"] = seeding_seconds
                ev["time_progress_pct"] = (
                    min(100.0, round((seeding_seconds / max_ret_secs) * 100.0, 1))
                    if max_ret_secs > 0
                    else 100.0
                )
                ev["remaining_seconds"] = max(0, int(max_ret_secs - seeding_seconds))
            else:
                ev["use_time_rule"] = False
                ev["target_seconds"] = None
                ev["remaining_seconds"] = None
        else:
            evicted_count += 1
            ev["slot_retained"] = False
            ev["protection_active"] = False
            ev["protection_label"] = None
            evict_msg = (
                f"Slot limit reached (Rank #{rank} > {available_overtime_slots} "
                f"available overtime slots; {queued_count} queued)"
            )
            ev["reason"] = f"{ev['reason']} | {evict_msg}" if ev.get("reason") else evict_msg

    return {
        "enabled": True,
        "max_transfer_slots": int(slot_cfg.max_transfer_slots),
        "slot_buffer": int(slot_cfg.slot_buffer),
        "target_max_active": target_max_active,
        "total_transfers": len(raw_transfers),
        "base_active_count": base_active_count,
        "queued_count": queued_count,
        "available_overtime_slots": available_overtime_slots,
        "retained_count": retained_count,
        "evicted_count": evicted_count,
        "candidate_count": len(overtime_candidates),
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
        self.last_slot_status: Optional[Dict[str, Any]] = None
        self.is_polling: bool = False

    def get_slot_status(self, cfg: AppConfig) -> Dict[str, Any]:
        target_max = max(
            0,
            int(cfg.slot_retention.max_transfer_slots)
            - int(cfg.slot_retention.slot_buffer),
        )
        if self.last_slot_status is not None:
            return {
                **self.last_slot_status,
                "enabled": bool(cfg.slot_retention.enabled),
                "max_transfer_slots": int(cfg.slot_retention.max_transfer_slots),
                "slot_buffer": int(cfg.slot_retention.slot_buffer),
                "target_max_active": target_max,
            }
        return {
            "enabled": bool(cfg.slot_retention.enabled),
            "max_transfer_slots": int(cfg.slot_retention.max_transfer_slots),
            "slot_buffer": int(cfg.slot_retention.slot_buffer),
            "target_max_active": target_max,
            "total_transfers": 0,
            "base_active_count": 0,
            "queued_count": 0,
            "available_overtime_slots": target_max,
            "retained_count": 0,
            "evicted_count": 0,
            "candidate_count": 0,
        }

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
            try:
                removed_logs = self.store.prune_logs(cfg.log_retention_days)
                if removed_logs:
                    logger.info(
                        "Auto-deleted %d cancellation log entr%s older than %d day(s).",
                        removed_logs,
                        "y" if removed_logs == 1 else "ies",
                        cfg.log_retention_days,
                    )
            except Exception as exc:
                logger.exception("Failed to prune cancellation log: %s", exc)
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

                # Second pass: evaluate per-tracker rules across all transfers first
                evaluated_items: List[
                    Tuple[
                        Dict[str, Any],
                        int,
                        str,
                        str,
                        List[Tuple[str, str]],
                        str,
                        Optional[TrackerRule],
                        Dict[str, Any],
                    ]
                ] = []
                evaluations_by_tid: Dict[int, Dict[str, Any]] = {}

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
                    evaluations_by_tid[tid] = evaluation
                    evaluated_items.append(
                        (
                            t,
                            tid,
                            t_name,
                            t_status,
                            extracted,
                            matched_tracker_id,
                            matched_rule,
                            evaluation,
                        )
                    )

                # Apply global slot-based retention across all transfers
                slot_status = apply_slot_retention(
                    raw_transfers,
                    evaluations_by_tid,
                    matched_for_transfer,
                    cfg,
                    transfer_tracker_map=transfer_tracker_map,
                )
                self.last_slot_status = slot_status

                # Third pass: execute cancellations and build enriched transfer payload
                enriched_transfers: List[Dict[str, Any]] = []
                canceled_actions: List[ActivityLogEntry] = []
                is_dry_run = cfg.dry_run if force_dry_run is None else force_dry_run

                for (
                    t,
                    tid,
                    t_name,
                    t_status,
                    extracted,
                    matched_tracker_id,
                    matched_rule,
                    evaluation,
                ) in evaluated_items:
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

                # Move transfers with an active notification (stalled download / long queued) to the top
                enriched_transfers.sort(
                    key=lambda et: 0
                    if et.get("evaluation", {}).get("notification_active")
                    else 1
                )

                self.cached_transfers = enriched_transfers
                return {
                    "ok": True,
                    "last_poll_at": self.last_poll_at,
                    "transfers": enriched_transfers,
                    "newly_discovered_trackers": newly_discovered,
                    "canceled": [a.model_dump(mode="json") for a in canceled_actions],
                    "slot_status": slot_status,
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
