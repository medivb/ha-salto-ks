"""Config flow for Salto KS: browser sign-in (authorization code with PKCE)."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SaltoKSAccessTokenClient, SaltoKSError
from .const import (
    CONF_INCLUDE_USER_DETAILS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .oauth import async_register_oauth

_LOGGER = logging.getLogger(__name__)


class SaltoKSConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Sign in on the Salto KS login page; only tokens are stored.

    Re-authentication uses the same browser sign-in. For entries created
    with username, password and client credentials (earlier versions), it
    replaces those stored credentials with tokens.
    """

    DOMAIN = DOMAIN
    VERSION = 1
    MINOR_VERSION = 4

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        async_register_oauth(self.hass)
        return await super().async_step_user(user_input)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Identify the account with the new token, then create or update the entry."""
        client = SaltoKSAccessTokenClient(
            async_get_clientsession(self.hass), data["token"]["access_token"]
        )
        try:
            me = await client.async_get_me()
        except SaltoKSError as err:
            _LOGGER.warning("Signed in, but the account could not be read: %s", err)
            return self.async_abort(reason="cannot_connect")
        if not me.get("id"):
            return self.async_abort(reason="cannot_connect")

        # Same unique ID as entries created with a password, so re-authentication
        # can switch those over without creating a new entry.
        await self.async_set_unique_id(str(me["id"]).lower())
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            if entry.unique_id != self.unique_id:
                return self.async_abort(reason="wrong_account")
            # Replaces all stored data: any username, password and client
            # credentials from earlier versions are removed.
            return self.async_update_reload_and_abort(entry, data=data)

        self._abort_if_unique_id_configured()
        title = str(me.get("email") or me.get("username") or "account")
        return self.async_create_entry(title=f"Salto KS ({title})", data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> SaltoKSOptionsFlow:
        return SaltoKSOptionsFlow()


class SaltoKSOptionsFlow(OptionsFlow):
    """Options: polling interval and whether entries keep user details."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        include_user = self.config_entry.options.get(CONF_INCLUDE_USER_DETAILS, False)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                        vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                    ),
                    vol.Required(CONF_INCLUDE_USER_DETAILS, default=include_user): bool,
                }
            ),
        )
