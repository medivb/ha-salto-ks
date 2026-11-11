# Salto KS Custom Integration for Home Assistant

This integration polls the Salto KS cloud API (configurable, default every 10 seconds) and exposes all information about your accessible sites you are allowed to see with the account you are using and the locks within those sites you have access to.

The locks are controlled via a feature in Salto KS called "remote opening". The account used for this integration must have the appropriate permission to perform remote openings. Since Salto KS is an always online real time cloud locking solution, this usually works pretty fast. However, in case there is an issue with connectivity, the operation might not work. If the unlock command could not be delivered, it will expire and become invalid within a few minutes. The integration will keep polling for lock state changes, so as soon as connectivity is restored, the correct state will be reflected.

## Installation

1. HACS (or settings → marketplace) → three dots → *Custom repositories* → add `https://github.com/medivb/ha-salto-ks` as type *Integration*, then install **Salto KS**. Or copy `custom_components/saltoks` into your Home Assistant `config/custom_components/` folder.
2. Restart Home Assistant.
3. *Settings → Devices & services → Add integration → Salto KS*.
4. Enter username, password and client ID.

Update interval and other options: *Configure* on the integration (update interval 5–300 seconds, default 10).

## Sign-in

- Use the username and password you normally use for the Salto KS web app (https://app.saltoks.com), together with the client ID you received for this integration.
- The integration stays signed in on its own. If your credentials stop working (for example after a password change), Home Assistant asks you to sign in again.
- Your session is kept in memory only.

## Devices

The integration creates:

- one device for your account, with your profile picture and account details;
- one device per site, named after the site;
- one device per lock, named after the lock and linked to its site.

Each lock device holds the lock itself, its Easy Office Mode settings, its last entry and the lock's details.

## Locks

- Locking or unlocking a lock shows it as *locking* / *unlocking* until Salto KS confirms the new state, usually within a few seconds.
- If the new state is not confirmed within 10 seconds, the lock shows the state reported by Salto KS and a warning is written to the log. The regular updates continue as usual.

## Easy Office Mode

Easy Office Mode is a schedule per lock. On the selected weekdays, between the start and end date, the lock can stay unlocked during the daily time window and is locked automatically at the end time. With *auto start* on, the lock is also unlocked automatically when the window starts. All times and dates are local time.

Each lock device has these settings:

| Setting | What it does |
|---|---|
| Easy Office Mode auto start | Unlock automatically when the window starts |
| Easy Office Mode Monday … Sunday | The days on which the schedule applies |
| Easy Office Mode start time / end time | The daily time window |
| Easy Office Mode start date / end date | The period in which the schedule applies; the end date is included |
| Remove Easy Office Mode schedule | Removes the schedule from the lock |

- Changes are saved in Salto KS straight away.
- The end time must be later than the start time, and the end date may not be before the start date.
- The end date is optional. Without an end date the schedule applies indefinitely; the end date then shows as *unknown*. To remove an end date, use the action below.
- A lock without a schedule shows all days off and no times or dates. The first change you make creates a schedule with: auto start and all days off, 09:00–17:00, today as start date and no end date.
- The remove button is only available while the lock has a schedule. Home Assistant does not ask for confirmation when you press it.

### Action: Set Easy Office Mode schedule

Changes several settings at once, for one or more locks. Settings you leave out stay as they are. `days` lists the days on which the schedule applies; other days are switched off. Use `end_date: null` to remove the end date.

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

### Action: Remove Easy Office Mode schedule

Removes the schedule from one or more locks.

```yaml
action: saltoks.remove_easy_office_mode_schedule
target:
  entity_id:
    - lock.front_door
    - lock.back_door
```

## Last entry

Each lock device has a *Last entry* event. It shows when the lock was last used and what happened (for example an opening or a denied attempt), with details such as how access was given. Every new entry is recorded in the history and logbook of Home Assistant and can be used to trigger automations.

- Only entries that happen while Home Assistant is running are reported. After the integration is first added, each lock shows its most recent entry.
- By default, entries do **not** include who used the lock. To include the person's name, alias and picture, turn on *Include user details in entries* under *Configure* on the integration. This is personal data and is stored in your Home Assistant history; only turn it on if you are allowed to keep this information.

Example: notify when the front door is opened.

```yaml
triggers:
  - trigger: state
    entity_id: event.front_door_last_entry
    not_from: unavailable
conditions:
  - condition: state
    entity_id: event.front_door_last_entry
    attribute: event_type
    state: opening
actions:
  - action: notify.my_mobile
    data:
      message: The front door was opened.
```

The event types depend on what Salto KS reports and are added as they occur; you can see them in the `event_types` attribute of the entity.

## Details per account, site and lock

All other information Salto KS provides about your account, sites and locks is shown as entities in the *Diagnostic* section of each device. New information is added automatically when Salto KS starts providing it. Pictures are shown as image entities.

Technical identifiers are disabled by default. To use one, open the entity settings and turn on *Enabled*.

*Download diagnostics* on the integration gives all collected information, with credentials and personal data removed.

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

Every update contacts Salto KS a few times per site. With many sites, or if Salto KS limits the number of requests for your account, increase the update interval under *Configure*.

## Tests

```bash
pip install pytest-homeassistant-custom-component
pytest
```

