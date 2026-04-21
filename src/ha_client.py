"""Home Assistant API Client.

Provides a clean interface for Home Assistant REST API interactions.
This module centralizes all Home Assistant API calls for better testability
and separation of concerns.
"""
import logging
import aiohttp
from datetime import datetime, timedelta, timezone
from typing import Optional
from .config import CONFIG

logger = logging.getLogger(__name__)


class HomeAssistantClient:
    """Async client for Home Assistant REST API."""

    def __init__(self, access_token: str):
        """Initialize the Home Assistant client.
        
        Args:
            access_token: Home Assistant Long-Lived Access Token
        """
        self.ha_url = CONFIG['options']['ha_url']
        self._access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def get_access_token(self) -> str:
        """Get the access token for external use.
        
        This method provides the access token for components that need
        to create their own API connections (e.g., StatisticsLoader, Weather).
        
        Returns:
            The Home Assistant access token
        """
        return self._access_token

    async def get_state(self, entity_id: str) -> dict | None:
        """Get the state of an entity from Home Assistant.
        
        Args:
            entity_id: The entity ID to query (e.g., 'sensor.battery_soc')
            
        Returns:
            Entity state dict or None if not found/error
        """
        url = f"{self.ha_url}/api/states/{entity_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                return None

    async def call_service(self, service: str, **service_data) -> bool:
        """Call a Home Assistant service.
        
        Args:
            service: Service in format 'domain/service_name' (e.g., 'mqtt/publish')
            **service_data: Service parameters
            
        Returns:
            True if service call was successful, False otherwise
        """
        domain, service_name = service.split('/')
        url = f"{self.ha_url}/api/services/{domain}/{service_name}"
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=service_data) as response:
                return response.status == 200

    async def get_avg_temperature_48h(self, entity_id: str) -> Optional[float]:
        """Fetch the 48-hour average value of a temperature sensor from HA history.

        Args:
            entity_id: The sensor entity ID to query (e.g., 'sensor.outside_temp')

        Returns:
            Average temperature over the last 48 hours as a float, or None on error.
        """
        start_time = datetime.now(timezone.utc) - timedelta(hours=48)
        # Use strftime to produce a clean UTC timestamp without the '+00:00' suffix,
        # which would be misinterpreted as a space when embedded in a URL.
        start_str = start_time.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        url = (
            f"{self.ha_url}/api/history/period/{start_str}"
            f"?filter_entity_id={entity_id}"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as response:
                    if response.status != 200:
                        logger.error(f"Failed to fetch 48h history for {entity_id}: HTTP {response.status}")
                        return None
                    data = await response.json()

            if not data or not data[0]:
                logger.warning(f"No history data returned for {entity_id}")
                return None

            values = []
            for state_change in data[0]:
                raw = state_change.get('state')
                if raw and raw not in ('unknown', 'unavailable', 'None'):
                    try:
                        values.append(float(raw))
                    except (ValueError, TypeError):
                        pass

            if not values:
                logger.warning(f"No valid numeric states in 48h history for {entity_id}")
                return None

            avg = sum(values) / len(values)
            logger.debug(f"48h average temperature for {entity_id}: {avg:.2f}°C ({len(values)} samples)")
            return avg

        except Exception as e:
            logger.error(f"Error fetching 48h average temperature for {entity_id}: {e}")
            return None
