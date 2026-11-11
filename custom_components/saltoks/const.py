"""Constants for the Salto KS integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "saltoks"

TOKEN_URL: Final = "https://identity.eu.my-clay.com/connect/token"
API_BASE_URL: Final = "https://connect.saltoks.com/v1.2"

CONF_CLIENT_ID: Final = "client_id"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_INCLUDE_USER_DETAILS: Final = "include_user_details"

# Entries (access events) are only available in API version 1.1.
ENTRIES_URL: Final = "https://connect.saltoks.com/v1.1/sites/{site_id}/entries"
# Newest entries requested per site per poll; new ones are found by id.
ENTRIES_PAGE_SIZE: Final = 50
# Entry fields that identify a person; only kept when the user opts in.
ENTRY_USER_FIELDS: Final = frozenset(
    {"user_id", "user_first_name", "user_last_name", "user_alias", "user_image_url"}
)

DEFAULT_SCAN_INTERVAL: Final = 10
MIN_SCAN_INTERVAL: Final = 5
MAX_SCAN_INTERVAL: Final = 300

# Lock states as used by the Salto KS API.
STATE_LOCKED: Final = "locked"
STATE_OFFICE_MODE: Final = "office_mode"

# After a locking request, poll the lock this often (seconds) until it reports
# the requested state, for at most LOCK_CONFIRM_TIMEOUT seconds.
LOCK_CONFIRM_INTERVAL: Final = 1.0
LOCK_CONFIRM_TIMEOUT: Final = 10.0

# Refresh the access token this many seconds before it expires.
TOKEN_EXPIRY_MARGIN: Final = 60

# Maximum number of concurrent lock-list requests per update cycle.
MAX_CONCURRENT_REQUESTS: Final = 5

# Nested metadata objects are flattened up to this depth.
MAX_FLATTEN_DEPTH: Final = 3

MANUFACTURER: Final = "SALTO Systems"

