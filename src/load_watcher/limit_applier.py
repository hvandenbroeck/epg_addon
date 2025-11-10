import logging
from tinydb import Query
from ..devices_config import device_actions

logger = logging.getLogger(__name__)


class LimitApplier:
    """Applies calculated load limits to devices."""
    
    def __init__(self, db):
        """Initialize the limit applier.
        
        Args:
            db: TinyDB database instance
        """
        self.db = db
    
    async def apply_device_limits(self, devices_instance):
        """Apply calculated load limits to devices using configured actions.
        
        Args:
            devices_instance: Instance of Devices class to execute actions
        """
        try:
            # Load the latest device limitations from database
            query = Query()
            limitations_data = self.db.search(query.id == 'device_limitations')
            
            if not limitations_data:
                logger.info("  No device limitations found in database to apply")
                return
            
            limitations = limitations_data[0]
            device_limits = limitations.get('limits', {})
            
            if not device_limits:
                logger.info("  No device limits to apply")
                return
            
            logger.info(f"🔧 Applying device limits for {len(device_limits)} device(s)")
            
            # Iterate through each device that has a calculated limit
            for device_name, limit_data in device_limits.items():
                # Get device configuration
                device_config = device_actions.get(device_name)
                if not device_config:
                    logger.warning(f"  ⚠️ Device '{device_name}' not found in device_actions")
                    continue
                
                # Check if device has apply_limit_actions configured under load_management
                load_mgmt = device_config.get('load_management', {})
                apply_actions = load_mgmt.get('apply_limit_actions')
                if not apply_actions:
                    logger.info(f"  ⏭️ {device_name}: No apply_limit_actions configured, skipping")
                    continue
                
                # Prepare action data with limit values
                limit_watts = limit_data.get('limit_watts', 0)
                limit_amps = limit_watts / 230  # Convert watts to amps (assuming 230V)
                
                # Create a copy of actions with substituted values
                processed_actions = self._process_actions(apply_actions, limit_watts, limit_amps)
                
                # Execute the actions using Devices.execute_device_action
                logger.info(f"  🎯 {device_name}: Applying limit of {limit_watts:.0f}W ({limit_amps:.1f}A)")
                await devices_instance.execute_device_action(
                    device=device_name,
                    actions=processed_actions,
                    action_label=f"limit_{int(limit_watts)}W"
                )
            
            logger.info("✅ Device limits applied successfully")
            
        except Exception as e:
            logger.error(f"❌ Error applying device limits: {e}", exc_info=True)
    
    def _process_actions(self, apply_actions, limit_watts, limit_amps):
        """Process actions by substituting placeholder values.
        
        Args:
            apply_actions: Dictionary containing entity and mqtt actions
            limit_watts: Limit value in watts
            limit_amps: Limit value in amps
            
        Returns:
            dict: Processed actions with substituted values
        """
        processed_actions = {}
        
        # Process entity actions
        if 'entity' in apply_actions:
            processed_actions['entity'] = []
            for action in apply_actions['entity']:
                processed_action = action.copy()
                # Substitute placeholders in value field
                if 'value' in processed_action:
                    value_str = str(processed_action['value'])
                    value_str = value_str.replace('{limit_watts}', str(int(limit_watts)))
                    value_str = value_str.replace('{limit_amps}', str(int(limit_amps)))
                    # Try to convert back to number if possible
                    try:
                        processed_action['value'] = float(value_str) if '.' in value_str else int(value_str)
                    except ValueError:
                        processed_action['value'] = value_str
                processed_actions['entity'].append(processed_action)
        
        # Process MQTT actions
        if 'mqtt' in apply_actions:
            processed_actions['mqtt'] = []
            for action in apply_actions['mqtt']:
                processed_action = action.copy()
                # Substitute placeholders in payload
                if 'payload' in processed_action:
                    payload_str = str(processed_action['payload'])
                    payload_str = payload_str.replace('{limit_watts}', str(int(limit_watts)))
                    payload_str = payload_str.replace('{limit_amps}', str(int(limit_amps)))
                    processed_action['payload'] = payload_str
                processed_actions['mqtt'].append(processed_action)
        
        return processed_actions
