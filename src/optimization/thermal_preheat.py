"""Whole-horizon heat-pump pre-heating via linear programming (PuLP/GLPK).

This is the thermal-mass counterpart of ``battery_lp.py``. Where that module treats the house
battery as a store of electricity, this one treats the heated floor slab as a store of *heat*:
one linear program over the whole price horizon decides a room-temperature setpoint trajectory
that pushes heating into the cheapest (and solar) hours and lets the slab coast through the
expensive ones, never crossing the comfort floor that applies at that time of day.

Why an LP (and why the old block/min-gap/max-gap MILP in ``thermal.py`` is the wrong model for a
weather-compensated floor-heating system):

- Floor heating never really turns *off*; it modulates a low flow temperature continuously, and
  the slab has an enormous thermal time constant (days, not minutes). The addon's only real lever
  is the zone *setpoint*. A block scheduler that toggles a device on and off for an hour cannot
  express "carry the house at 20.8 °C overnight on cheap power, then let it drift to 19.5 °C
  through the evening peak".
- Because the time constant far exceeds the optimization horizon, the slab is close to a loss-free
  heat store over a day: concentrating heat into the cheap overnight trough and midday solar, then
  coasting through peaks, costs almost nothing in comfort. The LP finds that automatically from the
  per-slot price / solar / outside-temperature inputs — no thresholds, no seasonal switch.

Physical model (first-order RC, one capacitance):

    C · (T[i+1] - T[i]) = q[i] - UA · (T[i] - Tout[i]) · Δt          (energy balance, kWh)

  T[i]    room temperature at the start of slot i (°C)        — decision variable
  q[i]    useful heat delivered to the house during slot i (kWh, ≥ 0) — decision variable
  C       effective heat capacity (kWh/°C) — how much heat raises the house 1 °C
  UA      heat-loss coefficient (kW/°C) — steady-state loss per °C of inside/outside difference
  Tout[i] forecast outside temperature during slot i (°C)
  Δt      slot length (hours)

Electrical cost couples in through the COP: the electricity drawn in slot i is ``q[i] / COP[i]``,
split (exactly like the battery's grid/solar charge split) into a grid part priced at the import
price and a solar part priced at the forgone export revenue, with the solar part capped by the
predicted PV surplus in that slot. ``COP[i]`` is derived from the forecast outside temperature.

Actuation: the per-slot ``T`` solution is rounded to the setpoint resolution and emitted as a
setpoint per slot. ``optimizer.recalculate_thermal_setpoints`` groups equal consecutive setpoints
into schedule-doc ranges carrying a ``setpoint`` value; the scheduler writes that value to the
device each range via the device's ``start`` action with ``{setpoint}`` in the payload. The plan is
re-solved every 15 min against the latest *measured* room temperature, the thermal analogue of the
battery re-solving against measured SOC.
"""
import logging
import math
from datetime import datetime, timedelta

import pulp

logger = logging.getLogger(__name__)

# Heat below this (kWh) in a slot is treated as "not heating" when summarising — keeps solver
# round-off from reporting spurious milliwatt-hours.
_EPS_KWH = 1e-4

# Penalty (EUR per °C of unmet comfort floor) on the soft slack that lets the LP stay feasible
# when the comfort floor cannot physically be held (e.g. heat rate too low on the coldest hour).
# Far above any real price so comfort is sacrificed only as a genuine last resort, and then by the
# smallest possible amount.
_COMFORT_SLACK_PENALTY = 1000.0

# Penalty (EUR per °C) on exceeding the pre-heat ceiling. The ceiling is soft because heating can
# only add heat (q >= 0); if the measured room temperature already sits above the ceiling the LP
# must be allowed to coast down at the passive-loss rate rather than becoming infeasible. The
# penalty keeps the solution at or below the ceiling whenever that is physically possible.
_CEILING_SLACK_PENALTY = 500.0


def _build_slot_series(predictions, horizon_start, slot_minutes, n_slots):
    """Project a list of ``{'timestamp', 'predicted_kwh'}`` dicts onto a per-slot kWh array.

    Identical in shape to ``battery_lp._build_slot_series``: values are already per-slot kWh, so
    this only places each prediction at its slot index. Missing slots default to 0.0.
    """
    series = [0.0] * n_slots
    if not predictions:
        return series
    for pred in predictions:
        t = pred['timestamp']
        if isinstance(t, str):
            t = datetime.fromisoformat(t.replace('Z', '+00:00'))
        if hasattr(t, 'replace'):
            t = t.replace(tzinfo=None)
        idx = int((t - horizon_start).total_seconds() / 60 / slot_minutes)
        if 0 <= idx < n_slots:
            series[idx] = max(0.0, float(pred['predicted_kwh']))
    return series


def _cop_per_slot(outside_temp, cop_ref, cop_slope, tout_ref, cop_min, cop_max):
    """Linear COP model as a function of outside temperature.

    COP falls as it gets colder (lower source temperature, higher flow temperature demand). A
    simple affine model ``cop_ref + cop_slope·(Tout − tout_ref)`` clamped to a sane band is enough
    here: the absolute level only scales electrical cost, and the *slope* is what makes the LP
    prefer to pre-heat on the milder part of the day. Pre-heating the slab hotter also lowers COP
    (higher flow temperature); that second-order effect is represented by the separate preheat
    penalty rather than making COP depend on the decision variable (which would break linearity).
    """
    out = []
    for t in outside_temp:
        c = cop_ref + cop_slope * (t - tout_ref)
        out.append(min(cop_max, max(cop_min, c)))
    return out


def plan_thermal_setpoints(
    prices: list[float],
    slot_minutes: int,
    horizon_start: datetime,
    current_temp: float | None,
    outside_temp: list[float],
    comfort_floor: list[float],
    max_setpoint: float,
    ua_kw_per_c: float,
    heat_capacity_kwh_per_c: float,
    max_heat_kw: float,
    predicted_solar: list[dict] | None = None,
    predicted_power_usage: list[dict] | None = None,
    cop_ref: float = 3.5,
    cop_slope: float = 0.08,
    tout_ref: float = 7.0,
    cop_min: float = 1.8,
    cop_max: float = 5.0,
    preheat_penalty_per_c: float = 0.0,
    setpoint_resolution: float = 0.5,
    previous_setpoint: float | None = None,
    export_prices: list[float] | None = None,
    export_block_threshold: float = 0.0,
    terminal_value_price: float | None = None,
    device_name: str = "wp",
) -> dict:
    """Optimize a heat-pump setpoint trajectory over the whole horizon with a single LP.

    Args:
        prices: import price per slot (EUR/kWh), one entry per horizon slot.
        slot_minutes: slot length in minutes.
        horizon_start: datetime of slot 0 (naive local time, as elsewhere in the codebase).
        current_temp: measured room temperature now (°C); ``None`` falls back to the first slot's
            comfort floor. This is the thermal "state of charge" the future is solved from.
        outside_temp: forecast outside temperature per slot (°C). Short series are padded with the
            last value; this must be at least length 1.
        comfort_floor: minimum acceptable room temperature per slot (°C), already expanded from the
            occupancy windows by the caller.
        max_setpoint: hard upper bound on the commanded setpoint (°C) — the pre-heat ceiling.
        ua_kw_per_c: heat-loss coefficient (kW/°C).
        heat_capacity_kwh_per_c: effective slab heat capacity (kWh/°C).
        max_heat_kw: maximum useful heat the system can deliver (kW); caps how fast it pre-charges.
        predicted_solar, predicted_power_usage: per-slot kWh prediction dicts; their difference is
            the PV surplus available to cover heating at ~zero marginal cost.
        cop_ref, cop_slope, tout_ref, cop_min, cop_max: parameters of the outside-temperature COP
            model (see ``_cop_per_slot``).
        preheat_penalty_per_c: extra cost (EUR per °C above the comfort floor, per slot) charged for
            carrying the slab hotter than needed — a linear stand-in for the COP loss of a higher
            flow temperature. 0 disables it.
        setpoint_resolution: rounding step for the commanded setpoint (°C), e.g. 0.5.
        previous_setpoint: the setpoint currently commanded for the slot in progress (°C), used to
            seed the anti-chatter hysteresis so the live command does not flip between re-solves.
        export_prices: revenue per exported kWh (EUR/kWh). Defaults to ``prices`` (dynamic
            injection ≈ day-ahead), clamped to 0 where below ``export_block_threshold``.
        export_block_threshold: price below which grid export is blocked.
        terminal_value_price: value credited per kWh of heat stored in the slab above the final
            comfort floor at the horizon end, so the plan does not dump heat in the last slots.
            Defaults to ``25th-percentile price / cop_ref`` — roughly the electrical cost to refill.
        device_name: for logging.

    Returns:
        dict with:
            setpoints: list of commanded setpoints per slot (°C), full horizon length; the entries
                before "now" are the applicable comfort floor (they are already in the past and are
                not scheduled, but keep the array aligned to slot indices).
            temps: predicted room-temperature trajectory per slot boundary (°C), length n+1, for UI.
            feasible: whether the LP solved to optimality.
            economics: summary numbers for logging / display.
    """
    n = len(prices)
    if n == 0:
        return {'setpoints': [], 'temps': [], 'feasible': False, 'economics': {}}

    slot_hours = slot_minutes / 60.0

    # Normalise per-slot input series to length n (pad outside temp / floor with their last value).
    def _pad(series, fallback):
        s = list(series or [])
        if not s:
            s = [fallback]
        if len(s) < n:
            s = s + [s[-1]] * (n - len(s))
        return s[:n]

    outside_temp = _pad(outside_temp, 10.0)
    comfort_floor = _pad(comfort_floor, min_floor := min(comfort_floor) if comfort_floor else 19.0)

    cop = _cop_per_slot(outside_temp, cop_ref, cop_slope, tout_ref, cop_min, cop_max)

    # Export revenue per slot: dynamic day-ahead by default, zeroed where export is blocked.
    if export_prices is None:
        export_prices = list(prices)
    export_prices = _pad([p if p > export_block_threshold else 0.0 for p in export_prices], 0.0)

    # Terminal value of heat left in the slab (anti-myopia; mirrors the battery LP). Clamped at 0
    # so a negative lower-quartile price (which happens on high-renewable days) can never flip the
    # terminal term into a penalty that would make the plan dump heat at the horizon end.
    if terminal_value_price is None:
        ordered = sorted(prices)
        p25 = ordered[max(0, int(0.25 * (len(ordered) - 1)))]
        terminal_value_price = max(0.0, p25) / cop_ref if cop_ref else max(0.0, p25)

    solar = _build_slot_series(predicted_solar, horizon_start, slot_minutes, n)
    usage = _build_slot_series(predicted_power_usage, horizon_start, slot_minutes, n)
    surplus = [max(0.0, solar[i] - usage[i]) for i in range(n)]

    # Solve from the current slot forward; the past is already executed and reflected in current_temp.
    now = datetime.now().replace(tzinfo=None)
    cur = max(0, int((now - horizon_start).total_seconds() / 60 / slot_minutes))

    def i2t(i):
        tot = i * slot_minutes
        return f"{tot // 60:02d}:{tot % 60:02d}"

    # Setpoints before "now" are not actionable; fill them with the comfort floor for alignment.
    setpoints = [round(comfort_floor[i] / setpoint_resolution) * setpoint_resolution for i in range(n)]

    if cur >= n:
        return {'setpoints': setpoints, 'temps': [], 'feasible': True,
                'economics': {'note': 'horizon elapsed'}}

    t0 = current_temp if current_temp is not None else comfort_floor[cur]

    e_slot = max_heat_kw * slot_hours  # max useful heat per slot (kWh)
    C = heat_capacity_kwh_per_c
    UA = ua_kw_per_c

    model = pulp.LpProblem(f"{device_name}_thermal", pulp.LpMinimize)
    F = range(cur, n)                 # slots whose heat we decide
    B = range(cur, n + 1)             # temperature boundaries cur..n

    # Boundary temperatures. The first is pinned to the measurement (which may sit above the
    # ceiling if the house is already warm). Both bounds are soft — a slack below the comfort floor
    # (``under``) and one above the ceiling (``over``) — so the LP is always feasible even when the
    # measurement starts outside the band and heating (q >= 0) cannot bring it back within one slot.
    T = {i: pulp.LpVariable(f"T_{i}", lowBound=0, upBound=max(max_setpoint, t0) + 5) for i in B}
    q = {i: pulp.LpVariable(f"q_{i}", lowBound=0, upBound=e_slot) for i in F}
    under = {i: pulp.LpVariable(f"u_{i}", lowBound=0) for i in B}   # comfort shortfall below floor (°C)
    over = {i: pulp.LpVariable(f"o_{i}", lowBound=0) for i in B}    # excess above the ceiling (°C)
    e_grid = {i: pulp.LpVariable(f"eg_{i}", lowBound=0) for i in F}
    e_solar = {i: pulp.LpVariable(f"es_{i}", lowBound=0, upBound=max(surplus[i], 0.0)) for i in F}

    # Comfort floor at a temperature boundary i (cur..n); the terminal boundary reuses the last
    # slot's floor since ``comfort_floor`` is indexed per slot (0..n-1).
    def cf(i):
        return comfort_floor[min(i, n - 1)]

    model += T[cur] == t0
    for i in F:
        # RC energy balance over the slot.
        model += C * (T[i + 1] - T[i]) == q[i] - UA * (T[i] - outside_temp[i]) * slot_hours
        # Electricity drawn = heat / COP, split into grid and solar-covered parts.
        model += e_grid[i] + e_solar[i] == q[i] / cop[i]
    for i in B:
        model += T[i] + under[i] >= cf(i)               # soft comfort floor
        if i > cur:
            model += T[i] <= max_setpoint + over[i]     # soft pre-heat ceiling (future only)

    model += (
        pulp.lpSum(prices[i] * e_grid[i] + export_prices[i] * e_solar[i]
                   + preheat_penalty_per_c * (T[i] - comfort_floor[i]) for i in F)
        + pulp.lpSum(_COMFORT_SLACK_PENALTY * under[i] + _CEILING_SLACK_PENALTY * over[i] for i in B)
        - terminal_value_price * C * T[n]
    )

    model.solve(pulp.GLPK(msg=0))
    feasible = pulp.LpStatus[model.status] == 'Optimal'
    if not feasible:
        logger.warning(f"⚠️ {device_name}: thermal LP not optimal "
                       f"(status={pulp.LpStatus[model.status]}); holding comfort floor this cycle")
        return {'setpoints': setpoints, 'temps': [], 'feasible': False, 'economics': {}}

    # Extract the trajectory and command a setpoint per future slot.
    temps = [None] * cur + [round(pulp.value(T[i]) or 0.0, 3) for i in B]
    grid_elec = solar_elec = heat_kwh = import_cost = 0.0
    peak_coast_c = 0.0
    # Hysteresis band for the commanded setpoint. The continuous optimum often sits near a rounding
    # boundary (e.g. ~20.7 °C with a 0.5 step), so plain nearest-rounding flips 20.5↔21.0 between
    # adjacent slots and between 15-min re-solves — pointless chatter on the heat pump. We instead
    # walk a sticky "staircase": keep the running commanded value until the target moves more than
    # this band away from it. ``running`` is seeded from the setpoint already commanded for the slot
    # in progress (``previous_setpoint``), so the live command is stable across re-solves too.
    band = setpoint_resolution * 0.75
    running = previous_setpoint
    for i in F:
        qi = pulp.value(q[i]) or 0.0
        eg = pulp.value(e_grid[i]) or 0.0
        es = pulp.value(e_solar[i]) or 0.0
        heat_kwh += qi
        grid_elec += eg
        solar_elec += es
        import_cost += prices[i] * eg
        # Command the setpoint for slot i as the target temperature at its end, clamped then
        # rounded. Round the comfort floor *up* to the resolution grid first, so nearest-rounding
        # can never emit a setpoint below the floor when the floor is not grid-aligned.
        tgt = pulp.value(T[i + 1]) or comfort_floor[i]
        tgt = min(max_setpoint, max(comfort_floor[i], tgt))
        floor_q = math.ceil(comfort_floor[i] / setpoint_resolution - 1e-9) * setpoint_resolution
        if running is not None and abs(tgt - running) < band and floor_q <= running <= max_setpoint:
            sp = running                                    # within the deadband — hold steady
        else:
            sp = min(max_setpoint, max(round(tgt / setpoint_resolution) * setpoint_resolution, floor_q))
        setpoints[i] = sp
        running = sp
        # Diagnostics: temperature given up by coasting (not heating) during an expensive slot.
        if prices[i] >= 0.40 and qi <= _EPS_KWH:
            drop = (pulp.value(T[i]) or 0.0) - (pulp.value(T[i + 1]) or 0.0)
            if drop > 0:
                peak_coast_c += drop

    comfort_shortfall = max((pulp.value(under[i]) or 0.0) for i in B) if n >= cur else 0.0
    end_temp = pulp.value(T[n]) or 0.0
    economics = {
        'heat_delivered_kwh': round(heat_kwh, 3),
        'grid_elec_kwh': round(grid_elec, 3),
        'solar_elec_kwh': round(solar_elec, 3),
        'import_cost_eur': round(import_cost, 3),
        'avg_import_price': round(import_cost / grid_elec, 4) if grid_elec > _EPS_KWH else None,
        'end_temp_c': round(end_temp, 2),
        'min_setpoint_c': round(min(setpoints[cur:]), 2) if cur < n else None,
        'max_setpoint_c': round(max(setpoints[cur:]), 2) if cur < n else None,
        'peak_coast_c': round(peak_coast_c, 2),
        'comfort_shortfall_c': round(comfort_shortfall, 3),
        'terminal_value_price': round(terminal_value_price, 4),
    }
    logger.info(
        f"🔥 {device_name}: thermal LP plan — deliver {heat_kwh:.1f} kWh heat "
        f"({grid_elec:.1f} kWh grid @ {economics['avg_import_price']}, {solar_elec:.1f} kWh solar), "
        f"setpoint {economics['min_setpoint_c']}–{economics['max_setpoint_c']} °C, "
        f"coast through peaks {peak_coast_c:.2f} °C, end {end_temp:.1f} °C"
        + (f", ⚠️ comfort short by {comfort_shortfall:.2f} °C" if comfort_shortfall > 0.05 else "")
    )
    return {'setpoints': setpoints, 'temps': temps, 'feasible': True, 'economics': economics}


def build_comfort_floor(horizon_start: datetime, n_slots: int, slot_minutes: int,
                        windows: list[dict] | None, default_min_temp: float) -> list[float]:
    """Expand occupancy windows into a per-slot comfort-floor array.

    Each window is ``{'start': 'HH:MM', 'end': 'HH:MM', 'min_temp': float, 'days': [..]}`` where
    ``days`` is an optional list of weekday names (``mon``..``sun``; omitted / empty = every day).
    A window may wrap past midnight (``end`` ≤ ``start``, e.g. sleep 23:00–06:30). Slots matched by
    several windows take the **highest** ``min_temp``; unmatched slots get ``default_min_temp``.
    """
    days_map = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
    floor = [default_min_temp] * n_slots
    for w in (windows or []):
        try:
            sh, sm = map(int, str(w['start']).split(':'))
            eh, em = map(int, str(w['end']).split(':'))
        except Exception:
            logger.warning(f"⚠️ thermal: skipping comfort window with bad time: {w}")
            continue
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        want_days = {days_map[d.lower()[:3]] for d in (w.get('days') or []) if d.lower()[:3] in days_map}
        mt = float(w.get('min_temp', default_min_temp))
        for i in range(n_slots):
            slot_dt = horizon_start + timedelta(minutes=i * slot_minutes)
            minute_of_day = slot_dt.hour * 60 + slot_dt.minute
            if start_min < end_min:
                in_win = start_min <= minute_of_day < end_min
                day_ref = slot_dt
            elif minute_of_day >= start_min:
                in_win = True            # evening part of a window that wraps past midnight
                day_ref = slot_dt
            elif minute_of_day < end_min:
                in_win = True            # morning tail — belongs to the day the window *started*
                day_ref = slot_dt - timedelta(days=1)
            else:
                in_win = False
                day_ref = slot_dt
            # ``days`` names the weekday the window starts on, so a "Fri 23:00–06:30" window raises
            # Friday night into Saturday morning (not the tail of Thursday night).
            if in_win and (not want_days or day_ref.weekday() in want_days):
                floor[i] = max(floor[i], mt)
    return floor
