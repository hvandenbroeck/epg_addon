import logging
import aiohttp
from datetime import datetime, timedelta
from tinydb import TinyDB, Query

from .config import CONFIG

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
