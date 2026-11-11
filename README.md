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

## Devices

Account (main device) → one device per site (named after `customer_reference`) → one device per lock (named after the lock's `customer_reference`). Each lock device holds the lock entity, the Easy Office Mode controls and the lock's metadata entities.

## Easy Office Mode

Easy Office Mode is a schedule per lock: on the enabled weekdays between the start and end date, the lock may stay unlocked during the daily window and is locked automatically by the gateway at the end time. With *auto start* enabled, the lock is also unlocked automatically when the window starts. Times and dates are local time.

Each lock device has these controls (configuration category):

| Control | Type |
|---|---|
| Easy Office Mode auto start | switch |
| Easy Office Mode Monday … Sunday | switch |
| Easy Office Mode start time / end time | time |
| Easy Office Mode start date / end date | date |
| Remove Easy Office Mode schedule | button |

- Changing a control sends the complete schedule. Changes to the same lock are sent one at a time; afterwards the lock is read back so the controls show what was stored.
- Dates are sent as `YYYY-MM-DDT00:00:00.000` (start) and `YYYY-MM-DDT23:59:59.999` (end), so the last day is included.
- The end time must be later than the start time, and the end date must not be before the start date; otherwise the change is refused with an error.
- A lock without a schedule shows all switches off and the times and dates as unknown. The first change creates a schedule with defaults for the other fields: auto start and all days off, 09:00–17:00, and today as start and end date.
- *Remove Easy Office Mode schedule* deletes the schedule. The button is unavailable while the lock has no schedule. Home Assistant does not ask for confirmation when a button is pressed.

### Action: `saltoks.set_easy_office_mode_schedule`

Changes several fields at once, for one or more locks (target lock entities, devices or areas). Fields that are left out keep their current value; at least one field is required. `days` lists the enabled weekdays, days not listed are disabled.

```yaml
action: saltoks.set_easy_office_mode_schedule
target:
  entity_id:
    - lock.front_door
    - lock.back_door
data:
  auto_start: true
  days: [monday, tuesday, wednesday, thursday, friday]
  start_time: "08:00"
  end_time: "17:30"
  start_date: "2026-01-01"
  end_date: "2026-12-31"
```

### Action: `saltoks.remove_easy_office_mode_schedule`

Removes the schedule of one or more locks. Locks without a schedule are skipped.

```yaml
action: saltoks.remove_easy_office_mode_schedule
target:
  entity_id:
    - lock.front_door
    - lock.back_door
```

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

```yaml
alias: Lock notifier
description: ''
triggers:
  - trigger: state
    entity_id:
      - lock.front_door
    to:
      - locked
      - unlocked
    from:
      - locking
      - unlocking
conditions: []
actions:
  - choose:
      - conditions:
          - condition: state
            entity_id: lock.front_door
            state:
              - locked
        sequence:
          - metadata: {}
            data:
              title: State of Salto KS lock changed
              message: Front door is now locked
            action: notify.my_mobile
      - conditions:
          - condition: state
            entity_id: lock.front_door
            state:
              - unlocked
        sequence:
          - metadata: {}
            data:
              title: State of Salto KS lock changed
              message: Front door is now unlocked
            action: notify.my_mobile
mode: single
```

## Please note

**Rate limits**: each poll makes 2 + (number of sites) requests (lock lists are fetched in parallel, up to 5 at a time). Increase the interval if Salto KS throttles the account.

## Tests

```bash
pip install pytest-homeassistant-custom-component
pytest
```

