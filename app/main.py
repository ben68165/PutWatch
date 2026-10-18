from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.models import (
    PUTWATCH_APP_DESCRIPTION,
    PUTWATCH_APP_NAME,
    PUTWATCH_REPO_URL,
    ActivityLogEntry,
    SettingsUpdateRequest,
    TrackerRule,
    TrackerRuleUpdateRequest,
)
from app.monitor import TransferMonitorService, describe_rule
from app.putio_client import PutioApiError, PutioClient, normalize_tracker_url
from app.storage import ConfigStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("putio_monitor.main")

store = ConfigStore()
monitor_service = TransferMonitorService(store=store)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    monitor_service.start()
    yield
    await monitor_service.stop()


app = FastAPI(
    title=PUTWATCH_APP_NAME,
    version="1.0.0",
    description=PUTWATCH_APP_DESCRIPTION,
    lifespan=lifespan,
)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]}"


def _configured_timezone() -> str:
    return (os.environ.get("TZ") or "Europe/Berlin").strip()


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def healthcheck() -> Dict[str, Any]:
    cfg = store.get()
    return {
        "status": "ok",
        "app": PUTWATCH_APP_NAME,
        "repo": PUTWATCH_REPO_URL,
        "monitoring_enabled": cfg.monitoring_enabled,
        "token_configured": bool(cfg.putio_token),
        "last_poll_at": monitor_service.last_poll_at,
    }


@app.get("/api/state")
async def get_state() -> Dict[str, Any]:
    cfg = store.get()
    trackers_sorted = sorted(
        cfg.trackers.values(),
        key=lambda r: (
            not r.enabled,
            -r.matched_transfers_count,
            r.tracker_id.lower(),
        ),
    )

    return {
        "app_metadata": {
            "name": PUTWATCH_APP_NAME,
            "repo_url": PUTWATCH_REPO_URL,
            "description": PUTWATCH_APP_DESCRIPTION,
        },
        "settings": {
            "has_token": bool(cfg.putio_token),
            "masked_token": _mask_token(cfg.putio_token),
            "putio_app_id": cfg.putio_app_id,
            "monitoring_enabled": cfg.monitoring_enabled,
            "poll_interval_minutes": cfg.poll_interval_minutes,
            "dry_run": cfg.dry_run,
            "auto_discover_trackers": cfg.auto_discover_trackers,
            "clean_completed_after_cancel": cfg.clean_completed_after_cancel,
            "timezone": _configured_timezone(),
        },
        "monitor": {
            "is_polling": monitor_service.is_polling,
            "last_poll_at": monitor_service.last_poll_at,
            "last_error": monitor_service.last_error,
            "account_info": monitor_service.account_info,
        },
        "transfers": monitor_service.cached_transfers,
        "trackers": [
            {
                **t.model_dump(mode="json"),
                "rule_summary": describe_rule(t),
            }
            for t in trackers_sorted
        ],
        "default_rule": {
            **cfg.default_rule.model_dump(mode="json"),
            "rule_summary": describe_rule(cfg.default_rule),
        },
        "activity_log": [
            entry.model_dump(mode="json") for entry in cfg.activity_log[:100]
        ],
    }


@app.post("/api/poll")
async def trigger_poll(
    execute_rules: bool = Query(
        default=True,
        description="Whether to execute enabled cancellation rules during this poll.",
    ),
    dry_run: Optional[bool] = Query(
        default=None,
        description="Optional override for dry_run mode on this poll.",
    ),
) -> Dict[str, Any]:
    result = await monitor_service.poll_once(
        execute_cancellations=execute_rules,
        force_dry_run=dry_run,
    )
    state = await get_state()
    return {
        "poll_result": result,
        "state": state,
    }


@app.put("/api/settings")
async def update_settings(req: SettingsUpdateRequest) -> Dict[str, Any]:
    cfg = store.get()
    token_changed = False

    if req.putio_token is not None:
        cleaned_token = req.putio_token.strip()
        if "..." not in cleaned_token:
            if cleaned_token != cfg.putio_token:
                cfg.putio_token = cleaned_token
                token_changed = True

    if req.putio_app_id is not None:
        cfg.putio_app_id = req.putio_app_id if req.putio_app_id > 0 else None
    if req.monitoring_enabled is not None:
        cfg.monitoring_enabled = req.monitoring_enabled
    if req.poll_interval_minutes is not None:
        cfg.poll_interval_minutes = max(1, min(1440, req.poll_interval_minutes))
    if req.dry_run is not None:
        cfg.dry_run = req.dry_run
    if req.auto_discover_trackers is not None:
        cfg.auto_discover_trackers = req.auto_discover_trackers
    if req.clean_completed_after_cancel is not None:
        cfg.clean_completed_after_cancel = req.clean_completed_after_cancel

    store.save(cfg)

    if token_changed:
        monitor_service.account_info = None
        if cfg.putio_token:
            try:
                client = PutioClient(token=cfg.putio_token)
                monitor_service.account_info = await client.get_account_info()
                # Ensure dedicated PutWatch OAuth app is registered in user's Put.io account
                app_reg = await client.ensure_putwatch_oauth_app()
                if app_reg and app_reg.get("app_id"):
                    cfg.putio_app_id = int(app_reg["app_id"])
                    if app_reg.get("token"):
                        cfg.putio_token = str(app_reg["token"])
                    store.save(cfg)
                monitor_service.last_error = None
            except PutioApiError as exc:
                monitor_service.last_error = f"Invalid Put.io token: {exc}"
                raise HTTPException(
                    status_code=400, detail=f"Put.io token verification failed: {exc}"
                ) from exc
            await monitor_service.poll_once(execute_cancellations=cfg.monitoring_enabled)
        else:
            monitor_service.cached_transfers = []
            monitor_service.last_error = "Put.io API token not configured."
    else:
        monitor_service.trigger_immediate_poll()

    return await get_state()


@app.post("/api/auth/oob/start")
async def start_oob_auth(
    app_id: Optional[int] = Query(
        default=None,
        description="Optional Put.io OAuth App ID override.",
    )
) -> Dict[str, Any]:
    cfg = store.get()
    if app_id and app_id > 0 and app_id != cfg.putio_app_id:
        cfg.putio_app_id = app_id
        store.save(cfg)

    try:
        oob_info = await PutioClient.request_oob_code(
            app_id=cfg.putio_app_id,
            client_name=PUTWATCH_APP_NAME,
        )
        return {
            "code": oob_info["code"],
            "qr_code_url": oob_info.get("qr_code_url"),
            "app_id_used": oob_info.get("app_id_used"),
            "link_url": "https://app.put.io/link",
            "app_name": PUTWATCH_APP_NAME,
            "repo_url": PUTWATCH_REPO_URL,
        }
    except PutioApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/auth/oob/check/{code}")
async def check_oob_auth(code: str) -> Dict[str, Any]:
    try:
        token = await PutioClient.check_oob_code(code)
    except PutioApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not token:
        return {"linked": False}

    cfg = store.get()
    cfg.putio_token = token

    try:
        client = PutioClient(token=token)
        # Immediately register/update the dedicated 'PutWatch' OAuth App (referencing
        # https://github.com/ben68165/PutWatch) and adopt its PutWatch token & app_id
        app_reg = await client.ensure_putwatch_oauth_app()
        if app_reg and app_reg.get("app_id"):
            cfg.putio_app_id = int(app_reg["app_id"])
            if app_reg.get("token"):
                cfg.putio_token = str(app_reg["token"])
                client = PutioClient(token=cfg.putio_token)
        monitor_service.account_info = await client.get_account_info()
        monitor_service.last_error = None
    except PutioApiError:
        pass

    store.save(cfg)
    await monitor_service.poll_once(execute_cancellations=cfg.monitoring_enabled)
    return {
        "linked": True,
        "state": await get_state(),
    }


@app.post("/api/trackers")
async def save_tracker_rule(req: TrackerRuleUpdateRequest) -> Dict[str, Any]:
    raw_id = req.tracker_id.strip()
    if not raw_id:
        raise HTTPException(status_code=400, detail="Tracker ID / domain cannot be empty.")

    if raw_id == "__default__":
        normalized_id = "__default__"
        sample_url = None
    elif raw_id.lower() == "untracked":
        normalized_id = "untracked"
        sample_url = "Direct / DHT (No Tracker)"
    else:
        norm = normalize_tracker_url(raw_id)
        normalized_id = norm[0] if norm else raw_id.lower()
        sample_url = norm[1] if norm and "://" in norm[1] else None

    if req.enabled and not req.use_time_rule and not req.use_ratio_rule:
        raise HTTPException(
            status_code=400,
            detail="An enabled rule must have at least one condition active (Time Rule and/or Ratio Rule).",
        )

    cfg = store.get()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if normalized_id == "__default__":
        existing = cfg.default_rule
    else:
        existing = cfg.trackers.get(normalized_id)

    rule = TrackerRule(
        tracker_id=normalized_id,
        display_name=(
            req.display_name.strip()
            if req.display_name and req.display_name.strip()
            else (existing.display_name if existing and existing.display_name else normalized_id)
        ),
        enabled=req.enabled,
        use_time_rule=req.use_time_rule,
        max_time_minutes=req.max_time_minutes,
        time_metric=req.time_metric,
        use_ratio_rule=req.use_ratio_rule,
        max_ratio=req.max_ratio,
        condition_operator=req.condition_operator,
        include_downloading=req.include_downloading,
        discovered_from_transfers=(
            existing.discovered_from_transfers if existing else False
        ),
        sample_announce_url=(
            existing.sample_announce_url if existing and existing.sample_announce_url else sample_url
        ),
        first_seen_at=existing.first_seen_at if existing else now_iso,
        last_seen_at=existing.last_seen_at if existing else None,
        matched_transfers_count=(
            existing.matched_transfers_count if existing else 0
        ),
        canceled_transfers_count=(
            existing.canceled_transfers_count if existing else 0
        ),
    )

    store.upsert_tracker(rule)

    if cfg.putio_token:
        await monitor_service.poll_once(execute_cancellations=cfg.monitoring_enabled)

    return await get_state()


@app.delete("/api/trackers/{tracker_id:path}")
async def delete_tracker_rule(tracker_id: str) -> Dict[str, Any]:
    if tracker_id == "__default__":
        raise HTTPException(status_code=400, detail="Cannot delete the default fallback rule.")
    deleted = store.delete_tracker(tracker_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Tracker '{tracker_id}' not found.")
    return await get_state()


@app.post("/api/transfers/{transfer_id}/cancel")
async def manual_cancel_transfer(transfer_id: int) -> Dict[str, Any]:
    cfg = store.get()
    if not cfg.putio_token:
        raise HTTPException(status_code=400, detail="Put.io token is not configured.")

    cached = next(
        (t for t in monitor_service.cached_transfers if int(t.get("id") or 0) == transfer_id),
        None,
    )
    t_name = str(cached.get("name")) if cached else f"Transfer #{transfer_id}"
    t_status = str(cached.get("status") or "SEEDING") if cached else "SEEDING"
    t_tracker = str(cached.get("primary_tracker") or "manual") if cached else "manual"
    t_seeding = int(cached.get("seconds_seeding") or 0) if cached else 0
    t_ratio = float(cached.get("current_ratio") or 0.0) if cached else 0.0

    client = PutioClient(token=cfg.putio_token)
    try:
        await client.cancel_and_remove_transfer(
            transfer_id=transfer_id,
            current_status=t_status,
            clean_after_cancel=cfg.clean_completed_after_cancel,
        )
    except PutioApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    store.append_log(
        ActivityLogEntry(
            id=str(uuid.uuid4())[:8],
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            transfer_id=transfer_id,
            transfer_name=t_name,
            tracker_id=t_tracker,
            status_at_cancel=t_status,
            seeding_seconds=t_seeding,
            ratio_at_cancel=t_ratio,
            rule_summary="Manual Action",
            reason="Manually canceled and removed by user",
            dry_run=False,
            success=True,
        )
    )

    await monitor_service.poll_once(execute_cancellations=cfg.monitoring_enabled)
    return await get_state()


@app.delete("/api/logs")
async def clear_activity_logs() -> Dict[str, Any]:
    store.clear_logs()
    return await get_state()
