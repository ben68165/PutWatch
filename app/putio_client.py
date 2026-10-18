from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import httpx

logger = logging.getLogger("putio_monitor.putio_client")

PUTIO_API_BASE = "https://api.put.io/v2"
DEFAULT_OAUTH_APP_ID = 3270  # Official Swagger / OOB app ID from Put.io OpenAPI spec


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
    Examples:
      'udp://tracker.opentrackr.org:1337/announce' -> ('tracker.opentrackr.org', 'udp://tracker.opentrackr.org:1337/announce')
      'https://nyaa.tracker.wf:7777/announce' -> ('nyaa.tracker.wf', 'https://nyaa.tracker.wf:7777/announce')
      'tracker.example.com' -> ('tracker.example.com', 'tracker.example.com')
    """
    if not raw_url or not isinstance(raw_url, str):
        return None
    cleaned = unquote(raw_url.strip())
    if not cleaned:
        return None

    # Ignore non-tracker placeholders
    if cleaned.lower() in ("none", "null", "n/a", "-"):
        return None

    parse_target = cleaned if "://" in cleaned else f"http://{cleaned}"
    try:
        parsed = urlparse(parse_target)
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return None
        # Strip leading 'www.' if present for cleaner grouping
        if host.startswith("www.") and len(host) > 4:
            host = host[4:]
        return host, raw_url.strip()
    except Exception:
        return None


def extract_trackers_from_transfer(transfer: Dict[str, Any]) -> List[Tuple[str, str]]:
    """
    Inspect a Put.io transfer dict and extract all associated trackers as (tracker_id, raw_url) pairs.
    Checks:
      1. `transfer['tracker']` (primary tracker field returned by Put.io)
      2. `transfer['trackers']` (if array of tracker URLs/objects is present)
      3. `transfer['source']` (parses `tr=` parameters from magnet links or domain from torrent URLs)
    Returns a deduplicated list of (tracker_id, sample_url) in priority order.
    If no tracker is found, returns [('untracked', 'Direct / DHT (No Tracker)')].
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

        # Support comma/semicolon/newline separated tracker strings
        for part in raw.replace(";", ",").replace("\n", ",").split(","):
            norm = normalize_tracker_url(part)
            if norm:
                t_id, sample = norm
                if t_id not in seen_ids:
                    seen_ids.add(t_id)
                    discovered.append((t_id, sample))

    # 1. Explicit `tracker` field
    _add_candidate(transfer.get("tracker"))

    # 2. Explicit `trackers` field (list or string)
    _add_candidate(transfer.get("trackers"))

    # 3. `source` field (magnet:?xt=...&tr=... or http(s) torrent link)
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
            # If it's a torrent file downloaded from a tracker URL and no other tracker field was set
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
            "User-Agent": "PutWatch/1.0",
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

                # Respect 429 rate limiting headers from Put.io API
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
        """
        POST /transfers/cancel
        According to Put.io OpenAPI 2.8.11:
        'If transfer is in SEEDING state, stops seeding. Else, removes transfer entry. Does not remove their files.'
        """
        if not transfer_ids:
            return {}
        ids_csv = ",".join(str(tid) for tid in transfer_ids)
        return await self._request(
            "POST",
            "/transfers/cancel",
            data={"transfer_ids": ids_csv},
        )

    async def clean_transfers(self, transfer_ids: Optional[List[int]] = None) -> Dict[str, Any]:
        """
        POST /transfers/clean
        'Clean finished transfers. Only removes transfer entries, does not remove files.'
        """
        data: Optional[Dict[str, Any]] = None
        if transfer_ids:
            data = {"transfer_ids": ",".join(str(tid) for tid in transfer_ids)}
        return await self._request("POST", "/transfers/clean", data=data)

    async def remove_transfers(self, transfer_ids: List[int]) -> Dict[str, Any]:
        """
        POST /transfers/remove
        Removes specified transfer entries by ID.
        """
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
        Note on Put.io API behavior:
        - When a transfer is in SEEDING state, calling `/transfers/cancel` stops seeding (transitioning it to COMPLETED).
        - Calling `/transfers/clean` (or a second `/transfers/cancel` / `/transfers/remove`) removes the completed
          transfer entry from the transfer list.
        """
        status_upper = (current_status or "").upper()
        await self.cancel_transfers([transfer_id])

        if clean_after_cancel and status_upper in ("SEEDING", "COMPLETED", "COMPLETING"):
            await asyncio.sleep(0.6)
            # Try clean and cancel/remove to guarantee the entry disappears from the transfer list
            try:
                await self.clean_transfers([transfer_id])
            except PutioApiError:
                pass
            try:
                await self.cancel_transfers([transfer_id])
            except PutioApiError:
                pass

    @classmethod
    async def request_oob_code(
        cls, app_id: int = DEFAULT_OAUTH_APP_ID, client_name: str = "PutWatch"
    ) -> str:
        """GET /oauth2/oob/code -> returns 4-8 character code for https://put.io/link."""
        client = cls(token="")
        res = await client._request(
            "GET",
            "/oauth2/oob/code",
            params={"app_id": app_id, "client_name": client_name},
            require_auth=False,
        )
        code = res.get("code")
        if not code:
            raise PutioApiError("Did not receive an OOB link code from Put.io.")
        return str(code)

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
