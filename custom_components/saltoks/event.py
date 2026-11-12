"""Last entry and last incident per Salto KS lock, as event entities."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import FEED_ENTRIES, FEED_INCIDENTS
from .coordinator import SaltoKSConfigEntry, SaltoKSCoordinator
from .entity import SaltoKSEntity, setup_per_lock_platform

PARALLEL_UPDATES = 0

UNKNOWN_EVENT_TYPE = "Unknown"
# Event types are composed from the event details (and the user), so the
# list is bounded; the oldest types are dropped first.
MAX_EVENT_TYPES = 100
# Only the local time is kept; the UTC time is not shown.
HIDDEN_FIELDS = frozenset({"utc_date_time"})


def _build(coordinator: SaltoKSCoordinator, site_id: str, lock_id: str) -> list:
    return [
        SaltoKSLastEntryEvent(coordinator, site_id, lock_id),
        SaltoKSLastIncidentEvent(coordinator, site_id, lock_id),
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SaltoKSConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry.async_on_unload(
        setup_per_lock_platform(entry.runtime_data, _build, async_add_entities)
    )


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def event_type_of(event: dict[str, Any]) -> str:
    """Event type as "{event_category} - {event_detail} - {first} {last}".

    Empty parts are left out together with their separator. The user part
    is only present for entries, and only when user details are included.
    """
    user = " ".join(
        part for part in (_text(event.get("user_first_name")), _text(event.get("user_last_name")))
        if part
    )
    parts = [_text(event.get("event_category")), _text(event.get("event_detail")), user]
    return " - ".join(part for part in parts if part) or UNKNOWN_EVENT_TYPE


class _SaltoKSLastEventBase(SaltoKSEntity, EventEntity):
    """Fires for every new event of the lock in one feed; the state is the
    time it was received and the event fields are its attributes.

    Salto KS does not document the possible values, so event types are added
    as they appear.
    """

    _feed: str

    def __init__(self, coordinator: SaltoKSCoordinator, site_id: str, lock_id: str) -> None:
        super().__init__(coordinator, site_id, lock_id)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_lock_{site_id}_{lock_id}_{self._attr_translation_key}"
        )
        self._attr_event_types = [UNKNOWN_EVENT_TYPE]
        self._processed_cycle = coordinator.data.events_cycle if coordinator.data else 0

    def _add_event_type(self, event_type: str) -> None:
        if event_type in self._attr_event_types:
            return
        types = [*self._attr_event_types, event_type]
        if len(types) > MAX_EVENT_TYPES:
            types = types[-MAX_EVENT_TYPES:]
        self._attr_event_types = types

    def _fire(self, event: dict[str, Any]) -> None:
        event_type = event_type_of(event)
        self._add_event_type(event_type)
        attributes = {k: v for k, v in event.items() if k not in HIDDEN_FIELDS}
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        restored_type = self.state_attributes.get("event_type") if self.state else None
        if restored_type:
            self._add_event_type(restored_type)
        # Fresh install: show the most recent event instead of "unknown".
        if self.state is None:
            latest = self.coordinator.latest_events[self._feed].get(
                (self._site_id or "", self._lock_id or "")
            )
            if latest:
                self._fire(latest)

    @callback
    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data
        if data and data.events_cycle != self._processed_cycle:
            self._processed_cycle = data.events_cycle
            key = (self._site_id or "", self._lock_id or "")
            for event in data.new_events.get(self._feed, {}).get(key, []):
                self._fire(event)
        super()._handle_coordinator_update()


class SaltoKSLastEntryEvent(_SaltoKSLastEventBase):
    """Last entry (access event) of the lock."""

    _feed = FEED_ENTRIES
    _attr_translation_key = "last_entry"


class SaltoKSLastIncidentEvent(_SaltoKSLastEventBase):
    """Last incident of the lock."""

    _feed = FEED_INCIDENTS
    _attr_translation_key = "last_incident"
