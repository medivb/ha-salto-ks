"""Lock platform for Salto KS."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import SaltoKSError
from .const import STATE_LOCKED, STATE_OFFICE_MODE
from .coordinator import SaltoKSConfigEntry, SaltoKSCoordinator
from .entity import SaltoKSEntity, lock_name

PARALLEL_UPDATES = 0


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


class SaltoKSLock(SaltoKSEntity, LockEntity):
    """A Salto KS lock, attached to its site device."""

    def __init__(self, coordinator: SaltoKSCoordinator, site_id: str, lock_id: str) -> None:
        super().__init__(coordinator, site_id, lock_id)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_lock_{site_id}_{lock_id}"

    @property
    def name(self) -> str:
        return lock_name(self.lock_data, self._lock_id or "")

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
