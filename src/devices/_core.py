import logging
import aiohttp
from ..devices_config import devices_config
from ..utils import ensure_list, evaluate_expression
from ..conditions import any_ev_ready
from ..config import CONFIG

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
        self.devices_config = devices_config

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

    def get_device(self, device_name):
        """Get device configuration by name.

        Args:
            device_name: Name of the device

        Returns:
            Device object or None if not found
        """
        return self.devices_config.get_device_by_name(device_name)

    def get_devices_by_type(self, device_type):
        """Get all devices of a specific type.

        Args:
            device_type: Type of device ('wp', 'hw', 'battery', 'ev')

        Returns:
            List of Device objects
        """
        return self.devices_config.get_devices_by_type(device_type)

    async def execute_device_action(self, device_name, actions, action_label, scheduled_time=None, context=None, skip_verification=False):
        """Execute MQTT and entity actions for a device.

        Args:
            device_name: Device name (unique identifier, e.g., 'wp', 'hw', 'ev1', 'ev2')
            actions: Dictionary containing mqtt and entity actions
            action_label: Label for the action ('start' or 'stop')
            scheduled_time: Optional datetime when action was scheduled
            context: Optional dictionary for expression evaluation (e.g., {'limit_watts': 3500})
            skip_verification: If True, don't register for post-action verification (used by retry logic)
        """
        time_info = f" at {scheduled_time}" if scheduled_time else ""
        logger.info(f"🔄 Executing {device_name.upper()} {action_label.upper()}{time_info}")

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
                logger.info(f"📡 MQTT {action_label.upper()} for {device_name}: {topic} → {evaluated_payload}")

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
            self._verifier.register_action(device_name, action_label, context)

    async def get_state(self, entity_id):
        """Get the current Home Assistant state dict for an entity (or None).

        Mirrors ``HomeAssistantClient.get_state`` / ``DeviceVerifier.get_entity_state`` so the
        condition evaluator can read entities from within scheduled jobs held only by ``Devices``.
        """
        url = f"{self.ha_url}/api/states/{entity_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as response:
                    if response.status == 200:
                        return await response.json()
                    logger.warning(f"Failed to get state for {entity_id}: HTTP {response.status}")
                    return None
        except Exception as e:
            logger.error(f"Error getting state for {entity_id}: {e}")
            return None

    async def execute_grid_export_block_start(self, device_name, actions, action_label, scheduled_time=None):
        """Gate the price-based grid-export block at the slot boundary.

        If any EV is ready to charge (its ``grid_export_unblock_condition`` is certainly TRUE),
        skip the block so export stays unblocked and the inverter runs at full production for the
        EV. Otherwise apply the block as scheduled. The periodic verifier keeps reconciling this
        for changes that happen mid-slot.
        """
        ev_devices = self.get_devices_by_type("ev")
        if await any_ev_ready(ev_devices, self.get_state):
            logger.info(
                f"☀️ {device_name}: an EV is ready → overriding price block, leaving grid export UNBLOCKED"
            )
            return
        await self.execute_device_action(device_name, actions, action_label, scheduled_time)

    def get_device_config(self, device_name):
        """Get configuration for a specific device.

        Args:
            device_name: Device name/identifier

        Returns:
            Device: Device object or None if not found
        """
        return self.devices_config.get_device_by_name(device_name)
