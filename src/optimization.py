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
    min_daily_hours,
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
    - Minimum daily runtime - ensures sufficient operation per calendar day
    - Support for locked slots (already scheduled/executed)
    - Initial gap state from previous schedule
    
    Args:
        prices: List of prices per slot for the entire horizon
        slot_minutes: Duration of each slot in minutes (e.g., 15 or 60)
        block_hours: Minimum runtime in hours when device turns on
        min_gap_hours: Minimum hours between end of one run and start of next
        max_gap_hours: Maximum hours allowed without running (pause limit)
        min_daily_hours: Minimum hours the device must run per calendar day
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
    slots_per_day = int(24 * 60 / slot_minutes)
    min_daily_slots = int(min_daily_hours * 60 / slot_minutes)
    
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
    
    # Constraint 4: Minimum daily runtime
    # Calculate which slots belong to which calendar day
    current_date = horizon_start_datetime.date()
    day_boundaries = []  # List of (day_start_slot, day_end_slot, date)
    
    slot = 0
    while slot < n_slots:
        day_start = slot
        # Calculate slots until midnight
        current_datetime = horizon_start_datetime + timedelta(minutes=slot * slot_minutes)
        next_midnight = datetime.combine(current_datetime.date() + timedelta(days=1), datetime.min.time())
        slots_until_midnight = int((next_midnight - current_datetime).total_seconds() / 60 / slot_minutes)
        day_end = min(slot + slots_until_midnight, n_slots)
        day_boundaries.append((day_start, day_end, current_datetime.date()))
        slot = day_end
    
    # Add minimum daily runtime constraints
    for day_start, day_end, date in day_boundaries:
        # Count running slots in this day
        # A slot t is running if any x[j] is 1 where j covers t
        # To count total running slots, we sum block_len * x[i] for starts in this day
        # But we need to be careful about blocks that span day boundaries
        
        # Simpler approach: require minimum number of block starts such that
        # total runtime >= min_daily_hours
        # Each start contributes block_hours of runtime
        day_slots = day_end - day_start
        available_hours = day_slots * slot_minutes / 60
        
        # Only enforce if the day has enough hours to meet minimum
        if available_hours >= min_daily_hours:
            # Starts that contribute to this day (start within the day or overlap into it)
            contributing_starts = [i for i in valid_starts 
                                 if i < day_end and i + block_len > day_start]
            if contributing_starts:
                min_blocks_needed = max(1, int(min_daily_hours / block_hours))
                # Count starts that begin within this day (simpler accounting)
                day_starts = [i for i in valid_starts if day_start <= i < day_end]
                if day_starts:
                    model += pulp.lpSum([x[i] for i in day_starts if i in x]) >= min_blocks_needed
                    logger.debug(f"Day {date}: min {min_blocks_needed} blocks from slots {day_start}-{day_end}")
    
    # Constraint 5: Respect locked slots
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
    min_daily_hours,
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
        min_daily_hours=min_daily_hours,
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
    min_daily_hours,
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
        min_daily_hours=min_daily_hours,
        locked_slots=locked_slots,
        initial_gap_slots=initial_gap_slots,
        horizon_start_datetime=horizon_start_datetime,
        device_name="HW"
    )
    return [slot_to_time(i, slot_minutes) for i in starts]


def optimize_battery(prices, slot_minutes, charge_time_percentage, slot_to_time, 
                     min_price_differential=0.0, round_trip_efficiency=0.90, 
                     cycle_cost_per_kwh=0.0, capacity_kwh=10.0):
    """
    Optimize battery charging periods with price differential threshold and cycle costs.
    
    Args:
        prices: List of prices per slot
        slot_minutes: Duration of each slot in minutes
        charge_time_percentage: Percentage of horizon time to use for charging (0.0-1.0)
        slot_to_time: Function to convert slot index to time
        min_price_differential: Minimum price difference (discharge - charge) to justify cycling (EUR/kWh)
        round_trip_efficiency: Battery round-trip efficiency (default 0.90 = 90%)
        cycle_cost_per_kwh: Cost per kWh due to battery degradation (EUR/kWh)
        capacity_kwh: Battery capacity in kWh
        
    Returns:
        List of start times for battery charging
    """
    if not prices or charge_time_percentage <= 0:
        return []
    
    # Calculate number of slots based on percentage
    n_slots_to_use = max(1, int(len(prices) * charge_time_percentage))
    
    if n_slots_to_use <= 0:
        return []
    
    # Calculate the effective cost threshold
    # We should only charge if we can discharge at a profitable price
    # Effective cost = charge_price / efficiency + cycle_cost
    # We need: discharge_price > (charge_price / efficiency + cycle_cost)
    # Rearranged: discharge_price - charge_price/efficiency > cycle_cost
    
    # Find potential discharge prices (top prices in the horizon)
    sorted_prices = sorted(prices, reverse=True)
    if n_slots_to_use >= len(sorted_prices):
        # If we want to use more slots than available, use best available
        avg_discharge_price = sum(sorted_prices[:min(len(sorted_prices), n_slots_to_use)]) / min(len(sorted_prices), n_slots_to_use)
    else:
        avg_discharge_price = sum(sorted_prices[:n_slots_to_use]) / n_slots_to_use
    
    # Calculate minimum charge price threshold
    # charge_price should be such that: avg_discharge_price - charge_price >= min_price_differential
    # And accounting for efficiency loss: avg_discharge_price - charge_price/efficiency >= cycle_cost
    # Combined: charge_price <= min(
    #   avg_discharge_price - min_price_differential,
    #   (avg_discharge_price - cycle_cost) * efficiency
    # )
    
    threshold_from_differential = avg_discharge_price - min_price_differential
    threshold_from_cycle_cost = (avg_discharge_price - cycle_cost_per_kwh) * round_trip_efficiency
    max_charge_price = min(threshold_from_differential, threshold_from_cycle_cost)
    
    logger.info(f"🔋 Battery charge optimization: avg_discharge_price={avg_discharge_price:.3f}, "
                f"max_charge_price={max_charge_price:.3f} (differential threshold={threshold_from_differential:.3f}, "
                f"cycle cost threshold={threshold_from_cycle_cost:.3f})")
    
    # Filter slots based on price threshold and select cheapest ones
    eligible_slots = [(i, prices[i]) for i in range(len(prices)) if prices[i] <= max_charge_price]
    
    if not eligible_slots:
        logger.warning(f"⚠️ No slots meet the price threshold for battery charging (max_charge_price={max_charge_price:.3f})")
        return []
    
    # Sort by price and take the cheapest n_slots_to_use
    eligible_slots.sort(key=lambda x: x[1])
    selected_slots = [slot for slot, _ in eligible_slots[:n_slots_to_use]]
    
    # Calculate expected profit
    if selected_slots:
        avg_charge_price = sum(prices[i] for i in selected_slots) / len(selected_slots)
        gross_revenue = avg_discharge_price - avg_charge_price
        net_revenue = gross_revenue * round_trip_efficiency - cycle_cost_per_kwh
        logger.info(f"💰 Battery charge profit estimate: gross={gross_revenue:.3f} EUR/kWh, "
                   f"net={net_revenue:.3f} EUR/kWh (after {(1-round_trip_efficiency)*100:.1f}% loss + cycle cost)")
    
    return [slot_to_time(i, slot_minutes) for i in sorted(selected_slots)]


def optimize_bat_discharge(prices, slot_minutes, discharge_time_percentage, slot_to_time,
                           min_price_differential=0.0, round_trip_efficiency=0.90,
                           cycle_cost_per_kwh=0.0, capacity_kwh=10.0):
    """
    Optimize battery discharge periods with price differential threshold and cycle costs.
    
    Args:
        prices: List of prices per slot
        slot_minutes: Duration of each slot in minutes
        discharge_time_percentage: Percentage of horizon time to use for discharging (0.0-1.0)
        slot_to_time: Function to convert slot index to time
        min_price_differential: Minimum price difference (discharge - charge) to justify cycling (EUR/kWh)
        round_trip_efficiency: Battery round-trip efficiency (default 0.90 = 90%)
        cycle_cost_per_kwh: Cost per kWh due to battery degradation (EUR/kWh)
        capacity_kwh: Battery capacity in kWh
        
    Returns:
        List of start times for battery discharge
    """
    if not prices or discharge_time_percentage <= 0:
        return []
    
    # Calculate number of discharge slots based on percentage (single slots, no blocks)
    n_slots_to_use = max(1, int(len(prices) * discharge_time_percentage))
    
    if n_slots_to_use <= 0:
        return []
    
    # Calculate minimum discharge price threshold
    # Find potential charge prices (bottom prices in the horizon)
    sorted_prices = sorted(prices)
    charge_slots = n_slots_to_use  # Match charge and discharge slot counts
    avg_charge_price = sum(sorted_prices[:charge_slots]) / charge_slots if charge_slots > 0 else sorted_prices[0]
    
    # Minimum discharge price per slot:
    # discharge_price >= charge_price/efficiency + cycle_cost + min_price_differential
    min_discharge_price = avg_charge_price / round_trip_efficiency + cycle_cost_per_kwh + min_price_differential
    
    logger.info(f"🔋 Battery discharge optimization: avg_charge_price={avg_charge_price:.3f}, "
                f"min_discharge_price={min_discharge_price:.3f}")
    
    # Filter slots based on threshold
    eligible_slots = [(i, prices[i]) for i in range(len(prices)) if prices[i] >= min_discharge_price]
    
    if not eligible_slots:
        logger.warning(f"⚠️ No slots meet the price threshold for battery discharge (min={min_discharge_price:.3f})")
        return []
    
    # Sort by price and take the highest n_slots_to_use
    eligible_slots.sort(key=lambda x: x[1], reverse=True)
    selected_slots = [slot for slot, _ in eligible_slots[:n_slots_to_use]]
    
    # Calculate expected profit
    if selected_slots:
        avg_discharge_price = sum(prices[i] for i in selected_slots) / len(selected_slots)
        gross_revenue = avg_discharge_price - avg_charge_price
        net_revenue = gross_revenue - (avg_charge_price * (1/round_trip_efficiency - 1)) - cycle_cost_per_kwh
        logger.info(f"💰 Battery discharge profit estimate: gross={gross_revenue:.3f} EUR/kWh, "
                   f"net={net_revenue:.3f} EUR/kWh")
    
    return [slot_to_time(i, slot_minutes) for i in sorted(selected_slots)]
    return [slot_to_time(i, slot_minutes) for i in sorted(selected_slots)]


def optimize_ev(prices, slot_minutes, max_price, slot_to_time):
    """Optimize EV charging by selecting all timeslots where price is below threshold."""
    slots = [i for i in range(len(prices)) if prices[i] <= max_price]
    return [slot_to_time(i, slot_minutes) for i in slots]
