# Implement EV "Charge by Deadline + Target SOC" mode

## Goal
Add a new EV charging mode to this addon: the user sets a target time ("charge by") and a target
battery SOC% via a Home Assistant dashboard card, and the addon schedules charging in the cheapest
available ENTSO-E price slots so the EV reaches that SOC by that deadline.

This is a new STANDALONE mode, mutually exclusive with `solar_charge_only=True`. A device using it
bypasses both the price-threshold `optimize_ev` path and the solar-surplus `EvSolarChargeController`
path entirely.

## New `Device` config fields
Add to `src/devices_config.py`, alongside the existing EV-specific fields (~line 123-149):

```python
ev_deadline_charge_enabled: bool = Field(default=False, description="When True, EV charging is scheduled to reach a target SOC by a deadline using cheapest-slot selection, bypassing both solar_charge_only and the price-threshold optimize_ev path. Mutually exclusive with solar_charge_only.")
ev_soc_entity: Optional[str] = Field(default=None, description="Home Assistant entity for the EV's current state of charge (%). Mirrors battery_soc_entity.")
ev_battery_capacity_kwh: Optional[float] = Field(default=None, description="EV usable battery capacity in kWh. Required for ev_deadline_charge_enabled; used to convert the SOC gap into an energy target.")
ev_deadline_charge_power_kw: Optional[float] = Field(default=None, description="Assumed charging power (kW) delivered during one 'on' slot, used only for planning how many slots are needed. Actual delivered power is still governed by load management/EvCharger. If unset, estimated from ev_max_current_limit at 230V single-phase.")
ev_deadline_target_soc_entity: Optional[str] = Field(default=None, description="HA input_number entity holding the user's target SOC (%) for deadline charging.")
ev_deadline_target_time_entity: Optional[str] = Field(default=None, description="HA input_datetime entity holding the user's 'charge by' deadline. A time-only helper (has_time, no has_date) is treated as a recurring daily deadline (rolls to the next occurrence); a full date+time helper is treated as a one-off absolute deadline.")
```

Add a pydantic `model_validator(mode="after")` on `Device` that raises `ValueError` if
`ev_deadline_charge_enabled and solar_charge_only` are both `True` — fail fast at process start
rather than silently picking one at runtime (config is a module-level global loaded once, so a
restart is already required for field changes anyway).

## Reading the live inputs
Add a small async helper (colocate with the planning algorithm, see below):

```python
async def read_ev_deadline_inputs(get_state, ev_device, now) -> tuple[Optional[float], Optional[float], Optional[datetime]]:
    """Returns (current_soc, target_soc, deadline_datetime), each None if unreadable."""
```

- **Current SOC**: read `ev_soc_entity` the same way `HeatpumpOptimizer._get_battery_soc` reads
  `battery_soc_entity` (`src/optimizer.py`) — `float(state['state'])`, treat
  `unknown`/`unavailable`/non-numeric as unreadable.
- **Target SOC**: read `ev_deadline_target_soc_entity` (an `input_number`), same numeric parse,
  clamp to `[0, 100]`.
- **Deadline**: read `ev_deadline_target_time_entity` (an `input_datetime`). HA reports its state
  as `"YYYY-MM-DD HH:MM:SS"`, `"HH:MM:SS"`, or `"YYYY-MM-DD"` depending on the helper's
  `has_date`/`has_time` flags:
  - **Time-only** (`"HH:MM:SS"`) → recurring daily deadline: combine with today's date; if that
    datetime is already `<= now`, roll forward to tomorrow.
  - **Date+time** → absolute one-off deadline. If it has already passed, do **not** auto-roll —
    log a clear warning ("EV deadline has passed; no further deadline-based charging will be
    scheduled until the input_datetime is updated") and treat it as no candidate slots (empty
    plan) for this device until the user updates the helper.
  - **Date-only** → unsupported; log a one-time warning, treat as unreadable.
- **current_soc >= target_soc**: energy needed is `<= 0` → return an empty plan (no special-casing
  needed, the energy-target math handles it — see below).
- **Any entity unreadable this cycle**: do NOT guess a numeric fallback (unlike battery's "assume
  50%" default — a wrong guess here is higher stakes in both directions). Keep the previously
  computed plan unchanged, log a warning, retry next cycle. If there's no previous plan (first run
  / after restart), fall back to an empty plan and log which entity is missing.
- **No plug-in/readiness gating**: fire start/stop actions regardless of connection state — do not
  add a `ConditionGroup`-based readiness gate for this feature.

## Core planning algorithm
New module `src/optimization/ev_deadline.py`, exporting:

```python
def plan_ev_deadline_charge(
    prices: list[float],
    slot_minutes: int,
    horizon_start: datetime,
    current_soc: float,
    target_soc: float,
    deadline: datetime,
    ev_battery_capacity_kwh: float,
    charge_power_kw: float,
    device_name: str = "ev",
    previous_charge_times: list[str] | None = None,
) -> list[str]:
```

Model this directly on `src/optimization/battery_limiter.py`'s `limit_battery_cycles` — reuse its
local `time_to_slot_idx`/`slot_idx_to_time` pattern (NOT `utils.slot_to_time`/`time_to_slot`, which
are single-day only; these need to allow slot indices beyond 24h for multi-day deadlines) and its
past/future slot preservation pattern (see below). Steps:

1. `energy_needed_kwh = ev_battery_capacity_kwh * max(0.0, target_soc - current_soc) / 100` — same
   formula used throughout the codebase (`battery_limiter.py`, `battery_soc_prediction.py`). If
   `<= 0`, return just the preserved past slots (no new charging) — self-correcting as SOC rises.
2. `current_slot_idx = floor((now - horizon_start) / slot_minutes)`.
3. `deadline_slot_idx_exclusive = floor((deadline - horizon_start) / slot_minutes)` — the deadline
   must never be exceeded, so the last usable slot's END must be `<= deadline`.
4. `known_end_idx = min(deadline_slot_idx_exclusive, len(prices))`. If
   `deadline_slot_idx_exclusive > len(prices)`, the deadline is beyond the currently known
   ENTSO-E price horizon (tomorrow's day-ahead prices aren't published until ~12-13:00 CET, and
   `price_fetcher.py` has no fallback for missing future slots — `horizon_end` just ends early).
   This is NOT infeasible by itself: log info and plan against known slots only; the next 15-min
   recalculation naturally extends the candidate set as more price data arrives.
5. Greedy cheapest-first selection until the energy target is met (simplified version of
   `battery_limiter`'s `simulate_soc`/greedy loop — charge-only, no discharge, no SOC-ceiling
   headroom check beyond "stop once satisfied"):
   ```python
   slot_hours = slot_minutes / 60
   energy_per_slot = charge_power_kw * slot_hours
   candidate_slots = range(current_slot_idx, known_end_idx)
   candidates_by_price = sorted(candidate_slots, key=lambda s: prices[s])
   selected, energy_acc = set(), 0.0
   for s in candidates_by_price:
       if energy_acc >= energy_needed_kwh:
           break
       selected.add(s)
       energy_acc += energy_per_slot
   ```
6. **Infeasible case**: if after the loop `energy_acc < energy_needed_kwh` AND
   `known_end_idx == deadline_slot_idx_exclusive` (we've genuinely seen every slot up to the
   deadline, this isn't just "waiting for tomorrow's prices") — log a clear warning
   (`f"⚠️ {device_name}: cannot reach {target_soc}% by {deadline} even using all remaining slots
   (need {energy_needed_kwh:.2f} kWh, can deliver {energy_acc:.2f} kWh) — charging every available
   slot regardless of price"`) and set `selected = set(candidate_slots)` (ignore price, use every
   remaining slot). If `candidate_slots` is empty (deadline already passed/imminent), log a
   distinct warning and return just the preserved past slots.
7. **Preserve in-progress plans across re-runs** (same pattern as `previous_limited_charge_times`
   in `limit_battery_cycles`): split `previous_charge_times` into `past_slots` (`< current_slot_idx`,
   preserved verbatim, never recomputed) and recompute only future slots via steps 2-6. Final
   result = `past_slots | selected`. This guarantees a slot whose start action already fired isn't
   retroactively cancelled just because SOC, target, or deadline changed mid-session.
8. Convert to `"HH:MM"` strings via a local `slot_idx_to_time` and return, sorted.

## Integration points

**`src/optimizer.py`** — in the EV loop (~line 355-361), branch before the existing
`solar_charge_only` check:
```python
if ev_device.ev_deadline_charge_enabled:
    results[device_name] = []   # real slots filled by recalculate_ev_deadline_plans(), same
                                 # deferred pattern battery SOC limiting already uses
    continue
```
Add a new method `recalculate_ev_deadline_plans()`, structured identically to the existing
`recalculate_battery_limits()` (same file): load `schedule_doc` from TinyDB, keep every entry
whose device is NOT one of the `ev_deadline_charge_enabled` device names, then for each such
device: `read_ev_deadline_inputs(...)`, extract `previous_charge_times` via the existing
`_extract_times` helper (keyed on the plain device name, no suffix), call
`plan_ev_deadline_charge(...)`, convert via `_times_to_schedule`, append to `new_schedule`, merge
with `merge_sequential_timeslots`, save to TinyDB, call `self.scheduler_instance.schedule_actions()`
— identical tail to `recalculate_battery_limits()`. Call this new method right where
`run_optimization()` already calls `recalculate_battery_limits()`.

**`optimization_plan.py`** — add a new cron job mirroring `scheduled_battery_recalc`, at the same
`:10,:25,:40,:55` marks, calling `optimizer.recalculate_ev_deadline_plans()`. Only schedule it when
`any(d.ev_deadline_charge_enabled for d in ev_devices)`, mirroring the existing
`if any(d.solar_charge_only for d in ev_devices):` guard used for the solar-charge job.

**`src/scheduler.py`**: no changes needed. Deadline-mode entries use the plain device name (no
suffix), which already falls through to the generic `cfg.start`/`cfg.stop` branch in
`schedule_actions()` — the same path `wp`/`hw`/plain price-threshold `ev` devices already use.
Confirm this by reading `schedule_actions()`'s `BATTERY_SUFFIXES` check before touching anything.

## Interaction with load management
The deadline planner only decides which slots the charger's `start`/`stop` actions fire in —
identical semantics to today's price-threshold EV output. Actual instantaneous current/kW is still
governed independently by `LoadWatcher` (`src/load_watcher/limit_calculator.py`/`limit_applier.py`,
priority-ordered via `load_management.load_priority`) and `EvCharger` (amp/phase stepping).
`ev_deadline_charge_power_kw` is a planning-time assumption only, not an execution-time guarantee.
Document (don't try to solve) the known limitation: in the infeasible-fallback case where the EV
requests every remaining slot, a higher-priority device (e.g. battery) competing for the same slots
could throttle the EV below its planning assumption and cause the deadline to still be missed even
though the plan "looks" feasible on paper.

## Dashboard / HA-helper side
The addon only reads these entities — it never creates them. The user creates them via HA Settings
→ Devices & Services → Helpers (or `configuration.yaml`). Suggested naming, prefixed by the EV
device's config `name`:
- `input_number.ev_target_soc` (`min: 0, max: 100, step: 1, unit_of_measurement: "%"`, slider mode)
  → `ev_deadline_target_soc_entity`
- `input_datetime.ev_charge_by` (`has_date: false, has_time: true` for the recommended recurring
  daily deadline) → `ev_deadline_target_time_entity`
- The EV's own SOC sensor (from its native HA integration) → `ev_soc_entity`

Example Lovelace entities card:
```yaml
type: entities
title: EV Charging Target
entities:
  - entity: input_number.ev_target_soc
    name: Target SOC (%)
  - entity: input_datetime.ev_charge_by
    name: Charge by
  - entity: sensor.<your_ev_soc_sensor>
    name: Current SOC
    secondary_info: last-changed
```

## UI / observability
Add an EV SOC-trajectory prediction mirroring `predict_battery_soc`
(`src/forecasting/battery_soc_prediction.py`) — new `predict_ev_soc` (charge-only, no
discharge/solar branches needed), same `[{'timestamp', 'soc_percent'}, ...]` shape, driven by the
final selected charge slots + `ev_deadline_charge_power_kw`. Store under a new `ev_soc` key in the
TinyDB `"predictions"` doc, populated inside `recalculate_ev_deadline_plans()` the same way
`battery_soc_predictions` is populated in `recalculate_battery_limits()`. Extend
`/api/predictions` and `/api/gantt` in `web/server.py` (currently iterating `battery_soc.items()`
around line 366-381) to also plot `ev_soc.items()` on the same secondary SOC% y-axis, with a
distinct color, so the user can see the planned charge curve rising toward their target on the
existing Gantt dashboard.

## Documentation
`documentation/device_configuration.md` is already stale relative to `devices_config.py` (missing
several existing EV fields like `ev_voltage_entity_l*`, `grid_export_unblock_condition`, the
`solar_*` tuning block). Add the new fields to its tables, and add a new section (mirroring
`documentation/battery_optimization.md`'s structure: overview, config fields, worked example,
troubleshooting) — either its own `documentation/ev_deadline_charging.md` or a new top-level
section — plus a cross-reference from `documentation/index.md`. While touching this area, fix the
pre-existing gaps for the fields nearest this feature rather than letting the staleness compound.

## Verification
- Configure a test EV device with `ev_deadline_charge_enabled=True`, an `input_number` and
  `input_datetime` helper, and a real or `input_number`-simulated `ev_soc_entity` (same pattern as
  the commented-out `input_number.battery_soc_simulation` example in `devices_config.py`'s default
  battery config).
- Run `optimization_plan.py` locally (or trigger `run_optimization()` directly) and confirm:
  - `db.json`'s `"schedule"` doc gets a plain-named entry for the EV device with slots that are all
    before the deadline slot and whose count roughly matches `energy_needed_kwh / energy_per_slot`.
  - The selected slots are the cheapest available ones in `prices` (spot-check a few against the
    horizon's price list).
  - Setting `ev_soc_entity` above `ev_deadline_target_soc_entity`'s value collapses the plan to empty.
  - Simulate "deadline beyond known horizon" (set the input_datetime far enough out) and confirm it
    logs the info message and re-plans wider on the next 15-min cycle once more prices are fetched.
  - Simulate the infeasible case (tiny time window, large SOC gap) and confirm the warning log and
    "use every remaining slot" fallback.
- Check the web UI (`/api/gantt`, port 8099) shows the EV's planned charge slots and (once added)
  its SOC trajectory line trending toward the target by the deadline.
- Confirm a slot already past `current_slot_idx` at plan time is never removed by a subsequent
  15-minute recalculation, even if SOC/target/deadline changed in between.
