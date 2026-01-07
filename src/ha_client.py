"""Home Assistant API Client.

Provides a clean interface for Home Assistant REST API interactions.
This module centralizes all Home Assistant API calls for better testability
and separation of concerns.
"""
import logging
import aiohttp
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
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

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
