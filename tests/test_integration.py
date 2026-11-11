"""End-to-end tests with mocked Salto KS / Clay endpoints."""

from __future__ import annotations

from datetime import timedelta
import json

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

ENTRY_DATA = {
    "username": "user@example.com",
    "password": "pw",
    "client_id": "my-client",
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


def _token(aioclient_mock, status=200, access="tok1"):
    body = ({"access_token": access, "refresh_token": "r1", "expires_in": 3600}
            if status == 200 else {"error": "invalid_grant"})
    aioclient_mock.post(TOKEN_URL, status=status, json=body)


def _api(aioclient_mock, locks=None, me=None):
    aioclient_mock.get(f"{API_BASE_URL}/me", json=me or ME)
    aioclient_mock.get(f"{API_BASE_URL}/sites", json=SITES)
    aioclient_mock.get(f"{API_BASE_URL}/sites/s1/locks", json=locks or LOCKS)


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_devices_and_entities(hass: HomeAssistant, aioclient_mock) -> None:
    _token(aioclient_mock)
    _api(aioclient_mock)
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    # Token request: password grant with Basic client auth.
    method, url, data, headers = aioclient_mock.mock_calls[0]
    assert str(url) == TOKEN_URL
    assert data["grant_type"] == "password"
    assert headers["Authorization"].startswith("Basic ")

    devreg = dr.async_get(hass)
    account = devreg.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
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

    image_id = entreg.async_get_entity_id("image", DOMAIN, f"{entry.entry_id}_profile_image")
    assert entreg.async_get(image_id).device_id == account.id
    assert hass.states.get(image_id).state != "unavailable"

    # Metadata entities.
    def uid(x):
        return f"{entry.entry_id}_{x}"
    lang = entreg.async_get_entity_id("sensor", DOMAIN, uid("me__language"))
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
    _token(aioclient_mock)
    _api(aioclient_mock)
    entry = await _setup(hass)
    lock_id = er.async_get(hass).async_get_entity_id(
        "lock", DOMAIN, f"{entry.entry_id}_lock_s1_l1")

    patch_url = f"{API_BASE_URL}/sites/s1/locks/l1/locking"
    aioclient_mock.patch(patch_url, json={})
    # After unlocking, the API reports office_mode and a brand-new field.
    unlocked = [{**LOCKS[0], "locked_state": "office_mode", "firmware": "1.2.3"}]
    aioclient_mock.clear_requests()
    _token(aioclient_mock)
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


async def test_invalid_credentials_start_reauth(hass: HomeAssistant, aioclient_mock) -> None:
    _token(aioclient_mock, status=400)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_client_token_lifecycle(hass: HomeAssistant, aioclient_mock) -> None:
    """Refresh grant on expiry, password grant when refresh is rejected, retry on 401."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from custom_components.saltoks.api import SaltoKSClient

    client = SaltoKSClient(async_get_clientsession(hass), "u", "p", "c")
    _token(aioclient_mock)
    await client.async_get_access_token()
    assert aioclient_mock.mock_calls[-1][2]["grant_type"] == "password"

    client._expires_at = 0  # simulate expiry
    await client.async_get_access_token()
    assert aioclient_mock.mock_calls[-1][2]["grant_type"] == "refresh_token"

    # Refresh rejected -> password grant.
    aioclient_mock.clear_requests()
    calls = []

    async def token_handler(method, url, data):
        calls.append(data["grant_type"])
        from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse
        if data["grant_type"] == "refresh_token":
            return AiohttpClientMockResponse(method, url, status=400,
                                             text=json.dumps({"error": "invalid_grant"}))
        return AiohttpClientMockResponse(method, url, status=200, text=json.dumps(
            {"access_token": "tok2", "refresh_token": "r2", "expires_in": 3600}))

    aioclient_mock.post(TOKEN_URL, side_effect=token_handler)
    client._expires_at = 0
    assert await client.async_get_access_token() == "tok2"
    assert calls == ["refresh_token", "password"]


def test_basic_auth_formats() -> None:
    from custom_components.saltoks.api import SaltoKSClient

    # The client ID is sent as-is, matching the manual curl request.
    assert SaltoKSClient(None, "u", "p", "abc123==")._basic_auth == "Basic abc123=="
    assert SaltoKSClient(None, "u", "p", " Basic abc123== ")._basic_auth == "Basic abc123=="


async def test_token_request_matches_manual_call(hass: HomeAssistant, aioclient_mock) -> None:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from custom_components.saltoks.api import SaltoKSClient

    _token(aioclient_mock)
    client = SaltoKSClient(async_get_clientsession(hass), "a@b.c", "pw", "CLIENTID")
    await client.async_authenticate()
    _, url, data, headers = aioclient_mock.mock_calls[-1]
    assert headers["Authorization"] == "Basic CLIENTID"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert data == {"grant_type": "password", "username": "a@b.c", "password": "pw"}


async def test_config_flow_shows_server_error(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(TOKEN_URL, status=400,
                        json={"error": "invalid_client", "error_description": "unknown client"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["errors"] == {"base": "invalid_client"}
    assert result["description_placeholders"]["server_message"] == "invalid_client: unknown client"


async def test_picture_urls_are_images_not_sensors(hass: HomeAssistant, aioclient_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id="u1")
    entry.add_to_hass(hass)
    entreg = er.async_get(hass)
    # Sensors created for picture URLs by an earlier version must be removed.
    for uid in ("me__image_url", "site_s1__owner.image_url"):
        entreg.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_{uid}",
                                   config_entry=entry)
    _token(aioclient_mock)
    _api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for uid in ("me__image_url", "site_s1__owner.image_url"):
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
        "image", DOMAIN, f"{entry.entry_id}_profile_image") is not None
    assert entreg.async_get_entity_id(
        "image", DOMAIN, f"{entry.entry_id}_me__image_url") is None

    # A changed URL updates the picture timestamp.
    before = state.state
    new_sites = {"items": [{**SITES["items"][0],
                            "owner": {"first_name": "Owner", "image_url": "https://img.example.com/o2.png"}}]}
    aioclient_mock.clear_requests()
    _token(aioclient_mock)
    aioclient_mock.get(f"{API_BASE_URL}/me", json=ME)
    aioclient_mock.get(f"{API_BASE_URL}/sites", json=new_sites)
    aioclient_mock.get(f"{API_BASE_URL}/sites/s1/locks", json=LOCKS)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(owner_img).state != before


async def test_id_entities_disabled_by_default(hass: HomeAssistant, aioclient_mock) -> None:
    _token(aioclient_mock)
    _api(aioclient_mock)
    entry = await _setup(hass)
    entreg = er.async_get(hass)

    def reg(uid):
        return entreg.async_get(entreg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{uid}"))

    for uid in ("me__id", "site_s1__id", "lock_s1_l1__id"):
        assert reg(uid).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert reg(uid).hidden_by is None
    for uid in ("me__language", "site_s1__timezone", "site_s1__owner.first_name"):
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
    _token(aioclient_mock)
    _api(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.minor_version == 3
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

    _token(aioclient_mock)
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
    _token(aioclient_mock)
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
    _token(aioclient_mock)
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
    _token(aioclient_mock)
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


async def test_setup_form_fields(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert {str(k) for k in result["data_schema"].schema} == {"username", "password", "client_id"}



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
    _token(aioclient_mock)
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
    _token(aioclient_mock)
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
    _token(aioclient_mock)
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
