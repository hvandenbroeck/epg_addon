"""Battery cycle limiter based on SOC constraints.

This module implements SOC-aware battery cycle limiting that:
- Prioritizes cheapest slots for charging
- Prioritizes most expensive slots for discharging
- Respects min/max SOC constraints
- Simulates battery state over time to ensure feasibility
"""
import logging
from datetime import datetime, timedelta

from ..config import CONFIG

logger = logging.getLogger(__name__)


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
    device_name: str = "battery"
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
        device_name: Name for logging purposes
        
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
    
    # Separate past and future slots - preserve all past slots unchanged
    past_charge_slots = {s for s in charge_slots if s < current_slot_idx}
    past_discharge_slots = {s for s in discharge_slots if s < current_slot_idx}
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
        return [], []
    
    # Energy per slot when charging at full speed
    slot_hours = slot_minutes / 60
    charge_energy_per_slot = battery_charge_speed_kw * slot_hours
    
    # Build predicted usage lookup (slot_idx -> kWh usage per slot)
    # Apply discharge buffer to reduce predicted usage
    usage_by_slot = {}
    if predicted_power_usage:
        discharge_buffer_percent = CONFIG['options'].get('battery_discharge_buffer_percent', 20)
        discharge_buffer_multiplier = 1.0 - (discharge_buffer_percent / 100.0)
        for pred in predicted_power_usage:
            pred_time = pred['timestamp']
            if isinstance(pred_time, str):
                pred_time = datetime.fromisoformat(pred_time.replace('Z', '+00:00'))
            if hasattr(pred_time, 'replace'):
                pred_time = pred_time.replace(tzinfo=None)
            slot_idx = int((pred_time - horizon_start).total_seconds() / 60 / slot_minutes)
            if slot_idx >= 0:
                # Reduce predicted usage by the discharge buffer percentage
                usage_by_slot[slot_idx] = pred['predicted_kwh'] * slot_hours * discharge_buffer_multiplier
    
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
        
        all_slots = sorted(selected_charge | selected_discharge) #Sort over time
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
                # Use battery's max discharge rate, not predicted usage
                # (predicted usage is just a hint for prioritization, not a constraint)
                discharge_energy = min(charge_energy_per_slot, available)
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
    
    logger.info(f"🔋 {device_name}: Final - {len(limited_charge_times)} charge, {len(limited_discharge_times)} discharge slots")
    logger.info(f"🔋 {device_name}: Energy balance: {energy_balance:+.2f} kWh, final_soc: {final_soc:.1f}% (started at {current_soc:.1f}%)")
    
    return limited_charge_times, limited_discharge_times
