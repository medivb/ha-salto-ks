"""Auto-discovered metadata sensors for Salto KS (account, sites and locks)."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import SaltoKSConfigEntry, SaltoKSCoordinator
from .entity import KIND_SENSOR, MetadataKey, SaltoKSMetadataEntity, setup_metadata_platform

PARALLEL_UPDATES = 0
MAX_STATE_LENGTH = 255
TIMESTAMP_HINTS = ("_at", "date", "time", "_utc", "expires", "updated", "created")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SaltoKSConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry.async_on_unload(
        setup_metadata_platform(
            entry.runtime_data, KIND_SENSOR, SaltoKSMetadataSensor, async_add_entities
        )
    )


def _as_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)


class SaltoKSMetadataSensor(SaltoKSMetadataEntity, SensorEntity):
    """Exposes one metadata field. Lists show their length (items in attributes),
    long strings and objects are truncated in the state and kept in full in the
    'value' attribute."""

    def __init__(self, coordinator: SaltoKSCoordinator, key: MetadataKey) -> None:
        super().__init__(coordinator, key)
        _, value = self.raw_value
        leaf = key.path[-1].lower()
        if any(h in leaf for h in TIMESTAMP_HINTS) and _as_timestamp(value):
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> Any:
        _, value = self.raw_value
        if value is None:
            return None
        if self.device_class == SensorDeviceClass.TIMESTAMP:
            return _as_timestamp(value)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            value = json.dumps(value, default=str)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        text = str(value)
        return text if len(text) <= MAX_STATE_LENGTH else text[: MAX_STATE_LENGTH - 1] + "…"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        _, value = self.raw_value
        if isinstance(value, list):
            return {"items": value}
        if isinstance(value, dict) or (isinstance(value, str) and len(value) > MAX_STATE_LENGTH):
            return {"value": value}
        return None
