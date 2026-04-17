"""Home Assistant API Client.

Provides a clean interface for Home Assistant REST API interactions.
This module centralizes all Home Assistant API calls for better testability
and separation of concerns.
"""
import logging
import aiohttp
from datetime import datetime, timedelta, timezone
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

    async def get_sensor_history_avg(self, entity_id: str, hours_back: int = 48) -> float | None:
        """Return the time-weighted average state of a numeric sensor over the last N hours.

        Uses the Home Assistant history API to fetch all state changes in the window
        and computes a simple mean of all recorded float values.

        Args:
            entity_id: The sensor entity ID (e.g., 'sensor.outside_temperature')
            hours_back: How many hours of history to include (default 48)

        Returns:
            Average value as a float, or None if no valid readings were found
        """
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=hours_back)
        url = (
            f"{self.ha_url}/api/history/period/{start_time.isoformat()}"
            f"?filter_entity_id={entity_id}"
        )

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch history for {entity_id}: HTTP {response.status}")
                    return None
                data = await response.json()

        if not data or not data[0]:
            logger.warning(f"No history returned for {entity_id}")
            return None

        values = []
        for state_change in data[0]:
            state = state_change.get('state')
            if state and state not in ('unknown', 'unavailable', 'None'):
                try:
                    values.append(float(state))
                except (ValueError, TypeError):
                    continue

        if not values:
            logger.warning(f"No valid numeric readings in history for {entity_id}")
            return None

        avg = sum(values) / len(values)
        logger.debug(f"Average of {entity_id} over last {hours_back}h: {avg:.2f} ({len(values)} samples)")
        return avg
