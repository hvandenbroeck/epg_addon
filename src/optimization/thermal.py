"""Thermal device optimization (Heat Pump and Hot Water).

This module implements MILP (Mixed Integer Linear Programming) optimization
for thermal devices like heat pumps and hot water heaters using PuLP/GLPK.

The optimization considers:
- Minimum run time (block_hours)
- Minimum gap between runs (min_gap_hours) 
- Maximum gap between runs (max_gap_hours)
- Locked slots (already scheduled/executed)
- Initial gap state from previous schedule
"""
import pulp
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def optimize_thermal_device(
    prices: list[float],
    slot_minutes: int,
    block_hours: float,
    min_gap_hours: float,
    max_gap_hours: float,
    locked_slots: set[int],
    initial_gap_slots: int,
    horizon_start_datetime: datetime,
    device_name: str = "device"
) -> list[int]:
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
    prices: list[float],
    slot_minutes: int,
    block_hours: float,
    min_gap_hours: float,
    max_gap_hours: float,
    locked_slots: set[int],
    initial_gap_slots: int,
    horizon_start_datetime: datetime,
    slot_to_time
) -> list[str]:
    """Optimize heat pump operation using sliding window constraints.
    
    Args:
        prices: List of prices per slot
        slot_minutes: Duration of each slot in minutes
        block_hours: Minimum runtime when turned on
        min_gap_hours: Minimum gap between runs
        max_gap_hours: Maximum gap between runs
        locked_slots: Set of locked slot indices
        initial_gap_slots: Slots since last run
        horizon_start_datetime: When the horizon starts
        slot_to_time: Function to convert slot index to time string
        
    Returns:
        List of start times as HH:MM strings
    """
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
    prices: list[float],
    slot_minutes: int,
    block_hours: float,
    min_gap_hours: float,
    max_gap_hours: float,
    locked_slots: set[int],
    initial_gap_slots: int,
    horizon_start_datetime: datetime,
    slot_to_time
) -> list[str]:
    """Optimize hot water operation using sliding window constraints.
    
    Args:
        prices: List of prices per slot
        slot_minutes: Duration of each slot in minutes
        block_hours: Minimum runtime when turned on
        min_gap_hours: Minimum gap between runs
        max_gap_hours: Maximum gap between runs
        locked_slots: Set of locked slot indices
        initial_gap_slots: Slots since last run
        horizon_start_datetime: When the horizon starts
        slot_to_time: Function to convert slot index to time string
        
    Returns:
        List of start times as HH:MM strings
    """
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
