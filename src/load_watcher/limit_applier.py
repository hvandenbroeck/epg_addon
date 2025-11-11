import logging
from tinydb import Query
from ..devices_config import device_actions
from ..config import CONFIG
from ..utils import evaluate_expression

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
                
                # Get three_phase_threshold_power from config (default to 4000 if not set)
                three_phase_threshold = CONFIG.get('options', {}).get('three_phase_threshold_power', 4000)
                
                # Calculate three_phase and single_phase values
                three_phase = 1 if limit_watts > three_phase_threshold else 0
                single_phase = 1 if limit_watts <= three_phase_threshold else 0
                
                # Context for expression evaluation
                context = {
                    'limit_watts': limit_watts,
                    'limit_amps': limit_amps,
                    'three_phase': three_phase,
                    'single_phase': single_phase
                }
                
                # Execute the actions using Devices.execute_device_action
                logger.info(f"  🎯 {device_name}: Applying limit of {limit_watts:.0f}W ({limit_amps:.1f}A)")
                await devices_instance.execute_device_action(
                    device=device_name,
                    actions=apply_actions,
                    action_label=f"limit_{int(limit_watts)}W",
                    context=context
                )
            
            logger.info("✅ Device limits applied successfully")
            
        except Exception as e:
            logger.error(f"❌ Error applying device limits: {e}", exc_info=True)
