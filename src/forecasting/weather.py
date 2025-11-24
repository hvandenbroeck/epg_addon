import aiohttp
import pandas as pd
import logging
from datetime import datetime, timedelta, timezone
from ..config import CONFIG

logger = logging.getLogger(__name__)

class Weather:
    """
    Fetches weather data using Home Assistant location and Open-Meteo API.
    """
    def __init__(self, access_token, user_agent="EPGAddon/1.0 github.com/hvandenbroeck"):
        self.ha_url = CONFIG['options']['ha_url']
        self.access_token = access_token
        # user_agent parameter kept for backwards compatibility but not used by Open-Meteo
        self.lat = None
        self.lon = None
        logger.info("Weather class initialized")

    async def _fetch_location(self):
        """Fetch latitude and longitude from Home Assistant config."""
        if self.lat is not None and self.lon is not None:
            logger.debug(f"Location already cached: lat={self.lat}, lon={self.lon}")
            return
        
        logger.info("Fetching location from Home Assistant")
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.ha_url}/api/config", headers=headers) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to get HA config: HTTP {resp.status}")
                    raise Exception(f"Failed to get HA config: {resp.status}")
                config = await resp.json()
        
        self.lat = config.get("latitude")
        self.lon = config.get("longitude")
        if self.lat is None or self.lon is None:
            logger.error("Latitude or longitude not found in HA config")
            raise Exception("Latitude or longitude not found in HA config")
        
        logger.info(f"Location fetched successfully: lat={self.lat}, lon={self.lon}")

    async def getTomorrowsTemperature(self):
        """
        Returns a dict with tomorrow's daily mean temperature (in Celsius) and sunshine duration (in seconds) from Open-Meteo.
        Returns: {'temperature': float, 'sunshine_duration': float}
        """
        logger.info("Fetching tomorrow's daily mean temperature and sunshine duration forecast")
        # Ensure location is fetched
        await self._fetch_location()

        # Get tomorrow's date
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
        start_date = tomorrow.isoformat()
        end_date = tomorrow.isoformat()
        
        logger.debug(f"Querying Open-Meteo daily forecast for date: {start_date}")
        openmeteo_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={self.lat}&longitude={self.lon}"
            f"&daily=temperature_2m_mean,sunshine_duration"
            f"&start_date={start_date}&end_date={end_date}"
            f"&timezone=UTC"
        )
        
        async with aiohttp.ClientSession() as session:
            async with session.get(openmeteo_url) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to get Open-Meteo daily forecast: HTTP {resp.status}")
                    raise Exception(f"Failed to get Open-Meteo daily forecast: {resp.status}")
                forecast = await resp.json()

        # Extract tomorrow's daily mean temperature and sunshine duration
        temperatures = forecast["daily"]["temperature_2m_mean"]
        sunshine_durations = forecast["daily"]["sunshine_duration"]
        logger.info(f"Successfully fetched tomorrow's forecast - temperature: {temperatures[0] if temperatures else 'N/A'}, sunshine duration: {sunshine_durations[0] if sunshine_durations else 'N/A'}s")
        
        if temperatures and sunshine_durations:
            return {'temperature': temperatures[0], 'sunshine_duration': sunshine_durations[0]}
        else:
            return {}

    async def getHistoricalTemperature(self):
        """
        Returns a DataFrame with historical daily average temperatures and sunshine duration from the last year.
        Columns: 'date' (datetime.date), 'temperature' (Celsius), 'sunshine_duration' (seconds)
        """
        logger.info("Fetching historical daily temperature and sunshine duration data for the last year")
        # Ensure location is fetched
        await self._fetch_location()
        
        # Calculate date range for last year
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=365)
        
        logger.debug(f"Querying Open-Meteo archive from {start_date} to {end_date} (daily)")
        # Query Open-Meteo historical API for daily temperature and sunshine duration
        openmeteo_url = (
            f"https://archive-api.open-meteo.com/v1/archive?"
            f"latitude={self.lat}&longitude={self.lon}"
            f"&start_date={start_date.isoformat()}&end_date={end_date.isoformat()}"
            f"&daily=temperature_2m_mean,sunshine_duration"
            f"&timezone=UTC"
        )
        
        async with aiohttp.ClientSession() as session:
            async with session.get(openmeteo_url) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to get Open-Meteo historical data: HTTP {resp.status}")
                    raise Exception(f"Failed to get Open-Meteo historical data: {resp.status}")
                data = await resp.json()
        
        # Extract daily data and create DataFrame
        dates = data["daily"]["time"]
        temperatures = data["daily"]["temperature_2m_mean"]
        sunshine_durations = data["daily"]["sunshine_duration"]
        
        logger.debug(f"Received {len(dates)} daily historical data points")
        # Convert to DataFrame
        df = pd.DataFrame({
            'date': pd.to_datetime(dates).date,
            'temperature': temperatures,
            'sunshine_duration': sunshine_durations
        })
        
        logger.info(f"Successfully created DataFrame with {len(df)} daily rows")
        return df