import pandas as pd
import numpy as np
import lightgbm as lgb
from datetime import datetime, timedelta
import logging
from sklearn.metrics import mean_absolute_error

logger = logging.getLogger(__name__)

class Prediction:
    def __init__(self, statistics_loader, weather):
        self.statistics_loader = statistics_loader
        self.weather = weather

    async def calculateTomorrowsPowerUsage(self):
        # 1. Get historical data as DataFrame
        logger.info("Fetching historical power usage data")
        df = await self.statistics_loader.fetch_statistics()

        # 2. Aggregate power usage by day first
        logger.info(f"Aggregating power usage data ({len(df)} rows) by day")
        df["date"] = df["timestamp"].dt.date
        daily_df = df.groupby("date").agg({
            "energy_used_per_hour": "sum"
        }).reset_index()
        daily_df["dayofweek"] = pd.to_datetime(daily_df["date"]).dt.dayofweek
        logger.info(f"Aggregated to {len(daily_df)} daily records")

        # 3. Get historical weather data (already daily)
        logger.info("Fetching historical weather data")
        weather_df = await self.weather.getHistoricalTemperature()
        
        # 4. Merge weather data with power usage data on date
        logger.info(f"Merging daily power usage data ({len(daily_df)} rows) with weather data ({len(weather_df)} rows)")
        daily_df = daily_df.merge(weather_df, on="date", how="left")
        
        # Log any missing temperature or sunshine duration data
        missing_temp = daily_df["temperature"].isna().sum()
        missing_sunshine = daily_df["sunshine_duration"].isna().sum()
        if missing_temp > 0:
            logger.warning(f"{missing_temp} days have missing temperature data")
            daily_df["temperature"] = daily_df["temperature"].ffill().bfill()
        if missing_sunshine > 0:
            logger.warning(f"{missing_sunshine} days have missing sunshine duration data")
            daily_df["sunshine_duration"] = daily_df["sunshine_duration"].ffill().bfill()
        
        logger.info(f"Final merged dataset has {len(daily_df)} daily records")

        # 5. Prepare Features and Target
        feature_cols = [
            "dayofweek", "temperature", "sunshine_duration"
        ]
        X = daily_df[feature_cols]
        y = daily_df["energy_used_per_hour"]

        # 6. Train/Test Split
        split_date = pd.to_datetime(daily_df["date"]).max() - pd.Timedelta(days=7)
        X_train = X[pd.to_datetime(daily_df["date"]) < split_date]
        y_train = y[pd.to_datetime(daily_df["date"]) < split_date]
        X_val = X[pd.to_datetime(daily_df["date"]) >= split_date]
        y_val = y[pd.to_datetime(daily_df["date"]) >= split_date]

        # 7. Train LightGBM Model
        logger.info(f"Training LightGBM model with {len(X_train)} daily samples")
        lgb_reg = lgb.LGBMRegressor(n_estimators=100, max_depth=4)
        lgb_reg.fit(X_train, y_train)

        # 8. Get tomorrow's weather forecast (daily mean temperature and sunshine duration)
        logger.info("Fetching tomorrow's weather forecast")
        tomorrow_weather = await self.weather.getTomorrowsTemperature()
        
        if not tomorrow_weather or 'temperature' not in tomorrow_weather or 'sunshine_duration' not in tomorrow_weather:
            logger.error("No complete weather forecast available for tomorrow")
            raise Exception("No complete weather forecast available for tomorrow")
        
        tomorrow_temp = tomorrow_weather['temperature']
        tomorrow_sunshine = tomorrow_weather['sunshine_duration']

        # 9. Predict for Tomorrow
        last_date = pd.to_datetime(daily_df["date"]).max()
        tomorrow_dayofweek = (last_date + pd.Timedelta(days=1)).dayofweek
        future_df = pd.DataFrame({
            "dayofweek": [tomorrow_dayofweek],
            "temperature": [tomorrow_temp],
            "sunshine_duration": [tomorrow_sunshine]
        })
        # Export the DataFrame to CSV for debugging
        future_df.to_csv("/app/tomorrow_features.csv", index=False)
        daily_df.to_csv("/app/features.csv", index=False)

        logger.info("Predicting tomorrow's total daily power usage")
        tomorrow_pred = lgb_reg.predict(future_df)

        # 10. Log Results
        logger.info(f"Predicted total power usage for tomorrow: {tomorrow_pred[0]:.2f} kWh (temp: {tomorrow_temp:.1f}°C, sunshine: {tomorrow_sunshine:.0f}s)")

        # 11. (Optional) Evaluate on Validation Set
        val_pred = lgb_reg.predict(X_val)
        mae = mean_absolute_error(y_val, val_pred)
        logger.info(f"Validation MAE (daily): {mae:.2f} kWh")
