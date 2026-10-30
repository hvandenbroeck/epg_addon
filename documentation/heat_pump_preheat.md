# Heat Pump Thermal Pre-Heating

Thermal pre-heating is an opt-in heat-pump planning mode that treats your heated floor slab as a
**heat battery**: it pushes heating into the cheapest (and solar) hours and lets the slab coast
through price peaks, while never letting the room fall below a comfort floor that depends on the
time of day. It replaces the block/min-gap/max-gap scheduler (see
[Heat Pump Runtime](heat_pump_runtime.md)) for the devices that enable it.

It is the thermal counterpart of the [Battery Optimization](battery_optimization.md) LP, and works
the same way: one linear program over the whole price horizon, re-solved every 15 minutes against
the latest **measured room temperature** (the thermal "state of charge").

## When to use it

Use it for weather-compensated, continuously-modulating systems with high thermal mass —
underfloor heating in particular. Such systems never really turn *off*; they modulate a low flow
temperature around the clock, and the slab holds heat for many hours. The only useful control lever
is the **zone setpoint**, and a block on/off scheduler cannot express "carry the house warm on
cheap power overnight, then drift down through the evening peak". The block scheduler remains the
right choice for genuinely on/off heat sources.

## How it works

For each enabled device the planner solves, over every slot of the horizon:

- **Decision:** a room-temperature trajectory `T[i]` and the useful heat `q[i] ≥ 0` delivered each
  slot, linked by a first-order RC thermal model

  ```
  C · (T[i+1] − T[i]) = q[i] − UA · (T[i] − Tout[i]) · Δt
  ```

  where `C` is the slab heat capacity (kWh/°C), `UA` the heat-loss coefficient (kW/°C), `Tout` the
  forecast outside temperature, and `Δt` the slot length in hours.
- **Comfort:** `T[i]` is kept within `[comfort_floor[i], preheat_max_setpoint]`. The floor varies by
  time of day from your occupancy windows. A soft slack keeps the problem feasible when the floor
  physically cannot be held (e.g. the coldest hour at a low heat rate), sacrificing comfort only as
  a last resort and by the smallest possible amount.
- **Cost:** electricity drawn is `q[i] / COP[i]`, split (exactly like the battery's grid/solar
  split) into a grid part priced at the import price and a solar part priced at the forgone export
  revenue and capped by the predicted PV surplus that slot. `COP[i]` is derived from the forecast
  outside temperature. A terminal credit on the heat left in the slab stops the plan dumping heat in
  the final slots, and an optional `preheat_penalty_per_c` discourages carrying the slab hotter than
  the arbitrage justifies (a linear stand-in for the lower COP of a higher flow temperature).

The per-slot `T` solution is rounded to `preheat_setpoint_resolution` and written to the device as a
**setpoint per slot** via the device's `start` action, using the `{setpoint}` expression (see
below). There is no "stop" action per range — the next range's setpoint overrides the previous one,
and the [device verifier](device_configuration.md#action-sets) holds the current setpoint in
between. The plan is recomputed at the daily 16:05 optimization and re-solved every 15 minutes
against the measured room temperature, so it tracks reality as the house warms or cools.

### Example behaviour

With a sharp evening price peak (17:00–21:00) the planner pre-heats the slab in the cheaper hour
just before, then delivers **no heat during the peak**, letting the room coast down along the
comfort floor — buying the same comfort for roughly a third less, with the heat stored before the
expensive window.

## Configuration

Set `wp_preheat_enabled: true` on a `wp` device and point the planner at the room-temperature
sensor it should track. The device's `start` action must set the zone to the planned value using
the `{setpoint}` expression; the `stop` action is used only as a fallback revert and is otherwise
unused in this mode.

```json
{
  "name": "wp",
  "type": "wp",
  "inside_temp_sensor": "sensor.ebusd_700_z2roomtemp",
  "outside_temp_sensor": "sensor.ebusd_700_displayedoutsidetemp",
  "wp_preheat_enabled": true,
  "preheat_max_setpoint": 22.0,
  "preheat_comfort_default_min_temp": 19.5,
  "preheat_comfort_windows": [
    { "start": "23:00", "end": "06:30", "min_temp": 19.5 },
    { "start": "06:30", "end": "08:30", "min_temp": 20.5 },
    { "start": "17:00", "end": "23:00", "min_temp": 20.5 },
    { "start": "08:30", "end": "17:00", "min_temp": 20.5, "days": ["sat", "sun"] }
  ],
  "thermal_heat_loss_kw_per_c": 0.024,
  "thermal_heat_capacity_kwh_per_c": 1.5,
  "thermal_max_heat_kw": 5.0,
  "start": {
    "mqtt": [
      { "topic": "ebusd/700/z2sfmode/set", "payload": "veto" },
      { "topic": "ebusd/700/z2quickvetotemp/set", "payload": "{setpoint}" }
    ]
  },
  "stop": {
    "mqtt": [
      { "topic": "ebusd/700/z2sfmode/set", "payload": "auto" },
      { "topic": "ebusd/700/z2quickvetotemp/set", "payload": "20" }
    ]
  }
}
```

### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `wp_preheat_enabled` | bool | `false` | Use the thermal setpoint LP instead of the block scheduler for this heat pump |
| `inside_temp_sensor` | string | — | **Required.** Room-temperature sensor the plan tracks (the thermal "SOC") |
| `outside_temp_sensor` | string | — | Fallback outside-temperature sensor if the weather forecast is unavailable |
| `preheat_max_setpoint` | number | `22.0` | Highest room setpoint the planner may command when pre-heating (°C) |
| `preheat_comfort_default_min_temp` | number | `19.5` | Minimum room temperature when no comfort window matches (the "away"/default floor) |
| `preheat_comfort_windows` | list | `[]` | Occupancy windows raising the minimum room temperature at certain times/days (see below) |
| `preheat_setpoint_resolution` | number | `0.5` | Rounding step for the commanded setpoint (°C) |
| `preheat_penalty_per_c` | number | `0.0` | Extra cost per °C above the comfort floor per slot (€); raise it to smooth the setpoint and reduce micro-cycling |
| `thermal_heat_loss_kw_per_c` | number | `0.024` | Heat-loss coefficient UA (kW per °C inside/outside difference) |
| `thermal_heat_capacity_kwh_per_c` | number | `1.5` | Effective slab heat capacity (kWh per °C) — energy to raise the house 1 °C |
| `thermal_max_heat_kw` | number | `5.0` | Maximum useful heat the system can deliver (kW) — caps the pre-charge rate |
| `thermal_cop_ref` | number | `3.5` | Reference heating COP at `thermal_cop_tout_ref` outside temperature |
| `thermal_cop_slope` | number | `0.08` | COP change per °C of outside temperature (higher when milder) |
| `thermal_cop_tout_ref` | number | `7.0` | Outside temperature (°C) at which COP equals `thermal_cop_ref` |

#### Comfort windows

Each window is `{ "start": "HH:MM", "end": "HH:MM", "min_temp": <°C>, "days": [..] }`:

- `start`/`end` are local times; a window may wrap past midnight (`end` ≤ `start`, e.g. a sleep
  window `23:00`–`06:30`).
- `days` restricts the window to certain weekdays (`mon`..`sun`); omit it for every day.
- Slots matched by several windows take the **highest** `min_temp`; unmatched slots use
  `preheat_comfort_default_min_temp`.

### Tuning the thermal model

The defaults were seeded from a 10-day energy-balance fit of a well-insulated underfloor house and
are a reasonable starting point, but `UA` and `C` are house-specific:

- **`thermal_heat_loss_kw_per_c` (UA)** — estimate from steady energy use: `UA ≈ average heating
  power (kW) ÷ average (inside − outside) temperature difference (°C)`.
- **`thermal_heat_capacity_kwh_per_c` (C)** — how much energy raises the house 1 °C. Larger = slower
  house = more aggressive pre-heating is safe. Because the time constant `τ = C ÷ UA` is far longer
  than the ~32 h horizon for a slab, the plan is fairly insensitive to the exact value; start
  conservative and increase it if the house holds heat better than the plan expects.
- **`preheat_max_setpoint`** — the pre-heat ceiling. Higher allows more load shifting but runs the
  slab (and flow temperature) hotter, which lowers COP; raise it only if your comfort allows.

## Interaction with other features

- **Solar:** the planner reads the same cached PV-surplus prediction as the battery LP and prefers
  to cover heating from surplus first. The two optimizers see the surplus independently (they do not
  co-schedule it), so on very sunny days the same kWh may be counted as available to both — a
  first-order approximation.
- **Load management:** unchanged. If the heat pump participates in load management, its power limit
  is still applied independently of the setpoint plan.

## Verifying it is working

- Logs: look for `🔥 wp: thermal LP plan — deliver … kWh heat …` at 16:05 and every 15 minutes, and
  `📅 Scheduled WP SETPOINT <value>°C at …` from the scheduler.
- The schedule doc (`db.json`, and the web UI Gantt) shows `wp` ranges carrying a `setpoint` value;
  `thermal_plan_summary` records the economics and `predictions.thermal_temp` the predicted room
  temperature trajectory.
- To revert to the block scheduler, set `wp_preheat_enabled: false` (and restore a fixed
  `z2quickvetotemp` payload) and restart the addon.
