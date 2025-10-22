import pulp

def optimize_wp(prices, slot_minutes, block_hours, n_blocks, slot_to_time):
    """Optimize heat pump operation periods."""
    block_len = int(block_hours * 60 / slot_minutes)
    window_costs = [sum(prices[i:i + block_len]) for i in range(len(prices) - block_len + 1)]
    model = pulp.LpProblem("WP_Optimization", pulp.LpMinimize)
    x = [pulp.LpVariable(f"x_{i}", cat="Binary") for i in range(len(window_costs))]
    model += pulp.lpSum([window_costs[i] * x[i] for i in range(len(x))])
    model += pulp.lpSum(x) == n_blocks
    for i in range(len(prices)):
        model += pulp.lpSum([x[j] for j in range(max(0, i - block_len + 1), min(i + 1, len(window_costs)))]) <= 1
    model.solve(pulp.GLPK(msg=0))
    starts = [i for i in range(len(x)) if pulp.value(x[i]) == 1]
    return [slot_to_time(i, slot_minutes) for i in starts]

def optimize_hw(prices, slot_minutes, block_hours, n_blocks, min_gap_hours, slot_to_time):
    """Optimize hot water operation periods."""
    block_len = int(block_hours * 60 / slot_minutes)
    window_costs = [sum(prices[i:i + block_len]) for i in range(len(prices) - block_len + 1)]
    model = pulp.LpProblem("HW_Optimization", pulp.LpMinimize)
    y = [pulp.LpVariable(f"y_{i}", cat="Binary") for i in range(len(window_costs))]
    model += pulp.lpSum([window_costs[i] * y[i] for i in range(len(y))])
    model += pulp.lpSum(y) == n_blocks
    for i in range(len(prices)):
        model += pulp.lpSum([y[j] for j in range(max(0, i - block_len + 1), min(i + 1, len(window_costs)))]) <= 1
    min_gap_slots = int(min_gap_hours * 60 / slot_minutes)
    for i in range(len(y)):
        for j in range(i + 1, len(y)):
            if j - i < min_gap_slots:
                model += y[i] + y[j] <= 1
    model.solve(pulp.GLPK(msg=0))
    starts = [i for i in range(len(y)) if pulp.value(y[i]) == 1]
    return [slot_to_time(i, slot_minutes) for i in starts]

def optimize_battery(prices, slot_minutes, n_slots_to_use, slot_to_time):
    """Optimize battery charging periods."""
    model = pulp.LpProblem("BAT_Optimization", pulp.LpMinimize)
    z = [pulp.LpVariable(f"z_{i}", cat="Binary") for i in range(len(prices))]
    model += pulp.lpSum([prices[i] * z[i] for i in range(len(prices))])
    model += pulp.lpSum(z) == n_slots_to_use
    model.solve(pulp.GLPK(msg=0))
    slots = [i for i in range(len(prices)) if pulp.value(z[i]) == 1]
    return [slot_to_time(i, slot_minutes) for i in slots]

def optimize_bat_discharge(prices, slot_minutes, block_hours, n_blocks, slot_to_time):
    """Optimize battery discharge periods to maximize revenue by selecting highest price periods."""
    block_len = int(block_hours * 60 / slot_minutes)
    window_costs = [sum(prices[i:i + block_len]) for i in range(len(prices) - block_len + 1)]
    model = pulp.LpProblem("BAT_DISCHARGE_Optimization", pulp.LpMaximize)
    x = [pulp.LpVariable(f"x_{i}", cat="Binary") for i in range(len(window_costs))]
    model += pulp.lpSum([window_costs[i] * x[i] for i in range(len(x))])
    model += pulp.lpSum(x) == n_blocks
    for i in range(len(prices)):
        model += pulp.lpSum([x[j] for j in range(max(0, i - block_len + 1), min(i + 1, len(window_costs)))]) <= 1
    model.solve(pulp.GLPK(msg=0))
    starts = [i for i in range(len(x)) if pulp.value(x[i]) == 1]
    return [slot_to_time(i, slot_minutes) for i in starts]
