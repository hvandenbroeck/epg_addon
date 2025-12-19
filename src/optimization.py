import pulp
import logging
from datetime import datetime, timedelta

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


def optimize_battery(prices, slot_minutes, charge_time_percentage, slot_to_time, 
                     max_charge_price=None):
    """
    Optimize battery charging periods using percentile-based price thresholds.
    
    Args:
        prices: List of prices per slot
        slot_minutes: Duration of each slot in minutes
        charge_time_percentage: Percentage of horizon time to use for charging (0.0-1.0)
        slot_to_time: Function to convert slot index to time
        max_charge_price: Maximum price threshold for charging (from historical percentile).
                         If None, uses fallback based on horizon prices.
        
    Returns:
        List of start times for battery charging
    """
    if not prices or charge_time_percentage <= 0:
        return []
    
    # Calculate number of slots based on percentage
    n_slots_to_use = max(1, int(len(prices) * charge_time_percentage))
    
    if n_slots_to_use <= 0:
        return []
    
    # Use provided max_charge_price or calculate fallback from current horizon
    if max_charge_price is None:
        # Fallback: use 30th percentile of current horizon prices
        import numpy as np
        max_charge_price = float(np.percentile(prices, 30))
        logger.info(f"🔋 Battery charge: using fallback threshold {max_charge_price:.4f} EUR/kWh (30th percentile of horizon)")
    else:
        logger.info(f"🔋 Battery charge: using historical threshold {max_charge_price:.4f} EUR/kWh")
    
    # Filter slots that are below the max charge price threshold
    eligible_slots = [(i, prices[i]) for i in range(len(prices)) if prices[i] <= max_charge_price]
    
    if not eligible_slots:
        logger.warning(f"⚠️ No slots below max_charge_price={max_charge_price:.4f} EUR/kWh")
        return []
    
    # Sort by price and take the cheapest n_slots_to_use
    eligible_slots.sort(key=lambda x: x[1])
    selected_slots = [slot for slot, _ in eligible_slots[:n_slots_to_use]]
    
    # Log selection info
    if selected_slots:
        avg_charge_price = sum(prices[i] for i in selected_slots) / len(selected_slots)
        min_price = min(prices[i] for i in selected_slots)
        max_selected_price = max(prices[i] for i in selected_slots)
        logger.info(f"💰 Battery charge: selected {len(selected_slots)} slots "
                   f"(avg={avg_charge_price:.4f}, range={min_price:.4f}-{max_selected_price:.4f} EUR/kWh)")
    
    return [slot_to_time(i, slot_minutes) for i in sorted(selected_slots)]


def optimize_bat_discharge(prices, slot_minutes, discharge_time_percentage, slot_to_time,
                           min_discharge_price=None):
    """
    Optimize battery discharge periods using percentile-based price thresholds.
    
    Args:
        prices: List of prices per slot
        slot_minutes: Duration of each slot in minutes
        discharge_time_percentage: Percentage of horizon time to use for discharging (0.0-1.0)
        slot_to_time: Function to convert slot index to time
        min_discharge_price: Minimum price threshold for discharging (from historical percentile).
                            If None, uses fallback based on horizon prices.
        
    Returns:
        List of start times for battery discharge
    """
    if not prices or discharge_time_percentage <= 0:
        return []
    
    # Calculate number of discharge slots based on percentage
    n_slots_to_use = max(1, int(len(prices) * discharge_time_percentage))
    
    if n_slots_to_use <= 0:
        return []
    
    # Use provided min_discharge_price or calculate fallback from current horizon
    if min_discharge_price is None:
        # Fallback: use 70th percentile of current horizon prices
        import numpy as np
        min_discharge_price = float(np.percentile(prices, 70))
        logger.info(f"🔋 Battery discharge: using fallback threshold {min_discharge_price:.4f} EUR/kWh (70th percentile of horizon)")
    else:
        logger.info(f"🔋 Battery discharge: using historical threshold {min_discharge_price:.4f} EUR/kWh")
    
    # Filter slots that are above the min discharge price threshold
    eligible_slots = [(i, prices[i]) for i in range(len(prices)) if prices[i] >= min_discharge_price]
    
    if not eligible_slots:
        logger.warning(f"⚠️ No slots above min_discharge_price={min_discharge_price:.4f} EUR/kWh")
        return []
    
    # Sort by price descending and take the highest n_slots_to_use
    eligible_slots.sort(key=lambda x: x[1], reverse=True)
    selected_slots = [slot for slot, _ in eligible_slots[:n_slots_to_use]]
    
    # Log selection info
    if selected_slots:
        avg_discharge_price = sum(prices[i] for i in selected_slots) / len(selected_slots)
        min_selected_price = min(prices[i] for i in selected_slots)
        max_price = max(prices[i] for i in selected_slots)
        logger.info(f"💰 Battery discharge: selected {len(selected_slots)} slots "
                   f"(avg={avg_discharge_price:.4f}, range={min_selected_price:.4f}-{max_price:.4f} EUR/kWh)")
    
    return [slot_to_time(i, slot_minutes) for i in sorted(selected_slots)]


def optimize_ev(prices, slot_minutes, max_price, slot_to_time):
    """Optimize EV charging by selecting all timeslots where price is below threshold."""
    slots = [i for i in range(len(prices)) if prices[i] <= max_price]
    return [slot_to_time(i, slot_minutes) for i in slots]
