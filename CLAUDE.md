# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Home Assistant addon that optimizes heat pumps, hot water, batteries, and EV chargers against
Nordpool/ENTSO-E day-ahead electricity prices, plus load management to avoid grid overload. See
[README.md](README.md) and [documentation/index.md](documentation/index.md) for user-facing docs
(device configuration reference, expressions syntax, battery optimization strategy, etc.) — read
those before touching device-config or optimization logic, they describe behavior this file won't
repeat.

## Running / building

There is no test suite, linter, or build step configured (no `requirements.txt`, `pyproject.toml`,
or pytest config) — dependencies are installed straight into the Dockerfile via `pip install`, and
verification is manual (run the addon, watch `/data/logs/epg_addon.log`, check the web UI).

- **Docker (real deployment path):** `Dockerfile` + `run.sh`. Entrypoint reads `SUPERVISOR_TOKEN`
  and `config.json`, starts the Flask UI in the background, then execs
  `optimization_plan.py --token <token>`.
- **Local run:** `python3 optimization_plan.py --token <HA_long_lived_token>` — requires
  `config.json` (HA URL, ENTSO-E token, tuning options) at the repo root and `/data/options.json`
  (per-device config, see `DEVICES_CONFIG_EXAMPLE.json`) to exist on disk, since both are read from
  hardcoded absolute paths (`/app/config.json` in [src/config.py](src/config.py), `/data/options.json`
  in [src/devices_config.py](src/devices_config.py)).
- **Web UI standalone:** `python3 -c "from web.server import run_server; run_server()"` — Flask app
  on port 8099.
- `test_expressions.py` is a standalone manual demo script for the expression evaluator (not
  pytest, not wired into CI) — run directly with `python3 test_expressions.py`; note its `sys.path`
  hack points at `/root/addons/epg_addon/src`, so fix that path if running from elsewhere.

## Architecture

**Entrypoint:** [optimization_plan.py](optimization_plan.py) wires everything together and owns
all APScheduler cron jobs: daily optimization (16:05 Europe/Brussels), battery SOC recalc
(every 15 min), load watcher + EV solar charge controller (every `load_watcher_interval_minutes`),
and periodic device verification (every 5 min). Read this file first when tracing what runs when.

**Two-tier configuration**, both pydantic/JSON-backed, loaded once at import time as module-level
globals (not re-read per request — restart the process to pick up changes):
- [src/config.py](src/config.py) — `CONFIG` dict from `/app/config.json`: HA connection, ENTSO-E
  token, load-watcher/battery tuning numbers.
- [src/devices_config.py](src/devices_config.py) — `devices_config` (pydantic `DevicesConfig`)
  from `/data/options.json`: the list of `Device` objects (type `wp`/`hw`/`battery`/`ev`), each
  carrying its MQTT/entity start-stop `ActionSet`s and type-specific tuning fields. This is the
  single source of truth for what devices exist; almost every module calls
  `devices_config.get_devices_by_type(...)`.

**Orchestration flow** (`HeatpumpOptimizer.run_optimization` in
[src/optimizer.py](src/optimizer.py), the largest and most central module):
1. Fetch ENTSO-E prices for a rolling horizon ([src/price_fetcher.py](src/price_fetcher.py)).
2. Compute battery charge/discharge price percentile thresholds from history
   ([src/forecasting/price_history.py](src/forecasting/price_history.py)).
3. Per device type, delegate to [src/optimization/](src/optimization/):
   - `thermal.py` — MILP via PuLP/GLPK for `wp`/`hw` block scheduling (respects min/max gap,
     locked slots already committed within `lock_hours`, and optional expected-daily-runtime from
     `runtime_calculator.py`).
   - `battery.py` — percentile/threshold-based charge & discharge slot selection.
   - `ev.py` — simple price-threshold selection (only for EVs *not* `solar_charge_only`).
   - `battery_limiter.py` — post-hoc SOC-aware cycle limiting, reconciling planned schedule against
     real current SOC (called again every 15 min by `recalculate_battery_limits`, independent of
     the once-daily full optimization).
4. Persist the merged schedule to **TinyDB** (`db.json`, at repo root) under doc id `"schedule"` —
   this file is the handoff point between optimization and scheduling/UI; also stores
   `"predictions"`, `"load_watcher"`, `"device_limitations"` docs.
5. [src/scheduler.py](src/scheduler.py) reads the TinyDB schedule and creates APScheduler
   `DateTrigger` jobs per start/stop event, resolving battery entries by suffix
   (`_charge`/`_discharge`/`_solar_only`/`_block_grid_export`) back to the right `ActionSet` on the
   `Device` config.

**Device action execution:** [src/devices/_core.py](src/devices/_core.py) (`Devices` class) is the
only place that calls Home Assistant services (`mqtt/publish`, entity services) — everything else
goes through it. Numeric fields in actions (`value`, `option`, MQTT `payload`) support the
`{expression}` mini-language evaluated by `evaluate_expression` in
[src/utils.py](src/utils.py) (safe AST subset: arithmetic + `round/int/float/abs/min/max/sqrt`,
context vars substituted by name — see [documentation/expressions.md](documentation/expressions.md)).
Every executed start/stop action registers with
[src/device_verifier.py](src/device_verifier.py), which re-checks entity/MQTT state on a fixed
schedule (6 checks over 3 minutes) and retries on mismatch, plus runs an independent periodic
sweep of all devices.

**Conditional gating:** [src/conditions.py](src/conditions.py) evaluates user-configured
`ConditionGroup`/`EntityCondition` trees against live HA state with an "unknown counts as false"
safety rule. Currently used for EV-readiness overrides of price-based grid-export blocking
(`grid_export_unblock_condition` on a `Device`) — both the scheduler's grid-export-block start job
and `Devices.execute_grid_export_block_start` consult `any_ev_ready()` before blocking export, so
an EV that needs the sun always wins over the price signal.

**EV solar charging** ([src/devices/ev_solar_charge.py](src/devices/ev_solar_charge.py), the
largest device module) is a separate control loop from price-based EV scheduling — active only
when a device has `solar_charge_only=True`; it dynamically sets charge current from live solar
surplus, handles phase switching/balancing, and battery-first lockout/discharge-blocking. It shares
the same `Devices` instance and HA client as the optimizer rather than owning its own connection.

**Load management** ([src/load_watcher/](src/load_watcher/)) is independent of the price
optimizer: it watches instantaneous grid load every `load_watcher_interval_minutes`, computes
available headroom against `max_peak_kW`, and applies per-device power limits
(`energy_monitor.py` → `peak_calculator.py` → `limit_calculator.py` → `limit_applier.py`), ordered
by each device's `load_management.load_priority`.

**Forecasting** ([src/forecasting/](src/forecasting/)) produces the ML usage/solar predictions
consumed by battery SOC limiting and the UI: `statistics_loader.py`/`weather.py` gather HA history
and weather, `prediction.py` runs the LightGBM models, `curtailment_history.py` excludes hours
where grid export was blocked from solar training (so curtailment doesn't get misread as low
production), `price_history.py` derives the battery percentile thresholds, `battery_soc_prediction.py`
projects SOC forward from the finalized schedule. Predictions are cached in TinyDB by
`_calculate_and_cache_predictions()` (run once at optimization time) rather than recomputed on
every 15-minute SOC recalculation.

**Web UI** ([web/server.py](web/server.py), Flask + Plotly, port 8099) reads only from TinyDB —
it has no write path back into optimization state. `/api/gantt` server-renders a Plotly figure
(schedule Gantt + price histogram + usage/solar/SOC predictions on shared x-axis) as an HTML
fragment consumed by [web/templates/index.html](web/templates/index.html).

## Conventions worth knowing

- Slot size is 15 minutes by default (`slot_minutes` in `config.json`); most time math flows
  through `slot_to_time`/`time_to_slot`/`slots_to_iso_ranges`/`merge_sequential_timeslots` in
  [src/utils.py](src/utils.py) — reuse these rather than re-deriving slot arithmetic.
  Battery/EV schedule-doc keys are built as `f"{device.name}_{suffix}"`
  (`_charge`, `_discharge`, `_solar_only`, `_block_grid_export`); the suffix table is duplicated
  between [src/scheduler.py](src/scheduler.py) and [src/device_verifier.py](src/device_verifier.py)
  — keep both in sync if it changes.
- Multiple devices of the same `type` are supported everywhere (the codebase iterates
  `devices_config.get_devices_by_type(...)`, not single named devices) — don't reintroduce
  single-device assumptions.
- TinyDB (`db.json`) is the only persistence layer and the sole handoff between the optimizer,
  scheduler, load watcher, and web UI — there is no other IPC between the daily optimization
  process and the Flask process beyond this file.
