"""Easy Office Mode switches (auto start and weekdays) for Salto KS locks."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import eom
from .coordinator import SaltoKSConfigEntry, SaltoKSCoordinator
from .entity import SaltoKSEomEntity, setup_per_lock_platform

PARALLEL_UPDATES = 0


def _build(coordinator: SaltoKSCoordinator, site_id: str, lock_id: str) -> list:
    return [
        SaltoKSEomSwitch(coordinator, site_id, lock_id, field)
        for field in (eom.AUTO_START, *eom.WEEKDAYS)
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SaltoKSConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry.async_on_unload(
        setup_per_lock_platform(entry.runtime_data, _build, async_add_entities)
    )


class SaltoKSEomSwitch(SaltoKSEomEntity, SwitchEntity):
    """Easy Office Mode auto start, or one weekday of the schedule."""

    @property
    def is_on(self) -> bool:
        return bool(self.eom_value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_apply(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_apply(False)
