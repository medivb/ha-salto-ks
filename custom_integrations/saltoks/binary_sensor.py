"""Auto-discovered boolean metadata for Salto KS (account, sites and locks)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SaltoKSConfigEntry
from .entity import KIND_BINARY, SaltoKSMetadataEntity, setup_metadata_platform

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SaltoKSConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry.async_on_unload(
        setup_metadata_platform(
            entry.runtime_data, KIND_BINARY, SaltoKSMetadataBinarySensor, async_add_entities
        )
    )


class SaltoKSMetadataBinarySensor(SaltoKSMetadataEntity, BinarySensorEntity):
    """Exposes one boolean metadata field."""

    @property
    def is_on(self) -> bool | None:
        _, value = self.raw_value
        return None if value is None else bool(value)
