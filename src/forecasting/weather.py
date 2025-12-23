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

    async def getUpcomingHourlyWeather(self):
        """
        Returns a DataFrame with weather forecast for remaining hours of today and all of tomorrow.
        Columns: 'timestamp' (datetime), 'hour' (int), 'temperature' (Celsius), 'cloud_cover' (%), 'date'
        """
        logger.info("Fetching upcoming hourly weather forecast (today remaining + tomorrow)")
        # Ensure location is fetched
        await self._fetch_location()

        # Get date range: today through tomorrow
        now = datetime.now(timezone.utc)
        today = now.date()
        current_hour = now.hour
        tomorrow = today + timedelta(days=1)
        start_date = today.isoformat()
        end_date = tomorrow.isoformat()
        
        logger.debug(f"Querying Open-Meteo hourly forecast from {start_date} to {end_date}, filtering from hour {current_hour}")
        openmeteo_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={self.lat}&longitude={self.lon}"
            f"&hourly=temperature_2m,cloud_cover"
            f"&start_date={start_date}&end_date={end_date}"
            f"&timezone=auto"
        )
        
        async with aiohttp.ClientSession() as session:
            async with session.get(openmeteo_url) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to get Open-Meteo hourly forecast: HTTP {resp.status}")
                    raise Exception(f"Failed to get Open-Meteo hourly forecast: {resp.status}")
                forecast = await resp.json()

        # Extract hourly data
        times = forecast["hourly"]["time"]
        temperatures = forecast["hourly"]["temperature_2m"]
        cloud_cover = forecast["hourly"]["cloud_cover"]
        
        df = pd.DataFrame({
            'timestamp': pd.to_datetime(times),
            'temperature': temperatures,
            'cloud_cover': cloud_cover
        })
        df['hour'] = df['timestamp'].dt.hour
        df['date'] = df['timestamp'].dt.date
        
        # Filter: keep remaining hours of today (current hour onwards) + all of tomorrow
        df = df[
            ((df['date'] == today) & (df['hour'] >= current_hour)) |
            (df['date'] == tomorrow)
        ].reset_index(drop=True)
        
        logger.info(f"Successfully fetched {len(df)} hours of upcoming weather forecast")
        return df

    async def getHistoricalHourlyWeather(self, days_back=365):
        """
        Returns a DataFrame with historical hourly weather data.
        Columns: 'timestamp', 'hour', 'date', 'temperature' (Celsius), 'cloud_cover' (%)
        
        Args:
            days_back: Number of days of historical data to retrieve
        """
        logger.info(f"Fetching historical hourly weather data for the last {days_back} days")
        # Ensure location is fetched
        await self._fetch_location()
        
        # Calculate date range
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=days_back)
        
        logger.debug(f"Querying Open-Meteo archive from {start_date} to {end_date} (hourly)")
        # Query Open-Meteo historical API for hourly weather
        openmeteo_url = (
            f"https://archive-api.open-meteo.com/v1/archive?"
            f"latitude={self.lat}&longitude={self.lon}"
            f"&start_date={start_date.isoformat()}&end_date={end_date.isoformat()}"
            f"&hourly=temperature_2m,cloud_cover"
            f"&timezone=auto"
        )

        logger.info(f"Openmeteo url: {openmeteo_url}")

        async with aiohttp.ClientSession() as session:
            async with session.get(openmeteo_url) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to get Open-Meteo historical hourly data: HTTP {resp.status}")
                    raise Exception(f"Failed to get Open-Meteo historical hourly data: {resp.status}")
                data = await resp.json()
        
        # Extract hourly data and create DataFrame
        times = data["hourly"]["time"]
        temperatures = data["hourly"]["temperature_2m"]
        cloud_cover = data["hourly"]["cloud_cover"]
        
        logger.debug(f"Received {len(times)} hourly historical data points")
        
        # Convert to DataFrame
        df = pd.DataFrame({
            'timestamp': pd.to_datetime(times),
            'temperature': temperatures,
            'cloud_cover': cloud_cover
        })
        df['hour'] = df['timestamp'].dt.hour
        df['date'] = df['timestamp'].dt.date
        df['dayofweek'] = df['timestamp'].dt.dayofweek
        
        logger.info(f"Successfully created DataFrame with {len(df)} hourly rows")
        return df