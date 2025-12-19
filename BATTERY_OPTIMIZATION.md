# Battery Optimization Features

This document describes the battery optimization features that use historical price percentiles to determine optimal charge and discharge thresholds.

## Features Implemented

### 1. Historical Percentile-Based Thresholds

The optimizer analyzes historical electricity prices to determine smart thresholds for battery operations:

- **Maximum Charge Price**: Derived from the Xth percentile of historical prices (default: 30th percentile)
- **Minimum Discharge Price**: Derived from the Yth percentile of historical prices (default: 70th percentile)

**How it works:**
- At each optimization run (typically daily at 16:00), the system fetches price history from the cache
- Calculates the configured percentiles from the last N days of data (default: 14 days)
- Uses these thresholds to filter which time slots are eligible for charging/discharging
- Only charges when current prices are below the max charge threshold
- Only discharges when current prices are above the min discharge threshold

### 2. Adaptive Thresholds

The percentile-based approach automatically adapts to market conditions:
- During periods of high volatility, thresholds adjust accordingly
- During stable periods, thresholds become tighter
- No manual tuning required for different seasons or market conditions

## Configuration Parameters

Add these parameters to your `config.json` under the `options` section:

```json
{
  "options": {
    "battery_charge_time_percentage": 0.25,
    "battery_discharge_time_percentage": 0.25,
    "battery_price_history_days": 14,
    "battery_charge_percentile": 30,
    "battery_discharge_percentile": 70
  }
}
```

### Parameter Descriptions

| Parameter | Description | Default | Example Values |
|-----------|-------------|---------|----------------|
| `battery_charge_time_percentage` | Percentage of horizon time to use for charging (0.0-1.0) | 0.25 | 0.15-0.35 |
| `battery_discharge_time_percentage` | Percentage of horizon time to use for discharging (0.0-1.0) | 0.25 | 0.15-0.35 |
| `battery_price_history_days` | Number of days of price history to analyze | 14 | 7-30 |
| `battery_charge_percentile` | Percentile threshold for max charge price (lower = more selective) | 30 | 20-40 |
| `battery_discharge_percentile` | Percentile threshold for min discharge price (higher = more selective) | 70 | 60-80 |

### Understanding Percentiles

- **30th percentile** means only 30% of historical prices were lower - you're charging in the cheapest 30% of times
- **70th percentile** means only 30% of historical prices were higher - you're discharging in the most expensive 30% of times

## How It Works

### Percentile Calculation

At each optimization run:
1. Fetches price data from the last N days (uses local cache first)
2. Calculates the configured percentiles
3. These become the thresholds for charge/discharge decisions

**Example with 14 days of data:**
- Historical prices range from 0.05 to 0.35 EUR/kWh
- 30th percentile = 0.12 EUR/kWh → max_charge_price
- 70th percentile = 0.25 EUR/kWh → min_discharge_price
- Result: Only charge below 0.12, only discharge above 0.25

### Slot Selection

**Charge Optimization:**
1. Filter slots where `price <= max_charge_price` (30th percentile)
2. From eligible slots, select the cheapest `n_slots_to_use`
3. `n_slots_to_use = total_horizon_slots × charge_time_percentage`

**Discharge Optimization:**
1. Filter slots where `price >= min_discharge_price` (70th percentile)
2. From eligible slots, select the most expensive `n_slots_to_use`
3. `n_slots_to_use = total_horizon_slots × discharge_time_percentage`

## Logging

The optimizer logs detailed information about price thresholds:

```
📊 Calculating price percentiles from last 14 days (charge=30th, discharge=70th)
📈 Price statistics (last 14 days): min=0.0450, max=0.3500, mean=0.1850, median=0.1750 EUR/kWh
🔋 Battery thresholds: max_charge_price=0.1200 EUR/kWh (30th percentile), min_discharge_price=0.2500 EUR/kWh (70th percentile)
🔋 Battery charge: selected 48 slots (avg=0.0850, range=0.0450-0.1200 EUR/kWh)
🔋 Battery discharge: selected 48 slots (avg=0.2900, range=0.2500-0.3500 EUR/kWh)
```

## Tuning Recommendations

### Conservative (Less Cycling, Higher Margins)
```json
{
  "battery_charge_percentile": 20,
  "battery_discharge_percentile": 80,
  "battery_charge_time_percentage": 0.15,
  "battery_discharge_time_percentage": 0.15
}
```
- Only charges in the cheapest 20% of historical prices
- Only discharges in the most expensive 20%
- Fewer cycles but with better margins

### Aggressive (More Cycling)
```json
{
  "battery_charge_percentile": 40,
  "battery_discharge_percentile": 60,
  "battery_charge_time_percentage": 0.35,
  "battery_discharge_time_percentage": 0.35
}
```
- Charges whenever prices are below median-ish (40th percentile)
- Discharges whenever prices are above median-ish (60th percentile)
- More frequent cycling

### Balanced (Recommended)
```json
{
  "battery_charge_percentile": 30,
  "battery_discharge_percentile": 70,
  "battery_charge_time_percentage": 0.25,
  "battery_discharge_time_percentage": 0.25
}
```
- Good balance between usage and selectivity
- 40% price spread between charge and discharge thresholds
- Sustainable cycling pattern

## Fallback Behavior

If historical price data is unavailable:
- The optimizer falls back to using the current horizon's 30th and 70th percentile
- A warning is logged but optimization continues
- This ensures the system remains operational even without full history

## Troubleshooting

### Battery Never Charges
- Check if `battery_charge_percentile` is too low
- Verify price history data exists (check logs for "Retrieved X hourly price records")
- Current prices may be unusually high compared to history

### Battery Never Discharges
- Check if `battery_discharge_percentile` is too high
- Current prices may be unusually low compared to history
- Consider increasing `battery_discharge_time_percentage`

### Want More/Less Cycling
- Adjust percentiles: lower charge / higher discharge = more selective
- Adjust time percentages: higher = more slots selected from eligible pool

## Price History Cache

Historical prices are stored in `/data/price_history.json`:
- Automatically fetched from ENTSO-E API when missing
- Cache is checked before each API call to minimize requests
- Old data (>400 days) is automatically cleaned up
- Data is stored hourly regardless of original resolution

## Future Enhancements

Potential improvements for future versions:

1. **Dynamic SOC Management**: Adjust charge/discharge limits based on price forecasts
2. **Multi-Day Optimization**: Look ahead 48-72 hours for better decisions
3. **Seasonal Adjustments**: Different strategies for summer (solar) vs winter
4. **State of Health Tracking**: Adjust cycle costs based on actual battery degradation
5. **Risk Management**: Conservative buffers when forecast confidence is low
