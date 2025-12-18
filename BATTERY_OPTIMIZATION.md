# Battery Optimization Features

This document describes the enhanced battery optimization features that improve cost savings by considering price differentials and battery degradation costs.

## Features Implemented

### 1. Price Differential Threshold

The optimizer now ensures that battery charge/discharge cycles only occur when the price difference justifies the operation. This prevents cycling when the price spread is too small to cover efficiency losses.

**How it works:**
- Before charging, the optimizer checks if future discharge prices are high enough to justify charging at current prices
- Takes into account round-trip efficiency losses (typically 10-15%)
- Only schedules charging when: `discharge_price - charge_price >= min_price_differential`

### 2. Cycle Cost Consideration

Battery degradation costs are now factored into the optimization decision. Each charge/discharge cycle reduces battery lifespan, and this cost is accounted for.

**How it works:**
- Calculates cost per kWh based on total battery replacement cost and warranty cycles
- Adds this cost to the optimization objective
- Prevents cycling when the profit margin doesn't cover degradation costs

## Configuration Parameters

Add these parameters to your `config.json` under the `options` section:

```json
{
  "options": {
    "battery_min_price_differential": 0.05,
    "battery_round_trip_efficiency": 0.90,
    "battery_cycle_cost_eur": 0.02,
    "battery_capacity_kwh": 10.24,
    "battery_charge_time_percentage": 0.25,
    "battery_discharge_time_percentage": 0.25
  }
}
```

### Parameter Descriptions

| Parameter | Description | Default | Example Values |
|-----------|-------------|---------|----------------|
| `battery_min_price_differential` | Minimum price difference (EUR/kWh) between charge and discharge to justify cycling | 0.05 | 0.03-0.10 |
| `battery_round_trip_efficiency` | Battery round-trip efficiency (0.0-1.0) | 0.90 | 0.85-0.95 |
| `battery_cycle_cost_eur` | Total cost per complete charge/discharge cycle (EUR) | 0.02 | 0.01-0.05 |
| `battery_capacity_kwh` | Battery capacity in kWh | 10.0 | 5.0-20.0 |
| `battery_charge_time_percentage` | Percentage of horizon time to use for charging (0.0-1.0) | 0.25 | 0.15-0.35 |
| `battery_discharge_time_percentage` | Percentage of horizon time to use for discharging (0.0-1.0) | 0.25 | 0.15-0.35 |

### Calculating Cycle Cost

To calculate appropriate `battery_cycle_cost_eur`:

```
cycle_cost = battery_cost / warranty_cycles

Example:
- Battery cost: 8,000 EUR
- Warranty cycles: 6,000
- Cycle cost: 8,000 / 6,000 = 1.33 EUR per cycle

For partial cycles (e.g., 50% DoD):
- Partial cycle cost: 1.33 * 0.5 = 0.67 EUR
```

The optimizer automatically converts this to cost per kWh: `cycle_cost_per_kwh = cycle_cost / capacity_kwh`

## How It Works

### Percentage-Based Slot Selection

Instead of using fixed slot counts, the optimizer now uses **percentages** of the total price horizon:

**Example with 48-hour horizon (192 slots):**
- `battery_charge_time_percentage: 0.25` → Uses 48 cheapest slots (25% of 192)
- `battery_discharge_time_percentage: 0.25` → Uses 48 most expensive slots (25% of 192)

This approach **automatically adapts** to different horizon lengths and ensures balanced charge/discharge patterns.

### Slot Pairing Logic

**Charge Optimization:**
1. Calculates how many slots to use: `n_charge = total_slots × charge_percentage`
2. Identifies potential discharge opportunities (top N most expensive slots)
3. Calculates average expected discharge price from those slots
4. Determines maximum acceptable charge price based on thresholds
5. Selects the N cheapest slots that meet the criteria

**Discharge Optimization:**
1. Calculates how many slots to use: `n_discharge = total_slots × discharge_percentage`
2. Identifies potential charge opportunities (bottom N cheapest slots)
3. Calculates average expected charge price from those slots
4. Determines minimum profitable discharge price based on thresholds
5. Selects the N most expensive slots that meet the criteria

**Key Point:** The percentages are applied to **the same horizon**, so charge and discharge decisions consider the same set of available prices. This creates an implicit pairing where:
- Charge slots are selected knowing what the top 25% discharge prices look like
- Discharge slots are selected knowing what the bottom 25% charge prices look like

## Logging

The optimizer logs detailed information about battery decisions:

```
🔋 Battery charge optimization: avg_discharge_price=0.250, max_charge_price=0.180
💰 Battery charge profit estimate: gross=0.070 EUR/kWh, net=0.041 EUR/kWh
```

- **gross**: Price difference before accounting for losses
- **net**: Actual profit after efficiency losses and cycle costs

## Expected Impact

### Benefits

1. **Prevents Unprofitable Cycling**: Won't charge/discharge when price spread is too small
2. **Protects Battery Lifespan**: Factors in degradation costs
3. **Increases Net Savings**: Focuses on high-profit opportunities
4. **More Conservative**: Better long-term economics

### Typical Scenarios

**Before:**
- Charged at 0.18 EUR/kWh, discharged at 0.20 EUR/kWh
- Gross profit: 0.02 EUR/kWh
- After 10% efficiency loss: -0.00 EUR/kWh (loss!)

**After:**
- Requires minimum 0.05 EUR/kWh differential
- Only charges at 0.15 EUR/kWh or less
- Discharges at 0.25 EUR/kWh or more
- Net profit: 0.07 EUR/kWh after all costs

## Tuning Recommendations

### Conservative (Protect Battery, Less Cycling)
```json
{
  "battery_min_price_differential": 0.10,
  "battery_cycle_cost_eur": 0.05,
  "battery_charge_time_percentage": 0.15,
  "battery_discharge_time_percentage": 0.15
}
```
- Uses only 15% cheapest/most expensive slots
- Requires 0.10 EUR/kWh spread
- Fewer but highly profitable cycles
- Longer battery life

### Aggressive (Maximize Usage, More Cycling)
```json
{
  "battery_min_price_differential": 0.03,
  "battery_cycle_cost_eur": 0.01,
  "battery_charge_time_percentage": 0.35,
  "battery_discharge_time_percentage": 0.35
}
```
- Uses 35% of slots
- Lower profit threshold
- More frequent cycling
- Faster battery degradation

### Balanced (Recommended)
```json
{
  "battery_min_price_differential": 0.05,
  "battery_cycle_cost_eur": 0.02,
  "battery_charge_time_percentage": 0.25,
  "battery_discharge_time_percentage": 0.25
}
```
- Good balance between usage and lifespan
- Reasonable profit margins (25% of horizon)
- Sustainable cycling pattern

## Monitoring

Track these metrics to evaluate performance:

1. **Cycle Count**: Number of charge/discharge cycles per week
2. **Average Profit**: Net EUR/kWh per cycle
3. **Missed Opportunities**: Days with high spreads but no cycling
4. **Wasted Capacity**: Cycles with minimal profit

You can review these in the optimization logs and adjust parameters accordingly.

## Troubleshooting

### Battery Never Charges
- Check if `min_price_differential` is too high
- Verify price data is available and correct
- Ensure `battery_capacity_kwh` matches your actual battery

### Too Much Cycling
- Increase `min_price_differential` (e.g., from 0.05 to 0.08)
- Increase `battery_cycle_cost_eur`
- Check if `battery_round_trip_efficiency` is accurate

### Low Profitability
- Review actual efficiency vs configured value
- Check if cycle cost calculation is correct
- Consider increasing `min_price_differential`

## Future Enhancements

Potential improvements for future versions:

1. **Dynamic SOC Management**: Adjust charge/discharge limits based on price forecasts
2. **Multi-Day Optimization**: Look ahead 48-72 hours for better decisions
3. **Seasonal Adjustments**: Different strategies for summer (solar) vs winter
4. **State of Health Tracking**: Adjust cycle costs based on actual battery degradation
5. **Risk Management**: Conservative buffers when forecast confidence is low
