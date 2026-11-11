"""Easy Office Mode start and end time for Salto KS locks."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import eom
from .coordinator import SaltoKSConfigEntry, SaltoKSCoordinator
from .entity import SaltoKSEomEntity, setup_per_lock_platform

PARALLEL_UPDATES = 0


def _build(coordinator: SaltoKSCoordinator, site_id: str, lock_id: str) -> list:
    return [
        SaltoKSEomTime(coordinator, site_id, lock_id, field)
        for field in (eom.START_TIME, eom.END_TIME)
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SaltoKSConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry.async_on_unload(
        setup_per_lock_platform(entry.runtime_data, _build, async_add_entities)
    )


class SaltoKSEomTime(SaltoKSEomEntity, TimeEntity):
    """Start or end time of the Easy Office Mode window (local time)."""

    @property
    def native_value(self) -> time | None:
        return self.eom_value

    async def async_set_value(self, value: time) -> None:
        await self._async_apply(value.replace(second=0, microsecond=0))
