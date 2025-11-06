import logging
import aiohttp
from datetime import datetime
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
        self.db = TinyDB('db.json')
        
    async def get_state(self, entity_id):
        """Get the state of an entity from Home Assistant."""
        url = f"{self.ha_url}/api/states/{entity_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                return None

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
            
            # Store current reading
            current_reading = {
                "timestamp": current_time.isoformat(),
                "total_energy_consumption": total_energy
            }
            
            # Retrieve previous reading
            query = Query()
            previous_readings = self.db.search(query.id == 'load_watcher')
            
            current_peak_kw = 0.0
            
            if previous_readings:
                previous_data = previous_readings[0]
                previous_time = datetime.fromisoformat(previous_data['timestamp'])
                previous_energy = previous_data['total_energy_consumption']
                
                # Calculate time difference in minutes
                time_diff_seconds = (current_time - previous_time).total_seconds()
                time_diff_minutes = time_diff_seconds / 60.0
                
                # Calculate energy difference
                energy_diff_kwh = total_energy - previous_energy
                
                # Calculate peak in kW by extrapolating to 1 hour
                if time_diff_minutes > 0:
                    current_peak_kw = energy_diff_kwh * (60.0 / time_diff_minutes)
                    logger.info(f"  📊 Energy diff: {energy_diff_kwh:.3f} kWh over {time_diff_minutes:.1f} min")
                    logger.info(f"  ⚡ Current peak: {current_peak_kw:.2f} kW")
                else:
                    logger.warning("  ⚠️ Time difference is zero, cannot calculate peak")
            else:
                logger.info("  📝 First run - no previous data to compare")
            
            # Calculate available power
            available_power_kw = self.max_peak_kw - current_peak_kw
            
            # Update database with current reading and calculated peak
            self.db.upsert({
                "id": "load_watcher",
                "timestamp": current_reading['timestamp'],
                "total_energy_consumption": total_energy,
                "current_peak_kw": current_peak_kw,
                "max_peak_kw": self.max_peak_kw,
                "available_power_kw": available_power_kw
            }, query.id == 'load_watcher')
            
            logger.info(f"✅ Load watcher complete. Total: {total_energy:.3f} kWh, Peak: {current_peak_kw:.2f} kW, Available: {available_power_kw:.2f} kW")
            
        except Exception as e:
            logger.error(f"❌ Error in load watcher: {e}", exc_info=True)
