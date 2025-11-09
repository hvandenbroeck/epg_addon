import logging
import aiohttp
from datetime import datetime, timedelta
from tinydb import TinyDB, Query

from .config import CONFIG
from .devices_config import device_actions

logger = logging.getLogger(__name__)


class LoadWatcher:
    def __init__(self, access_token):
        self.ha_url = CONFIG['options']['ha_url']
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self.energy_entities = CONFIG['options'].get('energy_consumption_entities', [])
        self.max_peak_kw = CONFIG['options'].get('max_peak_kW', 7.5)
        self.peak_calculation_minutes = CONFIG['options'].get('peak_calculation_minutes', 15)
        self.db = TinyDB('db.json')
        
    async def get_state(self, entity_id):
        """Get the state of an entity from Home Assistant."""
        url = f"{self.ha_url}/api/states/{entity_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                return None

    def get_slot_start_time(self, timestamp):
        """Get the start time of the time slot for a given timestamp.
        
        For example, with 15-minute slots:
        - 10:07 -> 10:00
        - 10:22 -> 10:15
        - 10:47 -> 10:45
        """
        minutes = (timestamp.minute // self.peak_calculation_minutes) * self.peak_calculation_minutes
        return timestamp.replace(minute=minutes, second=0, microsecond=0)

    async def run(self):
        """Main load watcher logic - runs every 5 minutes."""
        try:
            logger.info("🔋 Running load watcher...")
            
            # Fetch and sum energy consumption from all configured entities
            total_energy = 0.0
            for entity_id in self.energy_entities:
                state = await self.get_state(entity_id)
                if state and state.get('state') not in ['unavailable', 'unknown', None]:
                    try:
                        energy_value = float(state['state'])
                        total_energy += energy_value
                        logger.info(f"  {entity_id}: {energy_value} kWh")
                    except (ValueError, TypeError) as e:
                        logger.warning(f"  ⚠️ Could not parse state for {entity_id}: {state.get('state')}")
                else:
                    logger.warning(f"  ⚠️ No valid state for {entity_id}")
            
            # Get current timestamp
            current_time = datetime.now()
            
            # Store current reading in historical data
            query = Query()
            historical_data = self.db.search(query.id == 'load_watcher_history')
            
            if historical_data:
                readings = historical_data[0].get('readings', [])
            else:
                readings = []
            
            # Add current reading
            readings.append({
                "timestamp": current_time.isoformat(),
                "total_energy_consumption": total_energy
            })
            
            # Clean up readings older than 1 hour
            one_hour_ago = current_time - timedelta(hours=1)
            readings = [r for r in readings if datetime.fromisoformat(r['timestamp']) > one_hour_ago]
            
            # Save updated historical data
            self.db.upsert({
                "id": "load_watcher_history",
                "readings": readings
            }, query.id == 'load_watcher_history')
            
            logger.info(f"  💾 Stored reading. Historical data: {len(readings)} readings in last hour")
            
            # Calculate current peak over the configured time slot
            current_peak_kw = 0.0
            current_slot_start = self.get_slot_start_time(current_time)
            
            # Find all readings in the current time slot
            slot_readings = [
                r for r in readings 
                if datetime.fromisoformat(r['timestamp']) >= current_slot_start
            ]
            
            if len(slot_readings) >= 2:
                # Get earliest and latest reading in the slot
                slot_readings_sorted = sorted(slot_readings, key=lambda x: x['timestamp'])
                earliest = slot_readings_sorted[0]
                latest = slot_readings_sorted[-1]
                
                earliest_time = datetime.fromisoformat(earliest['timestamp'])
                latest_time = datetime.fromisoformat(latest['timestamp'])
                
                # Calculate time difference in minutes
                time_diff_seconds = (latest_time - earliest_time).total_seconds()
                time_diff_minutes = time_diff_seconds / 60.0
                
                # Calculate energy difference
                energy_diff_kwh = latest['total_energy_consumption'] - earliest['total_energy_consumption']
                
                # Calculate peak in kW by extrapolating to 1 hour to get peak in Watts
                if time_diff_minutes > 0:
                    current_peak_kw = energy_diff_kwh * (60.0 / time_diff_minutes)
                    logger.info(f"  📊 Slot {current_slot_start.strftime('%H:%M')}-{(current_slot_start + timedelta(minutes=self.peak_calculation_minutes)).strftime('%H:%M')}")
                    logger.info(f"  📊 Energy diff: {energy_diff_kwh:.3f} kWh over {time_diff_minutes:.1f} min ({len(slot_readings)} readings)")
                    logger.info(f"  ⚡ Current peak: {current_peak_kw:.2f} kW")
                else:
                    logger.warning("  ⚠️ Time difference is zero, cannot calculate peak")
            elif len(slot_readings) == 1:
                logger.info(f"  📝 Only 1 reading in current {self.peak_calculation_minutes}-min slot, need at least 2 to calculate peak")
            else:
                logger.info(f"  📝 No readings in current {self.peak_calculation_minutes}-min slot yet")
            
            # Calculate available power
            available_power_kw = self.max_peak_kw - current_peak_kw
            
            # Call calculate_limits to save device limits based on available power
            await self.calculate_limits(available_power_kw * 1000)  # Convert kW to W

            # Update database with current status
            self.db.upsert({
                "id": "load_watcher",
                "timestamp": current_time.isoformat(),
                "total_energy_consumption": total_energy,
                "current_peak_kw": current_peak_kw,
                "max_peak_kw": self.max_peak_kw,
                "available_power_kw": available_power_kw,
                "slot_start": current_slot_start.isoformat(),
                "slot_readings_count": len(slot_readings)
            }, query.id == 'load_watcher')
            
            logger.info(f"✅ Load watcher complete. Total: {total_energy:.3f} kWh, Peak: {current_peak_kw:.2f} kW, Available: {available_power_kw:.2f} kW")
            
        except Exception as e:
            logger.error(f"❌ Error in load watcher: {e}", exc_info=True)

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
            
            if not load_managed_devices:
                logger.info("  No devices with load management enabled")
                return
            
            # Initialize limits dictionary
            device_limits = {}
            
            if available_power_watts > 0:
                # Power available - increase limits (highest priority first)
                logger.info("  📈 Power available - increasing limits for high priority devices")
                sorted_devices = sorted(load_managed_devices, key=lambda x: x['priority'])
                remaining_power = available_power_watts
                
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
                    is_charging = False
                    if device['charge_sign'] == 'positive':
                        # Positive values mean charging
                        is_charging = current_load > 0
                    elif device['charge_sign'] == 'negative':
                        # Negative values mean charging
                        is_charging = current_load < 0
                        # Use absolute value for calculations
                        current_load = abs(current_load)
                    
                    # Skip device if not charging (e.g., battery discharging)
                    if not is_charging:
                        logger.info(f"    ⏭️ {device['name']} (P{device['priority']}): Not charging ({state['state']}{device['load_unit']}), skipping")
                        continue
                    
                    # Calculate new limit
                    calculated_limit = current_load + remaining_power
                    
                    # Cap at maximum
                    if calculated_limit > device['max_watts']:
                        new_limit = device['max_watts']
                        power_used = device['max_watts'] - current_load
                        remaining_power -= power_used
                    else:
                        new_limit = calculated_limit
                        remaining_power = 0
                    
                    device_limits[device['name']] = {
                        'limit_watts': new_limit,
                        'current_load_watts': current_load,
                        'max_watts': device['max_watts'],
                        'limiter_entity': device['limiter_entity'],
                        'priority': device['priority']
                    }
                    
                    logger.info(f"    ✓ {device['name']} (P{device['priority']}): {current_load:.0f}W → {new_limit:.0f}W (max: {device['max_watts']:.0f}W)")
                    
                    if remaining_power <= 0:
                        break
                
                if remaining_power > 0:
                    logger.info(f"    💡 Still {remaining_power:.0f}W available after adjusting all devices")
            
            else:
                # Power excess - decrease limits (lowest priority first)
                logger.info("  📉 Power excess - decreasing limits for low priority devices")
                sorted_devices = sorted(load_managed_devices, key=lambda x: x['priority'], reverse=True)
                excess_power = abs(available_power_watts)
                
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
                    is_charging = False
                    if device['charge_sign'] == 'positive':
                        # Positive values mean charging
                        is_charging = current_load > 0
                    elif device['charge_sign'] == 'negative':
                        # Negative values mean charging
                        is_charging = current_load < 0
                        # Use absolute value for calculations
                        current_load = abs(current_load)
                    
                    # Skip device if not charging (e.g., battery discharging)
                    if not is_charging:
                        logger.info(f"    ⏭️ {device['name']} (P{device['priority']}): Not charging ({state['state']}{device['load_unit']}), skipping")
                        continue
                    
                    # Calculate new limit
                    calculated_limit = current_load - excess_power
                    
                    # Check if limit goes negative
                    if calculated_limit < 0:
                        new_limit = 0
                        power_freed = current_load
                        excess_power -= power_freed
                    else:
                        new_limit = calculated_limit
                        excess_power = 0
                    
                    device_limits[device['name']] = {
                        'limit_watts': new_limit,
                        'current_load_watts': current_load,
                        'max_watts': device['max_watts'],
                        'limiter_entity': device['limiter_entity'],
                        'priority': device['priority']
                    }
                    
                    logger.info(f"    ✓ {device['name']} (P{device['priority']}): {current_load:.0f}W → {new_limit:.0f}W")
                    
                    if excess_power <= 0:
                        break
                
                if excess_power > 0:
                    logger.info(f"    ⚠️ Still {excess_power:.0f}W excess after adjusting all devices to 0")
            
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
