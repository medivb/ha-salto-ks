"""Base entities, device helpers and dynamic metadata discovery for Salto KS."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MAX_FLATTEN_DEPTH
from . import eom
from .api import SaltoKSError
from .coordinator import SaltoKSCoordinator, SaltoKSData

SCOPE_ME = "me"
SCOPE_SITE = "site"
SCOPE_LOCK = "lock"

KIND_SENSOR = "sensor"
KIND_BINARY = "binary_sensor"
KIND_IMAGE = "image"

# Fields with these names hold a picture URL and become image entities.
IMAGE_FIELDS = frozenset(
    {"image_url", "picture_url", "avatar_url", "photo_url", "profile_image_url"}
)


def is_image_field(path: tuple[str, ...]) -> bool:
    return bool(path) and path[-1].lower() in IMAGE_FIELDS


# Lock fields exposed as controls instead of read-only metadata entities.
SUPERSEDED_LOCK_FIELDS = frozenset({"easy_office_mode_schedule"})


def is_superseded_field(path: tuple[str, ...]) -> bool:
    return bool(path) and path[0] in SUPERSEDED_LOCK_FIELDS


def is_id_field(path: tuple[str, ...]) -> bool:
    """True for identifier fields such as id, site_id, owner.id or customerId."""
    if not path:
        return False
    leaf = path[-1]
    lower = leaf.lower()
    return (
        lower in {"id", "uuid", "guid"}
        or lower.endswith(("_id", "_uuid", "_guid"))
        or leaf.endswith("Id")
    )


# ----------------------------------------------------------------- naming


def site_name(site: dict[str, Any] | None, site_id: str) -> str:
    site = site or {}
    return str(site.get("customer_reference") or site.get("name") or f"Site {site_id}")


def lock_name(lock: dict[str, Any] | None, lock_id: str) -> str:
    lock = lock or {}
    return str(lock.get("customer_reference") or lock.get("name") or f"Lock {lock_id}")


def account_name(me: dict[str, Any]) -> str:
    full = " ".join(
        str(me[k]) for k in ("first_name", "last_name") if me.get(k)
    ).strip()
    return full or str(me.get("email") or me.get("username") or "Salto KS account")


def humanize(path: tuple[str, ...]) -> str:
    text = " ".join(path).replace("_", " ").replace(".", " ").strip()
    return text[:1].upper() + text[1:] if text else "Value"


# ---------------------------------------------------------------- devices


def account_device_info(coordinator: SaltoKSCoordinator) -> DeviceInfo:
    me = coordinator.data.me if coordinator.data else {}
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
        name=account_name(me),
        manufacturer=MANUFACTURER,
        model="Salto KS account",
    )


def site_device_info(coordinator: SaltoKSCoordinator, site_id: str) -> DeviceInfo:
    site = coordinator.data.sites.get(site_id) if coordinator.data else None
    return DeviceInfo(
        identifiers={(DOMAIN, f"site_{site_id}")},
        name=site_name(site, site_id),
        manufacturer=MANUFACTURER,
        model="Salto KS site",
        via_device=(DOMAIN, coordinator.config_entry.entry_id),
    )


def lock_device_info(coordinator: SaltoKSCoordinator, site_id: str, lock_id: str) -> DeviceInfo:
    lock = coordinator.data.locks.get(site_id, {}).get(lock_id) if coordinator.data else None
    return DeviceInfo(
        identifiers={(DOMAIN, f"lock_{site_id}_{lock_id}")},
        name=lock_name(lock, lock_id),
        manufacturer=MANUFACTURER,
        model="Salto KS lock",
        via_device=(DOMAIN, f"site_{site_id}"),
    )


# ------------------------------------------------------------- base class


class SaltoKSEntity(CoordinatorEntity[SaltoKSCoordinator]):
    """Base entity bound to the account, a site or a lock device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SaltoKSCoordinator,
        site_id: str | None = None,
        lock_id: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._site_id = site_id
        self._lock_id = lock_id
        if site_id is not None and lock_id is not None:
            self._attr_device_info = lock_device_info(coordinator, site_id, lock_id)
        elif site_id is not None:
            self._attr_device_info = site_device_info(coordinator, site_id)
        else:
            self._attr_device_info = account_device_info(coordinator)

    @property
    def site(self) -> dict[str, Any] | None:
        if self._site_id is None or not self.coordinator.data:
            return None
        return self.coordinator.data.sites.get(self._site_id)

    @property
    def lock_data(self) -> dict[str, Any] | None:
        if self._site_id is None or self._lock_id is None or not self.coordinator.data:
            return None
        return self.coordinator.data.locks.get(self._site_id, {}).get(self._lock_id)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._site_id is None:
            return True
        if self.site is None or self._site_id in self.coordinator.data.stale_sites:
            return False
        return self._lock_id is None or self.lock_data is not None


# --------------------------------------------------------------- metadata


@dataclass(frozen=True)
class MetadataKey:
    """Identifies a single metadata field of the account, a site or a lock."""

    scope: str
    path: tuple[str, ...]
    site_id: str | None = None
    lock_id: str | None = None

    @property
    def uid(self) -> str:
        if self.scope == SCOPE_ME:
            owner = "me"
        elif self.scope == SCOPE_SITE:
            owner = f"site_{self.site_id}"
        else:
            owner = f"lock_{self.site_id}_{self.lock_id}"
        return f"{owner}__{'.'.join(self.path)}"


def flatten(data: dict[str, Any], prefix: tuple[str, ...] = (), depth: int = 0
            ) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Yield (path, value) for every leaf. Lists and objects deeper than the
    depth limit are leaves."""
    for key, value in data.items():
        path = (*prefix, str(key))
        if isinstance(value, dict) and value and depth < MAX_FLATTEN_DEPTH:
            yield from flatten(value, path, depth + 1)
        else:
            yield path, value


def resolve_path(obj: dict[str, Any] | None, path: tuple[str, ...]) -> tuple[bool, Any]:
    """Return (found, value) for a flattened path."""
    current: Any = obj
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def source_object(data: SaltoKSData, key: MetadataKey) -> dict[str, Any] | None:
    if key.scope == SCOPE_ME:
        return data.me
    if key.scope == SCOPE_SITE:
        return data.sites.get(key.site_id or "")
    return data.locks.get(key.site_id or "", {}).get(key.lock_id or "")


def iter_metadata(data: SaltoKSData) -> Iterator[tuple[MetadataKey, str]]:
    """Yield every metadata field currently present with the platform kind."""

    def _kind(path: tuple[str, ...], value: Any) -> str:
        if is_image_field(path) and (value is None or isinstance(value, str)):
            return KIND_IMAGE
        return KIND_BINARY if isinstance(value, bool) else KIND_SENSOR

    for path, value in flatten(data.me):
        yield MetadataKey(SCOPE_ME, path), _kind(path, value)
    for site_id, site in data.sites.items():
        for path, value in flatten(site):
            yield MetadataKey(SCOPE_SITE, path, site_id), _kind(path, value)
        for lock_id, lock in data.locks.get(site_id, {}).items():
            for path, value in flatten(lock):
                if is_superseded_field(path):
                    continue
                yield MetadataKey(SCOPE_LOCK, path, site_id, lock_id), _kind(path, value)


class SaltoKSMetadataEntity(SaltoKSEntity):
    """Common logic for auto-discovered metadata entities."""

    _attr_should_poll = False

    def __init__(self, coordinator: SaltoKSCoordinator, key: MetadataKey) -> None:
        super().__init__(coordinator, key.site_id, key.lock_id)
        self.key = key
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key.uid}"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        # Identifier fields are disabled by default; users can enable them.
        if is_id_field(key.path):
            self._attr_entity_registry_enabled_default = False

    @property
    def name(self) -> str:
        return humanize(self.key.path)

    @property
    def raw_value(self) -> tuple[bool, Any]:
        if not self.coordinator.data:
            return False, None
        return resolve_path(source_object(self.coordinator.data, self.key), self.key.path)

    @property
    def available(self) -> bool:
        return super().available and self.raw_value[0]


def setup_metadata_platform(
    coordinator: SaltoKSCoordinator,
    kind: str,
    factory: Callable[[SaltoKSCoordinator, MetadataKey], Entity],
    async_add_entities: Callable[[list[Entity]], None],
) -> Callable[[], None]:
    """Add metadata entities now and whenever new fields/objects appear."""

    def _discover() -> None:
        if not coordinator.data:
            return
        new = []
        for key, value_kind in iter_metadata(coordinator.data):
            if value_kind != kind or key.uid in coordinator.claimed_metadata:
                continue
            coordinator.claimed_metadata.add(key.uid)
            new.append(factory(coordinator, key))
        if new:
            async_add_entities(new)

    _discover()
    return coordinator.async_add_listener(_discover)


# --------------------------------------------------------- per-lock controls


def setup_per_lock_platform(
    coordinator: SaltoKSCoordinator,
    build: Callable[[SaltoKSCoordinator, str, str], list[Entity]],
    async_add_entities: Callable[[list[Entity]], None],
) -> Callable[[], None]:
    """Create entities for every lock now and for locks that appear later."""
    known: set[tuple[str, str]] = set()

    def _discover() -> None:
        if not coordinator.data:
            return
        new: list[Entity] = []
        for site_id, locks in coordinator.data.locks.items():
            for lock_id in locks:
                if (site_id, lock_id) not in known:
                    known.add((site_id, lock_id))
                    new.extend(build(coordinator, site_id, lock_id))
        if new:
            async_add_entities(new)

    _discover()
    return coordinator.async_add_listener(_discover)


class SaltoKSEomEntity(SaltoKSEntity):
    """One field of a lock's Easy Office Mode schedule."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: SaltoKSCoordinator, site_id: str, lock_id: str, field: str
    ) -> None:
        super().__init__(coordinator, site_id, lock_id)
        self.field = field
        self._attr_translation_key = f"eom_{field.removeprefix('eom_')}"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_lock_{site_id}_{lock_id}_eom_{field}"
        )

    @property
    def eom_value(self) -> Any:
        return eom.to_values(eom.current_schedule(self.lock_data))[self.field]

    async def _async_apply(self, value: Any) -> None:
        try:
            await self.coordinator.async_set_eom_schedule(
                self._site_id or "", self._lock_id or "", {self.field: value}
            )
        except SaltoKSError as err:
            raise HomeAssistantError(
                f"Could not update the Easy Office Mode schedule: {err}"
            ) from err
