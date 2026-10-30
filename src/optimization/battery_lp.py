"""Whole-horizon battery optimization via linear programming (PuLP/GLPK).

This replaces the previous percentile-threshold slot selection (``battery.py``) and the greedy
SOC limiter (``battery_limiter.py``) with a single linear program solved over the entire price
horizon. One global optimization decides, per slot, whether to grid-charge, discharge to cover
household load, or hold (reserve) — honouring battery SOC dynamics, round-trip efficiency,
charge/discharge rate, predicted household load and solar, and prices.

Why an LP instead of the old heuristics:

- It is *globally* optimal rather than greedy, so it never spends cheap stored energy on a
  cheap hour when a dearer hour is still ahead — it reserves for the peak automatically because
  it sees the whole horizon at once.
- It needs no percentiles, time-percentages, price-difference thresholds or charge/discharge
  buffers. Those were all crude proxies for "is this slot worth it?" — a question the objective
  answers exactly.
- It handles seasons with no mode switch: in summer (large solar, small load) the cheapest way
  to serve the evening is to store the midday surplus, so the LP does that and barely grid-
  charges; in winter it arbitrages from the grid. Nothing special is coded for either — it
  falls out of the per-slot load/solar/price inputs.

Actuation is unchanged. The LP's per-slot decision is mapped back onto the existing schedule-doc
modes: a slot with grid charge becomes a ``_charge`` entry, a slot that discharges to load
becomes a ``_discharge`` entry, and every other slot is left empty — which the inverter already
treats as "hold and let PV charge" (the gap between scheduled slots). Storing solar surplus
therefore needs no explicit entry; it happens passively in the hold state, exactly as before.
"""
import logging
from datetime import datetime, timedelta

import pulp

logger = logging.getLogger(__name__)

# A slot's charge/discharge energy below this (kWh) is treated as "off" when mapping the LP
# solution back to discrete modes — it keeps solver round-off from emitting spurious 1 Wh slots.
_EPS_KWH = 1e-4


def _build_slot_series(predictions, horizon_start, slot_minutes, n_slots):
    """Project a list of {'timestamp', 'predicted_kwh'} dicts onto a per-slot kWh array.

    Values are already per-slot kWh (converted upstream by Prediction.get_cached_*), so this only
    places each prediction at its slot index. Missing slots default to 0.0.
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


def plan_battery_lp(
    prices: list[float],
    slot_minutes: int,
    horizon_start: datetime,
    current_soc: float | None,
    battery_capacity_kwh: float,
    battery_charge_speed_kw: float,
    min_soc_percent: float,
    max_soc_percent: float,
    predicted_power_usage: list[dict] | None = None,
    predicted_solar: list[dict] | None = None,
    round_trip_efficiency: float = 0.9,
    cycle_cost_per_kwh: float = 0.0,
    export_prices: list[float] | None = None,
    export_block_threshold: float = 0.0,
    terminal_value_price: float | None = None,
    previous_charge_times: list[str] | None = None,
    previous_discharge_times: list[str] | None = None,
    device_name: str = "battery",
) -> dict:
    """Optimize battery charge/discharge over the whole horizon with a single LP.

    Args:
        prices: import price per slot (EUR/kWh), one entry per horizon slot.
        slot_minutes: slot length in minutes.
        horizon_start: datetime of slot 0.
        current_soc: measured battery SOC in percent, or None (assumed at min).
        battery_capacity_kwh, battery_charge_speed_kw: battery physicals.
        min_soc_percent, max_soc_percent: usable SOC window.
        predicted_power_usage, predicted_solar: per-slot kWh prediction dicts.
        round_trip_efficiency: fraction surviving a charge/discharge cycle (loss on charge leg).
        cycle_cost_per_kwh: wear hurdle added per kWh put into the battery.
        export_prices: revenue per exported kWh (EUR/kWh). Defaults to ``prices`` (dynamic
            injection ≈ day-ahead), clamped to 0 where below ``export_block_threshold`` (export
            is blocked there, so surplus earns nothing rather than a negative price).
        export_block_threshold: price below which grid export is blocked.
        terminal_value_price: value credited per kWh of stored energy left at the horizon end,
            so the plan does not myopically dump the battery in the last slots. Defaults to
            ``round_trip_efficiency × 25th-percentile price`` — roughly what the energy cost to
            store, which keeps retained energy without inviting grid-charging purely to hoard.
        previous_charge_times, previous_discharge_times: previously committed times (HH:MM
            strings). Slots before "now" are preserved from these unchanged; the measured
            ``current_soc`` already reflects them, so only future slots are re-optimized.
        device_name: for logging.

    Returns:
        dict with:
            charge_times, discharge_times: HH:MM strings (relative to horizon_start) for the
                merged past + optimized-future plan, ready for ``_times_to_schedule``.
            feasible: whether the LP solved to optimality.
            economics: summary numbers for logging / display.
    """
    n = len(prices)
    if n == 0:
        return {'charge_times': [], 'discharge_times': [], 'feasible': False, 'economics': {}}

    slot_hours = slot_minutes / 60.0
    cap = battery_capacity_kwh
    e_max = battery_charge_speed_kw * slot_hours  # full-rate energy per slot (charge and discharge)
    eta = round_trip_efficiency if 0 < round_trip_efficiency <= 1 else 1.0
    s_min = min_soc_percent / 100.0 * cap
    s_max = max_soc_percent / 100.0 * cap

    # Export revenue per slot: dynamic day-ahead by default, zeroed where export is blocked.
    if export_prices is None:
        export_prices = list(prices)
    export_prices = [p if p > export_block_threshold else 0.0 for p in export_prices]

    # Terminal value of leftover stored energy (anti-myopia; see arg docs).
    if terminal_value_price is None:
        ordered = sorted(prices)
        p25 = ordered[max(0, int(0.25 * (len(ordered) - 1)))]
        terminal_value_price = eta * p25

    # Optimize from the current slot forward; preserve the past from the committed plan.
    def t2i(t):
        h, m = map(int, t.split(':'))
        return (h * 60 + m) // slot_minutes

    def i2t(i):
        tot = i * slot_minutes
        return f"{tot // 60:02d}:{tot % 60:02d}"

    now = datetime.now().replace(tzinfo=None)
    cur = max(0, int((now - horizon_start).total_seconds() / 60 / slot_minutes))

    prev_charge = {t2i(t) for t in (previous_charge_times or [])}
    prev_discharge = {t2i(t) for t in (previous_discharge_times or [])}
    past_charge = {i for i in prev_charge if i < cur}
    past_discharge = {i for i in prev_discharge if i < cur}

    if cur >= n:
        times_c = sorted(i2t(i) for i in past_charge)
        times_d = sorted(i2t(i) for i in past_discharge)
        return {'charge_times': times_c, 'discharge_times': times_d,
                'feasible': True, 'economics': {'note': 'horizon elapsed'}}

    load = _build_slot_series(predicted_power_usage, horizon_start, slot_minutes, n)
    solar = _build_slot_series(predicted_solar, horizon_start, slot_minutes, n)
    deficit = [max(0.0, load[i] - solar[i]) for i in range(n)]   # net load the battery may cover
    surplus = [max(0.0, solar[i] - load[i]) for i in range(n)]   # PV the battery may store

    s0 = s_min if current_soc is None else min(max(current_soc / 100.0 * cap, s_min), s_max)

    model = pulp.LpProblem(f"{device_name}_battery", pulp.LpMinimize)
    F = range(cur, n)  # future slots to decide
    gc = {i: pulp.LpVariable(f"gc_{i}", lowBound=0) for i in F}  # grid charge (AC in)
    sc = {i: pulp.LpVariable(f"sc_{i}", lowBound=0, upBound=max(surplus[i], 0)) for i in F}  # solar charge
    dd = {i: pulp.LpVariable(f"dd_{i}", lowBound=0, upBound=max(deficit[i], 0)) for i in F}  # discharge to load
    soc = {i: pulp.LpVariable(f"soc_{i}", lowBound=s_min, upBound=s_max) for i in range(cur, n + 1)}

    model += soc[cur] == s0
    for i in F:
        model += gc[i] + sc[i] <= e_max          # charge rate limit
        model += dd[i] <= e_max                   # discharge rate limit
        model += soc[i + 1] == soc[i] + eta * (gc[i] + sc[i]) - dd[i]  # SOC dynamics (loss on charge)

    # Objective: grid-charge costs import price; discharge saves import price; storing surplus
    # costs the forgone export revenue; wear per kWh charged; credit leftover terminal energy.
    model += (
        pulp.lpSum(prices[i] * gc[i] - prices[i] * dd[i]
                   + export_prices[i] * sc[i]
                   + cycle_cost_per_kwh * (gc[i] + sc[i]) for i in F)
        - terminal_value_price * soc[n]
    )

    status = model.solve(pulp.GLPK(msg=0))
    feasible = pulp.LpStatus[model.status] == 'Optimal'
    if not feasible:
        logger.warning(f"⚠️ {device_name}: battery LP not optimal (status={pulp.LpStatus[model.status]}); "
                       f"holding battery (no charge/discharge) this cycle")
        times_c = sorted(i2t(i) for i in past_charge)
        times_d = sorted(i2t(i) for i in past_discharge)
        return {'charge_times': times_c, 'discharge_times': times_d,
                'feasible': False, 'economics': {}}

    # Map the continuous solution back to discrete modes.
    fut_charge, fut_discharge = set(), set()
    grid_kwh = solar_kwh = disc_kwh = import_cost = disc_saving = 0.0
    for i in F:
        g, s, d = pulp.value(gc[i]) or 0.0, pulp.value(sc[i]) or 0.0, pulp.value(dd[i]) or 0.0
        if g > _EPS_KWH:
            fut_charge.add(i)
            grid_kwh += g
            import_cost += prices[i] * g
        if d > _EPS_KWH:
            fut_discharge.add(i)
            disc_kwh += d
            disc_saving += prices[i] * d
        solar_kwh += s

    charge_slots = past_charge | fut_charge
    discharge_slots = past_discharge | fut_discharge
    times_c = sorted(i2t(i) for i in charge_slots)
    times_d = sorted(i2t(i) for i in discharge_slots)

    end_soc_pct = (pulp.value(soc[n]) or 0.0) / cap * 100.0
    peak_disc_kwh = sum(pulp.value(dd[i]) or 0.0 for i in F if prices[i] >= 0.40)
    economics = {
        'grid_charge_kwh': round(grid_kwh, 3),
        'solar_charge_kwh': round(solar_kwh, 3),
        'discharge_kwh': round(disc_kwh, 3),
        'grid_charge_cost_eur': round(import_cost / eta if eta else import_cost, 3),
        'discharge_saving_eur': round(disc_saving, 3),
        'net_benefit_eur': round(disc_saving - import_cost / eta - cycle_cost_per_kwh * (grid_kwh + solar_kwh), 3),
        'end_soc_percent': round(end_soc_pct, 1),
        'terminal_value_price': round(terminal_value_price, 4),
        'avg_grid_charge_price': round(import_cost / grid_kwh, 4) if grid_kwh else None,
        'avg_discharge_price': round(disc_saving / disc_kwh, 4) if disc_kwh else None,
    }
    logger.info(
        f"🔋 {device_name}: LP plan — charge {len(fut_charge)} slots "
        f"({grid_kwh:.2f} kWh grid @ {economics['avg_grid_charge_price']}), "
        f"discharge {len(fut_discharge)} slots ({disc_kwh:.2f} kWh @ {economics['avg_discharge_price']}), "
        f"stored {solar_kwh:.2f} kWh solar, end SOC {end_soc_pct:.1f}%, "
        f"peak(≥0.40) covered {peak_disc_kwh:.2f} kWh, net {economics['net_benefit_eur']:+.2f} EUR"
    )
    return {'charge_times': times_c, 'discharge_times': times_d,
            'feasible': True, 'economics': economics}
