"""Async client for the Salto KS (Clay) cloud API."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from .const import API_BASE_URL, FEED_URLS, TOKEN_EXPIRY_MARGIN, TOKEN_URL

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
AUTH_ERRORS = {"invalid_grant", "invalid_client", "unauthorized_client"}
NEXT_LINK_KEYS = ("next_page_link", "nextPageLink", "@odata.nextLink", "next")
MAX_PAGES = 50


class SaltoKSError(Exception):
    """Base error for the Salto KS client."""


class SaltoKSAuthError(SaltoKSError):
    """Credentials were rejected; user interaction is required."""

    def __init__(self, message: str, error: str = "", description: str = "") -> None:
        super().__init__(message)
        self.error = error
        self.description = description


class SaltoKSApiError(SaltoKSError):
    """The API returned an error or could not be reached."""


class SaltoKSClient:
    """Salto KS API client with automatic token keep-alive.

    Token strategy:
      1. Re-use the access token while it is valid (minus a safety margin).
      2. Otherwise use the refresh token (grant_type=refresh_token).
      3. If refreshing fails, authenticate again with the stored credentials
         (grant_type=password).
      4. If that fails with an OAuth credential error, raise SaltoKSAuthError.
    A 401 from the API invalidates the access token and retries once.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        client_id: str,
    ) -> None:
        self._session = session
        self._username = username.strip()
        self._password = password
        self._client_id = client_id.strip()
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    # ------------------------------------------------------------------ auth

    @property
    def _basic_auth(self) -> str:
        """Authorization header for the token endpoint: ``Basic {client_id}``,
        with the client ID sent exactly as entered."""
        client_id = self._client_id
        if client_id.lower().startswith("basic "):
            client_id = client_id[6:].strip()
        return f"Basic {client_id}"

    def invalidate_access_token(self) -> None:
        """Force the next request to obtain a new access token."""
        self._access_token = None
        self._expires_at = 0.0

    async def _token_request(self, form: dict[str, str]) -> None:
        headers = {
            "Authorization": self._basic_auth,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        try:
            async with self._session.post(
                TOKEN_URL, data=form, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                text = await resp.text()
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SaltoKSApiError(f"Token endpoint unreachable: {err}") from err

        try:
            payload: dict[str, Any] = json.loads(text) if text else {}
        except ValueError:
            payload = {}

        if status >= 400:
            error = str(payload.get("error", ""))
            description = str(payload.get("error_description", ""))
            _LOGGER.debug(
                "Token endpoint (%s) returned HTTP %s: error=%s description=%s",
                form.get("grant_type"), status, error or "-", description or text[:200],
            )
            if status in (400, 401) and (error in AUTH_ERRORS or status == 401):
                raise SaltoKSAuthError(
                    f"Authentication rejected ({error or status}"
                    f"{': ' + description if description else ''})",
                    error or str(status),
                    description,
                )
            raise SaltoKSApiError(f"Token endpoint returned HTTP {status}: {error or text[:200]}")

        token = payload.get("access_token")
        if not token:
            raise SaltoKSApiError("Token endpoint returned no access_token")

        self._access_token = token
        # Some servers do not rotate refresh tokens; keep the previous one then.
        self._refresh_token = payload.get("refresh_token") or self._refresh_token
        expires_in = float(payload.get("expires_in") or 3600)
        self._expires_at = time.monotonic() + expires_in

    async def _password_grant(self) -> None:
        _LOGGER.debug("Authenticating with password grant")
        self._refresh_token = None
        form = {
            "grant_type": "password",
            "username": self._username,
            "password": self._password,
        }
        await self._token_request(form)

    async def _refresh_grant(self) -> None:
        _LOGGER.debug("Refreshing access token")
        await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": self._refresh_token or ""}
        )

    async def async_get_access_token(self) -> str:
        """Return a valid access token, refreshing or re-authenticating as needed."""
        async with self._token_lock:
            if self._access_token and time.monotonic() < (
                self._expires_at - TOKEN_EXPIRY_MARGIN
            ):
                return self._access_token

            if self._refresh_token:
                try:
                    await self._refresh_grant()
                    return self._access_token  # type: ignore[return-value]
                except SaltoKSAuthError:
                    _LOGGER.debug("Refresh token rejected; falling back to password grant")
                    self._refresh_token = None

            await self._password_grant()
            return self._access_token  # type: ignore[return-value]

    async def async_authenticate(self) -> None:
        """Perform a fresh password grant (used by the config flow)."""
        async with self._token_lock:
            await self._password_grant()

    # ------------------------------------------------------------------- api

    async def _request(
        self,
        method: str,
        path_or_url: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        url = path_or_url if path_or_url.startswith("http") else f"{API_BASE_URL}{path_or_url}"
        for attempt in range(2):
            token = await self.async_get_access_token()
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                ) as resp:
                    status = resp.status
                    text = await resp.text()
            except (aiohttp.ClientError, TimeoutError) as err:
                raise SaltoKSApiError(f"{method} {url} failed: {err}") from err

            if status == 401 and attempt == 0:
                _LOGGER.debug("Received 401; renewing token and retrying")
                self.invalidate_access_token()
                continue
            if status >= 400:
                raise SaltoKSApiError(f"{method} {url} returned HTTP {status}")
            if not text:
                return None
            try:
                return json.loads(text)
            except ValueError as err:
                raise SaltoKSApiError(f"{method} {url} returned invalid JSON") from err
        raise SaltoKSApiError(f"{method} {url} unauthorized after token renewal")

    async def _get_collection(self, path: str) -> list[dict[str, Any]]:
        """GET a collection. Supports plain lists and {"items": [...]} envelopes
        with optional next-page links."""
        items: list[dict[str, Any]] = []
        next_url: str | None = path
        for _ in range(MAX_PAGES):
            if not next_url:
                break
            payload = await self._request("GET", next_url)
            next_url = None
            if isinstance(payload, list):
                items.extend(x for x in payload if isinstance(x, dict))
            elif isinstance(payload, dict):
                page = payload.get("items", payload.get("value", payload.get("data", [])))
                if isinstance(page, list):
                    items.extend(x for x in page if isinstance(x, dict))
                for key in NEXT_LINK_KEYS:
                    if isinstance(payload.get(key), str) and payload[key]:
                        next_url = payload[key]
                        break
        return items

    async def async_get_me(self) -> dict[str, Any]:
        data = await self._request("GET", "/me")
        return data if isinstance(data, dict) else {}

    async def async_get_sites(self) -> list[dict[str, Any]]:
        return await self._get_collection("/sites")

    async def async_get_locks(self, site_id: str) -> list[dict[str, Any]]:
        return await self._get_collection(f"/sites/{site_id}/locks")

    async def async_get_lock(self, site_id: str, lock_id: str) -> dict[str, Any]:
        data = await self._request("GET", f"/sites/{site_id}/locks/{lock_id}")
        if isinstance(data, dict) and "locked_state" not in data:
            for key in ("item", "data", "value"):
                if isinstance(data.get(key), dict):
                    return data[key]
        return data if isinstance(data, dict) else {}

    async def async_set_eom_schedule(
        self, site_id: str, lock_id: str, schedule: dict[str, Any]
    ) -> Any:
        return await self._request(
            "PUT",
            f"/sites/{site_id}/locks/{lock_id}/easy_office_mode_schedule",
            json_body=schedule,
        )

    async def async_get_recent_events(
        self, feed: str, site_id: str, top: int
    ) -> list[dict[str, Any]]:
        """Newest entries or incidents of a site, newest first (first page only)."""
        data = await self._request(
            "GET",
            FEED_URLS[feed].format(site_id=site_id),
            params={"$orderby": "utc_date_time desc", "$top": str(top)},
        )
        items = data.get("items") if isinstance(data, dict) else data
        return [x for x in items or [] if isinstance(x, dict)]

    async def async_delete_eom_schedule(self, site_id: str, lock_id: str) -> Any:
        return await self._request(
            "DELETE", f"/sites/{site_id}/locks/{lock_id}/easy_office_mode_schedule"
        )

    async def async_set_locked_state(self, site_id: str, lock_id: str, state: str) -> Any:
        return await self._request(
            "PATCH",
            f"/sites/{site_id}/locks/{lock_id}/locking",
            json_body={"locked_state": state},
        )
