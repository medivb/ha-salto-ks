# Salto KS Custom Integration for Home Assistant

This integration polls the Salto KS cloud API (configurable, default every 10 seconds) and exposes all information about your accessible sites you are allowed to see with the account you are using and the locks within those sites you have access to.

The locks are controlled via a feature in Salto KS called "remote opening". The account used for this integration must have the appropriate permission to perform remote openings. Since Salto KS is an always online real time cloud locking solution, this usually works pretty fast. However, in case there is an issue with connectivity, the operation might not work. If the unlock command could not be delivered, it will expire and become invalid within a few minutes. The integration will keep polling for lock state changes, so as soon as connectivity is restored, the correct state will be reflected.

## Installation

1. HACS (or settings → marketplace) → three dots → *Custom repositories* → add `https://github.com/medivb/ha-salto-ks` as type *Integration*, then install **Salto KS**. Or copy `custom_components/saltoks` into your Home Assistant `config/custom_components/` folder.
2. Restart Home Assistant.
3. *Settings → Devices & services → Add integration → Salto KS*.
4. Enter username, password and client ID.

Polling interval: *Configure* on the integration (5–300 s, default 10 s).

## Authentication

- Specify a username and password you would normally use to access the Salto KS web frontend (https://app.saltoks.com) and request a client id for Basic username/password authentication.
- The access token is reused until 60 s before expiry, then renewed with `grant_type=refresh_token` when the server issued a refresh token; otherwise a new password grant is performed. Because the integration typically polls every 10 s, the session is kept alive continuously.
- If refreshing fails, the integration authenticates again with the stored username and password. A `401` from the API forces a token renewal and one retry.
- If the identity server rejects the credentials (`invalid_grant`, `invalid_client`, …), Home Assistant starts a re-authentication flow.
- Tokens are held in memory only; they are not written to disk.

## Locks

After a lock or unlock request, the integration tries to refresh the state of that particular lock every second until the lock reports the requested `locked_state`, for at most 10 seconds. Meanwhile the lock is shown as *locking* / *unlocking*, and each response is applied immediately. If the state is not confirmed within 10 seconds, a warning is logged and the entity shows the state the API reports; the regular polling continues as usual.

State: `locked_state == "locked"` → *locked*; any other value → *unlocked*; missing → *unknown*. The raw value is available as the `locked_state` attribute.

## Metadata entities (auto-discovered)

Every field returned by Salto KS becomes an entity (diagnostic category):

- picture URL fields → `image`, named after the parent object (for example "Owner picture"); no sensor is created for them
- booleans → `binary_sensor`
- strings / numbers → `sensor` (ISO date-time fields named `*_at`, `*date*`, `*time*` … get the timestamp device class)
- lists → `sensor` with the item count as state; full list in the `items` attribute
- nested objects are flattened up to 3 levels (`address.city` → "Address city")
- strings longer than 255 characters are truncated in the state; the full value is in the `value` attribute

Identifier fields (`id`, `*_id`, `*Id`, `uuid`, for example `owner.id`) are **disabled by default**. To use one, open the entity settings and turn on *Enabled*.

New fields, sites or locks that Salto KS returns later are added automatically at the next poll; no update of the integration is needed. Entities for fields that disappear become *unavailable*.

Diagnostics (*Download diagnostics*) contain all raw API data, with credentials and personal data redacted.

## Notifications

In order to be notified when one of the locks changes state, a simple automation can be created: 
~~~
alias: {your automation name}
description: ''
triggers:
  - trigger: state
    entity_id:
      - lock.{lock name}
    to:
      - locked
      - unlocked
    from:
      - unlocking
      - locking
conditions: []
actions:
  - choose:
      - conditions:
          - condition: state
            entity_id: lock.{lock name}
            state:
              - locked
        sequence:
          - metadata: {}
            data:
              title: State of Salto KS lock changed
              message: Door is now locked
            action: notify.{notify target}
      - conditions:
          - condition: state
            entity_id: lock.{lock name}
            state:
              - unlocked
        sequence:
          - metadata: {}
            data:
              title: State of Salto KS lock changed
              message: Door is now unlocked
            action: notify.{notify target}
mode: single
~~~
## Please note

**Rate limits**: each poll makes 2 + (number of sites) requests (lock lists are fetched in parallel, up to 5 at a time). Increase the interval if Salto KS throttles the account.

## Tests

```bash
pip install pytest-homeassistant-custom-component
pytest
```

