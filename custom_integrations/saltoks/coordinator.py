"""Polling coordinator for Salto KS."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SaltoKSApiError, SaltoKSAuthError, SaltoKSClient
from . import const
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_CONCURRENT_REQUESTS,
)

_LOGGER = logging.getLogger(__name__)

type SaltoKSConfigEntry = ConfigEntry[SaltoKSCoordinator]


@dataclass
class SaltoKSData:
    """Snapshot of all data retrieved in one polling cycle (raw API payloads)."""

    me: dict[str, Any] = field(default_factory=dict)
    sites: dict[str, dict[str, Any]] = field(default_factory=dict)
    locks: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    # Sites whose lock list could not be refreshed in the last cycle.
    stale_sites: set[str] = field(default_factory=set)


class SaltoKSCoordinator(DataUpdateCoordinator[SaltoKSData]):
    """Polls /me, /sites and /sites/{id}/locks."""

    config_entry: SaltoKSConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: SaltoKSConfigEntry, client: SaltoKSClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client
        # Metadata keys already claimed by an entity (shared across platforms).
        self.claimed_metadata: set[str] = set()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def _fetch_locks(self, site_id: str) -> list[dict[str, Any]]:
        async with self._semaphore:
            return await self.client.async_get_locks(site_id)

    async def _async_update_data(self) -> SaltoKSData:
        previous = self.data or SaltoKSData()
        try:
            me, site_list = await asyncio.gather(
                self.client.async_get_me(), self.client.async_get_sites()
            )
        except SaltoKSAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SaltoKSApiError as err:
            raise UpdateFailed(str(err)) from err

        sites = {str(s["id"]): s for s in site_list if s.get("id") is not None}
        results = await asyncio.gather(
            *(self._fetch_locks(site_id) for site_id in sites), return_exceptions=True
        )

        data = SaltoKSData(me=me, sites=sites)
        for site_id, result in zip(sites, results, strict=True):
            if isinstance(result, SaltoKSAuthError):
                raise ConfigEntryAuthFailed(str(result)) from result
            if isinstance(result, BaseException):
                _LOGGER.debug("Could not refresh locks for site %s: %s", site_id, result)
                data.locks[site_id] = previous.locks.get(site_id, {})
                data.stale_sites.add(site_id)
                continue
            data.locks[site_id] = {
                str(lock["id"]): lock for lock in result if lock.get("id") is not None
            }
        return data

    @callback
    def _store_lock(self, site_id: str, lock_id: str, lock: dict[str, Any]) -> None:
        """Store fresh data for one lock and notify entities."""
        if not self.data or site_id not in self.data.locks:
            return
        self.data.locks[site_id][lock_id] = lock
        self.async_update_listeners()

    async def async_set_locked_state(self, site_id: str, lock_id: str, state: str) -> bool:
        """Request a lock state change and wait until the lock reports it.

        After the PATCH, GET /sites/{site_id}/locks/{lock_id} is polled every
        LOCK_CONFIRM_INTERVAL seconds until locked_state equals the requested
        state, for at most LOCK_CONFIRM_TIMEOUT seconds. Every response is
        stored, so the entity always shows the state reported by the API.
        Returns True when the new state was confirmed in time.
        """
        try:
            response = await self.client.async_set_locked_state(site_id, lock_id, state)
        except SaltoKSAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise SaltoKSApiError(str(err)) from err

        if isinstance(response, dict) and response.get("locked_state") == state:
            current = (self.data.locks.get(site_id, {}).get(lock_id) if self.data else None) or {}
            self._store_lock(site_id, lock_id, {**current, **response})
            return True

        deadline = time.monotonic() + const.LOCK_CONFIRM_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(const.LOCK_CONFIRM_INTERVAL)
            try:
                lock = await self.client.async_get_lock(site_id, lock_id)
            except SaltoKSAuthError as err:
                self.config_entry.async_start_reauth(self.hass)
                raise SaltoKSApiError(str(err)) from err
            except SaltoKSApiError as err:
                _LOGGER.debug("Polling lock %s failed: %s", lock_id, err)
                continue
            if lock:
                self._store_lock(site_id, lock_id, lock)
            if lock.get("locked_state") == state:
                _LOGGER.debug("Lock %s confirmed state %s", lock_id, state)
                return True

        _LOGGER.warning(
            "Lock %s did not report state '%s' within %s seconds",
            lock_id, state, const.LOCK_CONFIRM_TIMEOUT,
        )
        return False
