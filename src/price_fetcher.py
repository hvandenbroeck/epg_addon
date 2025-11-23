import logging
from datetime import datetime, timedelta
import pandas as pd
from entsoe import EntsoePandasClient

logger = logging.getLogger(__name__)


class EntsoeePriceFetcher:
    """Fetches day-ahead electricity prices from ENTSO-E."""
    
    def __init__(self, api_token, country_code):
        """Initialize the ENTSO-E price fetcher.
        
        Args:
            api_token: ENTSO-E API token
            country_code: Two-letter country code (e.g., 'BE', 'NL', 'DE')
        """
        self.api_token = api_token
        self.country_code = country_code
        self.client = EntsoePandasClient(api_token)
        
    def get_prices(self):
        """Fetch day-ahead prices for today and tomorrow.
        
        Returns:
            dict: Dictionary with 'today' and 'tomorrow' price lists (15-minute slots), or None if fetch fails
        """
        try:
            logger.info(f"🔎 Fetching day-ahead prices from ENTSO-E for {self.country_code}...")
            
            # Get current date in local timezone
            now = datetime.now()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            
            # Fetch prices for today and tomorrow
            # ENTSO-E uses UTC, so we need to convert
            start = pd.Timestamp(today_start, tz='Europe/Brussels')
            end = start + pd.Timedelta(days=2)  # Fetch 2 days to ensure we get tomorrow's data
            
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
            
            logger.info(f"📊 Received {len(prices_series)} hourly prices from ENTSO-E")
            
            # Split into today and tomorrow
            price_sets = {}
            
            # Today's prices
            today_end = today_start + timedelta(days=1)
            today_prices = prices_series[
                (prices_series.index >= pd.Timestamp(today_start, tz='Europe/Brussels')) & 
                (prices_series.index < pd.Timestamp(today_end, tz='Europe/Brussels'))
            ]
            
            if len(today_prices) > 0:
                price_sets['today'] = today_prices.tolist()
                logger.info(f"✅ Today: {len(today_prices)} price slots")
            
            # Tomorrow's prices
            tomorrow_start = today_start + timedelta(days=1)
            tomorrow_end = tomorrow_start + timedelta(days=1)
            tomorrow_prices = prices_series[
                (prices_series.index >= pd.Timestamp(tomorrow_start, tz='Europe/Brussels')) & 
                (prices_series.index < pd.Timestamp(tomorrow_end, tz='Europe/Brussels'))
            ]
            
            if len(tomorrow_prices) > 0:
                price_sets['tomorrow'] = tomorrow_prices.tolist()
                logger.info(f"✅ Tomorrow: {len(tomorrow_prices)} price slots")
            
            if not price_sets:
                logger.error("⚠️ No valid price data for today or tomorrow")
                return None
            
            return price_sets
            
        except Exception as e:
            logger.error(f"❌ Error fetching prices from ENTSO-E: {e}", exc_info=True)
            return None
