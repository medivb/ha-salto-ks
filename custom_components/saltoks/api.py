"""Async client for the Salto KS (Clay) cloud API."""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from .const import API_BASE_URL, FEED_URLS

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
NEXT_LINK_KEYS = ("next_page_link", "nextPageLink", "@odata.nextLink", "next")
MAX_PAGES = 50


class SaltoKSError(Exception):
    """Base error for the Salto KS client."""


class SaltoKSAuthError(SaltoKSError):
    """The sign-in was rejected or expired; the user must sign in again."""

    def __init__(self, message: str, error: str = "", description: str = "") -> None:
        super().__init__(message)
        self.error = error
        self.description = description


class SaltoKSApiError(SaltoKSError):
    """The API returned an error or could not be reached."""


class SaltoKSClient:
    """Salto KS API client.

    Subclasses provide the access token. A 401 from the API invalidates the
    access token and the request is retried once.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def async_get_access_token(self) -> str:
        raise NotImplementedError

    def invalidate_access_token(self) -> None:
        raise NotImplementedError

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


class SaltoKSTokenClient(SaltoKSClient):
    """API client that uses tokens from the browser sign-in.

    ``oauth_session`` is Home Assistant's OAuth2Session: it refreshes the
    access token with the refresh token and stores the new tokens in the
    config entry.
    """

    def __init__(self, session: aiohttp.ClientSession, oauth_session: Any) -> None:
        super().__init__(session)
        self._oauth = oauth_session

    async def async_get_access_token(self) -> str:
        try:
            await self._oauth.async_ensure_token_valid()
        except aiohttp.ClientResponseError as err:
            if err.status in (400, 401):
                raise SaltoKSAuthError(
                    "Sign-in expired or was revoked; sign in again", str(err.status)
                ) from err
            raise SaltoKSApiError(f"Token refresh failed: HTTP {err.status}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SaltoKSApiError(f"Token refresh failed: {err}") from err
        return str(self._oauth.token["access_token"])

    def invalidate_access_token(self) -> None:
        """Mark the stored access token as expired so the next request
        refreshes it (used after a 401 from the API)."""
        entry = self._oauth.config_entry
        token = {**entry.data["token"], "expires_at": 0}
        self._oauth.hass.config_entries.async_update_entry(
            entry, data={**entry.data, "token": token}
        )


class SaltoKSAccessTokenClient(SaltoKSClient):
    """API client for a single access token (used during sign-in)."""

    def __init__(self, session: aiohttp.ClientSession, access_token: str) -> None:
        super().__init__(session)
        self._access_token_value = access_token

    async def async_get_access_token(self) -> str:
        return self._access_token_value

    def invalidate_access_token(self) -> None:
        """Nothing to renew; a second 401 is reported as an error."""
