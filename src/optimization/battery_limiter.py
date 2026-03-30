"""Battery cycle limiter based on SOC constraints.

This module implements SOC-aware battery cycle limiting that:
- Prioritizes cheapest slots for charging
- Prioritizes most expensive slots for discharging
- Respects min/max SOC constraints
- Simulates battery state over time to ensure feasibility
"""
import logging
import math
from datetime import datetime, timedelta

from ..config import CONFIG

logger = logging.getLogger(__name__)


def compute_soc_trajectory(
    charge_slots: set[int],
    discharge_slots: set[int],
    current_soc: float,
    battery_capacity_kwh: float,
    battery_charge_speed_kw: float,
    min_soc_percent: float,
    max_soc_percent: float,
    slot_minutes: int,
    start_slot: int,
    total_slots: int,
    usage_by_slot: dict | None = None,
    solar_by_slot: dict | None = None,
    solar_only_slots: set[int] | None = None,
) -> list[tuple[int, float]]:
    """Simulate battery SOC slot-by-slot over a range of slots.

    Iterates over every slot in [start_slot, total_slots] (inclusive) and
    applies charging, discharging, or solar-only passive charging as appropriate.

    Args:
        charge_slots: Set of slot indices scheduled for grid charging.
        discharge_slots: Set of slot indices scheduled for discharge (covers both
            regular and deep discharge).
        current_soc: Initial battery state of charge in percent (0–100).
        battery_capacity_kwh: Battery capacity in kWh.
        battery_charge_speed_kw: Maximum charge (and reference discharge) speed in kW.
        min_soc_percent: Minimum allowed SOC in percent.
        max_soc_percent: Maximum allowed SOC in percent.
        slot_minutes: Duration of each time slot in minutes.
        start_slot: First slot index to include in the output.
        total_slots: Total number of slots in the horizon (exclusive upper bound for
            SOC updates; an output record is produced for slot ``total_slots`` but no
            update is applied after it).
        usage_by_slot: Mapping of slot_idx → kWh of household consumption per slot.
        solar_by_slot: Mapping of slot_idx → kWh of solar production per slot.
        solar_only_slots: Set of slot indices where passive solar charging is active.
            Excess solar (production minus consumption) charges the battery up to the
            charge speed and max SOC cap.

    Returns:
        List of ``(slot_idx, soc_percent)`` tuples for every slot in
        ``[start_slot, total_slots]``.
    """
    slot_hours = slot_minutes / 60
    charge_energy_per_slot = battery_charge_speed_kw * slot_hours
    _usage = usage_by_slot or {}
    _solar = solar_by_slot or {}
    _solar_only = solar_only_slots or set()

    soc = float(current_soc)
    result: list[tuple[int, float]] = []

    for slot_idx in range(start_slot, total_slots + 1):
        result.append((slot_idx, round(soc, 1)))

        if slot_idx >= total_slots:
            break

        if slot_idx in charge_slots:
            headroom = battery_capacity_kwh * max(0.0, max_soc_percent - soc) / 100
            energy = min(charge_energy_per_slot, headroom)
            soc = min(max_soc_percent, soc + (energy / battery_capacity_kwh) * 100)

        elif slot_idx in discharge_slots:
            available = battery_capacity_kwh * max(0.0, soc - min_soc_percent) / 100
            gross_usage = _usage.get(slot_idx, 0.0)
            solar_offset = _solar.get(slot_idx, 0.0)
            net_usage = max(0.0, gross_usage - solar_offset)
            discharge_energy = min(net_usage, available)
            soc = max(min_soc_percent, soc - (discharge_energy / battery_capacity_kwh) * 100)

        elif slot_idx in _solar_only:
            # Passive solar charging: excess solar (beyond consumption) charges the battery
            gross_usage = _usage.get(slot_idx, 0.0)
            solar = _solar.get(slot_idx, 0.0)
            excess_solar = max(0.0, solar - gross_usage)
            headroom = battery_capacity_kwh * max(0.0, max_soc_percent - soc) / 100
            energy = min(excess_solar, charge_energy_per_slot, headroom)
            soc = min(max_soc_percent, soc + (energy / battery_capacity_kwh) * 100)

    return result


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
    deep_discharge_enabled: bool = False,
    deep_discharge_top_percent: float = 10.0,
) -> tuple[list[str], list[str], list[str]]:
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
        deep_discharge_enabled: When True, the top deep_discharge_top_percent of selected discharge
            slots (by price, most expensive first) are promoted to deep discharge slots.
        deep_discharge_top_percent: Percentage of selected discharge slots to promote to deep
            discharge (0-100). E.g. 10.0 means the 10% most expensive slots get deep discharge.

    Returns:
        Tuple of (limited_charge_times, limited_discharge_times, deep_discharge_times).
        When deep_discharge_enabled is False, deep_discharge_times is always an empty list.
        A slot appears in exactly one of limited_discharge_times or deep_discharge_times.
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
    
    # Resolve conflicts - this shouldn't happen, but just in case
    conflicts = charge_slots & discharge_slots
    if conflicts:
        logger.warning(f"⚠️ {device_name}: {len(conflicts)} slots have both charge and discharge, prioritizing discharge")
        charge_slots -= conflicts
    
    if not charge_slots and not discharge_slots:
        # No future slots to process - return past slots unchanged
        limited_charge_times = sorted([slot_idx_to_time(s) for s in past_charge_slots])
        limited_discharge_times = sorted([slot_idx_to_time(s) for s in past_discharge_slots])
        logger.info(f"🔋 {device_name}: No future slots, preserving {len(limited_charge_times)} past charge and {len(limited_discharge_times)} past discharge slots")
        return limited_charge_times, limited_discharge_times, []
    
    # Energy per slot when charging at full speed
    slot_hours = slot_minutes / 60
    charge_energy_per_slot = battery_charge_speed_kw * slot_hours
    
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
                logger.debug(f"🔋 {device_name}: Predicted usage for slot {slot_idx} before buffer: {pred['predicted_kwh']:.2f} kWh")
                usage_by_slot[slot_idx] = pred['predicted_kwh'] * slot_hours * discharge_buffer_multiplier

    # Build predicted solar production lookup (slot_idx -> kWh solar per slot)
    # Solar production reduces the net household demand that the battery needs to cover.
    solar_by_slot = {}
    if predicted_solar:
        for pred in predicted_solar:
            slot_idx = _pred_time_to_slot(pred['timestamp'])
            if slot_idx >= 0:
                solar_by_slot[slot_idx] = pred['predicted_kwh'] * slot_hours

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

        """Simulate SOC over time and return final SOC, feasibility, and energy statistics."""
        
        all_slots = sorted(selected_charge | selected_discharge)  # Sort over time
        soc = current_soc
        total_charged_kwh = 0
        total_discharged_kwh = 0
        
        for slot_idx in all_slots:
            if slot_idx in selected_charge:
                headroom = battery_capacity_kwh * (max_soc_percent - soc) / 100
                if headroom < charge_energy_per_slot * 0.1:
                    return None, False, 0, 0  # Can't charge - battery full
                energy = min(charge_energy_per_slot, headroom)
                soc += (energy / battery_capacity_kwh) * 100
                total_charged_kwh += energy
            elif slot_idx in selected_discharge:
                available = battery_capacity_kwh * (soc - min_soc_percent) / 100
                # Net demand = predicted usage minus solar production in this slot.
                # Solar covers part of household demand, reducing how much the battery needs to discharge.
                gross_usage = usage_by_slot.get(slot_idx, 0)
                solar_offset = solar_by_slot.get(slot_idx, 0)
                net_usage = max(0.0, gross_usage - solar_offset)
                discharge_energy = min(net_usage, available)
                if available < charge_energy_per_slot * 0.1:
                    return None, False, 0, 0  # Can't discharge - battery empty
                soc -= (discharge_energy / battery_capacity_kwh) * 100
                total_discharged_kwh += discharge_energy
        
        return soc, True, total_charged_kwh, total_discharged_kwh
    
    # Greedy selection: add slots in price order if they keep the schedule feasible
    selected_charge = set()
    selected_discharge = set()
    
    logger.info(f"🔋 {device_name}: Starting SOC limiting - current_soc={current_soc:.1f}%, "
                f"{len(charge_slots)} charge candidates, {len(discharge_slots)} discharge candidates")
    
    # First add discharge slots (most expensive first)
    logger.debug(f"🔋 {device_name}: Phase 1 - Adding discharge slots (most expensive first)")
    for slot_idx in discharge_by_price:
        candidate = selected_discharge | {slot_idx}
        slot_price = prices[slot_idx] if prices and slot_idx < len(prices) else None
        final_soc, feasible, charged_kwh, discharged_kwh = simulate_soc(selected_charge, candidate)
        if feasible:
            selected_discharge = candidate
            price_str = f", price={slot_price:.4f}" if slot_price is not None else ""
            logger.debug(f"🔋 {device_name}: ✓ Added discharge slot {slot_idx}, final_soc={final_soc:.1f}%{price_str}")
        else:
            price_str = f", price={slot_price:.4f}" if slot_price is not None else ""
            logger.debug(f"🔋 {device_name}: ✗ Rejected discharge slot {slot_idx} (SOC constraint violated{price_str})")
    
    # Get charge buffer percentage from config (default 20%)
    charge_buffer_percent = CONFIG['options'].get('battery_charge_buffer_percent', 20)
    
    logger.debug(f"🔋 {device_name}: Phase 2 - Adding charge slots (cheapest first, buffer={charge_buffer_percent}%)")
    logger.debug(f"🔋 {device_name}: Initial state: {len(selected_discharge)} discharge slots selected")
    
    # Then add charge slots (cheapest first) - but only if economically viable
    for slot_idx in charge_by_price:
        candidate = selected_charge | {slot_idx}
        slot_price = prices[slot_idx] if prices and slot_idx < len(prices) else None
        final_soc, feasible, charged_kwh, discharged_kwh = simulate_soc(candidate, selected_discharge)
        
        if not feasible:
            price_str = f", price={slot_price:.4f}" if slot_price is not None else ""
            logger.debug(f"🔋 {device_name}: ✗ Rejected charge slot {slot_idx} (SOC constraint violated{price_str})")
            continue
        
        # Check if adding this charge slot is economically viable
        # Only add charge if we haven't exceeded discharge needs by more than the buffer
        if discharged_kwh > 0:  # Only check if there are discharge slots
            max_charge_allowed = discharged_kwh * (1 + charge_buffer_percent / 100)
            if charged_kwh > max_charge_allowed:
                price_str = f", price={slot_price:.4f}" if slot_price is not None else ""
                logger.debug(f"🔋 {device_name}: ✗ Rejected charge slot {slot_idx} "
                           f"(charged {charged_kwh:.2f} kWh would exceed discharge needs "
                           f"{discharged_kwh:.2f} kWh + {charge_buffer_percent}% buffer = {max_charge_allowed:.2f} kWh{price_str})")
                continue
        
        selected_charge = candidate
        price_str = f", price={slot_price:.4f}" if slot_price is not None else ""
        energy_balance = charged_kwh - discharged_kwh
        logger.debug(f"🔋 {device_name}: ✓ Added charge slot {slot_idx}, "
                    f"charged={charged_kwh:.2f} kWh, discharged={discharged_kwh:.2f} kWh, "
                    f"balance={energy_balance:+.2f} kWh, final_soc={final_soc:.1f}%{price_str}")
        
        # Re-check if we can add more discharge slots now that we have more charge
        for d_slot in discharge_by_price:
            if d_slot not in selected_discharge:
                d_candidate = selected_discharge | {d_slot}
                d_final_soc, d_feasible, d_charged_kwh, d_discharged_kwh = simulate_soc(selected_charge, d_candidate)
                if d_feasible:
                    selected_discharge = d_candidate
                    d_slot_price = prices[d_slot] if prices and d_slot < len(prices) else None
                    price_str = f", price={d_slot_price:.4f}" if d_slot_price is not None else ""
                    logger.debug(f"🔋 {device_name}: ✓ Re-added discharge slot {d_slot} (now feasible with more charge, final_soc={d_final_soc:.1f}%{price_str})")
    
    # Combine past slots (unchanged) with limited future slots
    final_charge_slots = past_charge_slots | selected_charge
    final_discharge_slots = past_discharge_slots | selected_discharge
    
    logger.debug(f"🔋 {device_name}: Selection complete - {len(selected_charge)} future charge slots, "
                f"{len(selected_discharge)} future discharge slots added")
    
    # Convert back to time strings
    limited_charge_times = sorted([slot_idx_to_time(s) for s in final_charge_slots])
    limited_discharge_times = sorted([slot_idx_to_time(s) for s in final_discharge_slots])
    
    # Log results with price info if available
    final_soc, _, total_charged, total_discharged = simulate_soc(selected_charge, selected_discharge)
    energy_balance = total_charged - total_discharged
    
    if prices and selected_charge:
        avg_charge_price = sum(prices[s] for s in selected_charge if s < len(prices)) / len(selected_charge)
        logger.info(f"🔋 {device_name}: Selected {len(selected_charge)} charge slots (avg price: {avg_charge_price:.4f}, total: {total_charged:.2f} kWh)")
    if prices and selected_discharge:
        avg_discharge_price = sum(prices[s] for s in selected_discharge if s < len(prices)) / len(selected_discharge)
        logger.info(f"🔋 {device_name}: Selected {len(selected_discharge)} discharge slots (avg price: {avg_discharge_price:.4f}, total: {total_discharged:.2f} kWh)")
    
    # Split selected_discharge into regular and deep discharge
    # Past discharge slots are always kept as regular discharge (no split for past slots)
    deep_discharge_times: list[str] = []
    if deep_discharge_enabled and selected_discharge:
        # discharge_by_price is already sorted most-expensive-first; filter to selected slots
        ordered_selected = [s for s in discharge_by_price if s in selected_discharge]
        n_deep = math.ceil(len(ordered_selected) * deep_discharge_top_percent / 100)
        deep_slots = set(ordered_selected[:n_deep])
        regular_slots = set(ordered_selected[n_deep:])
        logger.info(
            f"🔋 {device_name}: Deep discharge split — "
            f"{n_deep} deep ({deep_discharge_top_percent}% of {len(ordered_selected)}) + "
            f"{len(regular_slots)} regular discharge slots"
        )
        # Past discharge slots stay in regular discharge (undifferentiated)
        final_discharge_slots = past_discharge_slots | regular_slots
        limited_discharge_times = sorted([slot_idx_to_time(s) for s in final_discharge_slots])
        deep_discharge_times = sorted([slot_idx_to_time(s) for s in deep_slots])
    else:
        if not deep_discharge_enabled:
            logger.debug(f"🔋 {device_name}: Deep discharge disabled")

    logger.info(f"🔋 {device_name}: Final - {len(limited_charge_times)} charge, {len(limited_discharge_times)} discharge, {len(deep_discharge_times)} deep discharge slots")
    logger.info(f"🔋 {device_name}: Energy balance: {energy_balance:+.2f} kWh, final_soc: {final_soc:.1f}% (started at {current_soc:.1f}%)")

    return limited_charge_times, limited_discharge_times, deep_discharge_times
