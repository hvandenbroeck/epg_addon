# EV Deadline Charging

Charge an EV to a target state of charge (SOC) by a deadline you set from a Home Assistant
dashboard card, using the cheapest available electricity price slots up until that deadline.

This is one of the two EV charging modes (the other being `solar_charge_only`) — see
[Optional – EV Only](device_configuration.md#optional--ev-only) for how the two relate. A device
with `ev_deadline_charge_enabled: true` is scheduled solely to meet the target SOC by the
deadline.

## How It Works

1. Every optimization cycle (daily at 16:05, and every 15 minutes in between), the add-on reads
   three live values from Home Assistant: the EV's current SOC, your target SOC, and your
   "charge by" deadline.
2. It computes the energy needed: `ev_battery_capacity_kwh × (target_soc − current_soc) / 100`.
3. It sorts all price slots between now and the deadline from cheapest to most expensive, and
   selects slots — cheapest first — until the selected slots can deliver that much energy at
   `ev_deadline_charge_power_kw`.
4. If the deadline is further out than the currently known ENTSO-E price horizon (day-ahead
   prices for tomorrow aren't published until roughly 12:00–13:00 CET), it plans against the
   slots it knows about and automatically re-plans with more slots as new price data arrives.
5. If even every remaining slot up to the deadline isn't enough energy, it charges in every
   remaining slot regardless of price and logs a warning — the deadline is prioritized over cost
   in that case.
6. The slot currently in progress is never retroactively cancelled by a later recalculation,
   even if you change the target SOC or deadline mid-session — charging is not yanked away
   mid-slot. Slots that have already finished drop out of the plan (they can no longer trigger
   anything), so a slot picked under a momentarily-wrong input doesn't linger on the Gantt.

Charging is still subject to load management: the deadline plan only decides which slots the
charger is "on" in. The actual current/power delivered in an "on" slot is governed independently
by load management (`enable_load_management`) and by any other device competing for available
grid headroom.

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
  "start": { "entity": [{"service": "switch/turn_on", "entity_id": "switch.ev_charger"}] },
  "stop":  { "entity": [{"service": "switch/turn_off", "entity_id": "switch.ev_charger"}] }
}
```

## Setting Up the Dashboard Card

The add-on only *reads* the target-SOC and deadline entities — you create them yourself as
Home Assistant helpers (Settings → Devices & Services → Helpers, or via `configuration.yaml`):

- **`input_number`** for the target SOC — e.g. `min: 0`, `max: 100`, `step: 1`,
  `unit_of_measurement: "%"`, slider mode.
- **`input_datetime`** for the deadline — `has_time: true`. Leave `has_date: false` for a
  recurring daily deadline (e.g. "always charge by 07:00"), the recommended default. Set
  `has_date: true` for a one-off deadline; if it passes unmet, charging for that device stops
  and a warning is logged until you update it — it does **not** automatically roll to the next
  day.

Then add a Lovelace entities card referencing them:

```yaml
type: entities
title: EV Charging Target
entities:
  - entity: input_number.ev_target_soc
    name: Target SOC (%)
  - entity: input_datetime.ev_charge_by
    name: Charge by
  - entity: sensor.my_car_battery_level
    name: Current SOC
    secondary_info: last-changed
```

## Triggering an Instant Recalculation

Changes to the target-SOC or deadline helpers are normally picked up within 15 minutes (the
recalculation cadence described above). To apply a change immediately instead of waiting, add a
dashboard button that calls the add-on directly on port **8100**:

```yaml
# configuration.yaml
rest_command:
  epg_recalculate_ev_deadline:
    url: "http://local-epg-addon.local.hass.io:8100/trigger/ev_deadline_recalc"
    method: POST
```

The hostname is Supervisor's internal DNS name for the add-on: the add-on slug with underscores
replaced by dashes, suffixed with `.local.hass.io` (so slug `local_epg_addon` →
`local-epg-addon.local.hass.io`). Use this rather than `localhost`, an mDNS `.local` name, or a
LAN IP:

- `localhost` resolves to the Home Assistant Core container itself, not the add-on.
- `homeassistant.local` (mDNS) does not resolve from inside the Core container.
- The raw Docker container name (`app_local_epg_addon`) does not resolve either.
- A LAN IP works but breaks if the host's address changes.

Then add a button to your dashboard:

```yaml
type: button
name: Recalculate EV Charging Plan
icon: mdi:refresh
tap_action:
  action: call-service
  service: rest_command.epg_recalculate_ev_deadline
```

This calls straight into the add-on's optimization process and runs the recalculation immediately
— no polling delay, no need to wait for the next 15-minute cycle or trigger a full optimization
run. Note this endpoint has no authentication, so it should only be reachable on your local
network (the same trust boundary as the existing web UI on port 8099).

## Viewing the Plan

The planned charge slots appear on the schedule Gantt chart in the web UI (port **8099**), and
once the EV's current SOC is readable, a predicted SOC trajectory line is drawn on the same
chart (dashed, distinct from the house battery SOC lines) so you can see the projected charge
curve rising toward your target by the deadline. See [Debug Logs](debug_logs.md) for how to
follow the planning decisions (slot selection, warnings) in real time.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No slots ever get scheduled | Check `ev_soc_entity`, `ev_deadline_target_soc_entity`, and `ev_deadline_target_time_entity` all resolve to a valid, non-`unknown`/`unavailable` state |
| Plan doesn't update after changing the target/deadline | Wait for the next 15-minute recalculation, press the "Recalculate now" dashboard button (see [Triggering an Instant Recalculation](#triggering-an-instant-recalculation)), or trigger a full optimization run |
| "Cannot reach target% by deadline" warning | Not enough time remains before the deadline to deliver the required energy at `ev_deadline_charge_power_kw` — every remaining slot is used regardless of price, but the deadline may still be missed. Raise `ev_deadline_charge_power_kw` (if the charger can actually deliver more) or move the deadline out. |
| Deadline stopped triggering charging after it passed | Expected for a one-off (`has_date: true`) deadline that wasn't updated — set a new one, or switch to a time-only (`has_date: false`) recurring helper |
| Config fails to load with a validation error mentioning both fields | `ev_deadline_charge_enabled` and `solar_charge_only` cannot both be `true` on the same device — use two separate EV devices if you need both modes |
