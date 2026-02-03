# Heat Pump Daily Runtime Calculation

## Overview

This feature calculates the expected daily runtime for heat pumps based on historical data from the last 10 days. The calculation considers:

- Inside temperature changes when the heat pump is running
- Outside temperature (lower outside temperature typically requires longer runtime)
- Historical heat pump on/off states

The calculated runtime is used to optimize the heat pump schedule by adjusting the maximum gap between runs to ensure sufficient daily operation.

## Configuration

To enable runtime calculation for a heat pump device, add the following sensor configuration fields to the device in your `DEVICES_CONFIG.json`:

```json
{
  "name": "wp",
  "type": "wp",
  "inside_temp_sensor": "sensor.ebusd_700_z2roomtemp",
  "outside_temp_sensor": "sensor.ebusd_700_displayedoutsidetemp",
  "heatpump_status_sensor": "sensor.ebusd_700_hc2pumpstatus_2",
  "block_hours": 1.0,
  "min_gap_hours": 3.0,
  "max_gap_hours": 8.0,
  ...
}
```

### Required Sensor Fields

- **inside_temp_sensor**: Entity ID of the inside temperature sensor
- **outside_temp_sensor**: Entity ID of the outside temperature sensor
- **heatpump_status_sensor**: Entity ID of the heat pump status sensor (1=on, 0=off)

If these sensors are not configured, the runtime calculation will be skipped and the optimization will use the default parameters.

## How It Works

### 1. Historical Data Collection

The system loads the last 10 days of historical data from Home Assistant using the history API endpoint:
- `/api/history/period/<timestamp>?filter_entity_id=<entity_id>`

The system automatically fetches history for:
- Inside temperature sensor
- Outside temperature sensor
- Heat pump status sensor (1=on, 0=off)

### 2. Daily Runtime Calculation

For each day in the historical data:
1. Calculate total hours the heat pump was on (status=1)
2. Calculate average outside temperature for the day
3. Weight the runtime by inverse temperature (colder days get higher weight)
4. Compute weighted average across all days

Example output:
```
Daily runtimes:
  2026-01-29: 13.44 hours (avg temp: 1.65°C)
  2026-01-30: 13.02 hours (avg temp: 3.83°C)
  2026-01-31: 13.44 hours (avg temp: 7.84°C)
  2026-02-01: 11.34 hours (avg temp: 6.29°C)
  
Expected daily runtime: 10.52 hours
```

### 3. Storage in Database

The calculated runtime is stored in TinyDB (`db.json`) in the `wp_daily_runtime` table:

```json
{
  "device": "wp",
  "expected_daily_runtime_hours": 10.52,
  "calculated_at": "2026-02-03T07:42:46.525Z",
  "inside_temp_sensor": "sensor.ebusd_700_z2roomtemp",
  "outside_temp_sensor": "sensor.ebusd_700_displayedoutsidetemp",
  "heatpump_status_sensor": "sensor.ebusd_700_hc2pumpstatus_2"
}
```

### 4. Integration with Optimization

The expected runtime is used to adjust the optimization parameters:

```python
# Calculate how many blocks per day are needed
blocks_needed_per_day = expected_daily_runtime / block_hours

# Adjust max_gap to ensure enough runs per day
optimal_max_gap = (24.0 / blocks_needed_per_day) - block_hours
adjusted_max_gap = min(max_gap_hours, optimal_max_gap)
```

This ensures the optimizer schedules enough heat pump runs to meet the expected daily runtime.

## Logging

The runtime calculation outputs detailed logs:

```
🔍 Calculating daily runtime for wp from historical data...
📊 wp: Expected daily runtime = 10.52 hours
WP: Using expected runtime 10.52h/day (~10.5 runs/day) -> adjusted max_gap to 1.30h
```

## Testing

The runtime calculation automatically fetches historical data from Home Assistant during each optimization run. To verify it's working:

1. Configure the sensor fields in your device configuration
2. Check the logs during optimization for runtime calculation messages
3. Verify the calculated runtime in the TinyDB database (`wp_daily_runtime` table)

The test scripts in the repository use CSV data for offline testing, but the production system uses live Home Assistant data.

## Notes

- The runtime calculation is performed during each optimization run
- Historical data is fetched directly from Home Assistant using the history API
- If sensors are not configured, the optimization uses default parameters
- The calculation requires at least a few days of historical data to be accurate
- The weighted average gives more importance to colder days (which typically require longer runtime)

## Future Enhancements

- Machine learning models to predict runtime based on weather forecasts
- Adaptive learning from actual vs. scheduled runtime
- Per-day runtime variation based on weather predictions
