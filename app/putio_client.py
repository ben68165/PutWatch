from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.models import (
    PUTWATCH_APP_DESCRIPTION,
    PUTWATCH_APP_NAME,
    PUTWATCH_REPO_URL,
)

logger = logging.getLogger("putio_monitor.putio_client")

PUTIO_API_BASE = "https://api.put.io/v2"
FALLBACK_BOOTSTRAP_APP_ID = 1  # Put.io official web/CLI bootstrap client ID if no custom PUTIO_APP_ID is set yet


class PutioApiError(Exception):
    """Raised when Put.io API returns an error status or invalid payload."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        error_type: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


def normalize_tracker_url(raw_url: str) -> Optional[Tuple[str, str]]:
    """
    Extract a normalized tracker hostname (tracker_id) and cleaned raw URL from a tracker string.
    """
    if not raw_url or not isinstance(raw_url, str):
        return None
    cleaned = unquote(raw_url.strip())
    if not cleaned:
        return None

    if cleaned.lower() in ("none", "null", "n/a", "-"):
        return None

    parse_target = cleaned if "://" in cleaned else f"http://{cleaned}"
    try:
        parsed = urlparse(parse_target)
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return None
        if host.startswith("www.") and len(host) > 4:
            host = host[4:]
        return host, raw_url.strip()
    except Exception:
        return None


def extract_trackers_from_transfer(transfer: Dict[str, Any]) -> List[Tuple[str, str]]:
    """
    Inspect a Put.io transfer dict and extract all associated trackers as (tracker_id, raw_url) pairs.
    """
    discovered: List[Tuple[str, str]] = []
    seen_ids: set[str] = set()

    def _add_candidate(raw: Any) -> None:
        if not raw:
            return
        if isinstance(raw, dict):
            for key in ("url", "announce", "tracker", "host"):
                if key in raw and isinstance(raw[key], str):
                    _add_candidate(raw[key])
            return
        if isinstance(raw, (list, tuple)):
            for item in raw:
                _add_candidate(item)
            return
        if not isinstance(raw, str):
            return

        for part in raw.replace(";", ",").replace("\n", ",").split(","):
            norm = normalize_tracker_url(part)
            if norm:
                t_id, sample = norm
                if t_id not in seen_ids:
                    seen_ids.add(t_id)
                    discovered.append((t_id, sample))

    _add_candidate(transfer.get("tracker"))
    _add_candidate(transfer.get("trackers"))

    source = transfer.get("source")
    if isinstance(source, str) and source.strip():
        src = source.strip()
        if src.lower().startswith("magnet:?"):
            query_str = src[8:]
            qs = parse_qs(query_str)
            for key in ("tr", "TR", "ws"):
                for tr_url in qs.get(key, []):
                    _add_candidate(tr_url)
        elif src.lower().startswith(("http://", "https://")) and not discovered:
            norm = normalize_tracker_url(src)
            if norm:
                t_id, sample = norm
                if t_id not in seen_ids:
                    seen_ids.add(t_id)
                    discovered.append((t_id, sample))

    if not discovered:
        return [("untracked", "Direct / DHT (No Tracker)")]

    return discovered


class PutioClient:
    """Async client for Put.io API v2 (https://api.put.io/v2)."""

    def __init__(self, token: str, base_url: str = PUTIO_API_BASE) -> None:
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": f"{PUTWATCH_APP_NAME}/1.0 (+{PUTWATCH_REPO_URL})",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        require_auth: bool = True,
    ) -> Dict[str, Any]:
        if require_auth and not self.token:
            raise PutioApiError(
                "Put.io OAuth token is not configured.", status_code=401
            )

        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            for attempt in range(2):
                try:
                    resp = await client.request(
                        method=method,
                        url=url,
                        headers=self._headers(),
                        params=params,
                        data=data,
                    )
                except httpx.RequestError as exc:
                    raise PutioApiError(
                        f"Network error contacting Put.io API: {exc}"
                    ) from exc

                if resp.status_code == 429 and attempt == 0:
                    reset_hdr = resp.headers.get("X-RateLimit-Reset")
                    wait_secs = 3.0
                    if reset_hdr:
                        try:
                            wait_secs = min(max(float(reset_hdr), 1.0), 10.0)
                        except ValueError:
                            pass
                    logger.warning(
                        "Hit Put.io 429 rate limit on %s, retrying in %.1fs",
                        path,
                        wait_secs,
                    )
                    await asyncio.sleep(wait_secs)
                    continue

                if resp.status_code >= 400:
                    err_msg = f"HTTP {resp.status_code}"
                    err_type = None
                    try:
                        payload = resp.json()
                        err_msg = (
                            payload.get("error_message")
                            or payload.get("status")
                            or err_msg
                        )
                        err_type = payload.get("error_type")
                    except Exception:
                        err_msg = resp.text[:200] or err_msg
                    raise PutioApiError(
                        err_msg, status_code=resp.status_code, error_type=err_type
                    )

                if not resp.content:
                    return {}
                try:
                    return resp.json()
                except Exception as exc:
                    raise PutioApiError(
                        f"Invalid JSON response from Put.io on {path}: {exc}",
                        status_code=resp.status_code,
                    ) from exc

        return {}

    async def get_account_info(self) -> Dict[str, Any]:
        """GET /account/info -> returns account info dictionary."""
        res = await self._request("GET", "/account/info")
        return res.get("info", res)

    async def list_transfers(self) -> List[Dict[str, Any]]:
        """GET /transfers/list -> returns list of Transfer objects."""
        res = await self._request("GET", "/transfers/list")
        transfers = res.get("transfers", [])
        if not isinstance(transfers, list):
            return []
        return transfers

    async def get_transfer(self, transfer_id: int) -> Dict[str, Any]:
        """GET /transfers/{id} -> returns single Transfer object."""
        res = await self._request("GET", f"/transfers/{transfer_id}")
        return res.get("transfer", res)

    async def cancel_transfers(self, transfer_ids: List[int]) -> Dict[str, Any]:
        """POST /transfers/cancel."""
        if not transfer_ids:
            return {}
        ids_csv = ",".join(str(tid) for tid in transfer_ids)
        return await self._request(
            "POST",
            "/transfers/cancel",
            data={"transfer_ids": ids_csv},
        )

    async def clean_transfers(self, transfer_ids: Optional[List[int]] = None) -> Dict[str, Any]:
        """POST /transfers/clean."""
        data: Optional[Dict[str, Any]] = None
        if transfer_ids:
            data = {"transfer_ids": ",".join(str(tid) for tid in transfer_ids)}
        return await self._request("POST", "/transfers/clean", data=data)

    async def remove_transfers(self, transfer_ids: List[int]) -> Dict[str, Any]:
        """POST /transfers/remove."""
        if not transfer_ids:
            return {}
        ids_csv = ",".join(str(tid) for tid in transfer_ids)
        return await self._request(
            "POST",
            "/transfers/remove",
            data={"transfer_ids": ids_csv},
        )

    async def cancel_and_remove_transfer(
        self,
        transfer_id: int,
        current_status: str = "SEEDING",
        clean_after_cancel: bool = True,
    ) -> None:
        """
        Cancels a transfer and removes its entry from the Put.io transfer list without deleting storage files.
        """
        status_upper = (current_status or "").upper()
        await self.cancel_transfers([transfer_id])

        if clean_after_cancel and status_upper in ("SEEDING", "COMPLETED", "COMPLETING"):
            await asyncio.sleep(0.6)
            try:
                await self.clean_transfers([transfer_id])
            except PutioApiError:
                pass
            try:
                await self.cancel_transfers([transfer_id])
            except PutioApiError:
                pass

    async def ensure_putwatch_oauth_app(self) -> Optional[Dict[str, Any]]:
        """
        Ensures a dedicated 'PutWatch' OAuth Application exists in the user's Put.io account
        with name='PutWatch', website='https://github.com/ben68165/PutWatch', and description.
        Returns {'app_id': int, 'token': Optional[str], 'app': dict}.
        Also revokes any leftover 'swagger-test' (app_id=3270) grants if present.
        """
        app_payload = {
            "name": PUTWATCH_APP_NAME,
            "description": PUTWATCH_APP_DESCRIPTION,
            "website": PUTWATCH_REPO_URL,
            "callback": PUTWATCH_REPO_URL,
            "hidden": "true",
        }
        try:
            res = await self._request("GET", "/oauth/apps")
            apps = res.get("apps", [])
            target_app = None
            if isinstance(apps, list):
                for a in apps:
                    if isinstance(a, dict) and str(a.get("name", "")).strip().lower() == PUTWATCH_APP_NAME.lower():
                        target_app = a
                        break

            if target_app and target_app.get("id"):
                app_id = int(target_app["id"])
                # Ensure website & description match PutWatch and the user's GitHub repo
                try:
                    await self._request(
                        "POST",
                        f"/oauth/apps/{app_id}",
                        data={**app_payload, "id": str(app_id)},
                    )
                except PutioApiError:
                    pass
            else:
                created = await self._request(
                    "POST",
                    "/oauth/apps/register",
                    data=app_payload,
                )
                target_app = created.get("app") or created
                app_id = int(target_app["id"])

            # Fetch the dedicated token for the PutWatch OAuth app
            details = await self._request("GET", f"/oauth/apps/{app_id}")
            app_token = details.get("token")

            # Clean up any old swagger-test (app_id 3270) grant if we switched to the PutWatch app token
            if app_token:
                try:
                    grants_res = await self._request("GET", "/oauth/grants/")
                    for g in grants_res.get("apps", []):
                        if isinstance(g, dict) and (
                            g.get("id") == 3270
                            or str(g.get("name", "")).lower() == "swagger-test"
                        ):
                            await self._request("POST", f"/oauth/grants/{g['id']}/delete")
                except PutioApiError:
                    pass

            return {
                "app_id": app_id,
                "token": str(app_token) if app_token else None,
                "app": details.get("app", target_app),
            }
        except Exception as exc:
            logger.warning("Could not auto-register PutWatch OAuth app: %s", exc)
            return None

    @classmethod
    async def request_oob_code(
        cls,
        app_id: Optional[int] = None,
        client_name: str = PUTWATCH_APP_NAME,
    ) -> Dict[str, Any]:
        """
        GET /oauth2/oob/code -> returns {'code': str, 'qr_code_url': str, 'app_id_used': int}.
        Uses the user's PutWatch OAuth App ID if configured, otherwise FALLBACK_BOOTSTRAP_APP_ID (1),
        and immediately registers/switches to the dedicated 'PutWatch' OAuth App (referencing
        https://github.com/ben68165/PutWatch) as soon as the code is linked.
        """
        effective_app_id = int(app_id) if app_id else FALLBACK_BOOTSTRAP_APP_ID
        client = cls(token="")
        res = await client._request(
            "GET",
            "/oauth2/oob/code",
            params={
                "app_id": effective_app_id,
                "client_name": client_name,
            },
            require_auth=False,
        )
        code = res.get("code")
        if not code:
            raise PutioApiError("Did not receive an OOB link code from Put.io.")
        return {
            "code": str(code),
            "qr_code_url": res.get("qr_code_url"),
            "app_id_used": effective_app_id,
        }

    @classmethod
    async def check_oob_code(cls, code: str) -> Optional[str]:
        """GET /oauth2/oob/code/{code} -> returns oauth_token once linked by user."""
        client = cls(token="")
        res = await client._request(
            "GET",
            f"/oauth2/oob/code/{code.strip()}",
            require_auth=False,
        )
        token = res.get("oauth_token")
        return str(token) if token else None
