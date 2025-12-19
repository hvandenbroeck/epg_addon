"""
Price History Manager

Manages historical electricity price data storage and retrieval.
Stores data in a separate TinyDB file to avoid repeated API calls.
"""

import logging
from datetime import datetime, timedelta, timezone
import pandas as pd
from tinydb import TinyDB, Query
from entsoe import EntsoePandasClient

logger = logging.getLogger(__name__)


class PriceHistoryManager:
    """Manages historical electricity price data with local caching."""
    
    def __init__(self, api_token, country_code, db_path='/data/price_history.json'):
        """Initialize the price history manager.
        
        Args:
            api_token: ENTSO-E API token
            country_code: Two-letter country code (e.g., 'BE', 'NL', 'DE')
            db_path: Path to the TinyDB database file
        """
        self.api_token = api_token
        self.country_code = country_code
        self.db_path = db_path
        self.client = EntsoePandasClient(api_token) if api_token else None
        
    def _get_date_range_to_fetch(self, start_date, end_date):
        """Determine which dates are missing from the database.
        
        Args:
            start_date: datetime.date - Start date
            end_date: datetime.date - End date
            
        Returns:
            list of (start_date, end_date) tuples that need to be fetched
        """
        with TinyDB(self.db_path) as db:
            price_query = Query()
            records = db.search(
                (price_query.date >= start_date.isoformat()) & 
                (price_query.date <= end_date.isoformat())
            )
            # Only consider dates with complete hourly data (24 hours)
            date_counts = {}
            for record in records:
                date_str = record['date']
                date_counts[date_str] = date_counts.get(date_str, 0) + 1
            
            existing_dates = set(date_str for date_str, count in date_counts.items() if count >= 24)
        
        # Generate all dates in range
        current_date = start_date
        all_dates = []
        while current_date <= end_date:
            all_dates.append(current_date)
            current_date += timedelta(days=1)
        
        # Find missing dates
        missing_dates = [d for d in all_dates if d.isoformat() not in existing_dates]
        
        if not missing_dates:
            return []
        
        # Group consecutive dates into ranges to minimize API calls
        ranges = []
        range_start = missing_dates[0]
        range_end = missing_dates[0]
        
        for i in range(1, len(missing_dates)):
            if missing_dates[i] == range_end + timedelta(days=1):
                range_end = missing_dates[i]
            else:
                ranges.append((range_start, range_end))
                range_start = missing_dates[i]
                range_end = missing_dates[i]
        
        ranges.append((range_start, range_end))
        return ranges
    
    def _fetch_prices_from_api(self, start_date, end_date):
        """Fetch prices from ENTSO-E API for a date range.
        
        Args:
            start_date: datetime.date
            end_date: datetime.date
            
        Returns:
            pandas.Series with hourly prices, or None if fetch fails
        """
        if not self.client:
            logger.error("ENTSO-E client not initialized (no API token)")
            return None
        
        try:
            # Convert to pandas Timestamp with timezone
            start = pd.Timestamp(start_date, tz='Europe/Brussels')
            end = pd.Timestamp(end_date + timedelta(days=1), tz='Europe/Brussels')
            
            logger.info(f"📡 Fetching prices from ENTSO-E API: {start_date} to {end_date}")
            
            # Query day-ahead prices
            prices_series = self.client.query_day_ahead_prices(
                self.country_code, 
                start=start, 
                end=end
            )
            
            if prices_series is None or len(prices_series) == 0:
                logger.warning(f"⚠️ No price data returned for {start_date} to {end_date}")
                return None
            
            # Convert to EUR/kWh (ENTSO-E returns EUR/MWh)
            prices_series = prices_series / 1000.0
            
            # Export raw API response for analysis (before any resampling)
            prices_df = prices_series.to_frame(name='price_eur_per_kwh')
            prices_df.index.name = 'timestamp'
            prices_df.to_csv("/app/entsoe_raw_prices.csv", mode='a', header=not pd.io.common.file_exists("/app/entsoe_raw_prices.csv"))
            logger.info("💾 Raw ENTSO-E prices appended to: /app/entsoe_raw_prices.csv")
            
            # Always resample to hourly to handle both 15-min and hourly data consistently
            # For hourly data, this just aligns timestamps; for 15-min data, it averages to hourly
            logger.info(f"📊 Fetched {len(prices_series)} records, resampling to hourly resolution")
            prices_series = prices_series.resample('H').mean()
            logger.info(f"✅ Resampled to {len(prices_series)} hourly prices")
            
            # Export resampled hourly prices for analysis
            resampled_df = prices_series.to_frame(name='price_eur_per_kwh')
            resampled_df.index.name = 'timestamp'
            resampled_df.to_csv("/app/entsoe_resampled_prices.csv", mode='a', header=not pd.io.common.file_exists("/app/entsoe_resampled_prices.csv"))
            logger.info("💾 Resampled hourly prices appended to: /app/entsoe_resampled_prices.csv")
            
            return prices_series
            
        except Exception as e:
            logger.error(f"❌ Error fetching prices from ENTSO-E: {e}")
            return None
    
    def _store_prices(self, prices_series):
        """Store prices in the database.
        
        Args:
            prices_series: pandas.Series with datetime index and price values
        """
        with TinyDB(self.db_path) as db:
            for timestamp, price in prices_series.items():
                # Convert timestamp to local timezone and extract components
                local_ts = timestamp.tz_convert('Europe/Brussels')
                date_str = local_ts.date().isoformat()
                hour = local_ts.hour
                
                db.upsert({
                    'date': date_str,
                    'hour': hour,
                    'timestamp': local_ts.isoformat(),
                    'price': float(price)
                }, (Query().date == date_str) & (Query().hour == hour))
        
        logger.info(f"💾 Stored {len(prices_series)} price records in database")
    
    def _cleanup_old_data(self, keep_days=400):
        """Remove price data older than keep_days.
        
        Args:
            keep_days: Number of days to keep in the database
        """
        cutoff_date = (datetime.now().date() - timedelta(days=keep_days)).isoformat()
        
        with TinyDB(self.db_path) as db:
            price_query = Query()
            removed = db.remove(price_query.date < cutoff_date)
            
        if removed:
            logger.info(f"🗑️ Cleaned up {len(removed)} old price records (before {cutoff_date})")
    
    async def fetch_historical_prices(self, days_back=365):
        """Fetch historical prices, using cache when available.
        
        Args:
            days_back: Number of days of historical data to retrieve
            
        Returns:
            pandas.DataFrame with columns: date, hour, timestamp, price
        """
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days_back)
        
        logger.info(f"📊 Fetching historical prices from {start_date} to {end_date}")
        
        # Check which dates are missing
        missing_ranges = self._get_date_range_to_fetch(start_date, end_date)
        
        if missing_ranges:
            logger.info(f"🔍 Found {len(missing_ranges)} date range(s) to fetch from API")
            
            # Fetch missing data in chunks to avoid overwhelming the API
            for range_start, range_end in missing_ranges:
                # Limit each API call to max 30 days
                current_start = range_start
                while current_start <= range_end:
                    current_end = min(current_start + timedelta(days=29), range_end)
                    
                    prices = self._fetch_prices_from_api(current_start, current_end)
                    if prices is not None:
                        self._store_prices(prices)
                    
                    current_start = current_end + timedelta(days=1)
        else:
            logger.info("✅ All price data available in cache")
        
        # Cleanup old data
        self._cleanup_old_data()
        
        # Retrieve all data from database
        with TinyDB(self.db_path) as db:
            price_query = Query()
            records = db.search(
                (price_query.date >= start_date.isoformat()) & 
                (price_query.date <= end_date.isoformat())
            )
        
        if not records:
            logger.warning("⚠️ No price data available in database")
            return pd.DataFrame(columns=['date', 'hour', 'timestamp', 'price'])
        
        # Convert to DataFrame
        df = pd.DataFrame(records)
        df = df[['date', 'hour', 'timestamp', 'price']].copy()
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp').reset_index(drop=True)
        
        logger.info(f"✅ Retrieved {len(df)} hourly price records from database")
        return df
    async def get_price_percentiles(self, days_back=14, charge_percentile=30, discharge_percentile=70):
        """Calculate price percentiles from historical data for battery thresholds.
        
        Uses cached data first, then fetches missing data from API if needed.
        
        Args:
            days_back: Number of days of historical data to analyze (default 14)
            charge_percentile: Percentile for max charge price (default 30 = only charge below 30th percentile)
            discharge_percentile: Percentile for min discharge price (default 70 = only discharge above 70th percentile)
            
        Returns:
            dict with 'max_charge_price' and 'min_discharge_price' in EUR/kWh,
            or None if insufficient data
        """
        import numpy as np
        
        logger.info(f"📊 Calculating price percentiles from last {days_back} days (charge={charge_percentile}th, discharge={discharge_percentile}th)")
        
        # Fetch historical prices (uses cache first)
        df = await self.fetch_historical_prices(days_back=days_back)
        
        if df.empty or len(df) < 24:  # Need at least 1 day of data
            logger.warning(f"⚠️ Insufficient price data for percentile calculation: {len(df)} records")
            return None
        
        prices = df['price'].values
        
        # Calculate percentiles
        max_charge_price = float(np.percentile(prices, charge_percentile))
        min_discharge_price = float(np.percentile(prices, discharge_percentile))
        
        # Log statistics
        price_min = float(np.min(prices))
        price_max = float(np.max(prices))
        price_mean = float(np.mean(prices))
        price_median = float(np.median(prices))
        
        logger.info(f"📈 Price statistics (last {days_back} days): "
                   f"min={price_min:.4f}, max={price_max:.4f}, "
                   f"mean={price_mean:.4f}, median={price_median:.4f} EUR/kWh")
        logger.info(f"🔋 Battery thresholds: max_charge_price={max_charge_price:.4f} EUR/kWh ({charge_percentile}th percentile), "
                   f"min_discharge_price={min_discharge_price:.4f} EUR/kWh ({discharge_percentile}th percentile)")
        
        return {
            'max_charge_price': max_charge_price,
            'min_discharge_price': min_discharge_price,
            'price_stats': {
                'min': price_min,
                'max': price_max,
                'mean': price_mean,
                'median': price_median,
                'days_analyzed': days_back,
                'data_points': len(prices)
            }
        }