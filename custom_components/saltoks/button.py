"""Button to remove the Easy Office Mode schedule of a Salto KS lock."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import eom
from .api import SaltoKSError
from .coordinator import SaltoKSConfigEntry, SaltoKSCoordinator
from .entity import SaltoKSEntity, setup_per_lock_platform

PARALLEL_UPDATES = 0


def _build(coordinator: SaltoKSCoordinator, site_id: str, lock_id: str) -> list:
    return [SaltoKSEomRemoveButton(coordinator, site_id, lock_id)]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SaltoKSConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry.async_on_unload(
        setup_per_lock_platform(entry.runtime_data, _build, async_add_entities)
    )


class SaltoKSEomRemoveButton(SaltoKSEntity, ButtonEntity):
    """Removes the schedule. Unavailable while the lock has no schedule."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "eom_remove"

    def __init__(self, coordinator: SaltoKSCoordinator, site_id: str, lock_id: str) -> None:
        super().__init__(coordinator, site_id, lock_id)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_lock_{site_id}_{lock_id}_eom_remove"
        )

    @property
    def available(self) -> bool:
        return super().available and eom.current_schedule(self.lock_data) is not None

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_remove_eom_schedule(
                self._site_id or "", self._lock_id or ""
            )
        except SaltoKSError as err:
            raise HomeAssistantError(
                f"Could not remove the Easy Office Mode schedule: {err}"
            ) from err
