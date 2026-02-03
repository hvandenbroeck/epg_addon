# Implementation Summary: Heat Pump Daily Runtime Calculation

## What Was Implemented

This implementation adds the ability to calculate expected daily runtime for heat pumps based on historical sensor data and use that information to optimize the heat pump schedule.

## Changes Made

### 1. Device Configuration (`src/devices_config.py`)

Added three new optional fields to the `Device` model for heat pump devices:

```python
inside_temp_sensor: Optional[str] = Field(default=None, description="Inside temperature sensor entity ID")
outside_temp_sensor: Optional[str] = Field(default=None, description="Outside temperature sensor entity ID")
heatpump_status_sensor: Optional[str] = Field(default=None, description="Heat pump on/off status sensor entity ID")
```

### 2. Runtime Calculator Module (`src/runtime_calculator.py`)

Created a new module that:
- Loads historical sensor data from CSV files (with future support for Home Assistant API)
- Calculates daily runtime for each day in the historical data
- Computes average outside temperature per day
- Calculates a weighted average runtime (lower temperatures get higher weight)

Key features:
- Analyzes last 10 days of history by default
- Handles time-series data with variable intervals
- Robust error handling for missing or invalid data

### 3. Optimization Integration (`src/optimization/thermal.py`)

Modified `optimize_wp()` function to:
- Accept an optional `expected_daily_runtime` parameter
- Adjust `max_gap_hours` based on expected runtime
- Calculate optimal gap to ensure sufficient daily runs
- Log the runtime-aware optimization decisions

Formula used:
```python
blocks_needed_per_day = expected_daily_runtime / block_hours
optimal_max_gap = (24.0 / blocks_needed_per_day) - block_hours
adjusted_max_gap = min(max_gap_hours, optimal_max_gap)
```

### 4. Optimizer Workflow (`src/optimizer.py`)

Enhanced the heat pump optimization workflow to:
- Import the RuntimeCalculator
- Check if runtime sensors are configured
- Load historical data from CSV (fallback for testing)
- Calculate expected daily runtime
- Store results in TinyDB (`wp_daily_runtime` table)
- Log the calculated runtime
- Pass runtime to optimization algorithm

### 5. Documentation

Created comprehensive documentation:
- **HEAT_PUMP_RUNTIME.md**: Complete feature documentation
- Updated **QUICK_START.md**: Added reference to new feature
- Updated **DEVICES_CONFIG_EXAMPLE.json**: Added sensor configuration example

### 6. Testing

Created test scripts:
- **test_runtime_calculator.py**: Simple test of runtime calculation
- **test_optimization_integration.py**: Detailed test showing daily runtimes and weighted averages

## How It Works

### Data Flow

1. **Historical Data Collection**: Load 10 days of sensor history
   - Inside temperature (e.g., `sensor.ebusd_700_z2roomtemp`)
   - Outside temperature (e.g., `sensor.ebusd_700_displayedoutsidetemp`)
   - Heat pump status (e.g., `sensor.ebusd_700_hc2pumpstatus_2`)

2. **Runtime Calculation**: For each day:
   - Sum hours when heat pump was ON (status=1)
   - Calculate average outside temperature
   - Weight runtime by inverse temperature (colder = higher weight)

3. **Storage**: Save to database:
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

4. **Optimization**: Adjust scheduling parameters to meet expected runtime

### Example Results

Based on the sample data in `data/history.csv`:

```
Daily runtimes:
  2026-01-29: 13.44 hours (outside temp: 1.65°C)
  2026-01-30: 13.02 hours (outside temp: 3.83°C)
  2026-01-31: 13.44 hours (outside temp: 7.84°C)
  2026-02-01: 11.34 hours (outside temp: 6.29°C)
  2026-02-02: 5.88 hours  (outside temp: 6.54°C)

Expected daily runtime: 10.52 hours (weighted average)
```

## Backward Compatibility

✅ All changes are backward compatible:
- New fields are optional
- Runtime calculation only runs if sensors are configured
- Optimization falls back to original behavior if runtime is not provided
- Existing device configurations continue to work unchanged

## Configuration Example

To enable runtime calculation, add these fields to your heat pump device:

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
  "start": { ... },
  "stop": { ... }
}
```

## Testing & Validation

All changes have been tested:
- ✅ Python syntax validation (all files compile)
- ✅ Runtime calculation with sample data
- ✅ CSV loading and parsing
- ✅ Daily runtime calculation (10.52 hours)
- ✅ Database storage
- ✅ Logging output
- ✅ Backward compatibility

## Future Enhancements

Potential improvements:
1. Integration with Home Assistant history API
2. Machine learning models for runtime prediction
3. Weather forecast integration
4. Adaptive learning from actual vs. scheduled runtime
5. Per-day variation based on weather predictions

## Files Modified

- `src/devices_config.py` - Added sensor configuration fields
- `src/optimizer.py` - Integrated runtime calculation
- `src/optimization/thermal.py` - Added runtime parameter to optimize_wp
- `DEVICES_CONFIG_EXAMPLE.json` - Updated example with sensor fields
- `QUICK_START.md` - Added documentation reference

## Files Created

- `src/runtime_calculator.py` - Runtime calculation module
- `test_runtime_calculator.py` - Simple test script
- `test_optimization_integration.py` - Detailed test with output
- `HEAT_PUMP_RUNTIME.md` - Feature documentation
- `RUNTIME_IMPLEMENTATION_SUMMARY.md` - This file
