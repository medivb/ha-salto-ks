"""Repairs for Salto KS: switch an older entry to the browser sign-in."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult


class SwitchToBrowserSignInFlow(ConfirmRepairFlow):
    """On confirmation, start the browser sign-in (re-authentication).

    After a successful sign-in, the stored username, password and client
    credentials are replaced by tokens and the issue disappears.
    """

    def __init__(self, entry_id: str) -> None:
        super().__init__()
        self._entry_id = entry_id

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            if entry := self.hass.config_entries.async_get_entry(self._entry_id):
                entry.async_start_reauth(self.hass)
            return self.async_create_entry(data={})
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    return SwitchToBrowserSignInFlow(str((data or {}).get("entry_id", "")))
