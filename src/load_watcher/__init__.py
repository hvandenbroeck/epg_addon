"""
Load Watcher Module

Monitors energy consumption, calculates peak usage, and manages device load limits
to prevent exceeding configured power thresholds.
"""

import logging
import aiohttp
from datetime import datetime
from tinydb import TinyDB, Query

from ..config import CONFIG
from ..devices import Devices
from .energy_monitor import EnergyMonitor
from .peak_calculator import PeakCalculator
from .limit_calculator import LimitCalculator
from .limit_applier import LimitApplier

logger = logging.getLogger(__name__)


class LoadWatcher:
    """Main orchestrator for load watching and management."""
    
    def __init__(self, access_token):
        """Initialize the load watcher.
        
        Args:
            access_token: Home Assistant access token
        """
        self.ha_url = CONFIG['options']['ha_url']
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self.energy_entities = CONFIG['options'].get('energy_consumption_entities', [])
        self.max_peak_kw = CONFIG['options'].get('max_peak_kW', 7.5)
        self.peak_calculation_minutes = CONFIG['options'].get('peak_calculation_minutes', 15)
        self.load_watcher_threshold_power = CONFIG['options'].get('load_watcher_threshold_power', 10)
        self.db = TinyDB('db.json')
        self.devices = Devices(access_token)
        
        # Initialize sub-components
        self.energy_monitor = EnergyMonitor(
            self.ha_url, 
            self.headers, 
            self.energy_entities, 
            self.db
        )
        self.peak_calculator = PeakCalculator(self.peak_calculation_minutes)
        self.limit_calculator = LimitCalculator(
            self.db,
            self.load_watcher_threshold_power,
            self.energy_monitor.get_state
        )
        self.limit_applier = LimitApplier(self.db)
    
    async def run(self):
        """Main load watcher logic - runs every 5 minutes."""
        try:
            logger.info("🔋 Running load watcher...")
            
            # Fetch current total energy consumption
            total_energy = await self.energy_monitor.fetch_total_energy_consumption()
            
            # Get current timestamp
            current_time = datetime.now()
            
            # Store current reading in historical data
            readings = self.energy_monitor.store_reading(current_time, total_energy)
            
            # Calculate current peak over the configured time slot
            current_peak_kw, current_slot_start, slot_readings_count = \
                self.peak_calculator.calculate_current_peak(readings, current_time)
            
            # Calculate available power
            available_power_kw = self.max_peak_kw - current_peak_kw
            
            # Calculate device limits based on available power
            await self.limit_calculator.calculate_limits(available_power_kw * 1000)  # Convert kW to W
            
            # Apply the calculated limits to devices
            await self.limit_applier.apply_device_limits(self.devices)
            
            # Update database with current status
            query = Query()
            self.db.upsert({
                "id": "load_watcher",
                "timestamp": current_time.isoformat(),
                "total_energy_consumption": total_energy,
                "current_peak_kw": current_peak_kw,
                "max_peak_kw": self.max_peak_kw,
                "available_power_kw": available_power_kw,
                "slot_start": current_slot_start.isoformat(),
                "slot_readings_count": slot_readings_count
            }, query.id == 'load_watcher')
            
            logger.info(f"✅ Load watcher complete. Total: {total_energy:.3f} kWh, Peak: {current_peak_kw:.2f} kW, Available: {available_power_kw:.2f} kW")
            
        except Exception as e:
            logger.error(f"❌ Error in load watcher: {e}", exc_info=True)
    
    def close(self):
        """Close the TinyDB database connection."""
        if hasattr(self, 'db') and self.db:
            self.db.close()
            logger.debug("TinyDB connection closed")


# Export the main class
__all__ = ['LoadWatcher']
