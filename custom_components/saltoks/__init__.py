"""The Salto KS integration."""

from __future__ import annotations

import base64

from homeassistant.const import CONF_PASSWORD, CONF_TOKEN, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    config_entry_oauth2_flow,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SaltoKSClient, SaltoKSTokenClient
from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, DOMAIN
from .oauth import async_register_oauth
from .coordinator import SaltoKSConfigEntry, SaltoKSCoordinator
from .entity import (
    account_name,
    is_id_field,
    is_image_field,
    is_superseded_field,
    lock_name,
    site_name,
)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DATE,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]


async def async_setup_entry(hass: HomeAssistant, entry: SaltoKSConfigEntry) -> bool:
    """Set up Salto KS from a config entry."""
    session = async_get_clientsession(hass)
    client: SaltoKSClient
    if CONF_TOKEN in entry.data:
        # Browser sign-in: only tokens are stored.
        async_register_oauth(hass)
        implementation = (
            await config_entry_oauth2_flow.async_get_config_entry_implementation(hass, entry)
        )
        client = SaltoKSTokenClient(
            session, config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
        )
        ir.async_delete_issue(hass, DOMAIN, _legacy_issue_id(entry))
    else:
        # Entry from an earlier version: username, password and client
        # credentials. Keeps working; a repair suggests the browser sign-in.
        client = SaltoKSClient(
            session,
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            entry.data[CONF_CLIENT_ID],
            entry.data.get(CONF_CLIENT_SECRET, ""),
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            _legacy_issue_id(entry),
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="switch_to_browser_sign_in",
            translation_placeholders={"title": entry.title},
            data={"entry_id": entry.entry_id},
        )
    coordinator = SaltoKSCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Create the account device first so site devices can reference it.
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=account_name(coordinator.data.me),
        manufacturer="SALTO Systems",
        model="Salto KS account",
    )

    @callback
    def _sync_device_names() -> None:
        """Keep device names aligned with customer_reference / profile changes."""
        if not coordinator.data:
            return
        wanted = {entry.entry_id: account_name(coordinator.data.me)}
        for site_id, site in coordinator.data.sites.items():
            wanted[f"site_{site_id}"] = site_name(site, site_id)
            for lock_id, lock in coordinator.data.locks.get(site_id, {}).items():
                wanted[f"lock_{site_id}_{lock_id}"] = lock_name(lock, lock_id)
        for ident, name in wanted.items():
            device = dev_reg.async_get_device(identifiers={(DOMAIN, ident)})
            if device and device.name != name:
                dev_reg.async_update_device(device.id, name=name)

    _remove_superseded_entities(hass, entry)

    entry.async_on_unload(coordinator.async_add_listener(_sync_device_names))
    # Reload only when the options change. The entry data also changes when
    # tokens are refreshed, which must not reload the integration.
    options = dict(entry.options)

    async def _async_options_updated(hass: HomeAssistant, entry: SaltoKSConfigEntry) -> None:
        if dict(entry.options) != options:
            await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _remove_superseded_entities(hass: HomeAssistant, entry: SaltoKSConfigEntry) -> None:
    """Remove metadata entities created by earlier versions for fields that
    are now exposed differently: picture URLs (now image entities) and the
    Easy Office Mode schedule (now controls on the lock device)."""
    ent_reg = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if reg_entry.domain not in (Platform.SENSOR, Platform.BINARY_SENSOR):
            continue
        if "__" not in reg_entry.unique_id:
            continue
        owner, field = reg_entry.unique_id.rsplit("__", 1)
        path = tuple(field.split("."))
        superseded = (
            reg_entry.domain == Platform.SENSOR and is_image_field(path)
        ) or ("_lock_" in owner and is_superseded_field(path))
        if superseded:
            ent_reg.async_remove(reg_entry.entity_id)


async def async_migrate_entry(hass: HomeAssistant, entry: SaltoKSConfigEntry) -> bool:
    """Migrate older config entries."""
    if entry.version > 1:
        return False
    if entry.minor_version < 4:
        # 1.4: the client ID field used to hold the ready-made Basic value,
        # base64("client_id:client_secret"). Split it into both fields.
        if not entry.data.get(CONF_CLIENT_SECRET):
            split = split_basic_value(entry.data.get(CONF_CLIENT_ID, ""))
            if split:
                client_id, client_secret = split
                hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_CLIENT_ID: client_id,
                        CONF_CLIENT_SECRET: client_secret,
                    },
                )
    if entry.minor_version < 3:
        # 1.3: identifier entities are disabled instead of hidden. Applied once,
        # so a later choice by the user to enable one is respected.
        ent_reg = er.async_get(hass)
        for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            if "__" not in reg_entry.unique_id:
                continue
            path = tuple(reg_entry.unique_id.rsplit("__", 1)[1].split("."))
            if not is_id_field(path):
                continue
            changes: dict = {}
            if reg_entry.hidden_by is er.RegistryEntryHider.INTEGRATION:
                changes["hidden_by"] = None
            if reg_entry.disabled_by is None:
                changes["disabled_by"] = er.RegistryEntryDisabler.INTEGRATION
            if changes:
                ent_reg.async_update_entity(reg_entry.entity_id, **changes)
        hass.config_entries.async_update_entry(entry, minor_version=3)
    if entry.minor_version < 4:
        hass.config_entries.async_update_entry(entry, minor_version=4)
    return True


def split_basic_value(value: str) -> tuple[str, str] | None:
    """Return (client_id, client_secret) when value is base64("id:secret")."""
    value = value.strip()
    if value.lower().startswith("basic "):
        value = value[6:].strip()
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    client_id, sep, client_secret = decoded.partition(":")
    if not sep or not client_id or not decoded.isprintable():
        return None
    return client_id, client_secret


def _legacy_issue_id(entry: SaltoKSConfigEntry) -> str:
    return f"switch_to_browser_sign_in_{entry.entry_id}"


async def async_remove_entry(hass: HomeAssistant, entry: SaltoKSConfigEntry) -> None:
    """Remove the repair issue of an entry that is deleted."""
    ir.async_delete_issue(hass, DOMAIN, _legacy_issue_id(entry))


async def async_unload_entry(hass: HomeAssistant, entry: SaltoKSConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SaltoKSConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing site and lock devices that are no longer returned by the API."""
    data = entry.runtime_data.data
    for domain, ident in device.identifiers:
        if domain != DOMAIN:
            continue
        if ident == entry.entry_id:
            return False
        if ident.startswith("site_") and ident.removeprefix("site_") in data.sites:
            return False
        if ident.startswith("lock_"):
            site_id, _, lock_id = ident.removeprefix("lock_").partition("_")
            if lock_id in data.locks.get(site_id, {}):
                return False
    return True
