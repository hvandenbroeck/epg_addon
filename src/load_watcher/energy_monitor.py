import logging
import aiohttp
from datetime import datetime, timedelta
from tinydb import TinyDB, Query

logger = logging.getLogger(__name__)


class EnergyMonitor:
    """Monitors energy consumption from Home Assistant entities and maintains historical data."""
    
    def __init__(self, ha_url, headers, energy_entities, db):
        """Initialize the energy monitor.
        
        Args:
            ha_url: Home Assistant URL
            headers: HTTP headers with authorization
            energy_entities: List of energy consumption entity IDs
            db: TinyDB database instance
        """
        self.ha_url = ha_url
        self.headers = headers
        self.energy_entities = energy_entities
        self.db = db
        
    async def get_state(self, entity_id):
        """Get the state of an entity from Home Assistant."""
        url = f"{self.ha_url}/api/states/{entity_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                return None
    
    async def fetch_total_energy_consumption(self):
        """Fetch and sum energy consumption from all configured entities.
        
        Returns:
            float: Total energy consumption in kWh
        """
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
        
        return total_energy
    
    def store_reading(self, timestamp, total_energy):
        """Store a current energy reading in historical data.
        
        Args:
            timestamp: Reading timestamp
            total_energy: Total energy consumption in kWh
            
        Returns:
            list: Updated list of readings (cleaned of old data)
        """
        query = Query()
        historical_data = self.db.search(query.id == 'load_watcher_history')
        
        if historical_data:
            readings = historical_data[0].get('readings', [])
        else:
            readings = []
        
        # Add current reading
        readings.append({
            "timestamp": timestamp.isoformat(),
            "total_energy_consumption": total_energy
        })
        
        # Clean up readings older than 1 hour
        one_hour_ago = timestamp - timedelta(hours=1)
        readings = [r for r in readings if datetime.fromisoformat(r['timestamp']) > one_hour_ago]
        
        # Save updated historical data
        self.db.upsert({
            "id": "load_watcher_history",
            "readings": readings
        }, query.id == 'load_watcher_history')
        
        logger.info(f"  💾 Stored reading. Historical data: {len(readings)} readings in last hour")
        
        return readings
    
    def get_historical_readings(self):
        """Get historical energy readings from database.
        
        Returns:
            list: List of historical readings
        """
        query = Query()
        historical_data = self.db.search(query.id == 'load_watcher_history')
        
        if historical_data:
            return historical_data[0].get('readings', [])
        return []
