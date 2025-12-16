import logging
import aiohttp
from .devices_config import device_actions
from .utils import ensure_list, evaluate_expression
from .config import CONFIG

logger = logging.getLogger(__name__)


class Devices:
    """Manages device actions and execution."""

    # Class-level reference to verifier (set externally)
    _verifier = None

    def __init__(self, access_token):
        self.ha_url = CONFIG['options']['ha_url']
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self.device_actions = device_actions

    @classmethod
    def set_verifier(cls, verifier):
        """Set the device verifier instance for action tracking.
        
        Args:
            verifier: DeviceVerifier instance
        """
        cls._verifier = verifier

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

    async def execute_device_action(self, device, actions, action_label, scheduled_time=None, context=None, skip_verification=False):
        """Execute MQTT and entity actions for a device.
        
        Args:
            device: Device identifier (e.g., 'wp', 'hw', 'bat')
            actions: Dictionary containing mqtt and entity actions
            action_label: Label for the action ('start' or 'stop')
            scheduled_time: Optional datetime when action was scheduled
            context: Optional dictionary for expression evaluation (e.g., {'limit_watts': 3500})
            skip_verification: If True, don't register for post-action verification (used by retry logic)
        """
        time_info = f" at {scheduled_time}" if scheduled_time else ""
        logger.info(f"🔄 Executing {device.upper()} {action_label.upper()}{time_info}")
        
        # Default context if not provided
        if context is None:
            context = {}
        
        # Handle MQTT actions
        mqtt_actions = ensure_list(actions.get("mqtt", []))
        for msg in mqtt_actions:
            topic = msg.get("topic")
            payload = msg.get("payload")
            if topic and payload is not None:
                # Evaluate expressions in payload
                evaluated_payload = evaluate_expression(payload, context)
                await self.call_service("mqtt/publish", topic=topic, payload=evaluated_payload)
                logger.info(f"📡 MQTT {action_label.upper()} for {device}: {topic} → {evaluated_payload}")

        # Handle entity actions
        entity_actions = ensure_list(actions.get("entity", []))
        for ent in entity_actions:
            service = ent.get("service")
            if service:
                service_data = {}
                for k, v in ent.items():
                    if k not in ["service", "state"]:
                        # Evaluate expressions in value and option fields
                        if k in ["value", "option"]:
                            service_data[k] = evaluate_expression(v, context)
                        else:
                            service_data[k] = v
                try:
                    await self.call_service(service, **service_data)
                    logger.info(f"🏠 Entity service call: {service}({service_data})")
                except Exception as e:
                    logger.error(f"❌ Failed to call service {service}: {e}")

        # Register action with verifier for post-action verification
        if self._verifier and action_label in ("start", "stop") and not skip_verification:
            self._verifier.register_action(device, action_label, context)

    def get_device_config(self, device):
        """Get configuration for a specific device.
        
        Args:
            device: Device identifier
            
        Returns:
            dict: Device configuration or None if not found
        """
        return self.device_actions.get(device)
