"""Diagnostics for Salto KS (all raw API data, with personal data redacted)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_CLIENT_ID
from .coordinator import SaltoKSConfigEntry

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_CLIENT_ID,
    "client_secret",  # may exist in entries created by earlier versions
    "access_token",
    "refresh_token",
    "email",
    "first_name",
    "last_name",
    "phone",
    "phone_number",
    "image_url",
    "address",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SaltoKSConfigEntry
) -> dict[str, Any]:
    data = asdict(entry.runtime_data.data)
    data["stale_sites"] = sorted(data["stale_sites"])
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "data": async_redact_data(data, TO_REDACT),
    }
