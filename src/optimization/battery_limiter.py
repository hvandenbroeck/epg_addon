"""Battery cycle limiter based on SOC constraints.

This module implements SOC-aware battery cycle limiting that:
- Prioritizes cheapest slots for charging
- Prioritizes most expensive slots for discharging
- Respects min/max SOC constraints
- Simulates battery state over time to ensure feasibility
"""
import logging
from datetime import datetime, timedelta
from typing import NamedTuple

from ..config import CONFIG

logger = logging.getLogger(__name__)


class SimResult(NamedTuple):
    """Outcome of simulating one candidate charge/discharge slot selection."""
    final_soc: float | None
    feasible: bool
    charged_kwh: float
    discharged_kwh: float
    grid_charged_kwh: float
    grid_charge_cost: float
    discharge_revenue: float


INFEASIBLE = SimResult(None, False, 0.0, 0.0, 0.0, 0.0, 0.0)


def limit_battery_cycles(
    charge_times: list[str],
    discharge_times: list[str],
    slot_minutes: int,
    horizon_start: datetime,
    current_soc: float | None,
    battery_capacity_kwh: float,
    battery_charge_speed_kw: float,
    min_soc_percent: float,
    max_soc_percent: float,
    prices: list[float] | None = None,
    predicted_power_usage: list[dict] | None = None,
    predicted_solar: list[dict] | None = None,
    device_name: str = "battery",
    previous_limited_charge_times: list[str] | None = None,
    previous_limited_discharge_times: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """
    Limit battery charge and discharge times based on battery capacity and SOC constraints.
    
    Prioritizes cheapest slots for charging and most expensive for discharging,
    while respecting SOC constraints over time.
    
    Args:
        charge_times: List of charge start times (strings like "HH:MM")
        discharge_times: List of discharge start times (strings like "HH:MM") 
        slot_minutes: Duration of each slot in minutes
        horizon_start: datetime when the horizon starts
        current_soc: Current battery state of charge in percent (0-100), or None
        battery_capacity_kwh: Battery capacity in kWh
        battery_charge_speed_kw: Battery charge speed in kW
        min_soc_percent: Minimum battery SOC in percent
        max_soc_percent: Maximum battery SOC in percent
        prices: List of prices per slot (used to prioritize cheap charge / expensive discharge)
        predicted_power_usage: List of dicts with 'timestamp' and 'predicted_kwh' keys, or None
        predicted_solar: List of dicts with 'timestamp' and 'predicted_kwh' keys for solar
            production per slot, or None. Solar production reduces net household demand from
            the battery during discharge slots.
        device_name: Name for logging purposes
        previous_limited_charge_times: Previously limited charge times (HH:MM strings) used to
            determine which past slots to preserve. If None, falls back to charge_times.
        previous_limited_discharge_times: Previously limited discharge times (HH:MM strings) used to
            determine which past slots to preserve. If None, falls back to discharge_times.

    Returns:
        Tuple of (limited_charge_times, limited_discharge_times)
    """
    logger.info(f"🔋 {device_name}: Input charge_times: {charge_times}")
    if not charge_times and not discharge_times:
        return [], []
    
    # Use current SOC or assume 0 % charged
    if current_soc is None:
        current_soc = 0
        logger.warning(f"⚠️ {device_name}: No SOC available, assuming {current_soc:.1f}%")
    
    # Helper: convert time string to slot index
    # Time strings are in HH:MM format relative to horizon_start (can exceed 23 hours)
    def time_to_slot_idx(time_str):
        hour, minute = map(int, time_str.split(':'))
        total_minutes = hour * 60 + minute
        return total_minutes // slot_minutes
    
    def slot_idx_to_time(slot_idx):
        total_minutes = slot_idx * slot_minutes
        return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"
    
    # Calculate current slot index to separate past from future
    now = datetime.now().replace(tzinfo=None)
    current_slot_idx = int((now - horizon_start).total_seconds() / 60 / slot_minutes)
    
    # Convert to slot indices
    charge_slots = {time_to_slot_idx(t) for t in charge_times} if charge_times else set()
    discharge_slots = {time_to_slot_idx(t) for t in discharge_times} if discharge_times else set()

    # Past slots are taken from the previously limited schedule so that we preserve
    # what was actually planned, not what the optimizer re-suggests for the past.
    prev_charge_slots = (
        {time_to_slot_idx(t) for t in previous_limited_charge_times}
        if previous_limited_charge_times is not None
        else charge_slots
    )
    prev_discharge_slots = (
        {time_to_slot_idx(t) for t in previous_limited_discharge_times}
        if previous_limited_discharge_times is not None
        else discharge_slots
    )

    # Separate past and future slots - preserve all past slots unchanged
    past_charge_slots = {s for s in prev_charge_slots if s < current_slot_idx}
    past_discharge_slots = {s for s in prev_discharge_slots if s < current_slot_idx}
    future_charge_slots = {s for s in charge_slots if s >= current_slot_idx}
    future_discharge_slots = {s for s in discharge_slots if s >= current_slot_idx}
    
    logger.info(f"🔋 {device_name}: Preserving {len(past_charge_slots)} past charge slots and {len(past_discharge_slots)} past discharge slots")
    
    # Only process future slots
    charge_slots = future_charge_slots
    discharge_slots = future_discharge_slots
    
    # A slot can legitimately be a candidate for both: the price-difference rules mark it
    # chargeable (a much dearer slot follows) and dischargeable (it's well above the day's
    # minimum) at once. Don't settle that up front - stripping it from the charge side here
    # used to leave no charge candidates at all ahead of an evening peak. The greedy selection
    # below assigns each slot to at most one side, so the SOC simulation decides instead.
    conflicts = charge_slots & discharge_slots
    if conflicts:
        logger.info(f"🔋 {device_name}: {len(conflicts)} slots are both charge and discharge candidates, "
                    f"resolved by SOC simulation")

    if not charge_slots and not discharge_slots:
        # No future slots to process - return past slots unchanged
        limited_charge_times = sorted([slot_idx_to_time(s) for s in past_charge_slots])
        limited_discharge_times = sorted([slot_idx_to_time(s) for s in past_discharge_slots])
        logger.info(f"🔋 {device_name}: No future slots, preserving {len(limited_charge_times)} past charge and {len(limited_discharge_times)} past discharge slots")
        return limited_charge_times, limited_discharge_times
    
    # Energy per slot when charging at full speed
    slot_hours = slot_minutes / 60
    charge_energy_per_slot = battery_charge_speed_kw * slot_hours

    # Round-trip losses are charged entirely to the import side: reaching a given SOC gain
    # costs more grid energy than the gain itself. Discharge revenue is then counted 1:1,
    # so the loss is applied once per cycle rather than double-counted on both legs.
    round_trip_efficiency = CONFIG['options'].get('battery_round_trip_efficiency', 0.9)
    if not 0 < round_trip_efficiency <= 1:
        logger.warning(f"⚠️ {device_name}: invalid battery_round_trip_efficiency "
                       f"{round_trip_efficiency}, falling back to 1.0 (no losses modelled)")
        round_trip_efficiency = 1.0
    cycle_cost_per_kwh = CONFIG['options'].get('battery_cycle_cost_per_kwh', 0.0)

    def _price_at(slot_idx):
        """Price for a slot, or None when no price series was supplied."""
        return prices[slot_idx] if prices and slot_idx < len(prices) else None

    # Build predicted usage lookup (slot_idx -> kWh usage per slot)
    # Apply discharge buffer to reduce predicted usage
    usage_by_slot = {}

    def _pred_time_to_slot(pred_time_raw):
        """Parse a prediction timestamp and return its slot index."""
        t = pred_time_raw
        if isinstance(t, str):
            t = datetime.fromisoformat(t.replace('Z', '+00:00'))
        if hasattr(t, 'replace'):
            t = t.replace(tzinfo=None)
        return int((t - horizon_start).total_seconds() / 60 / slot_minutes)

    if predicted_power_usage:
        discharge_buffer_percent = CONFIG['options'].get('battery_discharge_buffer_percent', 20)
        discharge_buffer_multiplier = 1.0 - (discharge_buffer_percent / 100.0)
        for pred in predicted_power_usage:
            slot_idx = _pred_time_to_slot(pred['timestamp'])
            if slot_idx >= 0:
                # Reduce predicted usage by the discharge buffer percentage
                # Note: pred['predicted_kwh'] is already per-slot kWh (converted upstream)
                logger.debug(f"🔋 {device_name}: Predicted usage for slot {slot_idx} before buffer: {pred['predicted_kwh']:.2f} kWh")
                usage_by_slot[slot_idx] = pred['predicted_kwh'] * discharge_buffer_multiplier

    # Build predicted solar production lookup (slot_idx -> kWh solar per slot)
    # Solar production reduces the net household demand that the battery needs to cover.
    # Note: pred['predicted_kwh'] is already per-slot kWh (converted upstream)
    solar_by_slot = {}
    if predicted_solar:
        for pred in predicted_solar:
            slot_idx = _pred_time_to_slot(pred['timestamp'])
            if slot_idx >= 0:
                solar_by_slot[slot_idx] = pred['predicted_kwh']

    if solar_by_slot:
        logger.debug(f"☀️ {device_name}: Solar production data available for {len(solar_by_slot)} slots")
    
    # Sort charge slots by price (cheapest first), discharge by price (most expensive first)
    if prices:
        charge_by_price = sorted(charge_slots, key=lambda s: prices[s] if s < len(prices) else float('inf'))
        discharge_by_price = sorted(discharge_slots, key=lambda s: -prices[s] if s < len(prices) else float('-inf'))
    else:
        # Without prices, just use time order
        charge_by_price = sorted(charge_slots)
        discharge_by_price = sorted(discharge_slots)
    
    def simulate_soc(selected_charge, selected_discharge):

        """Simulate SOC over time and return final SOC, feasibility, energy and cash flows."""

        all_slots = sorted(selected_charge | selected_discharge)  # Sort over time
        soc = current_soc
        total_charged_kwh = 0
        total_discharged_kwh = 0
        grid_charged_kwh = 0
        grid_charge_cost = 0
        discharge_revenue = 0

        for slot_idx in all_slots:
            price = _price_at(slot_idx)
            if slot_idx in selected_charge:
                headroom = battery_capacity_kwh * (max_soc_percent - soc) / 100
                if headroom < charge_energy_per_slot * 0.1:
                    return INFEASIBLE  # Can't charge - battery full
                energy = min(charge_energy_per_slot, headroom)
                soc += (energy / battery_capacity_kwh) * 100
                total_charged_kwh += energy
                grid_charged_kwh += energy
                if price is not None:
                    grid_charge_cost += price * energy / round_trip_efficiency
            elif slot_idx in selected_discharge:
                available = battery_capacity_kwh * (soc - min_soc_percent) / 100
                # Net demand = predicted usage minus solar production in this slot.
                # Solar covers part of household demand, reducing how much the battery needs to discharge.
                gross_usage = usage_by_slot.get(slot_idx, 0)
                solar_offset = solar_by_slot.get(slot_idx, 0)
                net_usage = gross_usage - solar_offset
                if net_usage < 0:
                    # Excess solar: charge the battery during a discharge slot.
                    # This energy is free, so it adds no grid cost - only wear.
                    headroom = battery_capacity_kwh * (max_soc_percent - soc) / 100
                    charge_energy = min(-net_usage, max(0.0, headroom), charge_energy_per_slot)
                    soc += (charge_energy / battery_capacity_kwh) * 100
                    total_charged_kwh += charge_energy
                else:
                    if available < charge_energy_per_slot * 0.1:
                        return INFEASIBLE  # Can't discharge - battery empty
                    discharge_energy = min(net_usage, available)
                    soc -= (discharge_energy / battery_capacity_kwh) * 100
                    total_discharged_kwh += discharge_energy
                    # Discharging serves household load, so the "revenue" is avoided import.
                    if price is not None:
                        discharge_revenue += price * discharge_energy

        return SimResult(soc, True, total_charged_kwh, total_discharged_kwh,
                         grid_charged_kwh, grid_charge_cost, discharge_revenue)

    def net_profit(sim):
        """Avoided import cost minus grid charge cost and battery wear, in EUR."""
        return (sim.discharge_revenue
                - sim.grid_charge_cost
                - cycle_cost_per_kwh * sim.charged_kwh)

    def discharge_pays(sim, baseline):
        """Whether a discharge slot that keeps the schedule feasible is also worth taking.

        Feasibility alone is not enough: discharge is modelled as partial (a slot takes what
        is left), so a cheap early slot stays "feasible" while draining the energy a dearer
        later slot was counting on - which is how a 0.24 morning used to empty the battery
        before a 0.47 evening peak. Accept the slot only if avoided import goes up, or - for a
        slot with solar surplus, which charges for free - at least doesn't go down.
        """
        if not sim.feasible:
            return False
        if not prices:
            return True
        eps = 1e-9
        if sim.discharge_revenue > baseline.discharge_revenue + eps:
            return True
        return (sim.discharge_revenue >= baseline.discharge_revenue - eps
                and sim.charged_kwh > baseline.charged_kwh + eps)

    # Greedy selection: add slots in price order if they keep the schedule feasible and pay
    selected_charge = set()
    selected_discharge = set()

    logger.info(f"🔋 {device_name}: Starting SOC limiting - current_soc={current_soc:.1f}%, "
                f"{len(charge_slots)} charge candidates, {len(discharge_slots)} discharge candidates")

    # First add discharge slots (most expensive first)
    logger.debug(f"🔋 {device_name}: Phase 1 - Adding discharge slots (most expensive first)")
    for slot_idx in discharge_by_price:
        candidate = selected_discharge | {slot_idx}
        slot_price = _price_at(slot_idx)
        price_str = f", price={slot_price:.4f}" if slot_price is not None else ""
        baseline = simulate_soc(selected_charge, selected_discharge)
        sim = simulate_soc(selected_charge, candidate)
        if discharge_pays(sim, baseline):
            selected_discharge = candidate
            logger.debug(f"🔋 {device_name}: ✓ Added discharge slot {slot_idx}, final_soc={sim.final_soc:.1f}%{price_str}")
        elif sim.feasible:
            logger.debug(f"🔋 {device_name}: ✗ Rejected discharge slot {slot_idx} "
                         f"(would take energy from dearer slots{price_str})")
        else:
            logger.debug(f"🔋 {device_name}: ✗ Rejected discharge slot {slot_idx} (SOC constraint violated{price_str})")
    
    # Get charge buffer percentage from config (default 20%)
    charge_buffer_percent = CONFIG['options'].get('battery_charge_buffer_percent', 20)
    
    logger.debug(f"🔋 {device_name}: Phase 2 - Adding charge slots (cheapest first, buffer={charge_buffer_percent}%)")
    logger.debug(f"🔋 {device_name}: Initial state: {len(selected_discharge)} discharge slots selected")
    
    def expand_discharge(charge_set, discharge_set, min_price=None):
        """Add every discharge slot the given charge set makes feasible and worthwhile, most
        expensive first.

        min_price is the break-even for energy bought by the charge slot being judged: a slot
        below it would only turn that grid energy into a loss, so it isn't offered at all.
        Otherwise the charge slot's spread check would see a blend - some energy to the peak,
        some to slots that don't cover the round-trip losses - and pass it anyway.
        """
        expanded = set(discharge_set)
        for d_slot in discharge_by_price:
            if d_slot in expanded or d_slot in charge_set:
                continue
            d_price = _price_at(d_slot)
            if min_price is not None and d_price is not None and d_price < min_price:
                break  # discharge_by_price is most expensive first, so the rest are cheaper still
            d_candidate = expanded | {d_slot}
            if discharge_pays(simulate_soc(charge_set, d_candidate), simulate_soc(charge_set, expanded)):
                expanded = d_candidate
        return expanded

    # Then add charge slots (cheapest first) - but only if economically viable
    for slot_idx in charge_by_price:
        candidate = selected_charge | {slot_idx}
        slot_price = _price_at(slot_idx)
        price_str = f", price={slot_price:.4f}" if slot_price is not None else ""

        if slot_idx in selected_discharge:
            logger.debug(f"🔋 {device_name}: ✗ Rejected charge slot {slot_idx} (already a discharge slot{price_str})")
            continue

        if not simulate_soc(candidate, selected_discharge).feasible:
            logger.debug(f"🔋 {device_name}: ✗ Rejected charge slot {slot_idx} (SOC constraint violated{price_str})")
            continue

        # Charging raises SOC, which unlocks discharge slots that were previously infeasible.
        # Those have to be folded in before judging the slot, because they are precisely what
        # repays it - scoring against the un-expanded set would reject even a very wide spread.
        break_even = (slot_price / round_trip_efficiency + cycle_cost_per_kwh
                      if slot_price is not None else None)
        candidate_discharge = expand_discharge(candidate, selected_discharge, break_even)
        sim = simulate_soc(candidate, candidate_discharge)
        baseline = simulate_soc(selected_charge, selected_discharge)

        # Check if adding this charge slot is economically viable
        # Only add charge if we haven't exceeded discharge needs by more than the buffer
        if sim.discharged_kwh > 0:  # Only check if there are discharge slots
            max_charge_allowed = sim.discharged_kwh * (1 + charge_buffer_percent / 100)
            if sim.charged_kwh > max_charge_allowed:
                logger.debug(f"🔋 {device_name}: ✗ Rejected charge slot {slot_idx} "
                           f"(charged {sim.charged_kwh:.2f} kWh would exceed discharge needs "
                           f"{sim.discharged_kwh:.2f} kWh + {charge_buffer_percent}% buffer = {max_charge_allowed:.2f} kWh{price_str})")
                continue

        # Price sanity only: when this slot both buys and delivers energy, what it delivers
        # must be worth more than it cost. Compared as energy-weighted *rates* rather than
        # totals, because charging runs at a fixed rate while discharge only trickles out at
        # household demand - so one charge slot is repaid across several thinner discharge
        # slots, and comparing totals would let the extra volume subsidise a spread that
        # never covered its losses.
        # How *much* to charge is deliberately left to charge_buffer_percent above. That
        # buffer is the hedge against an imperfect usage prediction and the value of cheap
        # energy carried past the horizon - neither of which is visible from inside a single
        # horizon, so a slot that shifts no extra energy here is simply not priced.
        spread = None
        if prices and baseline.feasible and sim.discharged_kwh > 0:
            extra_grid_kwh = sim.grid_charged_kwh - baseline.grid_charged_kwh
            extra_discharge_kwh = sim.discharged_kwh - baseline.discharged_kwh
            if extra_grid_kwh > 0 and extra_discharge_kwh > 0:
                charge_rate = (sim.grid_charge_cost - baseline.grid_charge_cost) / extra_grid_kwh
                discharge_rate = (sim.discharge_revenue - baseline.discharge_revenue) / extra_discharge_kwh
                spread = discharge_rate - charge_rate - cycle_cost_per_kwh
                if spread <= 0:
                    logger.debug(f"🔋 {device_name}: ✗ Rejected charge slot {slot_idx} "
                               f"(discharges at {discharge_rate:.4f} but costs {charge_rate:.4f} EUR/kWh "
                               f"after {round_trip_efficiency:.0%} round-trip losses, "
                               f"spread {spread:+.4f}{price_str})")
                    continue

        newly_enabled = candidate_discharge - selected_discharge
        selected_charge = candidate
        selected_discharge = candidate_discharge
        energy_balance = sim.charged_kwh - sim.discharged_kwh
        logger.debug(f"🔋 {device_name}: ✓ Added charge slot {slot_idx}, "
                    f"charged={sim.charged_kwh:.2f} kWh, discharged={sim.discharged_kwh:.2f} kWh, "
                    f"balance={energy_balance:+.2f} kWh, final_soc={sim.final_soc:.1f}%{price_str}"
                    + (f", spread {spread:+.4f} EUR/kWh" if spread is not None else "")
                    + (f", enabled {len(newly_enabled)} more discharge slots" if newly_enabled else ""))
    
    # Combine past slots (unchanged) with limited future slots
    final_charge_slots = past_charge_slots | selected_charge
    final_discharge_slots = past_discharge_slots | selected_discharge
    
    logger.debug(f"🔋 {device_name}: Selection complete - {len(selected_charge)} future charge slots, "
                f"{len(selected_discharge)} future discharge slots added")
    
    # Convert back to time strings
    limited_charge_times = sorted([slot_idx_to_time(s) for s in final_charge_slots])
    limited_discharge_times = sorted([slot_idx_to_time(s) for s in final_discharge_slots])
    
    # Log results with price info if available
    final_sim = simulate_soc(selected_charge, selected_discharge)
    total_charged = final_sim.charged_kwh
    total_discharged = final_sim.discharged_kwh
    final_soc = final_sim.final_soc
    energy_balance = total_charged - total_discharged

    # Averages are energy-weighted: a charge slot moves a full charge_energy_per_slot while a
    # discharge slot only moves what the house drew, so per-slot means are not comparable.
    if prices and selected_charge:
        avg_charge_price = (final_sim.grid_charge_cost * round_trip_efficiency / final_sim.grid_charged_kwh
                            if final_sim.grid_charged_kwh else 0)
        logger.info(f"🔋 {device_name}: Selected {len(selected_charge)} charge slots (avg price: {avg_charge_price:.4f}, total: {total_charged:.2f} kWh)")
    if prices and selected_discharge:
        avg_discharge_price = (final_sim.discharge_revenue / total_discharged
                               if total_discharged else 0)
        logger.info(f"🔋 {device_name}: Selected {len(selected_discharge)} discharge slots (avg price: {avg_discharge_price:.4f}, total: {total_discharged:.2f} kWh)")
    if prices:
        logger.info(f"🔋 {device_name}: Cycle economics: cost {final_sim.grid_charge_cost:.2f} EUR, "
                    f"avoided import {final_sim.discharge_revenue:.2f} EUR, "
                    f"net {net_profit(final_sim):+.2f} EUR "
                    f"(round-trip efficiency {round_trip_efficiency:.0%}, "
                    f"wear {cycle_cost_per_kwh:.4f} EUR/kWh)")
    if prices and not selected_charge and charge_slots:
        logger.info(f"🔋 {device_name}: No charge slots selected out of {len(charge_slots)} candidates - "
                    f"either the price spread did not cover {round_trip_efficiency:.0%} round-trip losses "
                    f"or SOC limits left no room (enable debug logging for the per-slot reason)")
    
    logger.info(f"🔋 {device_name}: Final - {len(limited_charge_times)} charge, {len(limited_discharge_times)} discharge slots")
    logger.info(f"🔋 {device_name}: Energy balance: {energy_balance:+.2f} kWh, final_soc: {final_soc:.1f}% (started at {current_soc:.1f}%)")
    
    return limited_charge_times, limited_discharge_times
