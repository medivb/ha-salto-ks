"""Lock platform for Salto KS."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import eom
from .api import SaltoKSError
from .const import STATE_LOCKED, STATE_OFFICE_MODE
from .coordinator import SaltoKSConfigEntry, SaltoKSCoordinator
from .entity import SaltoKSEntity

PARALLEL_UPDATES = 0

SERVICE_SET_EOM_SCHEDULE = "set_easy_office_mode_schedule"
SERVICE_REMOVE_EOM_SCHEDULE = "remove_easy_office_mode_schedule"
ATTR_AUTO_START = "auto_start"
ATTR_DAYS = "days"
SCHEDULE_FIELDS = (
    ATTR_AUTO_START,
    ATTR_DAYS,
    eom.START_TIME,
    eom.END_TIME,
    eom.START_DATE,
    eom.END_DATE,
)
SET_EOM_SCHEDULE_SCHEMA = vol.All(
    cv.make_entity_service_schema(
        {
            vol.Optional(ATTR_AUTO_START): cv.boolean,
            vol.Optional(ATTR_DAYS): vol.All(cv.ensure_list, [vol.In(eom.WEEKDAYS)]),
            vol.Optional(eom.START_TIME): cv.time,
            vol.Optional(eom.END_TIME): cv.time,
            vol.Optional(eom.START_DATE): cv.date,
            vol.Optional(eom.END_DATE): cv.date,
        }
    ),
    cv.has_at_least_one_key(*SCHEDULE_FIELDS),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SaltoKSConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _discover() -> None:
        new = []
        for site_id, locks in coordinator.data.locks.items():
            for lock_id in locks:
                if (site_id, lock_id) not in known:
                    known.add((site_id, lock_id))
                    new.append(SaltoKSLock(coordinator, site_id, lock_id))
        if new:
            async_add_entities(new)

    _discover()
    entry.async_on_unload(coordinator.async_add_listener(_discover))

    entity_platform.async_get_current_platform().async_register_entity_service(
        SERVICE_SET_EOM_SCHEDULE,
        SET_EOM_SCHEDULE_SCHEMA,
        "async_set_easy_office_mode_schedule",
    )
    entity_platform.async_get_current_platform().async_register_entity_service(
        SERVICE_REMOVE_EOM_SCHEDULE,
        None,
        "async_remove_easy_office_mode_schedule",
    )


class SaltoKSLock(SaltoKSEntity, LockEntity):
    """A Salto KS lock, attached to its site device."""

    def __init__(self, coordinator: SaltoKSCoordinator, site_id: str, lock_id: str) -> None:
        super().__init__(coordinator, site_id, lock_id)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_lock_{site_id}_{lock_id}"

    # The lock entity takes the name of its device.
    _attr_name = None

    @property
    def locked_state(self) -> str | None:
        return (self.lock_data or {}).get("locked_state")

    @property
    def is_locked(self) -> bool | None:
        state = self.locked_state
        if state is None:
            return None
        return state == STATE_LOCKED

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "site_id": self._site_id,
            "lock_id": self._lock_id,
            "locked_state": self.locked_state,
        }

    async def _set_state(self, state: str) -> None:
        if state == STATE_LOCKED:
            self._attr_is_locking = True
        else:
            self._attr_is_unlocking = True
        self.async_write_ha_state()
        try:
            await self.coordinator.async_set_locked_state(
                self._site_id or "", self._lock_id or "", state
            )
        except SaltoKSError as err:
            raise HomeAssistantError(f"Could not set lock to {state}: {err}") from err
        finally:
            self._attr_is_locking = False
            self._attr_is_unlocking = False
            self.async_write_ha_state()

    async def async_lock(self, **kwargs: Any) -> None:
        await self._set_state(STATE_LOCKED)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._set_state(STATE_OFFICE_MODE)

    async def async_set_easy_office_mode_schedule(self, **kwargs: Any) -> None:
        """Service action: change one or more fields of the schedule at once.

        Fields that are not given keep their current value. ``days`` lists the
        enabled weekdays; days not listed are disabled.
        """
        changes: dict[str, Any] = {}
        if ATTR_AUTO_START in kwargs:
            changes[eom.AUTO_START] = kwargs[ATTR_AUTO_START]
        if ATTR_DAYS in kwargs:
            enabled = set(kwargs[ATTR_DAYS])
            changes.update({day: day in enabled for day in eom.WEEKDAYS})
        for field in (eom.START_TIME, eom.END_TIME):
            if field in kwargs:
                changes[field] = kwargs[field].replace(second=0, microsecond=0)
        for field in (eom.START_DATE, eom.END_DATE):
            if field in kwargs:
                changes[field] = kwargs[field]
        try:
            await self.coordinator.async_set_eom_schedule(
                self._site_id or "", self._lock_id or "", changes
            )
        except SaltoKSError as err:
            raise HomeAssistantError(
                f"Could not update the Easy Office Mode schedule: {err}"
            ) from err

    async def async_remove_easy_office_mode_schedule(self) -> None:
        """Service action: remove the schedule (no-op when there is none)."""
        if eom.current_schedule(self.lock_data) is None:
            return
        try:
            await self.coordinator.async_remove_eom_schedule(
                self._site_id or "", self._lock_id or ""
            )
        except SaltoKSError as err:
            raise HomeAssistantError(
                f"Could not remove the Easy Office Mode schedule: {err}"
            ) from err
