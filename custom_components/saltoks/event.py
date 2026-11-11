"""Last entry (access event) per Salto KS lock, as an event entity."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .coordinator import SaltoKSConfigEntry, SaltoKSCoordinator
from .entity import SaltoKSEntity, setup_per_lock_platform

PARALLEL_UPDATES = 0

UNKNOWN_EVENT_TYPE = "unknown"


def _build(coordinator: SaltoKSCoordinator, site_id: str, lock_id: str) -> list:
    return [SaltoKSLastEntryEvent(coordinator, site_id, lock_id)]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SaltoKSConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry.async_on_unload(
        setup_per_lock_platform(entry.runtime_data, _build, async_add_entities)
    )


def event_type_of(entry: dict[str, Any]) -> str:
    """The event type is the entry's event_category, as a slug."""
    return slugify(str(entry.get("event_category") or "")) or UNKNOWN_EVENT_TYPE


class SaltoKSLastEntryEvent(SaltoKSEntity, EventEntity):
    """Fires for every new entry of the lock; the state is the time of the
    most recent one and the entry fields are its attributes.

    Event types are the event categories returned by Salto KS. They are not
    documented as a fixed list, so new categories are added as they appear.
    """

    _attr_translation_key = "last_entry"

    def __init__(self, coordinator: SaltoKSCoordinator, site_id: str, lock_id: str) -> None:
        super().__init__(coordinator, site_id, lock_id)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_lock_{site_id}_{lock_id}_last_entry"
        )
        self._attr_event_types = [UNKNOWN_EVENT_TYPE]
        self._processed_cycle = coordinator.data.entries_cycle if coordinator.data else 0

    def _fire(self, entry: dict[str, Any]) -> None:
        event_type = event_type_of(entry)
        if event_type not in self._attr_event_types:
            self._attr_event_types = sorted({*self._attr_event_types, event_type})
        self._trigger_event(event_type, dict(entry))
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        restored_type = self.state_attributes.get("event_type") if self.state else None
        if restored_type and restored_type not in self._attr_event_types:
            self._attr_event_types = sorted({*self._attr_event_types, restored_type})
        # Fresh install: show the most recent entry instead of "unknown".
        if self.state is None:
            latest = self.coordinator.latest_entries.get((self._site_id, self._lock_id))
            if latest:
                self._fire(latest)

    @callback
    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data
        if data and data.entries_cycle != self._processed_cycle:
            self._processed_cycle = data.entries_cycle
            for entry in data.new_entries.get((self._site_id or "", self._lock_id or ""), []):
                self._fire(entry)
        super()._handle_coordinator_update()
