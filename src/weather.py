import aiohttp
from datetime import datetime, timedelta, timezone
from .config import CONFIG

class Weather:
    """
    Fetches weather data using Home Assistant location and met.no API.
    """
    def __init__(self, access_token, user_agent="EPGAddon/1.0 github.com/hvandenbroeck"):
        self.ha_url = CONFIG['options']['ha_url']
        self.access_token = access_token
        self.user_agent = user_agent  # Required by met.no API

    async def getTomorrowsTemperature(self):
        """
        Returns a sorted list of tomorrow's hourly temperatures (in Celsius) from met.no.
        """
        # Step 1: Get location from Home Assistant
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.ha_url}/api/config", headers=headers) as resp:
                if resp.status != 200:
                    raise Exception(f"Failed to get HA config: {resp.status}")
                config = await resp.json()
        lat = config.get("latitude")
        lon = config.get("longitude")
        if lat is None or lon is None:
            raise Exception("Latitude or longitude not found in HA config")

        # Step 2: Query met.no for hourly forecast
        met_url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat}&lon={lon}"
        met_headers = {"User-Agent": self.user_agent}
        async with aiohttp.ClientSession() as session:
            async with session.get(met_url, headers=met_headers) as resp:
                if resp.status != 200:
                    raise Exception(f"Failed to get met.no forecast: {resp.status}")
                forecast = await resp.json()

        # Step 3: Extract tomorrow's hourly temperatures
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
        temps = []
        for entry in forecast["properties"]["timeseries"]:
            dt = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
            if dt.date() == tomorrow and "air_temperature" in entry["data"]["instant"]["details"]:
                temps.append((dt.hour, entry["data"]["instant"]["details"]["air_temperature"]))

        # Sort by hour and return only temperatures
        temps_sorted = [temp for hour, temp in sorted(temps)]
        return temps_sorted