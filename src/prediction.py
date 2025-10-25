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
        df = await self.statistics_loader.fetch_statistics()

        # 2. Feature Engineering (no temperature)
        df["target"] = df["energy_used_per_hour"]
        df["hour"] = df["timestamp"].dt.hour
        df["dayofweek"] = df["timestamp"].dt.dayofweek
        #df["dayofyear"] = df["timestamp"].dt.dayofyear
        df["hour_sin"] = np.sin(2 * np.pi * df["hour"] / 24)
        df["hour_cos"] = np.cos(2 * np.pi * df["hour"] / 24)
        #df["doy_sin"] = np.sin(2 * np.pi * df["dayofyear"] / 365)
        #df["doy_cos"] = np.cos(2 * np.pi * df["dayofyear"] / 365)

        # 3. Prepare Features and Target
        feature_cols = [
            "hour", "dayofweek", "hour_sin", "hour_cos"
        ]
        X = df[feature_cols]
        y = df["target"]

        # 4. Train/Test Split
        split_date = df["timestamp"].max() - pd.Timedelta(days=7)
        X_train = X[df["timestamp"] < split_date]
        y_train = y[df["timestamp"] < split_date]
        X_val = X[df["timestamp"] >= split_date]
        y_val = y[df["timestamp"] >= split_date]

        # 5. Train LightGBM Model
        lgb_reg = lgb.LGBMRegressor(n_estimators=100, max_depth=4)
        lgb_reg.fit(X_train, y_train)

        # 6. Predict for Tomorrow
        # Set tomorrow_date to 00:00 UTC of the next day
        last_ts = df["timestamp"].max()
        tomorrow_date = last_ts.normalize() + pd.Timedelta(days=1)
        future_hours = [i for i in range(24)]
        future_dayofweek = [(tomorrow_date + timedelta(hours=i)).dayofweek for i in future_hours]
        future_dayofyear = [(tomorrow_date + timedelta(hours=i)).timetuple().tm_yday for i in future_hours]
        hour_sin = [np.sin(2 * np.pi * h / 24) for h in future_hours]
        hour_cos = [np.cos(2 * np.pi * h / 24) for h in future_hours]
        doy_sin = [np.sin(2 * np.pi * doy / 365) for doy in future_dayofyear]
        doy_cos = [np.cos(2 * np.pi * doy / 365) for doy in future_dayofyear]
        future_df = pd.DataFrame({
            "hour": future_hours,
            "dayofweek": future_dayofweek,
            "hour_sin": hour_sin,
            "hour_cos": hour_cos
        })
        # Export the DataFrame to CSV for debugging
        future_df.to_csv("/app/tomorrow_features.csv", index=False)
        df.to_csv("/app/features.csv", index=False)

        tomorrow_pred = lgb_reg.predict(future_df)

        # 7. Log Results
        for i, p in enumerate(tomorrow_pred):
            logger.info(f"Hour {i}: Predicted total power usage = {p:.2f} kWh")

        # Log total daily predicted kWh
        total_predicted_kwh = np.sum(tomorrow_pred)
        logger.info(f"Total predicted power usage for tomorrow: {total_predicted_kwh:.2f} kWh")

        # 8. (Optional) Evaluate on Validation Set
        val_pred = lgb_reg.predict(X_val)
        mae = mean_absolute_error(y_val, val_pred)
        logger.info(f"Validation MAE: {mae:.2f} kWh")
