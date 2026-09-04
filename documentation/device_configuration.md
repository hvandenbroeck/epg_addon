# Device Configuration

Configuration is loaded from `/data/options.json`. See `DEVICES_CONFIG_EXAMPLE.json` in the repository root for a complete example.

## Configuration Structure

```json
{
  "devices": [
    {
      "name": "wp",
      "type": "wp",
      "enable_load_management": false,
      "start": { "mqtt": [...], "entity": [...] },
      "stop":  { "mqtt": [...], "entity": [...] }
    }
  ]
}
```

Each device has a **unique name** and a **device type**.

## Device Types

| Type | Description |
|------|-------------|
| `wp` | Heat pump |
| `hw` | Hot water |
| `battery` | Battery (requires separate `charge_start` / `charge_stop` / `discharge_start` / `discharge_stop` actions) |
| `ev` | Electric vehicle charger |

## Device Fields

### Required

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique device identifier |
| `type` | string | Device type: `wp`, `hw`, `battery`, or `ev` |

### Optional – General

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `start` | ActionSet | `{}` | Actions to execute when starting the device |
| `stop` | ActionSet | `{}` | Actions to execute when stopping the device |
| `enable_load_management` | boolean | `false` | Enable dynamic power limiting |
| `load_management` | LoadManagement | `null` | Load management settings (required if `enable_load_management` is `true`) |
| `block_hours` | number | device default | Minimum duration (hours) for a scheduled run |
| `min_gap_hours` | number | device default | Minimum gap between runs (hours) |
| `max_gap_hours` | number | device default | Maximum gap between runs (hours) |

### Optional – Battery Only

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `charge_start` | ActionSet | `{}` | Actions to start battery charging |
| `charge_stop` | ActionSet | `{}` | Actions to stop battery charging |
| `discharge_start` | ActionSet | `{}` | Actions to start battery discharging |
| `discharge_stop` | ActionSet | `{}` | Actions to stop battery discharging |
| `battery_soc_entity` | string | `null` | Entity ID for the battery's state of charge (%) |
| `battery_capacity_kwh` | number | `null` | Battery capacity in kWh, used for SOC-based cycle limiting |
| `battery_charge_speed_kw` | number | `null` | Battery charge speed in kW, used for SOC-based cycle limiting |
| `battery_min_soc_percent` | number | `20.0` | Minimum battery SOC (%) |
| `battery_max_soc_percent` | number | `80.0` | Maximum battery SOC (%) |

See [Battery Optimization](battery_optimization.md) for details.

### Optional – EV Only

An EV device supports exactly two charging modes — `solar_charge_only` (solar-surplus charging) or
`ev_deadline_charge_enabled` (charge to a target SOC by a deadline; see below). They're mutually
exclusive, and a device with neither set gets no automated charging schedule at all (a warning is
logged at optimization time).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `solar_charge_only` | boolean | `false` | Controlled by solar surplus instead of a deadline; see [Optional – EV Solar Charge Controller](#optional--ev-solar-charge-controller) below |
| `ev_min_current_limit` | number (A) | `6.0` | Minimum EV charging current |
| `ev_max_current_limit` | number (A) | `16.0` | Maximum EV charging current |
| `ev_voltage_entity_l1` / `_l2` / `_l3` | string | `null` | Entity for phase voltage (V); defaults to 230 V if unset |
| `ev_single_phase_line` | string | `"l1"` | Physical phase used in single-phase mode: `"l1"`, `"l2"`, or `"l3"` |
| `grid_export_unblock_condition` | ConditionGroup | `null` | EV-ready override that force-unblocks price-based grid-export blocking when true (e.g. charger connected and below target SOC) |

See [Expressions](expressions.md) for the `value`/`payload`/`option` expression syntax used in `apply_limit_actions`.

### Optional – EV Deadline Charging Only

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `ev_deadline_charge_enabled` | boolean | `false` | Enables "charge by deadline" mode. Mutually exclusive with `solar_charge_only`. |
| `ev_soc_entity` | string | `null` | Entity ID for the EV's current state of charge (%) |
| `ev_battery_capacity_kwh` | number | `null` | EV usable battery capacity in kWh (required) |
| `ev_deadline_charge_power_kw` | number | `null` | Assumed charging power (kW) used for planning. If unset, estimated from `ev_max_current_limit` at 230 V single-phase. |
| `ev_deadline_target_soc_entity` | string | `null` | `input_number` entity holding the target SOC (%) |
| `ev_deadline_target_time_entity` | string | `null` | `input_datetime` entity holding the "charge by" deadline |

See [EV Deadline Charging](ev_deadline_charging.md) for the full guide, including dashboard card setup.

### Optional – Heat Pump Runtime Calculation

| Field | Type | Description |
|-------|------|-------------|
| `inside_temp_sensor` | string | Entity ID of the inside temperature sensor |
| `outside_temp_sensor` | string | Entity ID of the outside temperature sensor |
| `heatpump_status_sensor` | string | Entity ID of the heat pump status sensor (1=on, 0=off) |

See [Heat Pump Runtime](heat_pump_runtime.md) for details.

## Action Sets

An `ActionSet` has two optional lists:

```json
{
  "mqtt":   [ <MQTTAction>, ... ],
  "entity": [ <EntityAction>, ... ]
}
```

### MQTT Action Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `topic` | string | ✓ | MQTT topic to publish to |
| `payload` | string / number | ✓ | Value to publish (supports [expressions](expressions.md)) |
| `topic_get` | string | | Read-back topic for state verification |
| `payload_check` | string / number | | Expected value when reading back |

### Entity Action Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `service` | string | ✓ | Home Assistant service, e.g. `switch/turn_on` |
| `entity_id` | string | ✓ | Target entity ID |
| `value` | string / number | | Value for `number/set_value` (supports [expressions](expressions.md)) |
| `option` | string | | Option for `select/select_option` |
| `value_check` | string / number | | Expected state after service call |
| `state_attribute` | string | | Check this attribute instead of the main state |

Switch services (`turn_on` / `turn_off`) automatically infer the expected state (`"on"` / `"off"`), so `value_check` is not needed for those.

## Load Management Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `instantaneous_load_entity` | string | ✓ | Sensor entity providing current power draw (W) |
| `load_priority` | integer | ✓ | Lower number = higher priority |
| `load_maximum_watts` | string / number | ✓ | Maximum allowed power draw (W) |
| `charge_sign` | string | | `"positive"` (default) or `"negative"` – sign convention for the power sensor |
| `apply_limit_actions` | ActionSet | | Actions to execute when a new power limit is set |

## Multiple Devices of the Same Type

You can configure any number of devices of the same type by giving each a unique name:

```json
{
  "devices": [
    {"name": "ev1", "type": "ev", "...": "..."},
    {"name": "ev2", "type": "ev", "...": "..."}
  ]
}
```

## Accessing Configuration in Code

```python
from src.devices_config import devices_config

# Get a single device by name
device = devices_config.get_device_by_name("ev1")

# Get all devices of a given type
ev_devices = devices_config.get_devices_by_type("ev")

# Iterate over all devices
for device in devices_config.devices:
    print(f"{device.name} ({device.type})")
```

### Optional – EV Solar Charge Controller

The EV solar charge controller adjusts charging current to match the available
solar surplus. It is configured globally via `config.json` (not per device),
since the solar production and house consumption sensors are property-wide.

Enable by adding the following keys to `config.json` under `options`:

| Option key | Type | Default | Description |
|------------|------|---------|-------------|
| `production_phase_l1_entity` | string | `null` | HA entity for grid **export** (feed-in) on phase L1 (W) |
| `production_phase_l2_entity` | string | `null` | HA entity for grid **export** (feed-in) on phase L2 (W) |
| `production_phase_l3_entity` | string | `null` | HA entity for grid **export** (feed-in) on phase L3 (W) |
| `consumption_phase_l1_entity` | string | `null` | HA entity for grid **import** on phase L1 (W) |
| `consumption_phase_l2_entity` | string | `null` | HA entity for grid **import** on phase L2 (W) |
| `consumption_phase_l3_entity` | string | `null` | HA entity for grid **import** on phase L3 (W) |

These are the per-phase grid **export** and **import** legs (not gross PV / house
load). The controller derives the surplus as ``export − import + ev_load − battery
discharge``, so feeding the net grid legs lets it isolate how much solar is available
to the car. Tuning options (global `minimum_charging_power` and the per-EV-device
`solar_*` fields) are listed under **Behaviour and tuning options** below.

The controller runs for every EV device that has `solar_charge_only: true`; set
the production/consumption entities here so it can measure the surplus. Omit the
phases you don't need; missing phases default to **0 W**.

Example `config.json` snippet:

```json
{
  "options": {
    "production_phase_l1_entity": "sensor.power_production_phase_l1",
    "production_phase_l2_entity": "sensor.power_production_phase_l2",
    "production_phase_l3_entity": "sensor.power_production_phase_l3",
    "consumption_phase_l1_entity": "sensor.power_consumption_phase_l1",
    "consumption_phase_l2_entity": "sensor.power_consumption_phase_l2",
    "consumption_phase_l3_entity": "sensor.power_consumption_phase_l3",
    "minimum_charging_power": 1380
  }
}
```

The controller runs on the same schedule as the load watcher and applies
limits to **all** configured EV devices via their existing
`load_management.apply_limit_actions`.

### Behaviour and tuning options

**Global options** (under `options` in `config.json`, optional — defaults shown):

| Option key | Type | Default | Description |
|------------|------|---------|-------------|
| `minimum_charging_power` | number (W) | `1380` | Minimum power the EV must be able to draw to charge (≈ 6 A × 230 V). The session never runs below this. |
| `phase_switch_delay_minutes` | number (min) | `5` | After dropping to single phase, how long to wait before switching back up to three phase (prevents phase chatter near the boundary). **Shared** with the load watcher. |

**Per-EV-device fields** (on the EV entry in `/data/options.json`, alongside
`solar_charge_only`, `ev_min_current_limit`, etc. — optional, defaults shown). These
are per device so different chargers can behave differently:

| Device field | Type | Default | Description |
|--------------|------|---------|-------------|
| `solar_round_down` | bool | `false` | When the surplus lands between two charge levels: `false` rounds **up** to the next level (uses all excess solar, drawing the dynamic gap — ~230 W per amp step, ~690 W across a three-phase step — from the grid); `true` rounds **down** (never imports to round, exports the small remainder instead). |
| `solar_start_margin` | number (W) | `200` | Extra surplus above the minimum required before a session **starts**, so it doesn't start on a borderline surplus and immediately stop. |
| `solar_stop_debounce` | integer (cycles) | `3` | Consecutive control cycles the surplus must stay below the minimum before the session is **stopped** — rides out passing clouds without flapping. |
| `solar_battery_soc_full` | number (%) | `0` (off) | Optional strict *battery-first lockout*. When `> 0`, the EV will not start until every battery with a SOC sensor reaches this level, and a running session stops if SOC later drops below `value − hysteresis`. Leave at `0` to disable (see below). |
| `solar_battery_soc_hysteresis` | number (%) | `5` | Resume band for the lockout above: once stopped on low SOC, the EV resumes only after SOC climbs back to `solar_battery_soc_full`. |
| `solar_block_battery_discharge` | bool | `false` | While the EV is actively charging, block battery discharge (via `discharge_stop`) and re-enable it (via `discharge_start`) once charging stops — only if discharge was active when charging began. |
| `solar_phase_balance_switching` | bool | `false` | While charging single-phase, detect the EV's phase importing from the grid while the other two phases export or idle, and switch early to 3-phase to use the spare solar on those phases. |
| `solar_phase_balance_import_threshold` | number (W) | `100` | Minimum import on the EV's phase to count as "starved" for the phase-balance switch. |
| `solar_phase_balance_export_margin` | number (W) | `0` | The other two phases must each be at or above this net export to count as having spare solar. |
| `solar_phase_balance_debounce` | integer (cycles) | `2` | Consecutive cycles the phase imbalance must persist before forcing an early switch to 3-phase (anti-flap). |

#### Battery priority

By default (`solar_battery_soc_full = 0`) the house battery already has priority
**without** any lockout: the surplus is the grid **export** overflow
(PV − house − battery charging), so the EV only ever uses solar the battery is not
absorbing. A nearly-empty battery charging at its maximum rate (say 2.5 kW) while the
inverter produces much more (say 6 kW) still leaves the ~3.5 kW overflow for the EV.
The battery is also never *discharged* to feed the EV — battery discharge is treated
as a deficit, so the EV steps down instead.

Set `solar_battery_soc_full` to a percentage (e.g. `100`) only if you want the
stricter behaviour of keeping the EV completely off until the battery is full. This
requires each battery device to define `battery_soc_entity`.

#### Negative prices / blocked export

When the add-on blocks grid export during negative-price slots (via a battery
device's `block_grid_export_start` / `block_grid_export_stop` switch), the inverter
curtails production and the export meter reads ~0. The controller detects this from
the export switch state and, instead of the (now meaningless) export figure, uses the
power flowing into the battery as the surplus signal — so the EV still soaks up the
otherwise-curtailed solar.

Because those curtailed hours record suppressed PV output, they would also mislead the
solar-production forecast (training it to under-predict on exactly the sunny, low-price
peak hours). The add-on therefore excludes curtailed periods from solar model training:
it reads the export switch history, records each curtailed 15-minute slot into the
shared `db.json` store, and drops the affected hours from the training set (because the
hourly training value is the sum over the whole hour, any single curtailed 15-minute slot
drops that whole hour). By default it uses the switch from
`block_grid_export_stop`; set `grid_export_switch_entity` on the battery device to point
at a different switch (its `off` state must mean export is blocked).
Note: because Home Assistant's history API only returns data within the recorder's
`purge_keep_days` window, full 90-day coverage is reached progressively over the first
couple of weeks of daily runs (or immediately if you raise `purge_keep_days`).

#### Phase integrity

A current limit means very different power in single- vs three-phase (e.g. 16 A is
~3.7 kW on one phase but ~11 kW on three), so the controller keeps the charger's phase
and its own model in lock-step:

- On every **start** it explicitly commands the intended phase rather than assuming it.
- Every **cycle** it reads the charger's *actual* phase (from the force-single-phase
  switch) and corrects its internal state if they disagree — so a charger left in or
  reverted to the wrong phase can't make a limit draw 3× the expected power.
- Every **step** is applied with the matching phase command, so the limit and phase
  never drift apart.
- While the `phase_switch_delay_minutes` dwell is active (just dropped to single phase),
  a limit increase that would require switching back up to three phase is **skipped**
  until the dwell expires — the limit is never raised into a phase change that isn't
  allowed yet.

This reads the entity referenced by the device's `switch_to_single_phase` action; if no
such switch is configured, the controller falls back to its internal phase state.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Configuration not loading | Check `/data/options.json` exists and is valid JSON |
| Device not found | Verify the device name matches exactly (case-sensitive) |
| Type validation error | Check field types match the model; enum values must be `"positive"` or `"negative"` |
| Start/stop action not executing | Verify `service`, `entity_id`, or MQTT `topic` values are correct |

## Further Reading

- [Quick Start](quick_start.md) – Minimal working configuration
- [Configuration Examples](configuration_examples.md) – Real-world patterns
- [Expressions](expressions.md) – Dynamic value calculations in `value` / `payload` fields
