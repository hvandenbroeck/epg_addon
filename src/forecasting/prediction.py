import pandas as pd
import numpy as np
import lightgbm as lgb
from datetime import datetime, timedelta
import logging
from sklearn.metrics import mean_absolute_error
from ..config import CONFIG

logger = logging.getLogger(__name__)

class Prediction:
    def __init__(self, statistics_loader, weather, price_history_manager=None):
        self.statistics_loader = statistics_loader
        self.weather = weather
        self.price_history_manager = price_history_manager
        self.days_back = CONFIG['options'].get('prediction_days_back', 365)

    async def calculateTomorrowsPowerUsage(self):
        """
        Predict tomorrow's hourly power usage using historical data, weather, and price features.
        Returns a DataFrame with hourly predictions.
        """
        # 1. Get historical hourly power usage data
        logger.info("📊 Fetching historical power usage data...")
        usage_df = await self.statistics_loader.fetch_statistics(days_back=self.days_back)
        
        if len(usage_df) == 0:
            logger.error("❌ No power usage data available")
            raise Exception("No power usage data available")
        
        # Ensure timestamp is datetime
        usage_df['timestamp'] = pd.to_datetime(usage_df['timestamp'])
        usage_df['hour'] = usage_df['timestamp'].dt.hour
        usage_df['date'] = usage_df['timestamp'].dt.date
        usage_df['dayofweek'] = usage_df['timestamp'].dt.dayofweek
        
        logger.info(f"✅ Loaded {len(usage_df)} hourly power usage records")
        
        # 2. Get historical hourly weather data
        logger.info("🌤️ Fetching historical weather data...")
        weather_df = await self.weather.getHistoricalHourlyWeather(days_back=self.days_back)
        
        if len(weather_df) == 0:
            logger.error("❌ No weather data available")
            raise Exception("No weather data available")
        
        logger.info(f"✅ Loaded {len(weather_df)} hourly weather records")
        
        # 3. Get historical hourly price data (if manager available)
        has_price_data = False
        if self.price_history_manager:
            logger.info("💰 Fetching historical price data...")
            price_df = await self.price_history_manager.fetch_historical_prices(days_back=self.days_back)
            
            if len(price_df) == 0:
                logger.warning("⚠️ No price data available, continuing without price features")
            else:
                # Ensure timestamp is datetime type (utc=True handles timezone-aware strings)
                price_df['timestamp'] = pd.to_datetime(price_df['timestamp'], utc=True)
                has_price_data = True
                logger.info(f"✅ Loaded {len(price_df)} hourly price records")
        else:
            logger.info("ℹ️ No price history manager configured, skipping price features")
        
        # 4. Merge all data on timestamp/hour
        logger.info("🔗 Merging datasets...")
        
        # Align timestamps for merging and remove timezone info to avoid merge conflicts
        usage_df['timestamp_aligned'] = usage_df['timestamp'].dt.floor('h').dt.tz_localize(None)
        weather_df['timestamp_aligned'] = weather_df['timestamp'].dt.floor('h').dt.tz_localize(None)
        
        # Merge usage and weather
        merged_df = usage_df.merge(
            weather_df[['timestamp_aligned', 'temperature', 'cloud_cover']], 
            on='timestamp_aligned', 
            how='left'
        )
        
        # Merge with price data if available
        if has_price_data:
            price_df['timestamp_aligned'] = price_df['timestamp'].dt.floor('h').dt.tz_localize(None)
            merged_df = merged_df.merge(
                price_df[['timestamp_aligned', 'price']], 
                on='timestamp_aligned', 
                how='left'
            )
            
            # Fill missing prices with forward fill then backward fill
            merged_df['price'] = merged_df['price'].ffill().bfill()
        
        # Fill missing weather data
        merged_df['temperature'] = merged_df['temperature'].ffill().bfill()
        merged_df['cloud_cover'] = merged_df['cloud_cover'].ffill().bfill()
        
        # Drop rows with missing target variable
        merged_df = merged_df.dropna(subset=['energy_used_per_hour'])
        
        logger.info(f"✅ Merged dataset has {len(merged_df)} hourly records")
        
        # 5. Prepare features and target
        feature_cols = ['hour', 'dayofweek', 'temperature', 'cloud_cover']
        if has_price_data:
            feature_cols.append('price')
        
        features = merged_df[feature_cols].copy()
        target_energy_usage = merged_df['energy_used_per_hour'].copy()
        
        # Remove any remaining NaN values
        valid_mask = ~(features.isna().any(axis=1) | target_energy_usage.isna())
        features = features[valid_mask]
        target_energy_usage = target_energy_usage[valid_mask]
        
        logger.info(f"📈 Training dataset: {len(features)} samples with features: {feature_cols}")
        
        # 6. Train/Test Split (use last 7 days for validation)
        split_date = merged_df['timestamp'].max() - pd.Timedelta(days=7)
        train_mask = merged_df.loc[valid_mask, 'timestamp'] < split_date
        val_mask = merged_df.loc[valid_mask, 'timestamp'] >= split_date
        
        features_train = features[train_mask]
        target_train = target_energy_usage[train_mask]
        features_val = features[val_mask]
        target_val = target_energy_usage[val_mask]
        
        logger.info(f"📊 Training samples: {len(features_train)}, Validation samples: {len(features_val)}")
        
        # 7. Train LightGBM Model
        logger.info("🤖 Training LightGBM model...")
        lgb_reg = lgb.LGBMRegressor(
            n_estimators=200,
            max_depth=6,
            learning_rate=0.05,
            num_leaves=31,
            random_state=42
        )
        lgb_reg.fit(features_train, target_train)
        logger.info("✅ Model training complete")
        
        # 8. Evaluate on validation set
        val_pred = lgb_reg.predict(features_val)
        mae = mean_absolute_error(target_val, val_pred)
        logger.info(f"📊 Validation MAE (hourly): {mae:.3f} kWh")
        
        # 9. Get tomorrow's weather forecast
        logger.info("🌤️ Fetching tomorrow's weather forecast...")
        tomorrow_weather_df = await self.weather.getTomorrowsHourlyWeather()
        
        if len(tomorrow_weather_df) == 0:
            logger.error("❌ No weather forecast available for tomorrow")
            raise Exception("No weather forecast available for tomorrow")
        
        logger.info(f"✅ Retrieved {len(tomorrow_weather_df)} hours of forecast")
        
        # 10. Prepare tomorrow's features
        tomorrow = (datetime.now().date() + timedelta(days=1))
        tomorrow_dayofweek = pd.Timestamp(tomorrow).dayofweek
        
        future_df = tomorrow_weather_df[['hour', 'temperature', 'cloud_cover']].copy()
        future_df['dayofweek'] = tomorrow_dayofweek
        
        # Add price data if available (use today's prices as proxy or average)
        if has_price_data:
            # Try to get similar day prices (same day of week from recent weeks)
            similar_day_prices = merged_df[
                (merged_df['dayofweek'] == tomorrow_dayofweek) & 
                (merged_df['timestamp'] >= merged_df['timestamp'].max() - pd.Timedelta(days=28))
            ].groupby('hour')['price'].mean()
            
            future_df['price'] = future_df['hour'].map(similar_day_prices)
            
            # If some hours are missing, fill with overall hourly average
            if future_df['price'].isna().any():
                hourly_avg_prices = merged_df.groupby('hour')['price'].mean()
                future_df['price'] = future_df['price'].fillna(
                    future_df['hour'].map(hourly_avg_prices)
                )
        
        # Ensure feature order matches training
        future_df = future_df[feature_cols]
        
        # 11. Make predictions
        logger.info("🔮 Predicting tomorrow's hourly power usage...")
        tomorrow_predictions = lgb_reg.predict(future_df)
        
        # 12. Create results DataFrame
        results_df = tomorrow_weather_df[['hour', 'timestamp', 'temperature', 'cloud_cover']].copy()
        results_df['predicted_kwh'] = tomorrow_predictions
        results_df['date'] = tomorrow
        
        if has_price_data:
            results_df['price'] = future_df['price'].values
        
        # Calculate total predicted usage
        total_predicted = tomorrow_predictions.sum()
        
        # 13. Log results in a nicely formatted way
        logger.info("=" * 80)
        logger.info("📅 TOMORROW'S HOURLY POWER USAGE PREDICTION")
        logger.info("=" * 80)
        logger.info(f"Date: {tomorrow.strftime('%A, %B %d, %Y')}")
        logger.info(f"Total Predicted Usage: {total_predicted:.2f} kWh")
        logger.info(f"Validation MAE: {mae:.3f} kWh per hour")
        logger.info("-" * 80)
        
        if has_price_data:
            logger.info(f"{'Hour':<6} {'Time':<8} {'Temp':<8} {'Cloud':<8} {'Predicted':<12} {'Price (€/kWh)':<15}")
        else:
            logger.info(f"{'Hour':<6} {'Time':<8} {'Temp':<8} {'Cloud':<8} {'Predicted':<12}")
        logger.info("-" * 80)
        
        for _, row in results_df.iterrows():
            hour_str = f"{int(row['hour']):02d}:00"
            time_str = row['timestamp'].strftime('%H:%M')
            temp_str = f"{row['temperature']:.1f}°C"
            cloud_str = f"{row['cloud_cover']:.0f}%"
            pred_str = f"{row['predicted_kwh']:.3f} kWh"
            
            if has_price_data:
                price_str = f"€{row['price']:.4f}"
                logger.info(f"{hour_str:<6} {time_str:<8} {temp_str:<8} {cloud_str:<8} {pred_str:<12} {price_str:<15}")
            else:
                logger.info(f"{hour_str:<6} {time_str:<8} {temp_str:<8} {cloud_str:<8} {pred_str:<12}")
        
        logger.info("-" * 80)
        logger.info(f"Peak hour: {results_df.loc[results_df['predicted_kwh'].idxmax(), 'hour']:02.0f}:00 "
                   f"({results_df['predicted_kwh'].max():.3f} kWh)")
        logger.info(f"Lowest hour: {results_df.loc[results_df['predicted_kwh'].idxmin(), 'hour']:02.0f}:00 "
                   f"({results_df['predicted_kwh'].min():.3f} kWh)")
        logger.info("=" * 80)
        
        # Export to CSV for debugging and web UI
        results_df.to_csv("/app/tomorrow_hourly_predictions.csv", index=False)
        logger.info("💾 Predictions saved to: /app/tomorrow_hourly_predictions.csv")
        
        # Export tomorrow's features for web UI download
        future_df.to_csv("/app/tomorrow_features.csv", index=False)
        logger.info("💾 Tomorrow's features saved to: /app/tomorrow_features.csv")
        
        # Export full merged dataset for analysis
        merged_df.to_csv("/app/merged_historical_data.csv", index=False)
        logger.info("💾 Merged historical data saved to: /app/merged_historical_data.csv")
        
        return results_df
