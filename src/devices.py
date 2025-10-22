import logging
import aiohttp
from .config import device_actions
from .utils import ensure_list

logger = logging.getLogger(__name__)


class Devices:
    """Manages device actions and execution."""

    def __init__(self, ha_url, access_token):
        """Initialize Devices with Home Assistant connection details.
        
        Args:
            ha_url: Home Assistant base URL
            access_token: Home Assistant authentication token
        """
        self.ha_url = ha_url
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self.device_actions = device_actions

    async def call_service(self, service, **service_data):
        """Call a Home Assistant service.
        
        Args:
            service: Service in format 'domain/service_name'
            **service_data: Service parameters
            
        Returns:
            bool: True if service call was successful
        """
        domain, service_name = service.split('/')
        url = f"{self.ha_url}/api/services/{domain}/{service_name}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=service_data) as response:
                return response.status == 200

    async def execute_device_action(self, device, actions, action_label, scheduled_time=None):
        """Execute MQTT and entity actions for a device.
        
        Args:
            device: Device identifier (e.g., 'wp', 'hw', 'bat')
            actions: Dictionary containing mqtt and entity actions
            action_label: Label for the action ('start' or 'stop')
            scheduled_time: Optional datetime when action was scheduled
        """
        time_info = f" at {scheduled_time}" if scheduled_time else ""
        logger.info(f"🔄 Executing {device.upper()} {action_label.upper()}{time_info}")
        
        # Handle MQTT actions
        mqtt_actions = ensure_list(actions.get("mqtt", []))
        for msg in mqtt_actions:
            topic = msg.get("topic")
            payload = msg.get("payload")
            if topic and payload is not None:
                await self.call_service("mqtt/publish", topic=topic, payload=payload)
                logger.info(f"📡 MQTT {action_label.upper()} for {device}: {topic} → {payload}")

        # Handle entity actions
        entity_actions = ensure_list(actions.get("entity", []))
        for ent in entity_actions:
            service = ent.get("service")
            if service:
                service_data = {k: v for k, v in ent.items() if k not in ["service", "state"]}
                try:
                    await self.call_service(service, **service_data)
                    logger.info(f"🏠 Entity service call: {service}({service_data})")
                except Exception as e:
                    logger.error(f"❌ Failed to call service {service}: {e}")

    def get_device_config(self, device):
        """Get configuration for a specific device.
        
        Args:
            device: Device identifier
            
        Returns:
            dict: Device configuration or None if not found
        """
        return self.device_actions.get(device)
