"""Browser sign-in for Salto KS: OAuth 2.0 authorization code with PKCE.

The integration has its own public client at the Salto KS identity server
(no client secret). The user signs in on the normal identity server login
page; Home Assistant only stores the resulting tokens.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
import hashlib
import secrets
from typing import Any

from aiohttp import ClientError
from yarl import URL

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_entry_oauth2_flow

from .const import DOMAIN, TOKEN_URL

AUTHORIZE_URL = "https://identity.eu.my-clay.com/connect/authorize"
OAUTH_CLIENT_ID = "6566f5da-839a-44bd-8a69-f862da174414"
# offline_access: a refresh token, so the user stays signed in.
OAUTH_SCOPES = "openid profile user_api.full_access offline_access"
# Always ask for the username and password, also when the browser is still
# signed in to Salto KS (possibly with another account), so the user decides
# which account Home Assistant uses.
OAUTH_PROMPT = "login"
# The only redirect address registered for the client; it is the same for
# every installation and forwards the browser to the user's own instance.
REDIRECT_URI = "https://my.home-assistant.io/redirect/oauth"

# Pending sign-ins kept per implementation (one code verifier each).
MAX_PENDING_SIGN_INS = 20
# Token fields that are not needed after sign-in and are not stored. The ID
# token only holds profile details, which the integration reads from the API.
DROPPED_TOKEN_FIELDS = ("id_token",)

DATA_IMPLEMENTATION = f"{DOMAIN}_oauth_implementation"


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class SaltoKSOAuth2Implementation(config_entry_oauth2_flow.LocalOAuth2Implementation):
    """Public client with PKCE (S256).

    Every sign-in gets its own code verifier, stored per flow and used once.
    No client secret is sent: the client ID goes in the request body.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass, DOMAIN, OAUTH_CLIENT_ID, "", AUTHORIZE_URL, TOKEN_URL)
        self._verifiers: OrderedDict[str, str] = OrderedDict()

    @property
    def name(self) -> str:
        return "Salto KS"

    @property
    def redirect_uri(self) -> str:
        # Always the registered My Home Assistant address, also when the
        # "my" integration is not loaded.
        return REDIRECT_URI

    async def async_generate_authorize_url(self, flow_id: str) -> str:
        verifier = secrets.token_urlsafe(64)  # 86 characters (43-128 allowed)
        self._verifiers[flow_id] = verifier
        self._verifiers.move_to_end(flow_id)
        while len(self._verifiers) > MAX_PENDING_SIGN_INS:
            self._verifiers.popitem(last=False)
        url = await super().async_generate_authorize_url(flow_id)
        return str(
            URL(url).update_query(
                {
                    "scope": OAUTH_SCOPES,
                    "code_challenge": _code_challenge(verifier),
                    "code_challenge_method": "S256",
                    "prompt": OAUTH_PROMPT,
                }
            )
        )

    async def async_resolve_external_data(self, external_data: Any) -> dict:
        state = external_data["state"]
        verifier = self._verifiers.pop(state.get("flow_id", ""), None)
        if verifier is None:
            # Handled by the flow as a failed sign-in.
            raise ClientError("No pending sign-in for this flow")
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": external_data["code"],
                "redirect_uri": state["redirect_uri"],
                "code_verifier": verifier,
            }
        )

    async def _token_request(self, data: dict) -> dict:
        token = await super()._token_request(data)
        for field in DROPPED_TOKEN_FIELDS:
            token.pop(field, None)
        return token


@callback
def async_register_oauth(hass: HomeAssistant) -> SaltoKSOAuth2Implementation:
    """Register the implementation once, so pending sign-ins keep their
    code verifier."""
    if (impl := hass.data.get(DATA_IMPLEMENTATION)) is None:
        impl = hass.data[DATA_IMPLEMENTATION] = SaltoKSOAuth2Implementation(hass)
        config_entry_oauth2_flow.async_register_implementation(hass, DOMAIN, impl)
    return impl
