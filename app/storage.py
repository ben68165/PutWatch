from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.models import ActivityLogEntry, AppConfig, TrackerRule

logger = logging.getLogger("putio_monitor.storage")


def _parse_iso_utc(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    cleaned = value.strip()
    try:
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        parsed = datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _resolve_default_config_path() -> Path:
    env_path = os.environ.get("CONFIG_PATH")
    if env_path:
        return Path(env_path)
    # In Portainer/Docker we mount /data; locally we use ./data
    docker_data = Path("/data")
    if docker_data.exists() and os.access(docker_data, os.W_OK):
        return docker_data / "config.json"
    return Path("data") / "config.json"


class ConfigStore:
    """Thread-safe JSON file store for AppConfig."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path: Path = path or _resolve_default_config_path()
        self._lock = threading.RLock()
        self._config: AppConfig = self._load_or_init()

    def _load_or_init(self) -> AppConfig:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cfg = AppConfig()
        file_existed = self.path.exists()
        if file_existed:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                # Migrate legacy poll_interval_seconds if present
                if "poll_interval_Minutes" not in raw and "poll_interval_minutes" not in raw and "poll_interval_seconds" in raw:
                    secs = int(raw.get("poll_interval_seconds") or 300)
                    raw["poll_interval_minutes"] = max(1, round(secs / 60))
                cfg = AppConfig.model_validate(raw)
            except Exception as exc:
                logger.error("Failed to parse config file %s: %s", self.path, exc)

        # Allow environment variables PUTIO_TOKEN, PUTIO_APP_ID, and POLL_INTERVAL (in minutes)
        env_token = os.environ.get("PUTIO_TOKEN", "").strip()
        env_app_id = os.environ.get("PUTIO_APP_ID", "").strip()
        env_interval = os.environ.get("POLL_INTERVAL", "").strip()
        modified = False

        if env_token and not cfg.putio_token:
            cfg.putio_token = env_token
            modified = True
        if env_app_id.isdigit() and not cfg.putio_app_id:
            cfg.putio_app_id = int(env_app_id)
            modified = True
        if env_interval.isdigit() and not file_existed:
            cfg.poll_interval_minutes = max(1, min(1440, int(env_interval)))
            modified = True

        if modified or not file_existed:
            self._write(cfg)

        return cfg

    def _write(self, cfg: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(cfg.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)

    def get(self) -> AppConfig:
        with self._lock:
            return self._config.model_copy(deep=True)

    def save(self, cfg: AppConfig) -> AppConfig:
        with self._lock:
            self._config = cfg
            self._write(self._config)
            return self._config.model_copy(deep=True)

    def upsert_tracker(self, rule: TrackerRule) -> TrackerRule:
        with self._lock:
            if rule.tracker_id == "__default__":
                self._config.default_rule = rule
            else:
                self._config.trackers[rule.tracker_id] = rule
            self._write(self._config)
            return rule.model_copy(deep=True)

    def delete_tracker(self, tracker_id: str) -> bool:
        with self._lock:
            if tracker_id in self._config.trackers:
                del self._config.trackers[tracker_id]
                self._write(self._config)
                return True
            return False

    def append_log(self, entry: ActivityLogEntry, max_entries: int = 500) -> None:
        with self._lock:
            self._config.activity_log.insert(0, entry)
            if len(self._config.activity_log) > max_entries:
                self._config.activity_log = self._config.activity_log[:max_entries]
            self._write(self._config)

    def prune_logs(self, retention_days: int) -> int:
        """Delete log entries older than `retention_days` days. 0 (or less) keeps everything.

        Returns the number of removed entries. The config file is only rewritten when something was removed.
        """
        if retention_days is None or retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        with self._lock:
            kept = []
            for entry in self._config.activity_log:
                ts = _parse_iso_utc(entry.timestamp)
                # Entries with an unparsable timestamp are kept rather than silently dropped
                if ts is None or ts >= cutoff:
                    kept.append(entry)
            removed = len(self._config.activity_log) - len(kept)
            if removed:
                self._config.activity_log = kept
                self._write(self._config)
            return removed

    def clear_logs(self) -> None:
        with self._lock:
            self._config.activity_log = []
            self._write(self._config)
