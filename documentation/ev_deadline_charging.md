# EV Deadline Charging

Charge an EV to a target state of charge (SOC) by a deadline you set from a Home Assistant
dashboard card, using the cheapest available electricity price slots up until that deadline.

This is one of the two EV charging modes (the other being `solar_charge_only`) — see
[Optional – EV Only](device_configuration.md#optional--ev-only) for how the two relate. A device
with `ev_deadline_charge_enabled: true` is scheduled solely to meet the target SOC by the
deadline.

## How It Works

1. **Slots are only (re)selected twice a day, or on demand**: at the daily 16:05 optimization,
   and whenever you press the **Recalculate now!** dashboard button (or change the "Charge by"
   day/time controls, which presses it for you). Each of those reads three live values from
   Home Assistant — the EV's current SOC, your target SOC, and your "charge by" deadline.
2. It computes the energy needed: `ev_battery_capacity_kwh × (target_soc − current_soc) / 100`.
3. It sorts all price slots between now and the deadline from cheapest to most expensive, and
   selects slots — cheapest first — until the selected slots can deliver that much energy at
   `ev_deadline_charge_power_kw`.
4. If the deadline is further out than the currently known ENTSO-E price horizon (day-ahead
   prices for tomorrow aren't published until roughly 12:00–13:00 CET), it plans against the
   slots it knows about; the plan is only extended with more slots the next time it is
   recalculated (16:05, or the button) once tomorrow's prices are in.
5. If even every remaining slot up to the deadline isn't enough energy, it charges in every
   remaining slot regardless of price and logs a warning — the deadline is prioritized over cost
   in that case.
6. **In between those two triggers, the plan is fixed.** Every 15 minutes the add-on re-checks
   whether the car has become ready to charge (see below) so a plugged-in car doesn't have to
   wait for the next full recalculation, but it does *not* re-run slot selection — so a car
   charging slower than predicted (SOC falling behind the plan) will not cause extra slots to be
   added automatically; that only happens the next time you press "Recalculate now" or at 16:05.
   The slot currently in progress is likewise never retroactively cancelled, even across a full
   recalculation — charging is not yanked away mid-slot. Slots that have already finished drop
   out of the plan (they can no longer trigger anything), so a slot picked under a
   momentarily-wrong input doesn't linger on the Gantt.

Charging is still subject to load management: the deadline plan only decides which slots the
charger is "on" in. The actual current/power delivered in an "on" slot is governed independently
by load management (`enable_load_management`) and by any other device competing for available
grid headroom.

## Only Scheduled When the Car Is Ready

The plan is always *computed*, but it is only *scheduled* when the EV is ready to charge
according to its `ev_ready_to_charge_condition` (the same condition that overrides price-based
grid-export blocking — see [Device Configuration](device_configuration.md)). Typically that
condition checks the charger reports the car as connected.

- **Car ready** → the plan goes into the live schedule: start/stop actions are created, the
  slots appear on the Gantt chart, and the planned energy counts as usage (see below).
- **Car not ready** (unplugged, or any entity in the condition is `unknown`/`unavailable`) →
  nothing is scheduled. Your target SOC and deadline are still read and taken into account: the
  provisional plan is shown in the web UI info box and exposed to Home Assistant so the dashboard
  can say what *would* be charged. The moment the car becomes ready (checked every 15 minutes, or
  instantly via the "Recalculate now" button) the plan is scheduled. Only a slot that is already
  in progress stays in the schedule so its stop action still fires.
- **No condition configured** → the plan is scheduled unconditionally, as before.

Held-back slots are deliberately **not** drawn on the Gantt chart, which shows only what is
actually scheduled; the "EV Deadline Charging" info box above it shows the full plan either way.

## Charging Now vs. Ready to Charge

"Ready to charge" and "charging" are two different questions, and the UI answers both:

- **Ready** (`ev_ready_to_charge_condition`) is about *permission*: may the plan be scheduled,
  and should grid export be force-unblocked? It's about the car being plugged in and below its
  target SOC.
- **Charging** (`ev_charging_condition`) is about *observation*: is the charger delivering power
  right now, whichever mode or automation started the session?

A car that is already charging is never announced as merely "ready to charge" — charging wins
the badge, the headline and the card, since "ready to charge" reads as "not charging yet".

Charging is detected in exactly one place, shared with the
[battery-discharge guard](device_configuration.md#battery-discharge-guard):

1. `ev_charging_condition`, when you configured one — for a charger whose status entity is a
   better signal than its power meter (e.g. a state of `charging`).
2. Otherwise `load_management.instantaneous_load_entity`, compared against the load watcher's
   `load_watcher_threshold_power` using the device's own `charge_sign` convention.

Either way the charger's live load (in W, sign-normalised so positive always means "drawing
power to charge") is read from `instantaneous_load_entity` and reported alongside the verdict,
so the UI and the card can show how fast the car is charging. An entity that is
`unknown`/`unavailable` counts as **not** charging.

The charging state is refreshed on the load-watcher interval (`load_watcher_interval_minutes`,
default 5), not only on the 15-minute plan recalculation, so the badge tracks a session that
starts between plan runs. It is stored in the same `ev_deadline` doc as the plan and served by
`/api/ev_deadline` as `charging`, `charging_power_w`, `charging_power_kw`, `charging_source`
(`condition` / `power` / `unavailable`), `charging_condition_configured` and
`charging_load_entity`. `headline` carries the combined line ("Charging now – 7.2 kW · On hold:
6 slot(s) ready to schedule"); `plan_headline` is the plan half on its own.

## Interaction With Battery Solar-Only Mode

The battery's solar-only decision compares predicted solar production with predicted usage.
When an EV deadline plan is scheduled (car ready), its energy is added to the predicted usage,
so a large planned charge stops the battery from switching to solar-only on a day the car will
eat the surplus. Provisional (held-back) plans are not counted. The numbers used are shown in
the web UI ("Solar-only check: solar … vs usage … + scheduled EV charge …") and logged at every
recalculation. The EV plan is always re-evaluated *before* the battery limits — on the daily
optimization, on the manual trigger, and on every 15-minute cycle (readiness gating only, see
above) — so the decision always sees the current plan.

## Configuration

Add these fields to the EV device entry in `/data/options.json`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `ev_deadline_charge_enabled` | boolean | `false` | Enables this mode. Mutually exclusive with `solar_charge_only` — setting both raises a config error. |
| `ev_soc_entity` | string | `null` | Entity ID for the EV's current SOC (%), e.g. from the vehicle's own HA integration |
| `ev_battery_capacity_kwh` | number | `null` | EV usable battery capacity in kWh (required) |
| `ev_deadline_charge_power_kw` | number | `null` | Assumed charging power (kW) used only for planning how many slots are needed. If unset, estimated as `ev_max_current_limit × 230V`. |
| `ev_deadline_target_soc_entity` | string | `null` | `input_number` entity holding your target SOC (%) |
| `ev_deadline_target_time_entity` | string | `null` | `input_datetime` entity holding your "charge by" deadline |
| `ev_charging_condition` | ConditionGroup | `null` | How to tell the car is *actually charging* — see [Charging now vs. ready to charge](#charging-now-vs-ready-to-charge). Optional: without it, the charger's power meter is used. |

Worth pairing with this mode: `block_battery_discharge_while_charging: true` keeps the
house battery from emptying itself into the car during a deadline slot — the block is
taken when the slot opens and released when charging stops. See
[Battery-discharge guard](device_configuration.md#battery-discharge-guard).

Example device entry:

```json
{
  "name": "ev",
  "type": "ev",
  "ev_deadline_charge_enabled": true,
  "ev_soc_entity": "sensor.my_car_battery_level",
  "ev_battery_capacity_kwh": 77,
  "ev_deadline_charge_power_kw": 7.4,
  "ev_deadline_target_soc_entity": "input_number.ev_target_soc",
  "ev_deadline_target_time_entity": "input_datetime.ev_charge_by",
  "block_battery_discharge_while_charging": true,
  "ev_charging_condition": {
    "logic": "and",
    "conditions": [
      {"entity_id": "sensor.ev_charger_status", "operator": "==", "value": "charging"}
    ]
  },
  "start": { "entity": [{"service": "switch/turn_on", "entity_id": "switch.ev_charger"}] },
  "stop":  { "entity": [{"service": "switch/turn_off", "entity_id": "switch.ev_charger"}] }
}
```

## Home Assistant Package (Deployed Automatically)

Everything Home Assistant needs for this mode ships with the add-on as a package,
[homeassistant/packages/ev_deadline.yaml](../homeassistant/packages/ev_deadline.yaml). On every
start the add-on copies it to `<HA config>/packages/ev_deadline.yaml` (the config dir is mounted
via `map: homeassistant_config:rw` in `config.yaml`). Two things you must do once:

1. Enable packages in `configuration.yaml`:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages/
   ```
2. Restart Home Assistant after the add-on deploys (or updates) the package.

The deployed copy is overwritten on every add-on start, so keep your own customisations in a
separate package file. If you previously created `input_number.ev_deadline_target_soc` or
`input_datetime.ev_deadline_target_time` as UI helpers, delete those so the package's YAML
definitions take over the entity IDs (until then HA logs a "does not generate unique IDs"
warning and keeps using the UI helper — nothing breaks).

The package provides:

| Entity | Purpose |
|--------|---------|
| `input_number.ev_deadline_target_soc` | Target SOC — point `ev_deadline_target_soc_entity` at it |
| `input_datetime.ev_deadline_target_time` | Absolute deadline read by the add-on — point `ev_deadline_target_time_entity` at it. Written by the automation below, don't edit it directly |
| `input_select.ev_deadline_day`, `input_datetime.ev_deadline_time` | Friendlier "Today/Tomorrow" + time inputs for the dashboard; an automation folds them into the absolute deadline and triggers a recalculation |
| `input_datetime.ev_deadline_last_recalc` | Timestamp of the last successful recalculation (button feedback) |
| `rest_command.epg_recalculate_ev_deadline`, `script.ev_deadline_recalculate` | The "Recalculate now!" button. If the car is not ready, the script raises a persistent notification explaining that nothing is scheduled yet, that the settings were saved and applied, and that the charge will be planned once the car is plugged in and ready |
| `sensor.epg_ev_deadline_plan` | REST sensor polling the add-on's `/api/ev_deadline` (port 8099) every minute; all plan details are attributes |
| `binary_sensor.ev_ready_to_charge` | `on` when the add-on's `ev_ready_to_charge_condition` is satisfied |
| `binary_sensor.ev_charging_now` | `on` when the car is actually charging — from `ev_charging_condition`, or the charger's power meter when no condition is set (see [Charging now vs. ready to charge](#charging-now-vs-ready-to-charge)). Attributes carry the power, and which signal decided it |
| `sensor.ev_charging_power` | The charger's live load in W, sign-normalised so positive means charging |
| `sensor.ev_deadline_plan_status` | One-line plan headline ("Charging now – 7.2 kW · Scheduled: 12 slot(s), ~18.5 kWh", "On hold: …", "Target SOC reached", …) |

The add-on is addressed as `local-epg-addon.local.hass.io` — Supervisor's internal DNS name (the
add-on slug with underscores replaced by dashes, suffixed with `.local.hass.io`). Use this
rather than `localhost`, an mDNS `.local` name, or a LAN IP:

- `localhost` resolves to the Home Assistant Core container itself, not the add-on.
- `homeassistant.local` (mDNS) does not resolve from inside the Core container.
- The raw Docker container name (`app_local_epg_addon`) does not resolve either.
- A LAN IP works but breaks if the host's address changes.

Neither the trigger endpoint (port **8100**) nor the web API (port **8099**) is authenticated, so
they should only be reachable on your local network.

## The Dashboard Card

A ready-made Lovelace card is in
[homeassistant/dashboard/ev_deadline_card.yaml](../homeassistant/dashboard/ev_deadline_card.yaml)
(replace the EV SOC sensor with your own). It shows:

- a **Charging** badge with the live **Charger load** while the car is charging, which replaces
  the **Car** (ready) badge — the ready badge only shows when the car is *not* charging, so the
  card never says "ready to charge" about a car that is already charging;
- a **markdown status** with three branches: while charging, *"Car is charging now at X kW"*
  followed by the plan headline and message; when ready but not charging, the plan headline and
  message; when not ready: *"Car is not ready to charge. Charging is planned automatically as
  soon as the car is plugged in and ready. Your target SOC and deadline are saved and already
  taken into account – right now that would be N slot(s), about X kWh."*;
- rows for **Charging now**, **Charger load**, **Car ready to charge** and the plan status;
- current/target SOC, Charge-by day and time, and the **Recalculate now!** tile.

Pressing **Recalculate now!** always applies your settings. When the car is ready, the plan is
scheduled immediately. When it isn't, the "last recalculated" timestamp still updates and a
persistent notification explains that no slots are scheduled yet, that your settings were saved
and applied, and that charging will be planned as soon as the car is plugged in and ready. A car
that is already charging never gets that notification, whatever the readiness condition says.

## Viewing the Plan

The web UI (port **8099**) has an **EV Deadline Charging** info box that always shows the plan:
readiness (or, while the car is charging, a green **⚡ Charging now – X kW** badge and card),
current → target SOC, deadline, energy needed/planned, average price, the live **Charger load**,
and the list of planned slots. When the car is not ready the box says so, and the slots are listed as
"held" — they are *not* on the Gantt chart. Once the car is ready the slots are scheduled and
appear on the chart, and once the EV's current SOC is readable a predicted SOC trajectory line is
drawn on the predictions panel (dashed, distinct from the house battery SOC lines) so you can see
the projected charge curve rising toward your target by the deadline. The same data is available
as JSON at `/api/ev_deadline` (`devices` keyed by device name, plus `primary` = the first
deadline-mode EV, always present). See [Debug Logs](debug_logs.md) for how to follow the
planning decisions (slot selection, warnings) in real time.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No slots ever get scheduled | Check `ev_soc_entity`, `ev_deadline_target_soc_entity`, and `ev_deadline_target_time_entity` all resolve to a valid, non-`unknown`/`unavailable` state. If the web UI shows "Car not ready to charge", the `ev_ready_to_charge_condition` is not satisfied — check every entity it references is readable |
| Plan is on hold although the car is plugged in | An entity in `ev_ready_to_charge_condition` is `unknown`/`unavailable` (unknown counts as not ready), or the condition itself is wrong — compare against the entity states in Developer Tools |
| `binary_sensor.ev_ready_to_charge` is unavailable | `sensor.epg_ev_deadline_plan` cannot reach the add-on on port 8099, or no deadline plan has been computed yet — check the add-on is running and the hostname in the package |
| Plan doesn't update after changing the target/deadline | Slots are only re-selected at 16:05 or on demand — the 15-minute background cycle deliberately does not reselect (see [How It Works](#how-it-works)). Press the "Recalculate now" dashboard button (see [Triggering an Instant Recalculation](#triggering-an-instant-recalculation)), or trigger a full optimization run |
| "Cannot reach target% by deadline" warning | Not enough time remains before the deadline to deliver the required energy at `ev_deadline_charge_power_kw` — every remaining slot is used regardless of price, but the deadline may still be missed. Raise `ev_deadline_charge_power_kw` (if the charger can actually deliver more) or move the deadline out. |
| Deadline stopped triggering charging after it passed | Expected for a one-off (`has_date: true`) deadline that wasn't updated — set a new one, or switch to a time-only (`has_date: false`) recurring helper |
| Config fails to load with a validation error mentioning both fields | `ev_deadline_charge_enabled` and `solar_charge_only` cannot both be `true` on the same device — use two separate EV devices if you need both modes |
