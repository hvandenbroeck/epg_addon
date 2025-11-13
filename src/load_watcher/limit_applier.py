import logging
from datetime import datetime, timedelta
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
            
            # Get phase switching config
            phase_switch_threshold = CONFIG.get('options', {}).get('phase_switch_threshold_power', 4000)
            phase_switch_delay_minutes = CONFIG.get('options', {}).get('phase_switch_delay_minutes', 5)
            
            current_time = datetime.now()
            
            # Track if we need to update the database
            limits_updated = False
            
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
                
                # Calculate three_phase and single_phase values
                three_phase = 1 if limit_watts > phase_switch_threshold else 0
                single_phase = 1 if limit_watts <= phase_switch_threshold else 0
                
                # Context for expression evaluation
                context = {
                    'limit_watts': limit_watts,
                    'limit_amps': limit_amps,
                    'three_phase': three_phase,
                    'single_phase': single_phase
                }
                
                # Handle automated phase switching if enabled
                automated_phase_switching = load_mgmt.get('automated_phase_switching', False)
                
                if automated_phase_switching:
                    # Get current phase state from limit_data
                    current_phase = limit_data.get('current_phase', 'three')
                    last_switch_to_single = limit_data.get('last_switch_to_single_timestamp')
                    
                    # Determine target phase based on limit
                    target_phase = 'single' if limit_watts < phase_switch_threshold else 'three'
                    
                    # Check if we need to switch phases
                    if target_phase != current_phase:
                        can_switch = True
                        
                        # If switching from single to three, check delay
                        if current_phase == 'single' and target_phase == 'three':
                            if last_switch_to_single:
                                last_switch_time = datetime.fromisoformat(last_switch_to_single)
                                time_since_switch = (current_time - last_switch_time).total_seconds() / 60
                                
                                if time_since_switch < phase_switch_delay_minutes:
                                    can_switch = False
                                    logger.info(f"  ⏳ {device_name}: Delay active - {time_since_switch:.1f}/{phase_switch_delay_minutes} min since switch to single phase")
                        
                        if can_switch:
                            # Execute phase switch action
                            if target_phase == 'single':
                                switch_actions = apply_actions.get('switch_to_single_phase')
                                if switch_actions:
                                    logger.info(f"  🔄 {device_name}: Switching to SINGLE phase (limit: {limit_watts:.0f}W < {phase_switch_threshold}W)")
                                    await devices_instance.execute_device_action(
                                        device=device_name,
                                        actions=switch_actions,
                                        action_label="switch_to_single_phase",
                                        context=context
                                    )
                                    # Update phase state in limit_data
                                    limit_data['current_phase'] = 'single'
                                    limit_data['last_switch_to_single_timestamp'] = current_time.isoformat()
                                    limits_updated = True
                            else:
                                switch_actions = apply_actions.get('switch_to_three_phase')
                                if switch_actions:
                                    logger.info(f"  🔄 {device_name}: Switching to THREE phase (limit: {limit_watts:.0f}W >= {phase_switch_threshold}W)")
                                    await devices_instance.execute_device_action(
                                        device=device_name,
                                        actions=switch_actions,
                                        action_label="switch_to_three_phase",
                                        context=context
                                    )
                                    # Update phase state in limit_data
                                    limit_data['current_phase'] = 'three'
                                    limits_updated = True
                
                # Apply the limit itself
                # Handle both new structure (with apply_limit subaction) and old structure (direct actions)
                limit_actions = apply_actions.get('apply_limit', apply_actions)
                
                # Execute the actions using Devices.execute_device_action
                logger.info(f"  🎯 {device_name}: Applying limit of {limit_watts:.0f}W ({limit_amps:.1f}A)")
                await devices_instance.execute_device_action(
                    device=device_name,
                    actions=limit_actions,
                    action_label=f"limit_{int(limit_watts)}W",
                    context=context
                )
            
            # Update database if phase states changed
            if limits_updated:
                self.db.upsert({
                    'id': 'device_limitations',
                    'limits': device_limits
                }, query.id == 'device_limitations')
            
            logger.info("✅ Device limits applied successfully")
            
        except Exception as e:
            logger.error(f"❌ Error applying device limits: {e}", exc_info=True)
