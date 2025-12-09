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
            
            # Load all devices with load_management enabled
            load_managed_devices = []
            for device_name, device_config in device_actions.items():
                if device_config.get('enable_load_management', False):
                    load_mgmt = device_config.get('load_management', {})
                    load_managed_devices.append({
                        'name': device_name,
                        'priority': load_mgmt.get('load_priority', 999),
                        'load_entity': load_mgmt.get('instantaneous_load_entity'),
                        'load_unit': load_mgmt.get('instantaneous_load_entity_unit', 'W'),
                        'limiter_entity': load_mgmt.get('load_limiter_entity'),
                        'max_watts': float(load_mgmt.get('load_maximum_watts', 0)),
                        'charge_sign': load_mgmt.get('charge_sign', 'positive')
                    })
                    logger.info(f"  📋 Loaded device: {device_name} (priority: {load_mgmt.get('load_priority', 999)})")

            if not load_managed_devices:
                logger.info("  No devices with load management enabled")
                return
            
            # Initialize limits dictionary
            device_limits = {}
            
            if available_power_watts > 0:
                device_limits = await self._calculate_limits_power_available(
                    load_managed_devices, available_power_watts
                )
            else:
                device_limits = await self._calculate_limits_power_excess(
                    load_managed_devices, available_power_watts
                )
            
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
    
    async def _calculate_limits_power_available(self, load_managed_devices, available_power_watts):
        """Calculate limits when power is available (increase limits).
        
        Args:
            load_managed_devices: List of devices with load management enabled
            available_power_watts: Available power in watts (positive)
            
        Returns:
            dict: Device limits dictionary
        """
        logger.info("  📈 Power available - increasing limits for high priority devices")
        sorted_devices = sorted(load_managed_devices, key=lambda x: x['priority'])
        logger.debug(f"    Processing {len(sorted_devices)} devices in priority order: {[d['name'] for d in sorted_devices]}")
        remaining_power = available_power_watts
        device_limits = {}
        
        for device in sorted_devices:
            # Get current device load
            state = await self.get_state(device['load_entity'])
            if not state or state.get('state') in ['unavailable', 'unknown', None]:
                logger.warning(f"    ⚠️ Could not get state for {device['name']} ({device['load_entity']})")
                continue
            
            try:
                current_load = float(state['state'])
            except (ValueError, TypeError):
                logger.warning(f"    ⚠️ Invalid load value for {device['name']}: {state.get('state')}")
                continue
            
            # Check if device is charging based on charge_sign
            is_charging, charging_power = self._check_device_charging(
                device, current_load, state
            )
            
            # If device is not charging, set limit to max and skip power allocation
            if not is_charging:
                logger.info(f"    ⏭️ {device['name']} (P{device['priority']}): Not charging ({state['state']}{device['load_unit']}), setting to max")
                device_limits[device['name']] = {
                    'limit_watts': device['max_watts'],
                    'current_load_watts': charging_power,
                    'max_watts': device['max_watts'],
                    'limiter_entity': device['limiter_entity'],
                    'priority': device['priority'],
                    'state': 'Not charging'
                }
                continue
            
            # Calculate new limit
            calculated_limit = charging_power + remaining_power
            logger.debug(f"    [available calc] {device['name']}: charging={charging_power:.0f}W, remaining={remaining_power:.0f}W, calculated_limit={calculated_limit:.0f}W, max={device['max_watts']:.0f}W")
            
            # Cap at maximum
            if calculated_limit > device['max_watts']:
                new_limit = device['max_watts']
                power_used = device['max_watts'] - charging_power
                remaining_power -= power_used
                device_state = 'Limit: device limit'
                logger.debug(f"    [available decision] {device['name']}: limit capped at max ({new_limit:.0f}W), used {power_used:.0f}W, remaining {remaining_power:.0f}W")
            else:
                new_limit = calculated_limit
                remaining_power = 0
                device_state = 'Limit: available power'
                logger.debug(f"    [available decision] {device['name']}: set to {new_limit:.0f}W, consumed all remaining power")
            
            device_limits[device['name']] = {
                'limit_watts': new_limit,
                'current_load_watts': charging_power,
                'max_watts': device['max_watts'],
                'limiter_entity': device['limiter_entity'],
                'priority': device['priority'],
                'state': device_state
            }
            
            logger.info(f"    ✓ {device['name']} (P{device['priority']}): {charging_power:.0f}W → {new_limit:.0f}W (max: {device['max_watts']:.0f}W) [{device_state}]")
            
            if remaining_power <= 0:
                break
        
        if remaining_power > 0:
            logger.info(f"    💡 Still {remaining_power:.0f}W available after adjusting all devices")
        
        return device_limits
    
    async def _calculate_limits_power_excess(self, load_managed_devices, available_power_watts):
        """Calculate limits when power is in excess (decrease limits).
        
        Args:
            load_managed_devices: List of devices with load management enabled
            available_power_watts: Available power in watts (negative)
            
        Returns:
            dict: Device limits dictionary
        """
        logger.info("  📉 Power excess - decreasing limits for low priority devices")
        sorted_devices = sorted(load_managed_devices, key=lambda x: x['priority'], reverse=True)
        logger.debug(f"    Processing {len(sorted_devices)} devices in reverse priority order: {[d['name'] for d in sorted_devices]}")
        excess_power = abs(available_power_watts)
        device_limits = {}
        
        for device in sorted_devices:
            # Get current device load
            state = await self.get_state(device['load_entity'])
            if not state or state.get('state') in ['unavailable', 'unknown', None]:
                logger.warning(f"    ⚠️ Could not get state for {device['name']} ({device['load_entity']})")
                continue
            
            try:
                current_load = float(state['state'])
            except (ValueError, TypeError):
                logger.warning(f"    ⚠️ Invalid load value for {device['name']}: {state.get('state')}")
                continue
            
            # Check if device is charging based on charge_sign
            is_charging, charging_power = self._check_device_charging(
                device, current_load, state
            )
            
            # If device is not charging, set limit to max and skip power allocation
            if (not is_charging or charging_power <= 0):
                logger.info(f"    ⏭️ {device['name']} (P{device['priority']}): Not charging ({state['state']}{device['load_unit']}), setting to max")
                device_limits[device['name']] = {
                    'limit_watts': device['max_watts'],
                    'current_load_watts': charging_power,
                    'max_watts': device['max_watts'],
                    'limiter_entity': device['limiter_entity'],
                    'priority': device['priority'],
                    'state': 'Not charging'
                }
                continue
            
            # Calculate new limit
            calculated_limit = charging_power - excess_power
            logger.debug(f"    [excess calc] {device['name']}: charging={charging_power:.0f}W, excess={excess_power:.0f}W, calculated_limit={calculated_limit:.0f}W")
            
            # Determine state
            device_state = 'Charging below limit'
            
            # Check if limit goes negative
            if calculated_limit < 0:
                new_limit = 0
                power_freed = charging_power
                excess_power -= power_freed
                device_state = 'Limit: available power'
                logger.debug(f"    [excess decision] {device['name']}: limit would be negative, clamping to 0, freed {power_freed:.0f}W, remaining excess {excess_power:.0f}W")
            else:
                new_limit = calculated_limit
                excess_power = 0
                device_state = 'Limit: available power'
                logger.debug(f"    [excess decision] {device['name']}: set to {new_limit:.0f}W, no excess remaining")
            
            device_limits[device['name']] = {
                'limit_watts': new_limit,
                'current_load_watts': charging_power,
                'max_watts': device['max_watts'],
                'limiter_entity': device['limiter_entity'],
                'priority': device['priority'],
                'state': device_state
            }
            
            logger.info(f"    ✓ {device['name']} (P{device['priority']}): {charging_power:.0f}W → {new_limit:.0f}W [{device_state}]")
            
            if excess_power <= 0:
                break
        
        if excess_power > 0:
            logger.info(f"    ⚠️ Still {excess_power:.0f}W excess after adjusting all devices to 0")
        
        return device_limits
    
    def _check_device_charging(self, device, current_load, state):
        """Check if a device is currently charging.
        
        Args:
            device: Device configuration dictionary
            current_load: Current load reading from sensor
            state: Full state object from Home Assistant
            
        Returns:
            tuple: (is_charging, charging_power)
        """
        is_charging = False
        charging_power = 0
        
        if device['charge_sign'] == 'positive':
            # Positive values mean charging
            charging_power = current_load
            is_charging = charging_power > self.load_watcher_threshold_power
            logger.debug(f"      [charge_sign=positive] {device['name']}: load={current_load:.0f}W, threshold={self.load_watcher_threshold_power}W, charging={is_charging}")
        elif device['charge_sign'] == 'negative':
            # Negative values mean charging
            charging_power = abs(current_load)
            is_charging = current_load < -self.load_watcher_threshold_power
            logger.debug(f"      [charge_sign=negative] {device['name']}: load={current_load:.0f}W, threshold={-self.load_watcher_threshold_power}W, charging={is_charging}")
        
        return is_charging, charging_power
