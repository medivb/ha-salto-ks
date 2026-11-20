"""End-to-end tests with mocked Salto KS / Clay endpoints."""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.saltoks.const import API_BASE_URL, DOMAIN, TOKEN_URL

# Entry created with the browser sign-in (tokens only).
ENTRY_DATA = {
    "auth_implementation": "saltoks",
    "token": {
        "access_token": "tok1",
        "refresh_token": "r1",
        "token_type": "Bearer",
        "expires_in": 3600,
        "expires_at": 4102444800,  # 2100-01-01: no refresh during tests
    },
}

ME = {"id": "u1", "first_name": "Test", "last_name": "User", "email": "t@example.com",
      "image_url": "https://img.example.com/u1.png", "language": "en"}
SITES = {"items": [{"id": "s1", "customer_reference": "HQ Amsterdam", "name": "hq",
                    "timezone": "Europe/Amsterdam", "is_active": True,
                    "owner": {"first_name": "Owner", "image_url": "https://img.example.com/o.png"}}]}
LOCKS = [{"id": "l1", "customer_reference": "Front door", "locked_state": "locked",
          "battery_level": "normal", "online": True,
          "last_seen_at": "2026-11-09T10:00:00Z", "tags": ["a", "b"]}]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def fast_lock_confirmation(monkeypatch):
    """Poll without real delays; the timeout still bounds the loop."""
    from custom_components.saltoks import const

    monkeypatch.setattr(const, "LOCK_CONFIRM_INTERVAL", 0)
    monkeypatch.setattr(const, "LOCK_CONFIRM_TIMEOUT", 0.2)


ENTRIES_URL = "https://connect.saltoks.com/v1.1/sites/s1/entries"
INCIDENTS_URL = "https://connect.saltoks.com/v1.1/sites/s1/incidents"


def _api(aioclient_mock, locks=None, me=None, entries=None, incidents=None):
    aioclient_mock.get(f"{API_BASE_URL}/me", json=me or ME)
    aioclient_mock.get(f"{API_BASE_URL}/sites", json=SITES)
    aioclient_mock.get(f"{API_BASE_URL}/sites/s1/locks", json=locks or LOCKS)
    aioclient_mock.get(ENTRIES_URL, json={"items": entries or [], "continuation_token": None})
    aioclient_mock.get(INCIDENTS_URL, json={"items": incidents or [], "continuation_token": None})


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_devices_and_entities(hass: HomeAssistant, aioclient_mock) -> None:
    _api(aioclient_mock)
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    # Browser sign-in entry: the stored access token is used; no password grant.
    assert not any(str(c[1]) == TOKEN_URL for c in aioclient_mock.mock_calls)
    me_call = next(c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/me"))
    assert me_call[3]["Authorization"] == "Bearer tok1"

    devreg = dr.async_get(hass)
    account = devreg.async_get_device(identifiers={(DOMAIN, "account_u1")})
    site = devreg.async_get_device(identifiers={(DOMAIN, "site_s1")})
    assert account.name == "Test User"
    assert site.name == "HQ Amsterdam"
    assert site.via_device_id == account.id

    entreg = er.async_get(hass)
    lock_id = entreg.async_get_entity_id("lock", DOMAIN, f"{entry.entry_id}_lock_s1_l1")
    assert hass.states.get(lock_id).state == "locked"
    lock_dev = devreg.async_get_device(identifiers={(DOMAIN, "lock_s1_l1")})
    assert lock_dev.name == "Front door"
    assert lock_dev.via_device_id == site.id
    assert entreg.async_get(lock_id).device_id == lock_dev.id
    assert hass.states.get(lock_id).attributes["friendly_name"] == "Front door"

    image_id = entreg.async_get_entity_id("image", DOMAIN, f"{entry.entry_id}_account_u1_profile_image")
    assert entreg.async_get(image_id).device_id == account.id
    assert hass.states.get(image_id).state != "unavailable"

    # Metadata entities.
    def uid(x):
        return f"{entry.entry_id}_{x}"
    lang = entreg.async_get_entity_id("sensor", DOMAIN, uid("account_u1__language"))
    assert hass.states.get(lang).state == "en"
    tz = entreg.async_get_entity_id("sensor", DOMAIN, uid("site_s1__timezone"))
    assert hass.states.get(tz).state == "Europe/Amsterdam"
    active = entreg.async_get_entity_id("binary_sensor", DOMAIN, uid("site_s1__is_active"))
    assert hass.states.get(active).state == "on"
    seen = entreg.async_get_entity_id("sensor", DOMAIN, uid("lock_s1_l1__last_seen_at"))
    assert hass.states.get(seen).attributes["device_class"] == "timestamp"
    tags = entreg.async_get_entity_id("sensor", DOMAIN, uid("lock_s1_l1__tags"))
    assert hass.states.get(tags).state == "2"
    assert hass.states.get(tags).attributes["items"] == ["a", "b"]
    online = entreg.async_get_entity_id("binary_sensor", DOMAIN, uid("lock_s1_l1__online"))
    assert entreg.async_get(online).device_id == lock_dev.id
    assert hass.states.get(online).attributes["friendly_name"] == "Front door Online"


async def test_lock_unlock_and_new_metadata(hass: HomeAssistant, aioclient_mock) -> None:
    _api(aioclient_mock)
    entry = await _setup(hass)
    lock_id = er.async_get(hass).async_get_entity_id(
        "lock", DOMAIN, f"{entry.entry_id}_lock_s1_l1")

    patch_url = f"{API_BASE_URL}/sites/s1/locks/l1/locking"
    aioclient_mock.patch(patch_url, json={})
    # After unlocking, the API reports office_mode and a brand-new field.
    unlocked = [{**LOCKS[0], "locked_state": "office_mode", "firmware": "1.2.3"}]
    aioclient_mock.clear_requests()
    _api(aioclient_mock, locks=unlocked)
    aioclient_mock.patch(patch_url, json={})
    aioclient_mock.get(f"{API_BASE_URL}/sites/s1/locks/l1", json=unlocked[0])

    await hass.services.async_call("lock", "unlock", {"entity_id": lock_id}, blocking=True)
    patch_calls = [c for c in aioclient_mock.mock_calls if c[0] == "PATCH"]
    assert patch_calls[-1][2] == {"locked_state": "office_mode"}

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert hass.states.get(lock_id).state == "unlocked"

    fw = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_lock_s1_l1__firmware")
    assert fw is not None and hass.states.get(fw).state == "1.2.3"

    await hass.services.async_call("lock", "lock", {"entity_id": lock_id}, blocking=True)
    patch_calls = [c for c in aioclient_mock.mock_calls if c[0] == "PATCH"]
    assert patch_calls[-1][2] == {"locked_state": "locked"}


async def test_entry_without_token_starts_reauth(hass: HomeAssistant, aioclient_mock) -> None:
    """An entry without a sign-in token asks to sign in again in the browser."""
    entry = MockConfigEntry(domain=DOMAIN, data={"auth_implementation": DOMAIN}, unique_id="u1")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_picture_urls_are_images_not_sensors(hass: HomeAssistant, aioclient_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1")
    entry.add_to_hass(hass)
    entreg = er.async_get(hass)
    # Sensors created for picture URLs by an earlier version must be removed.
    for uid in ("account_u1__image_url", "site_s1__owner.image_url"):
        entreg.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_{uid}",
                                   config_entry=entry)
    _api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for uid in ("account_u1__image_url", "site_s1__owner.image_url"):
        assert entreg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{uid}") is None
    # Other owner fields stay sensors.
    assert entreg.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_site_s1__owner.first_name") is not None

    site = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "site_s1")})
    owner_img = entreg.async_get_entity_id(
        "image", DOMAIN, f"{entry.entry_id}_site_s1__owner.image_url")
    assert entreg.async_get(owner_img).device_id == site.id
    state = hass.states.get(owner_img)
    assert state.state != "unavailable"
    assert state.attributes["friendly_name"] == "HQ Amsterdam Owner picture"

    # The profile image on the account device is unchanged.
    assert entreg.async_get_entity_id(
        "image", DOMAIN, f"{entry.entry_id}_account_u1_profile_image") is not None
    assert entreg.async_get_entity_id(
        "image", DOMAIN, f"{entry.entry_id}_account_u1__image_url") is None

    # A changed URL updates the picture timestamp.
    before = state.state
    new_sites = {"items": [{**SITES["items"][0],
                            "owner": {"first_name": "Owner", "image_url": "https://img.example.com/o2.png"}}]}
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE_URL}/me", json=ME)
    aioclient_mock.get(f"{API_BASE_URL}/sites", json=new_sites)
    aioclient_mock.get(f"{API_BASE_URL}/sites/s1/locks", json=LOCKS)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(owner_img).state != before


async def test_id_entities_disabled_by_default(hass: HomeAssistant, aioclient_mock) -> None:
    _api(aioclient_mock)
    entry = await _setup(hass)
    entreg = er.async_get(hass)

    def reg(uid):
        return entreg.async_get(entreg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{uid}"))

    for uid in ("account_u1__id", "site_s1__id", "lock_s1_l1__id"):
        assert reg(uid).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert reg(uid).hidden_by is None
    for uid in ("account_u1__language", "site_s1__timezone", "site_s1__owner.first_name"):
        assert reg(uid).disabled_by is None


@pytest.mark.parametrize("minor", [1, 2])
async def test_migration_disables_existing_id_entities(
    hass: HomeAssistant, aioclient_mock, minor: int
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1",
                            version=1, minor_version=minor)
    entry.add_to_hass(hass)
    entreg = er.async_get(hass)
    hidden = er.RegistryEntryHider.INTEGRATION if minor == 2 else None
    old_id = entreg.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_site_s1__id",
                                        config_entry=entry, hidden_by=hidden)
    old_tz = entreg.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_site_s1__timezone",
                                        config_entry=entry)
    _api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.minor_version == 5
    migrated = entreg.async_get(old_id.entity_id)
    assert migrated.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert migrated.hidden_by is None
    assert entreg.async_get(old_tz.entity_id).disabled_by is None


def test_is_id_field() -> None:
    from custom_components.saltoks.entity import is_id_field

    for path in [("id",), ("owner", "id"), ("site_id",), ("customerId",), ("uuid",)]:
        assert is_id_field(path), path
    for path in [("idle",), ("valid",), ("android",), ("customer_reference",), ()]:
        assert not is_id_field(path), path


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


async def test_picture_served_as_octet_stream(hass: HomeAssistant, aioclient_mock) -> None:
    """Azure Blob Storage serves pictures as application/octet-stream."""
    import respx

    _api(aioclient_mock)
    entry = await _setup(hass)
    owner_img = er.async_get(hass).async_get_entity_id(
        "image", DOMAIN, f"{entry.entry_id}_site_s1__owner.image_url")
    entity = hass.data["image"].get_entity(owner_img)

    with respx.mock:
        respx.get("https://img.example.com/o.png").respond(
            200, content=PNG_BYTES, headers={"content-type": "application/octet-stream"})
        assert await entity.async_image() == PNG_BYTES
    assert entity.content_type == "image/png"


def test_detect_image_type() -> None:
    from custom_components.saltoks.image import detect_image_type

    assert detect_image_type(PNG_BYTES, "application/octet-stream", "https://x/a") == "image/png"
    assert detect_image_type(b"\xff\xd8\xff\xe0", None, "https://x/a") == "image/jpeg"
    assert detect_image_type(b"", "image/webp", "https://x/a") == "image/webp"
    assert detect_image_type(b"????", "application/octet-stream", "https://x/a.jpg") == "image/jpeg"
    assert detect_image_type(b"????", "application/octet-stream", "https://x/a") is None


def _lock_sequence(aioclient_mock, states: list[str]):
    """Mock GET /sites/s1/locks/l1 returning the given states in order
    (the last one repeats). Returns the list of served states."""
    from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

    served: list[str] = []

    async def handler(method, url, data):
        state = states[min(len(served), len(states) - 1)]
        served.append(state)
        return AiohttpClientMockResponse(
            method, url, status=200, text=json.dumps({**LOCKS[0], "locked_state": state}))

    aioclient_mock.get(f"{API_BASE_URL}/sites/s1/locks/l1", side_effect=handler)
    return served


async def test_unlock_polls_until_state_confirmed(hass: HomeAssistant, aioclient_mock) -> None:
    _api(aioclient_mock)
    entry = await _setup(hass)
    lock_id = er.async_get(hass).async_get_entity_id(
        "lock", DOMAIN, f"{entry.entry_id}_lock_s1_l1")
    aioclient_mock.patch(f"{API_BASE_URL}/sites/s1/locks/l1/locking", json={})
    served = _lock_sequence(aioclient_mock, ["locked", "locked", "office_mode", "locked"])

    states: list[str] = []
    from homeassistant.core import callback

    @callback
    def _record(event):
        if event.data["entity_id"] == lock_id:
            states.append(event.data["new_state"].state)

    hass.bus.async_listen("state_changed", _record)

    await hass.services.async_call("lock", "unlock", {"entity_id": lock_id}, blocking=True)
    await hass.async_block_till_done()

    # Polling stops as soon as the requested state is reported.
    assert served == ["locked", "locked", "office_mode"]
    assert hass.states.get(lock_id).state == "unlocked"
    # Shown as "unlocking" while waiting; never falsely "unlocked" before confirmation.
    assert states[0] == "unlocking"
    assert states[-1] == "unlocked"
    assert "unlocked" not in states[:-1]


async def test_poll_gives_up_after_timeout(hass: HomeAssistant, aioclient_mock, caplog) -> None:
    _api(aioclient_mock)
    entry = await _setup(hass)
    lock_id = er.async_get(hass).async_get_entity_id(
        "lock", DOMAIN, f"{entry.entry_id}_lock_s1_l1")
    aioclient_mock.patch(f"{API_BASE_URL}/sites/s1/locks/l1/locking", json={})
    served = _lock_sequence(aioclient_mock, ["locked"])

    await hass.services.async_call("lock", "unlock", {"entity_id": lock_id}, blocking=True)
    await hass.async_block_till_done()

    assert len(served) >= 1
    assert hass.states.get(lock_id).state == "locked"
    assert "did not report state 'office_mode'" in caplog.text


async def test_poll_interval_and_timeout_defaults(hass: HomeAssistant, aioclient_mock, monkeypatch) -> None:
    """With the real settings the lock is checked every second for at most 10 seconds."""
    from custom_components.saltoks import const, coordinator as coord_mod

    monkeypatch.setattr(const, "LOCK_CONFIRM_INTERVAL", 1.0)
    monkeypatch.setattr(const, "LOCK_CONFIRM_TIMEOUT", 10.0)
    _api(aioclient_mock)
    entry = await _setup(hass)
    aioclient_mock.patch(f"{API_BASE_URL}/sites/s1/locks/l1/locking", json={})
    served = _lock_sequence(aioclient_mock, ["locked"])

    clock = {"t": 0.0}
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(coord_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: clock["t"])

    confirmed = await entry.runtime_data.async_set_locked_state("s1", "l1", "office_mode")
    assert confirmed is False
    assert sleeps == [1.0] * 10
    assert len(served) == 10


# ------------------------------------------------------------ Easy Office Mode

EOM = {
    "eom_auto_start": True,
    "monday": True, "tuesday": True, "wednesday": True, "thursday": True, "friday": True,
    "saturday": False, "sunday": False,
    "start_time": "10:00", "end_time": "17:00",
    "start_date": "2026-03-30T00:00:00.000", "end_date": "2026-12-31T23:59:59.999",
}
EOM_LOCKS = [
    {**LOCKS[0], "easy_office_mode_schedule": EOM},
    {"id": "l2", "customer_reference": "Back door", "locked_state": "locked",
     "easy_office_mode_schedule": []},
]


async def _setup_eom(hass, aioclient_mock):
    _api(aioclient_mock, locks=EOM_LOCKS)
    entry = await _setup(hass)
    for lock in EOM_LOCKS:
        aioclient_mock.put(
            f"{API_BASE_URL}/sites/s1/locks/{lock['id']}/easy_office_mode_schedule", json={})
        aioclient_mock.delete(
            f"{API_BASE_URL}/sites/s1/locks/{lock['id']}/easy_office_mode_schedule", json={})
        aioclient_mock.get(f"{API_BASE_URL}/sites/s1/locks/{lock['id']}", status=500)
    return entry


def _eid(hass, entry, platform, lock, field):
    return er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_lock_s1_{lock}_eom_{field}")


def _puts(aioclient_mock, lock="l1"):
    return [c[2] for c in aioclient_mock.mock_calls
            if c[0] == "PUT" and str(c[1]).endswith(f"/locks/{lock}/easy_office_mode_schedule")]


async def test_eom_controls_reflect_schedule(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup_eom(hass, aioclient_mock)
    entreg = er.async_get(hass)
    lock_dev = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "lock_s1_l1")})

    auto = _eid(hass, entry, "switch", "l1", "eom_auto_start")
    assert hass.states.get(auto).state == "on"
    assert hass.states.get(auto).attributes["friendly_name"] == "Front door Easy Office Mode auto start"
    assert entreg.async_get(auto).device_id == lock_dev.id
    assert entreg.async_get(auto).entity_category == "config"
    assert hass.states.get(_eid(hass, entry, "switch", "l1", "monday")).state == "on"
    assert hass.states.get(_eid(hass, entry, "switch", "l1", "sunday")).state == "off"
    assert hass.states.get(_eid(hass, entry, "time", "l1", "start_time")).state == "10:00:00"
    assert hass.states.get(_eid(hass, entry, "time", "l1", "end_time")).state == "17:00:00"
    assert hass.states.get(_eid(hass, entry, "date", "l1", "start_date")).state == "2026-03-30"
    assert hass.states.get(_eid(hass, entry, "date", "l1", "end_date")).state == "2026-12-31"

    # Lock without schedule (empty list): switches off, times and dates unknown.
    assert hass.states.get(_eid(hass, entry, "switch", "l2", "eom_auto_start")).state == "off"
    assert hass.states.get(_eid(hass, entry, "time", "l2", "start_time")).state == "unknown"
    assert hass.states.get(_eid(hass, entry, "date", "l2", "end_date")).state == "unknown"

    # The schedule is not exposed as read-only metadata.
    for uid in ("lock_s1_l1__easy_office_mode_schedule.monday",
                "lock_s1_l1__easy_office_mode_schedule.start_time",
                "lock_s1_l2__easy_office_mode_schedule"):
        for platform in ("sensor", "binary_sensor"):
            assert entreg.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{uid}") is None


async def test_eom_control_sends_full_schedule(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup_eom(hass, aioclient_mock)

    await hass.services.async_call("switch", "turn_on",
                                   {"entity_id": _eid(hass, entry, "switch", "l1", "saturday")},
                                   blocking=True)
    assert _puts(aioclient_mock)[-1] == {**EOM, "saturday": True}
    assert hass.states.get(_eid(hass, entry, "switch", "l1", "saturday")).state == "on"

    await hass.services.async_call("time", "set_value",
                                   {"entity_id": _eid(hass, entry, "time", "l1", "start_time"),
                                    "time": "08:30:00"}, blocking=True)
    assert _puts(aioclient_mock)[-1] == {**EOM, "saturday": True, "start_time": "08:30"}

    await hass.services.async_call("date", "set_value",
                                   {"entity_id": _eid(hass, entry, "date", "l1", "end_date"),
                                    "date": "2027-01-15"}, blocking=True)
    body = _puts(aioclient_mock)[-1]
    assert body["start_date"] == "2026-03-30T00:00:00.000"
    assert body["end_date"] == "2027-01-15T23:59:59.999"


async def test_eom_read_back_after_put(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup_eom(hass, aioclient_mock)
    aioclient_mock.clear_requests()
    stored = {**EOM, "start_time": "07:00"}  # what the API reports after the PUT
    aioclient_mock.put(f"{API_BASE_URL}/sites/s1/locks/l1/easy_office_mode_schedule", json={})
    aioclient_mock.get(f"{API_BASE_URL}/sites/s1/locks/l1",
                       json={**EOM_LOCKS[0], "easy_office_mode_schedule": stored})
    await hass.services.async_call("switch", "turn_off",
                                   {"entity_id": _eid(hass, entry, "switch", "l1", "monday")},
                                   blocking=True)
    assert hass.states.get(_eid(hass, entry, "time", "l1", "start_time")).state == "07:00:00"


async def test_eom_validation(hass: HomeAssistant, aioclient_mock) -> None:
    from homeassistant.exceptions import ServiceValidationError

    entry = await _setup_eom(hass, aioclient_mock)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("time", "set_value",
                                       {"entity_id": _eid(hass, entry, "time", "l1", "end_time"),
                                        "time": "09:00:00"}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("date", "set_value",
                                       {"entity_id": _eid(hass, entry, "date", "l1", "end_date"),
                                        "date": "2026-01-01"}, blocking=True)
    assert _puts(aioclient_mock) == []


async def test_eom_first_change_without_schedule(hass: HomeAssistant, aioclient_mock,
                                                  freezer) -> None:
    freezer.move_to("2026-11-11 12:00:00+00:00")
    entry = await _setup_eom(hass, aioclient_mock)
    await hass.services.async_call("switch", "turn_on",
                                   {"entity_id": _eid(hass, entry, "switch", "l2", "monday")},
                                   blocking=True)
    assert _puts(aioclient_mock, "l2")[-1] == {
        "eom_auto_start": False, "monday": True, "tuesday": False, "wednesday": False,
        "thursday": False, "friday": False, "saturday": False, "sunday": False,
        "start_time": "09:00", "end_time": "17:00",
        "start_date": "2026-11-11T00:00:00.000", "end_date": "",
    }


async def test_eom_service_multiple_locks(hass: HomeAssistant, aioclient_mock, freezer) -> None:
    freezer.move_to("2026-11-11 12:00:00+00:00")
    entry = await _setup_eom(hass, aioclient_mock)
    entreg = er.async_get(hass)
    locks = [entreg.async_get_entity_id("lock", DOMAIN, f"{entry.entry_id}_lock_s1_{lid}")
             for lid in ("l1", "l2")]

    await hass.services.async_call(DOMAIN, "set_easy_office_mode_schedule", {
        "entity_id": locks,
        "days": ["monday", "wednesday"],
        "start_time": "07:30",
        "end_date": "2027-06-30",
    }, blocking=True)

    l1, l2 = _puts(aioclient_mock, "l1")[-1], _puts(aioclient_mock, "l2")[-1]
    assert l1 == {**EOM, "tuesday": False, "thursday": False, "friday": False,
                  "start_time": "07:30", "end_date": "2027-06-30T23:59:59.999"}
    assert l2["monday"] and l2["wednesday"] and not l2["tuesday"]
    assert l2["start_time"] == "07:30" and l2["end_time"] == "17:00"
    assert l2["start_date"] == "2026-11-11T00:00:00.000"
    assert l2["end_date"] == "2027-06-30T23:59:59.999"

    # An empty end date removes it: the schedule runs indefinitely.
    for empty in (None, ""):
        await hass.services.async_call(DOMAIN, "set_easy_office_mode_schedule",
                                       {"entity_id": locks[0], "end_date": empty}, blocking=True)
        assert _puts(aioclient_mock, "l1")[-1]["end_date"] == ""

    # At least one schedule field is required.
    import voluptuous as vol
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "set_easy_office_mode_schedule",
                                       {"entity_id": locks}, blocking=True)


async def test_superseded_eom_sensors_removed(hass: HomeAssistant, aioclient_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1",
                            version=1, minor_version=3)
    entry.add_to_hass(hass)
    entreg = er.async_get(hass)
    old = [
        entreg.async_get_or_create(platform, DOMAIN, f"{entry.entry_id}_{uid}", config_entry=entry)
        for platform, uid in (
            ("binary_sensor", "lock_s1_l1__easy_office_mode_schedule.monday"),
            ("sensor", "lock_s1_l1__easy_office_mode_schedule.start_time"),
            ("sensor", "lock_s1_l2__easy_office_mode_schedule"),
        )
    ]
    keep = entreg.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_lock_s1_l1__battery_level",
                                      config_entry=entry)
    _api(aioclient_mock, locks=EOM_LOCKS)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for reg in old:
        assert entreg.async_get(reg.entity_id) is None
    assert entreg.async_get(keep.entity_id) is not None


def _deletes(aioclient_mock):
    return [str(c[1]) for c in aioclient_mock.mock_calls if c[0] == "DELETE"]


async def test_eom_remove_button(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup_eom(hass, aioclient_mock)
    entreg = er.async_get(hass)
    btn = _eid(hass, entry, "button", "l1", "remove")
    lock_dev = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "lock_s1_l1")})
    assert entreg.async_get(btn).device_id == lock_dev.id
    assert hass.states.get(btn).attributes["friendly_name"] == "Front door Remove Easy Office Mode schedule"
    # No schedule on l2: the button is unavailable.
    assert hass.states.get(_eid(hass, entry, "button", "l2", "remove")).state == "unavailable"

    await hass.services.async_call("button", "press", {"entity_id": btn}, blocking=True)
    assert _deletes(aioclient_mock) == [
        f"{API_BASE_URL}/sites/s1/locks/l1/easy_office_mode_schedule"]
    assert hass.states.get(_eid(hass, entry, "switch", "l1", "monday")).state == "off"
    assert hass.states.get(_eid(hass, entry, "time", "l1", "start_time")).state == "unknown"
    assert hass.states.get(btn).state == "unavailable"


async def test_eom_remove_service_multiple_locks(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup_eom(hass, aioclient_mock)
    entreg = er.async_get(hass)
    locks = [entreg.async_get_entity_id("lock", DOMAIN, f"{entry.entry_id}_lock_s1_{lid}")
             for lid in ("l1", "l2")]
    await hass.services.async_call(DOMAIN, "remove_easy_office_mode_schedule",
                                   {"entity_id": locks}, blocking=True)
    # l2 has no schedule and is skipped.
    assert _deletes(aioclient_mock) == [
        f"{API_BASE_URL}/sites/s1/locks/l1/easy_office_mode_schedule"]
    assert hass.states.get(_eid(hass, entry, "date", "l1", "end_date")).state == "unknown"


@pytest.mark.parametrize("returned", [None, ""])
async def test_eom_indefinite_schedule(hass: HomeAssistant, aioclient_mock, returned) -> None:
    """A schedule without end date (returned as null or empty) is shown and kept as indefinite."""
    locks = [{**EOM_LOCKS[0], "easy_office_mode_schedule": {**EOM, "end_date": returned}}, EOM_LOCKS[1]]
    _api(aioclient_mock, locks=locks)
    entry = await _setup(hass)
    aioclient_mock.put(f"{API_BASE_URL}/sites/s1/locks/l1/easy_office_mode_schedule", json={})
    aioclient_mock.get(f"{API_BASE_URL}/sites/s1/locks/l1", status=500)

    end = hass.states.get(_eid(hass, entry, "date", "l1", "end_date"))
    assert end.state == "unknown"
    assert end.attributes["no_end_date"] is True
    assert hass.states.get(_eid(hass, entry, "date", "l2", "end_date")).attributes["no_end_date"] is False

    # Changing another field keeps the schedule indefinite.
    await hass.services.async_call("switch", "turn_on",
                                   {"entity_id": _eid(hass, entry, "switch", "l1", "sunday")},
                                   blocking=True)
    assert _puts(aioclient_mock)[-1] == {**EOM, "sunday": True, "end_date": ""}


# ------------------------------------------------------------------- entries

def _entry(entry_id, when, lock="l1", category="Opening", **extra):
    return {
        "id": entry_id, "event_category": category, "event_detail": "Key",
        "utc_date_time": when, "local_date_time": when, "lock_id": lock,
        "lock_customer_reference": "Front door",
        "user_id": "u9", "user_first_name": "Jane", "user_last_name": "Doe",
        "user_alias": "jd", "user_image_url": "https://img.example.com/u9.png",
        "access_by": "Mobile", "access_detail": "App", **extra,
    }


async def _poll(hass, aioclient_mock, entries, locks=None, incidents=None):
    aioclient_mock.clear_requests()
    _api(aioclient_mock, locks=locks, entries=entries, incidents=incidents)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done(wait_background_tasks=True)


def _event_id(hass, entry, lock="l1", kind="last_entry"):
    return er.async_get(hass).async_get_entity_id(
        "event", DOMAIN, f"{entry.entry_id}_lock_s1_{lock}_{kind}")


async def test_last_entry_event(hass: HomeAssistant, aioclient_mock, freezer) -> None:
    from homeassistant.core import callback

    old = _entry("e1", "2026-11-11T08:00:00.000")
    _api(aioclient_mock, locks=EOM_LOCKS, entries=[old])
    entry = await _setup(hass)

    # Request: newest entries of the site, newest first.
    call = next(c for c in aioclient_mock.mock_calls if "/entries" in str(c[1]))
    assert call[1].query["$orderby"] == "utc_date_time desc"
    assert call[1].query["$top"] == "50"

    ev = _event_id(hass, entry)
    lock_dev = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "lock_s1_l1")})
    assert er.async_get(hass).async_get(ev).device_id == lock_dev.id
    state = hass.states.get(ev)
    assert state.attributes["friendly_name"] == "Front door Last entry"
    # Fresh install: the most recent entry is shown; user details are left out by default.
    assert state.attributes["event_type"] == "Opening - Key"
    assert state.attributes["id"] == "e1"
    # Only the local time is kept.
    assert "utc_date_time" not in state.attributes
    assert state.attributes["local_date_time"] == "2026-11-11T08:00:00.000"
    assert "user_first_name" not in state.attributes and "user_id" not in state.attributes
    assert state.attributes["access_by"] == "Mobile"
    # Lock without entries.
    assert hass.states.get(_event_id(hass, entry, "l2")).state == "unknown"

    fired: list[tuple[str, str]] = []

    @callback
    def _record(event):
        if event.data["entity_id"] in (ev, _event_id(hass, entry, "l2")):
            new = event.data["new_state"]
            fired.append((event.data["entity_id"], new.attributes.get("id")))

    hass.bus.async_listen("state_changed", _record)

    # Next poll: two new entries for l1 (returned newest first) and one for l2.
    freezer.tick(11)
    await _poll(hass, aioclient_mock, [
        _entry("e3", "2026-11-11T09:05:00.000", category="Denied access"),
        _entry("e4", "2026-11-11T09:01:00.000", lock="l2"),
        _entry("e2", "2026-11-11T09:00:00.000"),
        old,
    ], locks=EOM_LOCKS)
    l1 = [i for e, i in fired if e == ev]
    assert l1 == ["e2", "e3"]  # oldest first, each once
    assert [i for e, i in fired if e != ev] == ["e4"]
    state = hass.states.get(ev)
    assert state.attributes["event_type"] == "Denied access - Key"
    assert set(state.attributes["event_types"]) >= {"Opening - Key", "Denied access - Key"}

    # The same entries are not reported again, also not on other updates.
    fired.clear()
    entry.runtime_data.async_update_listeners()
    freezer.tick(11)
    await _poll(hass, aioclient_mock, [_entry("e3", "2026-11-11T09:05:00.000")], locks=EOM_LOCKS)
    assert fired == []


async def test_entries_with_user_details(hass: HomeAssistant, aioclient_mock) -> None:
    _api(aioclient_mock, entries=[_entry("e1", "2026-11-11T08:00:00.000")])
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1",
                            options={"include_user_details": True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    attrs = hass.states.get(_event_id(hass, entry)).attributes
    assert attrs["user_first_name"] == "Jane" and attrs["user_id"] == "u9"
    assert attrs["event_type"] == "Opening - Key - Jane Doe"


async def test_entries_failure_does_not_fail_update(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{API_BASE_URL}/me", json=ME)
    aioclient_mock.get(f"{API_BASE_URL}/sites", json=SITES)
    aioclient_mock.get(f"{API_BASE_URL}/sites/s1/locks", json=LOCKS)
    aioclient_mock.get(ENTRIES_URL, status=500)
    entry = await _setup(hass)
    assert entry.state.value == "loaded"
    lock = er.async_get(hass).async_get_entity_id("lock", DOMAIN, f"{entry.entry_id}_lock_s1_l1")
    assert hass.states.get(lock).state == "locked"


async def test_diagnostics_redacts_entries(hass: HomeAssistant, aioclient_mock) -> None:
    from custom_components.saltoks.diagnostics import async_get_config_entry_diagnostics

    _api(aioclient_mock, entries=[_entry("e1", "2026-11-11T08:00:00.000")])
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1",
                            options={"include_user_details": True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    json.dumps(diag)
    latest = diag["data"]["latest_events"]["entries"]["s1/l1"]
    assert latest["user_first_name"] == "**REDACTED**"
    assert latest["event_category"] == "Opening"


async def test_options_include_user_details(hass: HomeAssistant, aioclient_mock) -> None:
    _api(aioclient_mock)
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert {str(k) for k in result["data_schema"].schema} == {"scan_interval", "include_user_details"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"scan_interval": 10, "include_user_details": True})
    await hass.async_block_till_done()  # the entry reloads with the new options
    assert entry.options["include_user_details"] is True


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"event_category": "Opening", "event_detail": "Key",
          "user_first_name": "Jane", "user_last_name": "Doe"}, "Opening - Key - Jane Doe"),
        ({"event_category": "Opening", "event_detail": "",
          "user_first_name": "Jane", "user_last_name": "Doe"}, "Opening - Jane Doe"),
        ({"event_category": "Opening", "event_detail": "Key"}, "Opening - Key"),
        ({"event_category": "Opening", "event_detail": None,
          "user_first_name": None, "user_last_name": ""}, "Opening"),
        ({"event_category": "Opening", "user_first_name": "Jane"}, "Opening - Jane"),
        ({"event_category": "Opening", "user_last_name": "Doe"}, "Opening - Doe"),
        ({}, "Unknown"),
    ],
)
def test_event_type_composition(fields, expected) -> None:
    from custom_components.saltoks.event import event_type_of

    assert event_type_of(fields) == expected


def _incident(incident_id, when, lock="l1", category="Battery", detail="Low", **extra):
    return {
        "id": incident_id, "event_category": category, "event_detail": detail,
        "utc_date_time": when, "local_date_time": when, "lock_id": lock,
        "lock_customer_reference": "Front door", "ttl_in_seconds": 0, **extra,
    }


async def test_last_incident_event(hass: HomeAssistant, aioclient_mock, freezer) -> None:
    from homeassistant.core import callback

    _api(aioclient_mock, locks=EOM_LOCKS,
         incidents=[_incident("i1", "2026-11-11T07:00:00.000")])
    entry = await _setup(hass)

    call = next(c for c in aioclient_mock.mock_calls if "/incidents" in str(c[1]))
    assert call[1].query["$orderby"] == "utc_date_time desc"
    assert call[1].query["$top"] == "50"

    inc = _event_id(hass, entry, kind="last_incident")
    lock_dev = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "lock_s1_l1")})
    assert er.async_get(hass).async_get(inc).device_id == lock_dev.id
    state = hass.states.get(inc)
    assert state.attributes["friendly_name"] == "Front door Last incident"
    assert state.attributes["event_type"] == "Battery - Low"
    assert "utc_date_time" not in state.attributes
    # Incidents and entries are separate: no entries yet.
    assert hass.states.get(_event_id(hass, entry)).state == "unknown"

    fired: list[str] = []

    @callback
    def _record(event):
        if event.data["entity_id"] == inc:
            if (new := event.data["new_state"]) and "event_type" in new.attributes:
                fired.append(new.attributes["event_type"])

    hass.bus.async_listen("state_changed", _record)
    freezer.tick(11)
    await _poll(hass, aioclient_mock, [], locks=EOM_LOCKS, incidents=[
        _incident("i2", "2026-11-11T09:00:00.000", category="Intrusion", detail=""),
        _incident("i1", "2026-11-11T07:00:00.000"),
    ])
    assert fired == ["Intrusion"]


async def test_event_types_are_bounded(hass: HomeAssistant, aioclient_mock, freezer) -> None:
    from custom_components.saltoks.event import MAX_EVENT_TYPES

    _api(aioclient_mock)
    entry = await _setup(hass)
    many = [_entry(f"x{i}", f"2026-11-11T10:{i // 60:02d}:{i % 60:02d}.000", category=f"Cat {i}")
            for i in range(MAX_EVENT_TYPES + 20)]
    freezer.tick(11)
    await _poll(hass, aioclient_mock, many)
    types = hass.states.get(_event_id(hass, entry)).attributes["event_types"]
    assert len(types) == MAX_EVENT_TYPES
    assert f"Cat {MAX_EVENT_TYPES + 19} - Key" in types


# ------------------------------------------------------- browser sign-in (PKCE)

def _token_calls(aioclient_mock):
    return [c for c in aioclient_mock.mock_calls if str(c[1]) == TOKEN_URL]


async def _browser_sign_in(hass, hass_client_no_auth, aioclient_mock, result, me=None,
                           api=None):
    """Complete the external sign-in step of a flow that shows the login page.
    ``api`` registers the API responses for the signed-in account."""
    from yarl import URL

    assert result["type"] == "external"
    state = URL(result["url"]).query["state"]
    client = await hass_client_no_auth()
    resp = await client.get(f"/auth/external/callback?code=abcd&state={state}")
    assert resp.status == 200
    aioclient_mock.post(TOKEN_URL, json={
        "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600,
        "token_type": "Bearer", "id_token": "header.payload.signature",
        "scope": "openid profile user_api.full_access offline_access",
    })
    if api:
        api()
    else:
        _api(aioclient_mock, me=me)
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def test_browser_sign_in_creates_entry(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, current_request_with_host
) -> None:
    import base64
    import hashlib
    from yarl import URL
    from custom_components.saltoks.oauth import (
        AUTHORIZE_URL, OAUTH_CLIENT_ID, OAUTH_SCOPES, REDIRECT_URI)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    url = URL(result["url"])
    assert str(url.with_query(None)) == AUTHORIZE_URL
    query = url.query
    assert query["response_type"] == "code"
    assert query["client_id"] == OAUTH_CLIENT_ID == "6566f5da-839a-44bd-8a69-f862da174414"
    assert query["redirect_uri"] == REDIRECT_URI == "https://my.home-assistant.io/redirect/oauth"
    assert query["scope"] == OAUTH_SCOPES == "openid profile user_api.full_access offline_access"
    assert query["code_challenge_method"] == "S256"
    # Always ask for the account, even when the browser is already signed in.
    assert query["prompt"] == "login"
    assert "login_hint" not in query
    challenge = query["code_challenge"]

    result = await _browser_sign_in(hass, hass_client_no_auth, aioclient_mock, result)
    assert result["type"] == "create_entry"
    assert result["title"] == "Salto KS (t@example.com)"
    assert result["result"].unique_id == "u1"
    # Only tokens are stored: no username, password or client secret. The ID
    # token is not kept either.
    data = result["data"]
    assert set(data) == {"auth_implementation", "token"}
    assert data["token"]["access_token"] == "new-access"
    assert data["token"]["refresh_token"] == "new-refresh"
    assert "id_token" not in data["token"]

    # Token request: public client with PKCE.
    form = _token_calls(aioclient_mock)[0][2]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "abcd"
    assert form["client_id"] == OAUTH_CLIENT_ID
    assert form["redirect_uri"] == REDIRECT_URI
    assert "client_secret" not in form
    verifier = form["code_verifier"]
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert expected == challenge

    await hass.async_block_till_done()
    assert result["result"].state is ConfigEntryState.LOADED


async def test_each_sign_in_gets_its_own_verifier(
    hass: HomeAssistant, current_request_with_host
) -> None:
    from yarl import URL

    first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    second = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert (URL(first["url"]).query["code_challenge"]
            != URL(second["url"]).query["code_challenge"])


async def test_browser_sign_in_same_account_aborts(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, current_request_with_host
) -> None:
    MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await _browser_sign_in(hass, hass_client_no_auth, aioclient_mock, result)
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_reauth_with_other_account_aborts(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, current_request_with_host
) -> None:
    _api(aioclient_mock)
    entry = await _setup(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    # The login page asks for the account and is pre-filled with this entry's account.
    from yarl import URL
    query = URL(result["url"]).query
    assert query["prompt"] == "login"
    assert query["login_hint"] == "t@example.com"
    aioclient_mock.clear_requests()
    result = await _browser_sign_in(hass, hass_client_no_auth, aioclient_mock, result,
                                    me={**ME, "id": "someone-else"})
    assert result["type"] == "abort"
    assert result["reason"] == "wrong_account"
    assert entry.data["token"]["access_token"] == "tok1"


async def test_expired_token_is_refreshed_without_reload(hass: HomeAssistant, aioclient_mock) -> None:
    from custom_components.saltoks.oauth import OAUTH_CLIENT_ID

    expired = {**ENTRY_DATA, "token": {**ENTRY_DATA["token"], "expires_at": 0}}
    aioclient_mock.post(TOKEN_URL, json={
        "access_token": "tok2", "refresh_token": "r2", "expires_in": 3600,
        "token_type": "Bearer", "id_token": "x.y.z"})
    _api(aioclient_mock)
    entry = MockConfigEntry(domain=DOMAIN, data=expired, unique_id="u1")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    form = _token_calls(aioclient_mock)[0][2]
    assert form == {"grant_type": "refresh_token", "client_id": OAUTH_CLIENT_ID,
                    "refresh_token": "r1"}
    assert entry.data["token"]["access_token"] == "tok2"
    assert entry.data["token"]["refresh_token"] == "r2"
    assert "id_token" not in entry.data["token"]
    me_call = next(c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/me"))
    assert me_call[3]["Authorization"] == "Bearer tok2"
    # Storing the new token must not reload the integration.
    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "token": {**entry.data["token"], "access_token": "tok3"}})
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator


async def test_refresh_rejected_starts_reauth(hass: HomeAssistant, aioclient_mock) -> None:
    expired = {**ENTRY_DATA, "token": {**ENTRY_DATA["token"], "expires_at": 0}}
    aioclient_mock.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
    _api(aioclient_mock)
    entry = MockConfigEntry(domain=DOMAIN, data=expired, unique_id="u1")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth"
               for f in hass.config_entries.flow.async_progress())


async def test_api_401_refreshes_token_and_retries(hass: HomeAssistant, aioclient_mock) -> None:
    from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

    calls = {"me": 0}

    async def me_handler(method, url, data):
        calls["me"] += 1
        if calls["me"] == 1:
            return AiohttpClientMockResponse(method, url, status=401, text="")
        return AiohttpClientMockResponse(method, url, status=200, text=json.dumps(ME))

    aioclient_mock.post(TOKEN_URL, json={
        "access_token": "tok2", "refresh_token": "r2", "expires_in": 3600, "token_type": "Bearer"})
    aioclient_mock.get(f"{API_BASE_URL}/me", side_effect=me_handler)
    aioclient_mock.get(f"{API_BASE_URL}/sites", json=SITES)
    aioclient_mock.get(f"{API_BASE_URL}/sites/s1/locks", json=LOCKS)
    aioclient_mock.get(ENTRIES_URL, json={"items": []})
    aioclient_mock.get(INCIDENTS_URL, json={"items": []})
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert len(_token_calls(aioclient_mock)) == 1
    assert entry.data["token"]["access_token"] == "tok2"


async def test_options_change_reloads(hass: HomeAssistant, aioclient_mock) -> None:
    _api(aioclient_mock)
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    hass.config_entries.async_update_entry(entry, options={"scan_interval": 30})
    await hass.async_block_till_done()
    assert entry.runtime_data is not coordinator


async def test_diagnostics_redacts_tokens(hass: HomeAssistant, aioclient_mock) -> None:
    from custom_components.saltoks.diagnostics import async_get_config_entry_diagnostics

    _api(aioclient_mock)
    entry = await _setup(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["token"] == "**REDACTED**"
    assert "tok1" not in json.dumps(diag)



# ---------------------------------------------------------- switching account

def _api_account(aioclient_mock, me, sites):
    """Responses for an account: ``sites`` maps a site id to its lock ids."""
    aioclient_mock.get(f"{API_BASE_URL}/me", json=me)
    aioclient_mock.get(f"{API_BASE_URL}/sites", json={"items": [
        {"id": sid, "customer_reference": f"Site {sid}"} for sid in sites]})
    for sid, lock_ids in sites.items():
        aioclient_mock.get(f"{API_BASE_URL}/sites/{sid}/locks", json=[
            {"id": lid, "customer_reference": f"Lock {lid}", "locked_state": "locked"}
            for lid in lock_ids])
        for feed in ("entries", "incidents"):
            aioclient_mock.get(f"https://connect.saltoks.com/v1.1/sites/{sid}/{feed}",
                               json={"items": []})


ME_U2 = {"id": "U2", "first_name": "Other", "last_name": "Person", "email": "o@example.com",
         "language": "nl"}


async def test_reconfigure_switches_account(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, current_request_with_host
) -> None:
    devreg, entreg = dr.async_get(hass), er.async_get(hass)
    # Account u1 sees sites s1 (lock l1) and s3 (lock l9).
    _api_account(aioclient_mock, ME, {"s1": ["l1"], "s3": ["l9"]})
    entry = await _setup(hass)

    def eid(platform, uid):
        return entreg.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{uid}")

    old_language = eid("sensor", "account_u1__language")
    assert hass.states.get(old_language).state == "en"

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    from yarl import URL
    query = URL(result["url"]).query
    assert query["prompt"] == "login"
    assert "login_hint" not in query  # another account may be chosen

    # Account U2 sees s1 (lock l1, shared) and s2 (lock l3, new); not s3.
    aioclient_mock.clear_requests()
    result = await _browser_sign_in(
        hass, hass_client_no_auth, aioclient_mock, result,
        api=lambda: _api_account(aioclient_mock, ME_U2, {"s1": ["l1"], "s2": ["l3"]}))
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.unique_id == "u2"
    assert entry.title == "Salto KS (o@example.com)"
    assert entry.data["token"]["access_token"] == "new-access"

    # New account device and entities; the old ones are unavailable.
    new_account = devreg.async_get_device(identifiers={(DOMAIN, "account_u2")})
    old_account = devreg.async_get_device(identifiers={(DOMAIN, "account_u1")})
    assert new_account.name == "Other Person"
    assert old_account is not None
    assert hass.states.get(eid("sensor", "account_u2__language")).state == "nl"
    assert hass.states.get(old_language).state == "unavailable"

    # Shared site and lock keep working; s3 is unavailable; s2 and l3 are new.
    assert hass.states.get(eid("lock", "lock_s1_l1")).state == "locked"
    assert hass.states.get(eid("lock", "lock_s3_l9")).state == "unavailable"
    assert hass.states.get(eid("lock", "lock_s2_l3")).state == "locked"
    s1 = devreg.async_get_device(identifiers={(DOMAIN, "site_s1")})
    s2 = devreg.async_get_device(identifiers={(DOMAIN, "site_s2")})
    assert s1.via_device_id == new_account.id
    assert s2.via_device_id == new_account.id
    l3 = devreg.async_get_device(identifiers={(DOMAIN, "lock_s2_l3")})
    assert l3.via_device_id == s2.id

    # The old account device and the inaccessible site can be removed; the
    # current account device and accessible sites cannot.
    from custom_components.saltoks import async_remove_config_entry_device
    s3 = devreg.async_get_device(identifiers={(DOMAIN, "site_s3")})
    assert await async_remove_config_entry_device(hass, entry, old_account)
    assert await async_remove_config_entry_device(hass, entry, s3)
    assert not await async_remove_config_entry_device(hass, entry, new_account)
    assert not await async_remove_config_entry_device(hass, entry, s1)


async def test_reconfigure_to_account_of_other_entry_aborts(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, current_request_with_host
) -> None:
    MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u2").add_to_hass(hass)
    _api(aioclient_mock)
    entry = await _setup(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    aioclient_mock.clear_requests()
    result = await _browser_sign_in(hass, hass_client_no_auth, aioclient_mock, result, me=ME_U2)
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    assert entry.unique_id == "u1"
    assert entry.data["token"]["access_token"] == "tok1"


async def test_reconfigure_same_account(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, current_request_with_host
) -> None:
    _api(aioclient_mock)
    entry = await _setup(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    aioclient_mock.clear_requests()
    result = await _browser_sign_in(hass, hass_client_no_auth, aioclient_mock, result)
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.unique_id == "u1"
    assert entry.data["token"]["access_token"] == "new-access"
    assert len(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)) == 3


async def test_migration_moves_account_device_to_account(hass: HomeAssistant, aioclient_mock) -> None:
    """Existing account device and entities keep their device, entity IDs and history."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1",
                            version=1, minor_version=4)
    entry.add_to_hass(hass)
    devreg, entreg = dr.async_get(hass), er.async_get(hass)
    device = devreg.async_get_or_create(config_entry_id=entry.entry_id,
                                        identifiers={(DOMAIN, entry.entry_id)}, name="Test User")
    language = entreg.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_me__language",
                                          config_entry=entry, device_id=device.id)
    image = entreg.async_get_or_create("image", DOMAIN, f"{entry.entry_id}_profile_image",
                                       config_entry=entry, device_id=device.id)
    site_id = entreg.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_site_s1__timezone",
                                         config_entry=entry)
    _api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.minor_version == 5
    moved = devreg.async_get_device(identifiers={(DOMAIN, "account_u1")})
    assert moved.id == device.id
    assert entreg.async_get(language.entity_id).unique_id == f"{entry.entry_id}_account_u1__language"
    assert entreg.async_get(image.entity_id).unique_id == f"{entry.entry_id}_account_u1_profile_image"
    assert entreg.async_get(site_id.entity_id).unique_id == f"{entry.entry_id}_site_s1__timezone"
    assert hass.states.get(language.entity_id).state == "en"


# ------------------------------------------------------------------ entity IDs

async def test_lock_entity_ids_start_with_site_name(hass: HomeAssistant, aioclient_mock) -> None:
    _api(aioclient_mock, locks=EOM_LOCKS)
    entry = await _setup(hass)
    entreg = er.async_get(hass)

    def eid(platform, uid):
        return entreg.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{uid}")

    expected = {
        eid("lock", "lock_s1_l1"): "lock.hq_amsterdam_front_door",
        eid("event", "lock_s1_l1_last_entry"): "event.hq_amsterdam_front_door_last_entry",
        eid("event", "lock_s1_l1_last_incident"): "event.hq_amsterdam_front_door_last_incident",
        eid("switch", "lock_s1_l1_eom_monday"): "switch.hq_amsterdam_front_door_easy_office_mode_monday",
        eid("switch", "lock_s1_l1_eom_eom_auto_start"):
            "switch.hq_amsterdam_front_door_easy_office_mode_auto_start",
        eid("time", "lock_s1_l1_eom_start_time"):
            "time.hq_amsterdam_front_door_easy_office_mode_start_time",
        eid("date", "lock_s1_l1_eom_end_date"):
            "date.hq_amsterdam_front_door_easy_office_mode_end_date",
        eid("button", "lock_s1_l1_eom_remove"):
            "button.hq_amsterdam_front_door_remove_easy_office_mode_schedule",
        eid("sensor", "lock_s1_l1__battery_level"): "sensor.hq_amsterdam_front_door_battery_level",
        eid("binary_sensor", "lock_s1_l1__online"): "binary_sensor.hq_amsterdam_front_door_online",
        eid("lock", "lock_s1_l2"): "lock.hq_amsterdam_back_door",
        # Site and account entities are not affected.
        eid("sensor", "site_s1__timezone"): "sensor.hq_amsterdam_timezone",
        eid("sensor", "account_u1__language"): "sensor.test_user_language",
    }
    assert expected == {v: v for v in expected.values()}
    # Friendly names stay "<lock> <entity>".
    assert hass.states.get("event.hq_amsterdam_front_door_last_entry").attributes[
        "friendly_name"] == "Front door Last entry"


async def test_existing_entity_ids_are_kept(hass: HomeAssistant, aioclient_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1",
                            version=1, minor_version=5)
    entry.add_to_hass(hass)
    entreg = er.async_get(hass)
    old = entreg.async_get_or_create("event", DOMAIN, f"{entry.entry_id}_lock_s1_l1_last_entry",
                                     config_entry=entry, suggested_object_id="front_door_last_entry")
    _api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert old.entity_id == "event.front_door_last_entry"
    assert entreg.async_get(old.entity_id) is not None
    assert hass.states.get("event.hq_amsterdam_front_door_last_entry") is None


async def test_new_site_after_switch_gets_site_entity_ids(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock, current_request_with_host
) -> None:
    _api_account(aioclient_mock, ME, {"s1": ["l1"]})
    entry = await _setup(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    aioclient_mock.clear_requests()
    await _browser_sign_in(
        hass, hass_client_no_auth, aioclient_mock, result,
        api=lambda: _api_account(aioclient_mock, ME_U2, {"s1": ["l1"], "s2": ["l3"]}))
    await hass.async_block_till_done()
    assert hass.states.get("lock.site_s2_lock_l3").state == "locked"
    assert hass.states.get("event.site_s2_lock_l3_last_entry") is not None
    assert hass.states.get("lock.site_s1_lock_l1").state == "locked"


def test_entity_names_match_translations() -> None:
    from custom_components.saltoks.entity import ENTITY_NAMES

    strings = json.loads(
        (Path(__file__).parent.parent / "custom_components/saltoks/strings.json").read_text())
    translated = {key: value["name"] for platform in strings["entity"].values()
                  for key, value in platform.items()}
    for key, name in ENTITY_NAMES.items():
        assert translated[key] == name
