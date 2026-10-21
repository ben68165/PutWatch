from __future__ import annotations

from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field


ConditionOperator = Literal["OR", "AND"]
TopSeededMetric = Literal["up_speed", "uploaded", "ratio"]

PUTWATCH_APP_NAME = "PutWatch"
PUTWATCH_REPO_URL = "https://github.com/ben68165/PutWatch"
PUTWATCH_APP_DESCRIPTION = (
    "PutWatch - Automatically monitors active Put.io transfers and cancels/removes "
    "transfers based on per-tracker seeding time and share ratio rules."
)


class TrackerRule(BaseModel):
    """Configuration and cancellation rule for a specific tracker."""

    tracker_id: str = Field(
        ...,
        description="Normalized tracker identifier (usually the tracker hostname, e.g. 'tracker.opentrackr.org').",
    )
    display_name: str = Field(
        default="",
        description="Friendly display label for the tracker shown across all tabs.",
    )
    enabled: bool = Field(
        default=False,
        description="Whether automatic cancellation is active for this tracker.",
    )
    # Option to cancel transfers whose status is COMPLETED (instant/cached on first scan, or switched to
    # COMPLETED later). The field name is kept for backwards compatibility with existing config files.
    cancel_completed_on_first_scan: bool = Field(
        default=False,
        description="If True, every transfer with status COMPLETED (already completed on first scan, or switched to COMPLETED later) is canceled and removed immediately without needing to meet seed time or ratio.",
    )
    # Option to cancel immediately once download to Put.io is finished
    cancel_on_completed: bool = Field(
        default=False,
        description="If True, cancel and remove the transfer as soon as its files finish downloading to Put.io (unless protected by a keep-alive rule).",
    )
    # Time threshold (in minutes, converted to seconds during evaluation)
    use_time_rule: bool = Field(
        default=True,
        description="Whether to evaluate the seeding/active time threshold.",
    )
    max_time_minutes: float = Field(
        default=120.0,
        ge=0.0,
        description="Cancel after this many minutes of seeding (or total active time if time_metric='total_age').",
    )
    time_metric: Literal["seeding_time", "total_age"] = Field(
        default="seeding_time",
        description="Metric to measure time against: 'seeding_time' (seconds_seeding) or 'total_age' (since created_at).",
    )
    # Ratio threshold
    use_ratio_rule: bool = Field(
        default=False,
        description="Whether to evaluate the share ratio threshold.",
    )
    max_ratio: float = Field(
        default=1.0,
        ge=0.0,
        description="Cancel once current_ratio reaches or exceeds this value.",
    )
    # Combination operator when both use_time_rule and use_ratio_rule are True
    condition_operator: ConditionOperator = Field(
        default="OR",
        description="How to combine time and ratio rules when both are enabled: 'OR' (either reached) or 'AND' (both reached).",
    )
    # Extended Keep-Alive protection for Top Seeded & Largest transfers per tracker
    keep_top_seeded_enabled: bool = Field(
        default=False,
        description="Keep the top N seeding transfers on this tracker alive for keep_alive_hours.",
    )
    keep_top_seeded_count: int = Field(
        default=3,
        ge=1,
        le=500,
        description="Number of top seeding transfers to keep alive longer.",
    )
    top_seeded_metric: TopSeededMetric = Field(
        default="up_speed",
        description="How to rank top seeded transfers: 'up_speed' (upload speed + total uploaded), 'uploaded' (total uploaded bytes), or 'ratio' (share ratio).",
    )
    keep_largest_enabled: bool = Field(
        default=False,
        description="Additionally keep the M largest transfers on this tracker alive for keep_alive_hours.",
    )
    keep_largest_count: int = Field(
        default=3,
        ge=1,
        le=500,
        description="Number of largest transfers (by file size) per tracker to additionally keep alive longer.",
    )
    keep_alive_hours: float = Field(
        default=72.0,
        ge=0.5,
        description="How many hours (or days * 24) to keep top seeded and/or largest transfers alive before canceling.",
    )
    include_downloading: bool = Field(
        default=False,
        description="Legacy flag; superseded by cancel_stalled_download_enabled.",
    )
    # Stalled Download Detection (applies to DOWNLOADING transfers only, not queued)
    notify_stalled_download_enabled: bool = Field(
        default=False,
        description="If True, highlight and move DOWNLOADING transfers to the top of the transfer list after notify_stalled_download_hours.",
    )
    notify_stalled_download_hours: float = Field(
        default=6.0,
        ge=0.1,
        description="Hours (days * 24 + hours) after which a DOWNLOADING transfer is highlighted at the top.",
    )
    cancel_stalled_download_enabled: bool = Field(
        default=False,
        description="If True, evaluate and cancel/delete DOWNLOADING transfers after cancel_stalled_download_hours.",
    )
    cancel_stalled_download_hours: float = Field(
        default=24.0,
        ge=0.1,
        description="Hours (days * 24 + hours) after which a DOWNLOADING transfer is canceled and removed.",
    )
    # Queued Transfer Notification (applies to IN_QUEUE / WAITING transfers)
    notify_long_queued_enabled: bool = Field(
        default=False,
        description="If True, highlight and move IN_QUEUE / WAITING transfers to the top of the transfer list after notify_long_queued_hours.",
    )
    notify_long_queued_hours: float = Field(
        default=6.0,
        ge=0.1,
        description="Hours (days * 24 + hours) after which a queued transfer is highlighted at the top.",
    )
    # Discovery metadata
    discovered_from_transfers: bool = Field(
        default=True,
        description="Whether this tracker was automatically discovered from active Put.io transfers.",
    )
    sample_announce_url: Optional[str] = Field(
        default=None,
        description="Example raw tracker URL seen on a transfer.",
    )
    first_seen_at: Optional[str] = Field(default=None)
    last_seen_at: Optional[str] = Field(default=None)
    matched_transfers_count: int = Field(default=0)
    canceled_transfers_count: int = Field(default=0)


class ActivityLogEntry(BaseModel):
    """Audit log entry when a transfer is canceled and removed."""

    id: str
    timestamp: str
    transfer_id: int
    transfer_name: str
    tracker_id: str
    status_at_cancel: str
    seeding_seconds: int
    ratio_at_cancel: float
    rule_summary: str
    reason: str
    dry_run: bool = False
    success: bool = True
    error_message: Optional[str] = None


class AppConfig(BaseModel):
    """Persistent application configuration stored in /data/config.json."""

    putio_token: str = Field(
        default="",
        description="Put.io OAuth2 Bearer Token.",
    )
    putio_app_id: Optional[int] = Field(
        default=None,
        description="Put.io OAuth App ID (Client ID) registered for PutWatch (https://github.com/ben68165/PutWatch).",
    )
    monitoring_enabled: bool = Field(
        default=True,
        description="Master switch for the 24/7 background monitoring worker.",
    )
    poll_interval_minutes: int = Field(
        default=5,
        ge=1,
        le=1440,
        description="How often (in minutes) to poll Put.io transfers and evaluate tracker rules.",
    )
    dry_run: bool = Field(
        default=False,
        description="If True, logs matching transfers without actually canceling/removing them on Put.io.",
    )
    auto_discover_trackers: bool = Field(
        default=True,
        description="Automatically add newly seen trackers from active transfers into the tracker config list.",
    )
    clean_completed_after_cancel: bool = Field(
        default=True,
        description="After canceling a seeding transfer, also remove/clean its entry from the Put.io transfer list (files in storage are kept).",
    )
    hide_transfer_names: bool = Field(
        default=False,
        description="If True, blurs transfer names in the Active Transfers tab for privacy.",
    )
    pin_transfers_header: bool = Field(
        default=True,
        description="If True, pins the filter, privacy toggle, and grouping header on the Active Transfers tab to the top and compacts it when scrolling down (unless device screen space is too small).",
    )
    log_retention_days: int = Field(
        default=7,
        ge=0,
        le=3650,
        description="Cancellation log entries older than this many days are deleted automatically (0 = keep forever).",
    )
    default_rule: TrackerRule = Field(
        default_factory=lambda: TrackerRule(
            tracker_id="__default__",
            display_name="Default / Fallback Rule (Unconfigured Trackers)",
            enabled=False,
            cancel_completed_on_first_scan=False,
            cancel_on_completed=False,
            use_time_rule=True,
            max_time_minutes=240.0,
            time_metric="seeding_time",
            use_ratio_rule=False,
            max_ratio=2.0,
            condition_operator="OR",
            discovered_from_transfers=False,
        )
    )
    trackers: Dict[str, TrackerRule] = Field(
        default_factory=dict,
        description="Map of tracker_id -> TrackerRule.",
    )
    activity_log: List[ActivityLogEntry] = Field(
        default_factory=list,
        description="Recent cancellation events (most recent first).",
    )


class SettingsUpdateRequest(BaseModel):
    putio_token: Optional[str] = None
    putio_app_id: Optional[int] = None
    monitoring_enabled: Optional[bool] = None
    poll_interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    dry_run: Optional[bool] = None
    auto_discover_trackers: Optional[bool] = None
    clean_completed_after_cancel: Optional[bool] = None
    hide_transfer_names: Optional[bool] = None
    pin_transfers_header: Optional[bool] = None
    log_retention_days: Optional[int] = Field(default=None, ge=0, le=3650)


class TrackerRuleUpdateRequest(BaseModel):
    tracker_id: str
    display_name: Optional[str] = None
    enabled: bool = False
    cancel_completed_on_first_scan: bool = False
    cancel_on_completed: bool = False
    use_time_rule: bool = True
    max_time_minutes: float = Field(default=120.0, ge=0.0)
    time_metric: Literal["seeding_time", "total_age"] = "seeding_time"
    use_ratio_rule: bool = False
    max_ratio: float = Field(default=1.0, ge=0.0)
    condition_operator: ConditionOperator = "OR"
    keep_top_seeded_enabled: bool = False
    keep_top_seeded_count: int = Field(default=3, ge=1, le=500)
    top_seeded_metric: TopSeededMetric = "up_speed"
    keep_largest_enabled: bool = False
    keep_largest_count: int = Field(default=3, ge=1, le=500)
    keep_alive_hours: float = Field(default=72.0, ge=0.1)
    include_downloading: bool = False
    notify_stalled_download_enabled: bool = False
    notify_stalled_download_hours: float = Field(default=6.0, ge=0.1)
    cancel_stalled_download_enabled: bool = False
    cancel_stalled_download_hours: float = Field(default=24.0, ge=0.1)
    notify_long_queued_enabled: bool = False
    notify_long_queued_hours: float = Field(default=6.0, ge=0.1)
