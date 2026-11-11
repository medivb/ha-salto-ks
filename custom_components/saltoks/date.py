"""Easy Office Mode start and end date for Salto KS locks."""

from __future__ import annotations

from datetime import date

from homeassistant.components.date import DateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import eom
from .coordinator import SaltoKSConfigEntry, SaltoKSCoordinator
from .entity import SaltoKSEomEntity, setup_per_lock_platform

PARALLEL_UPDATES = 0


def _build(coordinator: SaltoKSCoordinator, site_id: str, lock_id: str) -> list:
    return [
        SaltoKSEomDate(coordinator, site_id, lock_id, field)
        for field in (eom.START_DATE, eom.END_DATE)
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SaltoKSConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry.async_on_unload(
        setup_per_lock_platform(entry.runtime_data, _build, async_add_entities)
    )


class SaltoKSEomDate(SaltoKSEomEntity, DateEntity):
    """First or last day of the Easy Office Mode schedule (the last day is included)."""

    @property
    def native_value(self) -> date | None:
        return self.eom_value

    async def async_set_value(self, value: date) -> None:
        await self._async_apply(value)
