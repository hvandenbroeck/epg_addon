import pulp
import logging
from datetime import datetime, timedelta
import numpy as np

logger = logging.getLogger(__name__)


def optimize_thermal_device(
    prices,
    slot_minutes,
    block_hours,
    min_gap_hours,
    max_gap_hours,
    locked_slots,
    initial_gap_slots,
    horizon_start_datetime,
    device_name="device"
):
    """
    Optimize heat pump or hot water operation using sliding window constraints.
    
    This function implements a rolling horizon optimization with:
    - Minimum run time (block_hours) - device runs at least this long each time
    - Minimum gap between runs (min_gap_hours) - prevents rapid cycling
    - Maximum gap between runs (max_gap_hours) - ensures device runs regularly
    - Support for locked slots (already scheduled/executed)
    - Initial gap state from previous schedule
    
    Args:
        prices: List of prices per slot for the entire horizon
        slot_minutes: Duration of each slot in minutes (e.g., 15 or 60)
        block_hours: Minimum runtime in hours when device turns on
        min_gap_hours: Minimum hours between end of one run and start of next
        max_gap_hours: Maximum hours allowed without running (pause limit)
        locked_slots: Set of slot indices that are locked (already scheduled/executed)
        initial_gap_slots: Number of slots since last run at start of horizon
        horizon_start_datetime: datetime when the horizon starts (for day boundary calc)
        device_name: Name for logging purposes
        
    Returns:
        List of slot indices where the device should start running
    """
    n_slots = len(prices)
    if n_slots == 0:
        return []
    
    block_len = int(block_hours * 60 / slot_minutes)  # slots per block
    min_gap_slots = int(min_gap_hours * 60 / slot_minutes)
    max_gap_slots = int(max_gap_hours * 60 / slot_minutes)
    
    # Window size for "max gap" constraint: if max_gap is 6 hours, we can't have
    # 7 consecutive hours off, so window = max_gap_slots + block_len
    # (ensuring at least one block runs within any max_gap window)
    window_size = max_gap_slots + block_len
    
    logger.info(f"🔧 Optimizing {device_name}: {n_slots} slots, block={block_len} slots, "
                f"min_gap={min_gap_slots}, max_gap={max_gap_slots}, initial_gap={initial_gap_slots}")
    
    # Create the optimization model
    model = pulp.LpProblem(f"{device_name}_Optimization", pulp.LpMinimize)
    
    # Decision variables: x[i] = 1 if device STARTS at slot i
    # Only create variables for valid start positions (not too close to end)
    valid_starts = range(n_slots - block_len + 1)
    x = {i: pulp.LpVariable(f"x_{i}", cat="Binary") for i in valid_starts}
    
    # Helper: y[t] = 1 if device is RUNNING at slot t
    # y[t] = sum of x[j] for all j where starting at j would cover slot t
    def slots_covering(t):
        """Return start indices where the block would cover slot t."""
        return [j for j in range(max(0, t - block_len + 1), min(t + 1, len(valid_starts)))]
    
    # Objective: minimize total cost
    # Cost of starting at slot i = sum of prices for slots i to i+block_len-1
    window_costs = {}
    for i in valid_starts:
        window_costs[i] = sum(prices[i:i + block_len])
    
    model += pulp.lpSum([window_costs[i] * x[i] for i in valid_starts])
    
    # Constraint 1: No overlapping blocks
    # At any slot, at most one block can be running
    for t in range(n_slots):
        covering = slots_covering(t)
        if covering:
            model += pulp.lpSum([x[j] for j in covering if j in x]) <= 1
    
    # Constraint 2: Minimum gap between consecutive runs
    # If a block starts at i, no block can start until i + block_len + min_gap_slots
    for i in valid_starts:
        forbidden_range = range(i + 1, min(i + block_len + min_gap_slots, len(valid_starts)))
        for j in forbidden_range:
            if j in x:
                model += x[i] + x[j] <= 1
    
    # Constraint 3: Maximum gap (sliding window) - must run at least once in any window
    # For the initial part of the horizon, account for initial_gap_slots
    # If we've already been off for initial_gap_slots, the first run must happen sooner
    
    # Calculate effective window starts considering initial gap
    # At slot 0, we've already been off for initial_gap_slots
    # So we need to run within (max_gap_slots - initial_gap_slots) slots
    remaining_allowed_gap = max(0, max_gap_slots - initial_gap_slots)
    
    # First window: must have at least one run starting within remaining_allowed_gap
    # The run covers block_len slots, so valid starts are 0 to remaining_allowed_gap
    if remaining_allowed_gap < n_slots - block_len + 1:
        first_window_starts = range(0, min(remaining_allowed_gap + 1, len(valid_starts)))
        if first_window_starts:
            model += pulp.lpSum([x[i] for i in first_window_starts if i in x]) >= 1
            logger.debug(f"Initial gap constraint: must start within slots 0-{remaining_allowed_gap}")
    
    # Regular sliding window constraints for the rest of the horizon
    # For each window of (max_gap_slots + block_len) consecutive slots, at least one must be running
    for window_start in range(0, n_slots - window_size + 1):
        # All start positions that would cause the device to run within this window
        window_end = window_start + window_size
        # A block starting at i runs from i to i+block_len-1
        # It's active in window [window_start, window_end) if any of those slots are covered
        active_starts = [i for i in valid_starts if i < window_end and i + block_len > window_start]
        if active_starts:
            model += pulp.lpSum([x[i] for i in active_starts if i in x]) >= 1
    
    # Constraint 4: Respect locked slots
    # If a slot is locked, the corresponding x must be 1
    for locked_start in locked_slots:
        if locked_start in x:
            model += x[locked_start] == 1
            logger.debug(f"Locked slot: {locked_start}")
    
    # Solve the model
    model.solve(pulp.GLPK(msg=0))
    
    if model.status != pulp.LpStatusOptimal:
        logger.warning(f"⚠️ {device_name} optimization did not find optimal solution. Status: {pulp.LpStatus[model.status]}")
        # Try to return something reasonable - just the locked slots
        return list(locked_slots)
    
    # Extract results
    starts = [i for i in valid_starts if pulp.value(x[i]) == 1]
    logger.info(f"✅ {device_name} optimization complete: {len(starts)} runs scheduled at slots {starts}")
    
    return starts


def optimize_wp(
    prices,
    slot_minutes,
    block_hours,
    min_gap_hours,
    max_gap_hours,
    locked_slots,
    initial_gap_slots,
    horizon_start_datetime,
    slot_to_time
):
    """Optimize heat pump operation using sliding window constraints."""
    starts = optimize_thermal_device(
        prices=prices,
        slot_minutes=slot_minutes,
        block_hours=block_hours,
        min_gap_hours=min_gap_hours,
        max_gap_hours=max_gap_hours,
        locked_slots=locked_slots,
        initial_gap_slots=initial_gap_slots,
        horizon_start_datetime=horizon_start_datetime,
        device_name="WP"
    )
    return [slot_to_time(i, slot_minutes) for i in starts]


def optimize_hw(
    prices,
    slot_minutes,
    block_hours,
    min_gap_hours,
    max_gap_hours,
    locked_slots,
    initial_gap_slots,
    horizon_start_datetime,
    slot_to_time
):
    """Optimize hot water operation using sliding window constraints."""
    starts = optimize_thermal_device(
        prices=prices,
        slot_minutes=slot_minutes,
        block_hours=block_hours,
        min_gap_hours=min_gap_hours,
        max_gap_hours=max_gap_hours,
        locked_slots=locked_slots,
        initial_gap_slots=initial_gap_slots,
        horizon_start_datetime=horizon_start_datetime,
        device_name="HW"
    )
    return [slot_to_time(i, slot_minutes) for i in starts]


def optimize_battery(prices, slot_minutes, slot_to_time, 
                     max_charge_price=None, price_difference_threshold=None):
    """
    Optimize battery charging periods using percentile-based price thresholds.
    Returns all eligible charging slots (limiting is done in limit_battery_cycles).
    
    Args:
        prices: List of prices per slot
        slot_minutes: Duration of each slot in minutes
        slot_to_time: Function to convert slot index to time
        max_charge_price: Maximum price threshold for charging (from historical percentile).
                         If None, uses fallback based on horizon prices.
        price_difference_threshold: Additional threshold for opportunistic charging.
                                   Mark slot as charge if any future slot is more expensive by this amount.
        
    Returns:
        List of start times for battery charging
    """
    if not prices:
        return []
    
    # Use provided max_charge_price or calculate fallback from current horizon
    if max_charge_price is None:
        # Fallback: use 30th percentile of current horizon prices
        max_charge_price = float(np.percentile(prices, 30))
        logger.info(f"🔋 Battery charge: using fallback threshold {max_charge_price:.4f} EUR/kWh (30th percentile of horizon)")
    else:
        logger.info(f"🔋 Battery charge: using historical threshold {max_charge_price:.4f} EUR/kWh")
    
    # Filter slots that are below the max charge price threshold
    eligible_slots = [(i, prices[i]) for i in range(len(prices)) if prices[i] <= max_charge_price]
    logger.info(f"🔋 Battery charge: initially {len(eligible_slots)} eligible slots below threshold {max_charge_price:.4f} EUR/kWh")

    # Add price difference logic: mark slots as charge slots if there's a future slot more expensive by threshold
    if price_difference_threshold is not None and price_difference_threshold > 0:
        logger.info(f"🔋 Battery charge: applying price difference threshold {price_difference_threshold:.4f} EUR/kWh")
        for i in range(len(prices)):
            current_price = prices[i]
            # Check if any future slot is more expensive by at least the threshold
            has_expensive_future = any(
                prices[j] >= current_price + price_difference_threshold 
                for j in range(i + 1, len(prices))
            )
            if has_expensive_future:
                # Add to eligible slots if not already there
                if not any(slot_idx == i for slot_idx, _ in eligible_slots):
                    eligible_slots.append((i, current_price))
        logger.info(f"🔋 Battery charge: after price difference logic, {len(eligible_slots)} eligible slots")
    
    if not eligible_slots:
        logger.warning(f"⚠️ No slots below max_charge_price={max_charge_price:.4f} EUR/kWh")
        return []
    
    # Sort by price and return all eligible slots (limiting is done later)
    eligible_slots.sort(key=lambda x: x[1])
    selected_slots = [slot for slot, _ in eligible_slots]
    
    # Log selection info
    if selected_slots:
        avg_charge_price = sum(prices[i] for i in selected_slots) / len(selected_slots)
        min_price = min(prices[i] for i in selected_slots)
        max_selected_price = max(prices[i] for i in selected_slots)
        logger.info(f"💰 Battery charge: selected {len(selected_slots)} eligible slots "
                   f"(avg={avg_charge_price:.4f}, range={min_price:.4f}-{max_selected_price:.4f} EUR/kWh)")
    
    return [slot_to_time(i, slot_minutes) for i in sorted(selected_slots)]


def optimize_bat_discharge(prices, slot_minutes, slot_to_time,
                           min_discharge_price=None, price_difference_threshold=None):
    """
    Optimize battery discharge periods using percentile-based price thresholds.
    Returns all eligible discharge slots (limiting is done in limit_battery_cycles).
    
    Args:
        prices: List of prices per slot
        slot_minutes: Duration of each slot in minutes
        slot_to_time: Function to convert slot index to time
        min_discharge_price: Minimum price threshold for discharging (from historical percentile).
                            If None, uses fallback based on horizon prices.
        price_difference_threshold: Additional threshold for opportunistic discharging.
                                   Mark slot as discharge if it's more expensive than any slot being processed by this amount.
        
    Returns:
        List of start times for battery discharge
    """
    if not prices:
        return []
    
    # Use provided min_discharge_price or calculate fallback from current horizon
    if min_discharge_price is None:
        # Fallback: use 70th percentile of current horizon prices
        min_discharge_price = float(np.percentile(prices, 70))
        logger.info(f"🔋 Battery discharge: using fallback threshold {min_discharge_price:.4f} EUR/kWh (70th percentile of horizon)")
    else:
        logger.info(f"🔋 Battery discharge: using historical threshold {min_discharge_price:.4f} EUR/kWh")
    
    # Filter slots that are above the min discharge price threshold
    eligible_slots = [(i, prices[i]) for i in range(len(prices)) if prices[i] >= min_discharge_price]
    logger.info(f"🔋 Battery discharge: initially {len(eligible_slots)} eligible slots above threshold {min_discharge_price:.4f} EUR/kWh")
    
    # Add price difference logic: mark slots as discharge slots if they are more expensive than earlier slots by threshold
    if price_difference_threshold is not None and price_difference_threshold > 0:
        logger.info(f"🔋 Battery discharge: applying price difference threshold {price_difference_threshold:.4f} EUR/kWh")
        for i in range(len(prices)):
            current_price = prices[i]
            # Check if this slot is more expensive than any earlier slot by at least the threshold
            is_expensive_compared_to_past = any(
                current_price >= prices[j] + price_difference_threshold 
                for j in range(0, i)
            )
            if is_expensive_compared_to_past:
                # Add to eligible slots if not already there
                if not any(slot_idx == i for slot_idx, _ in eligible_slots):
                    eligible_slots.append((i, current_price))
        logger.info(f"🔋 Battery discharge: after price difference logic, {len(eligible_slots)} eligible slots")
    
    if not eligible_slots:
        logger.warning(f"⚠️ No slots above min_discharge_price={min_discharge_price:.4f} EUR/kWh")
        return []
    
    # Sort by price descending and return all eligible slots (limiting is done later)
    eligible_slots.sort(key=lambda x: x[1], reverse=True)
    selected_slots = [slot for slot, _ in eligible_slots]
    
    # Log selection info
    if selected_slots:
        avg_discharge_price = sum(prices[i] for i in selected_slots) / len(selected_slots)
        min_selected_price = min(prices[i] for i in selected_slots)
        max_price = max(prices[i] for i in selected_slots)
        logger.info(f"💰 Battery discharge: selected {len(selected_slots)} eligible slots "
                   f"(avg={avg_discharge_price:.4f}, range={min_selected_price:.4f}-{max_price:.4f} EUR/kWh)")
    
    return [slot_to_time(i, slot_minutes) for i in sorted(selected_slots)]


def optimize_ev(prices, slot_minutes, max_price, slot_to_time):
    """Optimize EV charging by selecting all timeslots where price is below threshold."""
    slots = [i for i in range(len(prices)) if prices[i] <= max_price]
    return [slot_to_time(i, slot_minutes) for i in slots]


def limit_battery_cycles(
    charge_times,
    discharge_times,
    slot_minutes,
    horizon_start,
    current_soc,
    battery_capacity_kwh,
    battery_charge_speed_kw,
    min_soc_percent,
    max_soc_percent,
    prices=None,
    predicted_power_usage=None,
    device_name="battery"
):
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
    
    # Convert to slot indices
    charge_slots = {time_to_slot_idx(t) for t in charge_times} if charge_times else set()
    discharge_slots = {time_to_slot_idx(t) for t in discharge_times} if discharge_times else set()
    
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
    usage_by_slot = {}
    if predicted_power_usage:
        for pred in predicted_power_usage:
            pred_time = pred['timestamp']
            if isinstance(pred_time, str):
                pred_time = datetime.fromisoformat(pred_time.replace('Z', '+00:00'))
            if hasattr(pred_time, 'replace'):
                pred_time = pred_time.replace(tzinfo=None)
            slot_idx = int((pred_time - horizon_start).total_seconds() / 60 / slot_minutes)
            if slot_idx >= 0:
                usage_by_slot[slot_idx] = pred['predicted_kwh'] * slot_hours
    
    # Sort charge slots by price (cheapest first), discharge by price (most expensive first)
    if prices:
        charge_by_price = sorted(charge_slots, key=lambda s: prices[s] if s < len(prices) else float('inf'))
        discharge_by_price = sorted(discharge_slots, key=lambda s: -prices[s] if s < len(prices) else float('-inf'))
    else:
        # Without prices, just use time order
        charge_by_price = sorted(charge_slots)
        discharge_by_price = sorted(discharge_slots)
    
    def simulate_soc(selected_charge, selected_discharge):
        """Simulate SOC over time and return final SOC and feasibility."""
        all_slots = sorted(selected_charge | selected_discharge)
        soc = current_soc
        for slot_idx in all_slots:
            if slot_idx in selected_charge:
                headroom = battery_capacity_kwh * (max_soc_percent - soc) / 100
                if headroom < charge_energy_per_slot * 0.1:
                    return None, False  # Can't charge - battery full
                energy = min(charge_energy_per_slot, headroom)
                soc += (energy / battery_capacity_kwh) * 100
            elif slot_idx in selected_discharge:
                available = battery_capacity_kwh * (soc - min_soc_percent) / 100
                # Use battery's max discharge rate, not predicted usage
                # (predicted usage is just a hint for prioritization, not a constraint)
                discharge_energy = min(charge_energy_per_slot, available)
                if available < charge_energy_per_slot * 0.1:
                    return None, False  # Can't discharge - battery empty
                soc -= (discharge_energy / battery_capacity_kwh) * 100
        return soc, True
    
    # Greedy selection: add slots in price order if they keep the schedule feasible
    selected_charge = set()
    selected_discharge = set()
    
    logger.info(f"🔋 {device_name}: Starting SOC limiting - current_soc={current_soc:.1f}%, "
                f"{len(charge_slots)} charge candidates, {len(discharge_slots)} discharge candidates")
    
    # First add discharge slots (most expensive first)
    for slot_idx in discharge_by_price:
        candidate = selected_discharge | {slot_idx}
        final_soc, feasible = simulate_soc(selected_charge, candidate)
        if feasible:
            selected_discharge = candidate
            logger.debug(f"🔋 {device_name}: Added discharge slot {slot_idx}, final_soc={final_soc:.1f}%")
        else:
            logger.debug(f"🔋 {device_name}: Rejected discharge slot {slot_idx} (infeasible)")
    
    # Then add charge slots (cheapest first) - this also enables more discharge
    for slot_idx in charge_by_price:
        candidate = selected_charge | {slot_idx}
        _, feasible = simulate_soc(candidate, selected_discharge)
        if feasible:
            selected_charge = candidate
            # Re-check if we can add more discharge slots now that we have more charge
            for d_slot in discharge_by_price:
                if d_slot not in selected_discharge:
                    d_candidate = selected_discharge | {d_slot}
                    _, d_feasible = simulate_soc(selected_charge, d_candidate)
                    if d_feasible:
                        selected_discharge = d_candidate
    
    # Convert back to time strings
    limited_charge_times = sorted([slot_idx_to_time(s) for s in selected_charge])
    limited_discharge_times = sorted([slot_idx_to_time(s) for s in selected_discharge])
    
    # Log results with price info if available
    if prices and selected_charge:
        avg_charge_price = sum(prices[s] for s in selected_charge if s < len(prices)) / len(selected_charge)
        logger.info(f"🔋 {device_name}: Selected {len(selected_charge)} charge slots (avg price: {avg_charge_price:.4f})")
    if prices and selected_discharge:
        avg_discharge_price = sum(prices[s] for s in selected_discharge if s < len(prices)) / len(selected_discharge)
        logger.info(f"🔋 {device_name}: Selected {len(selected_discharge)} discharge slots (avg price: {avg_discharge_price:.4f})")
    
    logger.info(f"🔋 {device_name}: Final - {len(limited_charge_times)} charge, {len(limited_discharge_times)} discharge slots")
    
    return limited_charge_times, limited_discharge_times