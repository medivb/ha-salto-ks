"""The Salto KS integration."""

from __future__ import annotations

from homeassistant.const import CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import (
    config_entry_oauth2_flow,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SaltoKSTokenClient
from .const import DOMAIN, MANUFACTURER
from .coordinator import SaltoKSConfigEntry, SaltoKSCoordinator
from .oauth import async_register_oauth
from .entity import (
    account_device_identifier,
    account_name,
    is_id_field,
    is_image_field,
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
    if CONF_TOKEN not in entry.data:
        # No sign-in token stored: sign in again in the browser.
        raise ConfigEntryAuthFailed("Sign in to Salto KS in the browser")
    async_register_oauth(hass)
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, entry
    )
    client = SaltoKSTokenClient(
        async_get_clientsession(hass),
        config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation),
    )
    coordinator = SaltoKSCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Create the account device first so site devices can reference it. The
    # device belongs to the signed-in account: after switching to another
    # account, a new device is created and the old one becomes unavailable.
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={account_device_identifier(coordinator)},
        name=account_name(coordinator.data.me),
        manufacturer=MANUFACTURER,
        model="Salto KS account",
    )

    @callback
    def _sync_device_names() -> None:
        """Keep device names aligned with customer_reference / profile changes."""
        if not coordinator.data:
            return
        wanted = {account_device_identifier(coordinator)[1]: account_name(coordinator.data.me)}
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
    """Remove metadata entities created by earlier versions for details that
    are now exposed differently: picture URLs (now image entities) and all
    lock details (now attributes of the lock entity)."""
    ent_reg = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if reg_entry.domain not in (Platform.SENSOR, Platform.BINARY_SENSOR, Platform.IMAGE):
            continue
        if "__" not in reg_entry.unique_id:
            continue
        owner, field = reg_entry.unique_id.rsplit("__", 1)
        path = tuple(field.split("."))
        superseded = (
            reg_entry.domain == Platform.SENSOR and is_image_field(path)
        ) or owner.startswith(f"{entry.entry_id}_lock_")
        if superseded:
            ent_reg.async_remove(reg_entry.entity_id)


async def async_migrate_entry(hass: HomeAssistant, entry: SaltoKSConfigEntry) -> bool:
    """Migrate older config entries."""
    if entry.version > 1:
        return False
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
    if entry.minor_version < 5:
        # 1.5: the account device and the account entities belong to the
        # account instead of the entry, so switching accounts creates a new
        # device. Existing ones keep their device, entity IDs and history.
        if entry.unique_id:
            _migrate_account_identifiers(hass, entry, entry.unique_id.lower())
        hass.config_entries.async_update_entry(entry, minor_version=5)
    return True


def _migrate_account_identifiers(
    hass: HomeAssistant, entry: SaltoKSConfigEntry, account: str
) -> None:
    dev_reg = dr.async_get(hass)
    if device := dev_reg.async_get_device(identifiers={(DOMAIN, entry.entry_id)}):
        dev_reg.async_update_device(
            device.id, new_identifiers={(DOMAIN, f"account_{account}")}
        )
    ent_reg = er.async_get(hass)
    old_prefix = f"{entry.entry_id}_me__"
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        new_unique_id = None
        if reg_entry.unique_id.startswith(old_prefix):
            field = reg_entry.unique_id.removeprefix(old_prefix)
            new_unique_id = f"{entry.entry_id}_account_{account}__{field}"
        elif reg_entry.unique_id == f"{entry.entry_id}_profile_image":
            new_unique_id = f"{entry.entry_id}_account_{account}_profile_image"
        if new_unique_id:
            ent_reg.async_update_entity(reg_entry.entity_id, new_unique_id=new_unique_id)


async def async_unload_entry(hass: HomeAssistant, entry: SaltoKSConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SaltoKSConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing devices that are no longer returned by the API: sites,
    locks, and the account device of a previously used account."""
    coordinator = entry.runtime_data
    data = coordinator.data
    current_account = account_device_identifier(coordinator)[1]
    for domain, ident in device.identifiers:
        if domain != DOMAIN:
            continue
        if ident == current_account:
            return False
        if ident.startswith("site_") and ident.removeprefix("site_") in data.sites:
            return False
        if ident.startswith("lock_"):
            site_id, _, lock_id = ident.removeprefix("lock_").partition("_")
            if lock_id in data.locks.get(site_id, {}):
                return False
    return True
