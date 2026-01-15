# Battery Cycle Limiter Documentation

## Overview

The battery cycle limiter is a sophisticated algorithm that optimizes battery charge and discharge schedules while respecting physical and economic constraints. It ensures the battery operates within safe state-of-charge (SOC) limits while maximizing the economic value of energy arbitrage.

## Core Functionality

The limiter takes price-based charge and discharge schedules (from the price optimizer) and refines them based on:

1. **Physical Constraints**: Battery capacity, charge/discharge rates, min/max SOC limits
2. **State Tracking**: Current battery SOC and projected SOC throughout the optimization horizon
3. **Economic Viability**: Ensures charging doesn't significantly exceed discharge needs (preventing wasteful cycling)

## Algorithm Overview

### Input Processing

The algorithm receives:
- **Charge times**: List of time slots when battery should charge (from price optimization)
- **Discharge times**: List of time slots when battery should discharge (from price optimization)
- **Battery parameters**: Capacity (kWh), charge speed (kW), min/max SOC (%)
- **Current SOC**: Real-time battery state of charge
- **Prices**: Electricity prices per time slot
- **Predicted usage**: Optional forecasted power consumption

### Two-Phase Greedy Selection

The algorithm uses a **greedy approach** to select the best charge/discharge slots:

#### Phase 1: Discharge Selection
```
Goal: Maximize high-value discharge opportunities
Process:
  1. Sort discharge slots by price (most expensive first)
  2. For each slot (in price order):
     - Create candidate schedule with this slot added
     - Simulate battery SOC over time
     - If SOC stays within limits [min_soc, max_soc]:
       ✓ Add slot to selection
     - Else:
       ✗ Reject slot (would violate SOC constraints)
```

#### Phase 2: Charge Selection (with Economic Viability Check)
```
Goal: Add cheap charging, but only what's needed
Process:
  1. Sort charge slots by price (cheapest first)
  2. For each slot (in price order):
     - Create candidate schedule with this slot added
     - Simulate battery SOC over time
     - If SOC would violate limits:
       ✗ Reject slot (physical constraint)
     - If charge exceeds discharge needs + buffer:
       ✗ Reject slot (economic constraint)
     - Else:
       ✓ Add slot to selection
       → Re-check if previously rejected discharge slots are now feasible
```

## Economic Viability Check

To prevent excessive battery cycling that provides minimal economic benefit, the algorithm enforces:

```
Maximum allowed charging = Discharge needs × (1 + charge_buffer_percent / 100)
```

**Example** (with 20% buffer):
- If 10 kWh discharge is scheduled
- Maximum charging allowed: 10 × 1.20 = 12 kWh
- Any charge slot that would push total charging above 12 kWh is rejected

This prevents scenarios where the battery charges excessively for minimal arbitrage opportunities.

## Key Concepts

### State of Charge (SOC)
The percentage of battery capacity currently filled (0-100%). The algorithm:
- Starts with **current_soc** (from Home Assistant sensor)
- Projects SOC forward through each time slot
- Rejects schedules that violate **min_soc_percent** or **max_soc_percent**

### Time Slot Separation
The algorithm distinguishes between:
- **Past slots**: Already executed, preserved unchanged
- **Future slots**: Can be optimized based on current conditions

This allows the algorithm to run periodically (e.g., every 15 minutes) and adapt to actual battery behavior.

### Greedy vs Optimal
The algorithm uses a **greedy approach** (locally optimal choices) rather than global optimization because:
1. Much faster computation (important for real-time recalculation)
2. Simpler to understand and debug
3. Price-sorting ensures high-quality results in practice
4. Handles dynamic conditions (changing SOC, updated forecasts)

## Workflow Integration

```
1. Price Optimizer (optimization/battery.py)
   ↓ Generates initial charge/discharge schedule based on price thresholds
   
2. Battery Limiter (optimization/battery_limiter.py)
   ↓ Refines schedule based on SOC constraints and economic viability
   
3. Scheduler (scheduler.py)
   ↓ Schedules actual Home Assistant actions
   
4. Recalculation (every 15 min via optimizer.recalculate_battery_limits)
   ↓ Adapts to actual SOC changes and updated forecasts
```
