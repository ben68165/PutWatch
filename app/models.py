from __future__ import annotations

from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field


ConditionOperator = Literal["OR", "AND"]


class TrackerRule(BaseModel):
    """Configuration and cancellation rule for a specific tracker."""

    tracker_id: str = Field(
        ...,
        description="Normalized tracker identifier (usually the tracker hostname, e.g. 'tracker.opentrackr.org').",
    )
    display_name: str = Field(
        default="",
        description="Optional friendly display name for the tracker.",
    )
    enabled: bool = Field(
        default=False,
        description="Whether automatic cancellation is active for this tracker.",
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
    include_downloading: bool = Field(
        default=False,
        description="If True, also evaluate transfers that are still DOWNLOADING/IN_QUEUE (useful when time_metric='total_age'). By default, only SEEDING/COMPLETED transfers are canceled.",
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
    monitoring_enabled: bool = Field(
        default=True,
        description="Master switch for the 24/7 background monitoring worker.",
    )
    poll_interval_seconds: int = Field(
        default=30,
        ge=10,
        le=3600,
        description="How often (in seconds) to poll Put.io transfers and evaluate tracker rules.",
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
    default_rule: TrackerRule = Field(
        default_factory=lambda: TrackerRule(
            tracker_id="__default__",
            display_name="Default / Fallback Rule (Unconfigured Trackers)",
            enabled=False,
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
    monitoring_enabled: Optional[bool] = None
    poll_interval_seconds: Optional[int] = Field(default=None, ge=10, le=3600)
    dry_run: Optional[bool] = None
    auto_discover_trackers: Optional[bool] = None
    clean_completed_after_cancel: Optional[bool] = None


class TrackerRuleUpdateRequest(BaseModel):
    tracker_id: str
    display_name: Optional[str] = None
    enabled: bool = False
    use_time_rule: bool = True
    max_time_minutes: float = Field(default=120.0, ge=0.0)
    time_metric: Literal["seeding_time", "total_age"] = "seeding_time"
    use_ratio_rule: bool = False
    max_ratio: float = Field(default=1.0, ge=0.0)
    condition_operator: ConditionOperator = "OR"
    include_downloading: bool = False
