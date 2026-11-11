"""Polling coordinator for Salto KS."""

from __future__ import annotations

import asyncio
from collections import deque
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
from . import const, eom
from .const import (
    CONF_INCLUDE_USER_DETAILS,
    CONF_SCAN_INTERVAL,
    ENTRIES_PAGE_SIZE,
    ENTRY_USER_FIELDS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_CONCURRENT_REQUESTS,
)

_LOGGER = logging.getLogger(__name__)

MAX_SEEN_ENTRIES = 1000

type SaltoKSConfigEntry = ConfigEntry[SaltoKSCoordinator]


@dataclass
class SaltoKSData:
    """Snapshot of all data retrieved in one polling cycle (raw API payloads)."""

    me: dict[str, Any] = field(default_factory=dict)
    sites: dict[str, dict[str, Any]] = field(default_factory=dict)
    locks: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    # Sites whose lock list could not be refreshed in the last cycle.
    stale_sites: set[str] = field(default_factory=set)
    # Entries that appeared since the previous cycle, per (site_id, lock_id),
    # oldest first. Consumers use entries_cycle to process each batch once.
    new_entries: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    entries_cycle: int = 0


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
        self._eom_locks: dict[tuple[str, str], asyncio.Lock] = {}
        # Entry ids already processed per site (bounded), and the newest
        # entry per (site_id, lock_id).
        self._seen_entries: dict[str, deque[str]] = {}
        self._seen_entry_ids: dict[str, set[str]] = {}
        self.latest_entries: dict[tuple[str, str], dict[str, Any]] = {}
        self._entries_cycle = 0

    async def _fetch_locks(self, site_id: str) -> list[dict[str, Any]]:
        async with self._semaphore:
            return await self.client.async_get_locks(site_id)

    async def _fetch_entries(self, site_id: str) -> list[dict[str, Any]]:
        async with self._semaphore:
            return await self.client.async_get_recent_entries(site_id, ENTRIES_PAGE_SIZE)

    def _process_entries(
        self, data: SaltoKSData, site_id: str, entries: list[dict[str, Any]]
    ) -> None:
        """Record new entries. The first successful fetch of a site only sets
        the baseline (newest entry per lock) without reporting anything new."""
        include_user = self.config_entry.options.get(CONF_INCLUDE_USER_DETAILS, False)
        first_fetch = site_id not in self._seen_entry_ids
        seen = self._seen_entry_ids.setdefault(site_id, set())
        order = self._seen_entries.setdefault(site_id, deque())

        def _sort_key(entry: dict[str, Any]) -> str:
            return str(entry.get("utc_date_time") or "")

        for entry in sorted(entries, key=_sort_key):
            entry_id = str(entry.get("id") or "")
            lock_id = str(entry.get("lock_id") or "")
            if not entry_id or not lock_id or entry_id in seen:
                continue
            seen.add(entry_id)
            order.append(entry_id)
            if not include_user:
                entry = {k: v for k, v in entry.items() if k not in ENTRY_USER_FIELDS}
            self.latest_entries[(site_id, lock_id)] = entry
            if not first_fetch:
                data.new_entries.setdefault((site_id, lock_id), []).append(entry)

        while len(order) > MAX_SEEN_ENTRIES:
            seen.discard(order.popleft())

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

        # Entries (access events): one request per site. A failure here does
        # not fail the update; the entries are picked up at the next poll.
        entry_results = await asyncio.gather(
            *(self._fetch_entries(site_id) for site_id in sites), return_exceptions=True
        )
        for site_id, result in zip(sites, entry_results, strict=True):
            if isinstance(result, SaltoKSAuthError):
                raise ConfigEntryAuthFailed(str(result)) from result
            if isinstance(result, BaseException):
                _LOGGER.debug("Could not refresh entries for site %s: %s", site_id, result)
                continue
            self._process_entries(data, site_id, result)
        self._entries_cycle += 1
        data.entries_cycle = self._entries_cycle
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

    async def async_set_eom_schedule(
        self, site_id: str, lock_id: str, changes: dict[str, Any]
    ) -> None:
        """Apply changes to a lock's Easy Office Mode schedule.

        The PUT replaces the whole schedule, so the request is built from the
        current schedule plus the changes. Changes per lock are serialised so
        quick successive edits cannot overwrite each other.
        """
        guard = self._eom_locks.setdefault((site_id, lock_id), asyncio.Lock())
        async with guard:
            lock = (self.data.locks.get(site_id, {}).get(lock_id) if self.data else None) or {}
            payload = eom.build_payload(eom.current_schedule(lock), changes)
            try:
                await self.client.async_set_eom_schedule(site_id, lock_id, payload)
            except SaltoKSAuthError as err:
                self.config_entry.async_start_reauth(self.hass)
                raise SaltoKSApiError(str(err)) from err

            # Show the new schedule straight away, then read back what was stored.
            self._store_lock(site_id, lock_id, {**lock, eom.EOM_PROPERTY: payload})
            await self._async_read_back(site_id, lock_id)

    async def async_remove_eom_schedule(self, site_id: str, lock_id: str) -> None:
        """Remove a lock's Easy Office Mode schedule (DELETE)."""
        guard = self._eom_locks.setdefault((site_id, lock_id), asyncio.Lock())
        async with guard:
            lock = (self.data.locks.get(site_id, {}).get(lock_id) if self.data else None) or {}
            try:
                await self.client.async_delete_eom_schedule(site_id, lock_id)
            except SaltoKSAuthError as err:
                self.config_entry.async_start_reauth(self.hass)
                raise SaltoKSApiError(str(err)) from err
            # The API reports a lock without a schedule as an empty list.
            self._store_lock(site_id, lock_id, {**lock, eom.EOM_PROPERTY: []})
            await self._async_read_back(site_id, lock_id)

    async def _async_read_back(self, site_id: str, lock_id: str) -> None:
        try:
            fresh = await self.client.async_get_lock(site_id, lock_id)
        except SaltoKSApiError as err:
            _LOGGER.debug("Reading back lock %s failed: %s", lock_id, err)
            return
        if fresh:
            self._store_lock(site_id, lock_id, fresh)
