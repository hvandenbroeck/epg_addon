# Forecasting Module

This module contains all components related to predicting power usage for the next day.

## Files

### `prediction.py`
Main prediction logic using LightGBM (Light Gradient Boosting Machine) for forecasting tomorrow's total daily power usage based on:
- Historical power consumption data
- Weather data (temperature, sunshine duration)
- Day of week patterns

**Key Class:** `Prediction`
- `calculateTomorrowsPowerUsage()`: Trains a model and predicts tomorrow's power usage

### `statistics_loader.py`
Loads historical power usage statistics from Home Assistant API.

**Key Class:** `StatisticsLoader`
- `fetch_statistics()`: Fetches hourly energy statistics for the past 365 days
- Returns a pandas DataFrame with timestamp and energy usage data

### `weather.py`
Fetches weather data using Home Assistant location and Open-Meteo API.

**Key Class:** `Weather`
- `getTomorrowsTemperature()`: Gets tomorrow's weather forecast (temperature & sunshine)
- `getHistoricalTemperature()`: Gets historical weather data for model training

### `HAConfig.py`
Fetches energy dashboard configuration from Home Assistant via WebSocket API.

**Key Class:** `HAEnergyDashboardFetcher`
- `fetch_energy_dashboard_config()`: Retrieves the energy dashboard configuration including which entities to monitor

## Usage

```python
from src.forecasting import Prediction, StatisticsLoader, Weather

# Initialize components
statistics_loader = StatisticsLoader(access_token)
weather = Weather(access_token)
prediction = Prediction(statistics_loader, weather)

# Calculate tomorrow's predicted power usage
await prediction.calculateTomorrowsPowerUsage()
```

## Dependencies

- `pandas`: Data manipulation and analysis
- `numpy`: Numerical computing
- `lightgbm`: Gradient boosting framework for ML
- `scikit-learn`: Machine learning metrics
- `aiohttp`: Async HTTP client
- `websockets`: WebSocket client for Home Assistant API
