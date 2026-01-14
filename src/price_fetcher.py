import logging
import asyncio
from datetime import datetime, timedelta
import pandas as pd
from entsoe import EntsoePandasClient

logger = logging.getLogger(__name__)

# Slot duration in minutes for price data
SLOT_MINUTES = 15


class EntsoeePriceFetcher:
    """Fetches day-ahead electricity prices from ENTSO-E."""
    
    def __init__(self, api_token, country_code, retry_interval_minutes=5, retry_max_hours=2):
        """Initialize the ENTSO-E price fetcher.
        
        Args:
            api_token: ENTSO-E API token
            country_code: Two-letter country code (e.g., 'BE', 'NL', 'DE')
            retry_interval_minutes: Minutes between retry attempts (default: 5)
            retry_max_hours: Maximum hours to keep retrying (default: 2)
        """
        self.api_token = api_token
        self.country_code = country_code
        self.client = EntsoePandasClient(api_token)
        self.retry_interval_minutes = retry_interval_minutes
        self.retry_max_hours = retry_max_hours

    def get_horizon_prices(self, horizon_start=None, lock_hours=2):
        """Fetch prices for a rolling horizon from now until end of tomorrow.
        
        This method builds a price array for the optimization horizon:
        - Starts from the current time (rounded to 15-minute slot boundary)
        - Extends to end of tomorrow (or as far as data is available)
        - Returns metadata about the horizon for constraint calculations
        - Automatically retries on failure (503 errors, etc.) based on configured intervals
        
        Args:
            horizon_start: datetime for horizon start (defaults to now)
            lock_hours: Number of hours from now to lock (don't reschedule)
            
        Returns:
            dict with:
                'prices': List of prices per 15-minute slot for the horizon
                'horizon_start': datetime when horizon starts
                'horizon_end': datetime when horizon ends
                'lock_end_slot': Slot index where lock period ends
                'slot_minutes': Minutes per slot (15 minutes)
            Returns None if all retry attempts fail
        """
        max_attempts = (self.retry_max_hours * 60) // self.retry_interval_minutes
        
        for attempt in range(1, int(max_attempts) + 1):
            result = self._fetch_prices(horizon_start, lock_hours, attempt, int(max_attempts))
            if result is not None:
                return result
            
            # If fetch failed and we have more attempts, wait before retry
            if attempt < max_attempts:
                logger.warning(f"⏳ Waiting {self.retry_interval_minutes} minutes before retry...")
                import time
                time.sleep(self.retry_interval_minutes * 60)
        
        logger.error(f"❌ Failed to fetch prices after {int(max_attempts)} attempts over {self.retry_max_hours} hours")
        return None
    
    def _fetch_prices(self, horizon_start, lock_hours, attempt, max_attempts):
        """Internal method to fetch prices (single attempt).
        
        Args:
            horizon_start: datetime for horizon start
            lock_hours: Number of hours from now to lock
            attempt: Current attempt number
            max_attempts: Total number of attempts
            
        Returns:
            dict with price data or None if fetch fails
        """
        try:
            if attempt > 1:
                logger.info(f"🔄 Retry attempt {attempt}/{max_attempts}")
            logger.info(f"🔎 Fetching horizon prices from ENTSO-E for {self.country_code}...")
            
            now = horizon_start or datetime.now()
            # Round down to current 15-minute slot
            current_minute = (now.minute // SLOT_MINUTES) * SLOT_MINUTES
            current_slot_start = now.replace(minute=current_minute, second=0, microsecond=0)
            
            # Calculate dates
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            tomorrow_start = today_start + timedelta(days=1)
            tomorrow_end = tomorrow_start + timedelta(days=1)
            
            # Fetch prices for today and tomorrow
            start = pd.Timestamp(today_start, tz='Europe/Brussels')
            end = pd.Timestamp(tomorrow_end, tz='Europe/Brussels')
            
            # Query day-ahead prices
            prices_series = self.client.query_day_ahead_prices(
                self.country_code, 
                start=start, 
                end=end
            )
            
            if prices_series is None or len(prices_series) == 0:
                logger.error("⚠️ No price data returned from ENTSO-E")
                return None
            
            # Convert to EUR/kWh (ENTSO-E returns EUR/MWh)
            prices_series = prices_series / 1000.0
            
            logger.info(f"📊 Received {len(prices_series)} price points from ENTSO-E")
            
            # Build the horizon: from current slot to end of tomorrow in 15-minute intervals
            horizon_prices = []
            horizon_datetimes = []
            
            current_slot = current_slot_start
            while current_slot < tomorrow_end:
                slot_ts = pd.Timestamp(current_slot, tz='Europe/Brussels')
                # Find the price for this slot (may need to look up the hour if data is hourly)
                if slot_ts in prices_series.index:
                    horizon_prices.append(prices_series[slot_ts])
                    horizon_datetimes.append(current_slot)
                else:
                    # If 15-minute data not available, use hourly price for this slot
                    hour_ts = pd.Timestamp(current_slot.replace(minute=0), tz='Europe/Brussels')
                    if hour_ts in prices_series.index:
                        horizon_prices.append(prices_series[hour_ts])
                        horizon_datetimes.append(current_slot)
                current_slot += timedelta(minutes=SLOT_MINUTES)
            
            if not horizon_prices:
                logger.error("⚠️ No prices available for the horizon")
                return None
            
            # Calculate lock end slot (slots that shouldn't be rescheduled)
            lock_end_slot = min(lock_hours * (60 // SLOT_MINUTES), len(horizon_prices))
            
            horizon_end = horizon_datetimes[-1] + timedelta(minutes=SLOT_MINUTES) if horizon_datetimes else current_slot_start
            
            logger.info(f"✅ Horizon: {current_slot_start.strftime('%Y-%m-%d %H:%M')} to {horizon_end.strftime('%Y-%m-%d %H:%M')} "
                       f"({len(horizon_prices)} slots @ {SLOT_MINUTES}min, lock until slot {lock_end_slot})")
            
            return {
                'prices': horizon_prices,
                'horizon_start': current_slot_start,
                'horizon_end': horizon_end,
                'lock_end_slot': lock_end_slot,
                'slot_minutes': SLOT_MINUTES,
            }
            
        except Exception as e:
            is_retryable = "503" in str(e) or "Service Temporarily Unavailable" in str(e) or "HTTPError" in str(type(e).__name__)
            if is_retryable and attempt < max_attempts:
                logger.warning(f"⚠️ Retryable error on attempt {attempt}/{max_attempts}: {e}")
            else:
                logger.error(f"❌ Error fetching horizon prices from ENTSO-E (attempt {attempt}/{max_attempts}): {e}", exc_info=True)
            return None
