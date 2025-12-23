# Forecasting Module

This module contains all components related to predicting power usage for the next day on an hourly basis.

## Files

### `prediction.py`
Main prediction logic using LightGBM (Light Gradient Boosting Machine) for forecasting power usage based on:
- Historical hourly power consumption data
- Hourly weather data (temperature, cloud cover)
- Electricity price data (if available)
- Hour of day and day of week patterns

**Key Class:** `Prediction`
- `calculatePowerUsage()`: Trains a model and predicts hourly power usage for the remaining hours of today AND all of tomorrow
- `calculateTomorrowsPowerUsage()`: [DEPRECATED] Returns only tomorrow's predictions for backwards compatibility
- Returns a DataFrame with hourly predictions including timestamp, temperature, cloud cover, date, and prices
- Outputs nicely formatted hourly predictions to logs

### `statistics_loader.py`
Loads historical power usage statistics from Home Assistant API.

**Key Class:** `StatisticsLoader`
- `fetch_statistics()`: Fetches hourly energy statistics for the past 365 days
- Returns a pandas DataFrame with timestamp and energy usage data

### `weather.py`
Fetches weather data using Home Assistant location and Open-Meteo API.

**Key Class:** `Weather`
- `getUpcomingHourlyWeather()`: Gets weather forecast for remaining hours of today and all of tomorrow (temperature & cloud cover)
- `getHistoricalHourlyWeather()`: Gets historical hourly weather data for model training

### `price_history.py`
Manages historical electricity price data with intelligent caching to minimize API calls.

**Key Class:** `PriceHistoryManager`
- `fetch_historical_prices()`: Retrieves price data from cache or ENTSO-E API
- Automatically detects missing dates and fetches only what's needed
- Stores data in `price_history.json` TinyDB file
- Auto-cleans old data (keeps last 400 days by default)
- Fetches in chunks (max 30 days per API call) to avoid overwhelming the API

**Database:** `price_history.json`
- Hourly price records with date, hour, timestamp, and price
- Automatically managed - cleaned up after data is no longer needed
- Persistent across program restarts

### `HAConfig.py`
Fetches energy dashboard configuration from Home Assistant via WebSocket API.

**Key Class:** `HAEnergyDashboardFetcher`
- `fetch_energy_dashboard_config()`: Retrieves the energy dashboard configuration including which entities to monitor

## Usage

```python
from src.forecasting import Prediction, StatisticsLoader, Weather, PriceHistoryManager

# Initialize components
statistics_loader = StatisticsLoader(access_token)
weather = Weather(access_token)

# Optional: Initialize price history manager for better predictions
entsoe_token = "your_entsoe_api_token"
price_history_manager = PriceHistoryManager(entsoe_token, "BE")

# Create prediction instance (with or without price data)
prediction = Prediction(statistics_loader, weather, price_history_manager)

# Calculate power usage for remaining hours of today AND tomorrow
results_df = await prediction.calculatePowerUsage()

# Or use the legacy method for just tomorrow (backwards compatible)
tomorrow_df = await prediction.calculateTomorrowsPowerUsage()

# Results DataFrame contains: hour, timestamp, temperature, cloud_cover, date, predicted_kwh, price (if available)
```

## Output Format

The prediction outputs a nicely formatted table to logs:

```
================================================================================
📅 POWER USAGE PREDICTION (TODAY + TOMORROW)
================================================================================

📌 TODAY (Tuesday, November 26, 2025) - Remaining 8 hours
Predicted Usage: 15.32 kWh
--------------------------------------------------------------------------------
Hour   Time     Temp     Cloud    Predicted    Price (€/kWh)
--------------------------------------------------------------------------------
16:00  16:00    8.2°C    45%      1.234 kWh    €0.1234
17:00  17:00    7.9°C    50%      1.823 kWh    €0.1456
...

📌 TOMORROW (Wednesday, November 27, 2025)
Predicted Usage: 45.32 kWh
--------------------------------------------------------------------------------
Hour   Time     Temp     Cloud    Predicted    Price (€/kWh)
--------------------------------------------------------------------------------
00:00  00:00    6.2°C    35%      1.234 kWh    €0.1234
01:00  01:00    5.9°C    40%      1.123 kWh    €0.1156
...
--------------------------------------------------------------------------------
Total Predicted Usage: 60.64 kWh
Validation MAE: 0.123 kWh per hour
Peak hour: 18:00 (2.456 kWh)
Lowest hour: 03:00 (0.987 kWh)
================================================================================
```

## Dependencies

- `pandas`: Data manipulation and analysis
- `numpy`: Numerical computing
- `lightgbm`: Gradient boosting framework for ML
- `scikit-learn`: Machine learning metrics
- `aiohttp`: Async HTTP client
- `websockets`: WebSocket client for Home Assistant API
- `tinydb`: Lightweight JSON database for price history
- `entsoe-py`: ENTSO-E API client for electricity prices
