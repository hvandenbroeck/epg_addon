import logging
from datetime import datetime
from tinydb import Query
from ..devices_config import device_actions

logger = logging.getLogger(__name__)


class LimitCalculator:
    """Calculates load limits for devices based on available power."""
    
    def __init__(self, db, load_watcher_threshold_power, get_state_func):
        """Initialize the limit calculator.
        
        Args:
            db: TinyDB database instance
            load_watcher_threshold_power: Threshold power to consider device charging (W)
            get_state_func: Async function to get entity state from Home Assistant
        """
        self.db = db
        self.load_watcher_threshold_power = load_watcher_threshold_power
        self.get_state = get_state_func
    
    async def calculate_limits(self, available_power_watts):
        """Calculate and set load limits for devices based on available power.
        
        Args:
            available_power_watts: Available power in watts (positive = surplus, negative = excess usage)
        """
        try:
            # Check if absolute value is bigger than 100W
            if abs(available_power_watts) <= 100:
                logger.info(f"⚡ Available power ({available_power_watts:.0f}W) is within 100W threshold, skipping limit adjustments")
                return
            
            logger.info(f"⚡ Calculating load limits with available power: {available_power_watts:.0f}W")
            logger.debug(f"  Threshold power: {self.load_watcher_threshold_power}W")
            
            # Load previous limits from database
            query = Query()
            previous_limits_doc = self.db.get(query.id == 'device_limitations')
            previous_limits = previous_limits_doc.get('limits', {}) if previous_limits_doc else {}
            
            # Initialize limits dictionary with all load-managed devices
            device_limits = {}
            for device_name, device_config in device_actions.items():
                if not device_config.get('enable_load_management', False):
                    continue
                    
                load_mgmt = device_config.get('load_management', {})
                max_watts = float(load_mgmt.get('load_maximum_watts', 0))
                priority = load_mgmt.get('load_priority', 999)
                
                logger.info(f"  📋 Loaded device: {device_name} (priority: {priority})")
                
                if device_name in previous_limits:
                    # Keep previous limit
                    device_limits[device_name] = previous_limits[device_name].copy()
                    device_limits[device_name]['state'] = 'Previous limit maintained'
                    logger.debug(f"  🔄 {device_name}: Initialized with previous limit ({device_limits[device_name]['limit_watts']:.0f}W)")
                else:
                    # Use default (max_watts)
                    device_limits[device_name] = {
                        'limit_watts': max_watts,
                        'current_load_watts': 0,
                        'max_watts': max_watts,
                        'limiter_entity': load_mgmt.get('load_limiter_entity'),
                        'priority': priority,
                        'load_entity': load_mgmt.get('instantaneous_load_entity'),
                        'load_unit': load_mgmt.get('instantaneous_load_entity_unit', 'W'),
                        'charge_sign': load_mgmt.get('charge_sign', 'positive'),
                        'state': 'Default limit'
                    }
                    logger.debug(f"  🆕 {device_name}: Initialized with default limit ({max_watts:.0f}W)")

            if not device_limits:
                logger.info("  No devices with load management enabled")
                return
            
            if available_power_watts > 0:
                await self._calculate_limits_power_available(device_limits, available_power_watts)
            else:
                await self._calculate_limits_power_excess(device_limits, available_power_watts)
            
            # Save limits to database
            query = Query()
            self.db.upsert({
                'id': 'device_limitations',
                'timestamp': datetime.now().isoformat(),
                'available_power_watts': available_power_watts,
                'limits': device_limits
            }, query.id == 'device_limitations')
            
            logger.info(f"  💾 Saved device limitations to database ({len(device_limits)} devices)")
            
        except Exception as e:
            logger.error(f"❌ Error calculating limits: {e}", exc_info=True)
    
    async def _calculate_limits_power_available(self, device_limits, available_power_watts):
        """Calculate limits when power is available (increase limits).
        
        Args:
            device_limits: Dictionary of all load-managed devices with their limits
            available_power_watts: Available power in watts (positive)
        """
        logger.info("  📈 Power available - increasing limits for high priority devices")
        sorted_devices = sorted(device_limits.items(), key=lambda x: x[1]['priority'])
        logger.debug(f"    Processing {len(sorted_devices)} devices in priority order: {[name for name, _ in sorted_devices]}")
        remaining_power = available_power_watts
        
        for device_name, device in sorted_devices:
            # Get current device load
            state = await self.get_state(device['load_entity'])
            if not state or state.get('state') in ['unavailable', 'unknown', None]:
                logger.warning(f"    ⚠️ Could not get state for {device_name} ({device['load_entity']})")
                continue
            
            try:
                current_load = float(state['state'])
            except (ValueError, TypeError):
                logger.warning(f"    ⚠️ Invalid load value for {device_name}: {state.get('state')}")
                continue
            
            # Check if device is charging based on charge_sign
            is_charging, charging_power = self._check_device_charging(
                device_name, device, current_load
            )
            
            # If device is not charging, set limit to max and skip power allocation
            if not is_charging:
                logger.info(f"    ⏭️ {device_name} (P{device['priority']}): Not charging ({state['state']}{device['load_unit']}), setting to max")
                device_limits[device_name].update({
                    'limit_watts': device['max_watts'],
                    'current_load_watts': charging_power,
                    'state': 'Not charging'
                })
                continue
            
            # Calculate new limit
            calculated_limit = charging_power + remaining_power
            logger.debug(f"    [available calc] {device_name}: charging={charging_power:.0f}W, remaining={remaining_power:.0f}W, calculated_limit={calculated_limit:.0f}W, max={device['max_watts']:.0f}W")
            
            # Cap at maximum
            if calculated_limit > device['max_watts']:
                new_limit = device['max_watts']
                power_used = device['max_watts'] - charging_power
                remaining_power -= power_used
                device_state = 'Limit: device limit'
                logger.debug(f"    [available decision] {device_name}: limit capped at max ({new_limit:.0f}W), used {power_used:.0f}W, remaining {remaining_power:.0f}W")
            else:
                new_limit = calculated_limit
                remaining_power = 0
                device_state = 'Limit: available power'
                logger.debug(f"    [available decision] {device_name}: set to {new_limit:.0f}W, consumed all remaining power")
            
            device_limits[device_name].update({
                'limit_watts': new_limit,
                'current_load_watts': charging_power,
                'state': device_state
            })
            
            logger.info(f"    ✓ {device_name} (P{device['priority']}): {charging_power:.0f}W → {new_limit:.0f}W (max: {device['max_watts']:.0f}W) [{device_state}]")
            
            if remaining_power <= 0:
                logger.info(f"    ℹ️ No remaining power, keeping previous/default limits for unprocessed devices")
                break
        
        if remaining_power > 0:
            logger.info(f"    💡 Still {remaining_power:.0f}W available after adjusting all devices")
    
    async def _calculate_limits_power_excess(self, device_limits, available_power_watts):
        """Calculate limits when power is in excess (decrease limits).
        
        Args:
            device_limits: Dictionary of all load-managed devices with their limits
            available_power_watts: Available power in watts (negative)
        """
        logger.info("  📉 Power excess - decreasing limits for low priority devices")
        sorted_devices = sorted(device_limits.items(), key=lambda x: x[1]['priority'], reverse=True)
        logger.debug(f"    Processing {len(sorted_devices)} devices in reverse priority order: {[name for name, _ in sorted_devices]}")
        excess_power = abs(available_power_watts)
        
        for device_name, device in sorted_devices:
            # Get current device load
            state = await self.get_state(device['load_entity'])
            if not state or state.get('state') in ['unavailable', 'unknown', None]:
                logger.warning(f"    ⚠️ Could not get state for {device_name} ({device['load_entity']})")
                continue
            
            try:
                current_load = float(state['state'])
            except (ValueError, TypeError):
                logger.warning(f"    ⚠️ Invalid load value for {device_name}: {state.get('state')}")
                continue
            
            # Check if device is charging based on charge_sign
            is_charging, charging_power = self._check_device_charging(
                device_name, device, current_load
            )
            
            # If device is not charging, set limit to max and skip power allocation
            if (not is_charging or charging_power <= 0):
                logger.info(f"    ⏭️ {device_name} (P{device['priority']}): Not charging ({state['state']}{device['load_unit']}), setting to max")
                device_limits[device_name].update({
                    'limit_watts': device['max_watts'],
                    'current_load_watts': charging_power,
                    'state': 'Not charging'
                })
                continue
            
            # Calculate new limit
            calculated_limit = charging_power - excess_power
            logger.debug(f"    [excess calc] {device_name}: charging={charging_power:.0f}W, excess={excess_power:.0f}W, calculated_limit={calculated_limit:.0f}W")
            
            # Check if limit goes negative
            if calculated_limit < 0:
                new_limit = 0
                power_freed = charging_power
                excess_power -= power_freed
                device_state = 'Limit: available power'
                logger.debug(f"    [excess decision] {device_name}: limit would be negative, clamping to 0, freed {power_freed:.0f}W, remaining excess {excess_power:.0f}W")
            else:
                new_limit = calculated_limit
                excess_power = 0
                device_state = 'Limit: available power'
                logger.debug(f"    [excess decision] {device_name}: set to {new_limit:.0f}W, no excess remaining")
            
            device_limits[device_name].update({
                'limit_watts': new_limit,
                'current_load_watts': charging_power,
                'state': device_state
            })
            
            logger.info(f"    ✓ {device_name} (P{device['priority']}): {charging_power:.0f}W → {new_limit:.0f}W [{device_state}]")
            
            if excess_power <= 0:
                logger.info(f"    ℹ️ No excess remaining, keeping previous/default limits for unprocessed devices")
                break
        
        if excess_power > 0:
            logger.info(f"    ⚠️ Still {excess_power:.0f}W excess after adjusting all devices to 0")
    
    def _check_device_charging(self, device_name, device, current_load):
        """Check if a device is currently charging.
        
        Args:
            device_name: Name of the device
            device: Device configuration dictionary
            current_load: Current load reading from sensor
            
        Returns:
            tuple: (is_charging, charging_power)
        """
        is_charging = False
        charging_power = 0
        
        if device['charge_sign'] == 'positive':
            # Positive values mean charging
            charging_power = current_load
            is_charging = charging_power > self.load_watcher_threshold_power
            logger.debug(f"      [charge_sign=positive] {device_name}: load={current_load:.0f}W, threshold={self.load_watcher_threshold_power}W, charging={is_charging}")
        elif device['charge_sign'] == 'negative':
            # Negative values mean charging
            charging_power = abs(current_load)
            is_charging = current_load < -self.load_watcher_threshold_power
            logger.debug(f"      [charge_sign=negative] {device_name}: load={current_load:.0f}W, threshold={-self.load_watcher_threshold_power}W, charging={is_charging}")
        
        return is_charging, charging_power
