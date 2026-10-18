from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from app.models import ActivityLogEntry, AppConfig, TrackerRule

logger = logging.getLogger("putio_monitor.storage")


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
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                cfg = AppConfig.model_validate(raw)
            except Exception as exc:
                logger.error("Failed to parse config file %s: %s", self.path, exc)

        # Allow environment variables PUTIO_TOKEN and POLL_INTERVAL to seed config
        env_token = os.environ.get("PUTIO_TOKEN", "").strip()
        env_interval = os.environ.get("POLL_INTERVAL", "").strip()
        modified = False
        if env_token and not cfg.putio_token:
            cfg.putio_token = env_token
            modified = True
        if env_interval.isdigit() and not self.path.exists():
            cfg.poll_interval_seconds = max(10, min(3600, int(env_interval)))
            modified = True

        if modified or not self.path.exists():
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

    def clear_logs(self) -> None:
        with self._lock:
            self._config.activity_log = []
            self._write(self._config)
