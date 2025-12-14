import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class PeakCalculator:
    """Calculates peak power consumption over time slots."""
    
    def __init__(self, peak_calculation_minutes):
        """Initialize the peak calculator.
        
        Args:
            peak_calculation_minutes: Time slot duration in minutes (e.g., 15)
        """
        self.peak_calculation_minutes = peak_calculation_minutes
    
    def get_slot_start_time(self, timestamp):
        """Get the start time of the time slot for a given timestamp.
        
        For example, with 15-minute slots:
        - 10:07 -> 10:00
        - 10:22 -> 10:15
        - 10:47 -> 10:45
        
        Args:
            timestamp: Datetime to get slot start for
            
        Returns:
            datetime: Start time of the time slot
        """
        minutes = (timestamp.minute // self.peak_calculation_minutes) * self.peak_calculation_minutes
        return timestamp.replace(minute=minutes, second=0, microsecond=0)
    
    def calculate_current_peak(self, readings, current_time):
        """Calculate current peak power over the configured time slot.
        
        Args:
            readings: List of historical energy readings
            current_time: Current timestamp
            
        Returns:
            tuple: (current_peak_kw, slot_start, slot_readings_count)
        """
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
            
            # Calculate peak in kW by extrapolating to 1 hour
            if time_diff_minutes > 0:
                current_peak_kw = energy_diff_kwh * (60.0 / time_diff_minutes)
                logger.info(f"  📊 Slot {current_slot_start.strftime('%H:%M')}-{(current_slot_start + timedelta(minutes=self.peak_calculation_minutes)).strftime('%H:%M')}")
                logger.info(f"  📊 Energy diff: {energy_diff_kwh:.3f} kWh over {time_diff_minutes:.1f} min ({len(slot_readings)} readings)")
                logger.info(f"  ⚡ Current peak: {current_peak_kw:.2f} kW")
            else:
                logger.warning("  ⚠️ Time difference is zero, cannot calculate peak")
        elif len(slot_readings) == 1:
            # Try to find the latest reading before the current slot
            first_in_slot = slot_readings[0]
            first_in_slot_time = datetime.fromisoformat(first_in_slot['timestamp'])
            # Filter readings before the current slot start
            previous_readings = [r for r in readings if datetime.fromisoformat(r['timestamp']) < current_slot_start]
            if previous_readings:
                # Get the latest previous reading
                previous_reading = max(previous_readings, key=lambda x: x['timestamp'])
                previous_time = datetime.fromisoformat(previous_reading['timestamp'])
                # Calculate time difference in minutes
                time_diff_seconds = (first_in_slot_time - previous_time).total_seconds()
                time_diff_minutes = time_diff_seconds / 60.0
                # Calculate energy difference
                energy_diff_kwh = first_in_slot['total_energy_consumption'] - previous_reading['total_energy_consumption']
                # Calculate peak in kW by extrapolating to 1 hour
                if time_diff_minutes > 0:
                    current_peak_kw = energy_diff_kwh * (60.0 / time_diff_minutes)
                    logger.info(f"  📊 Slot {current_slot_start.strftime('%H:%M')}-{(current_slot_start + timedelta(minutes=self.peak_calculation_minutes)).strftime('%H:%M')}")
                    logger.info(f"  📊 Energy diff (using previous slot): {energy_diff_kwh:.3f} kWh over {time_diff_minutes:.1f} min (2 readings)")
                    logger.info(f"  ⚡ Current peak: {current_peak_kw:.2f} kW")
                else:
                    logger.warning("  ⚠️ Time difference is zero, cannot calculate peak (previous slot)")
            else:
                logger.info(f"  📝 Only 1 reading in current {self.peak_calculation_minutes}-min slot and no previous reading available, need at least 2 to calculate peak")
        else:
            logger.info(f"  📝 No readings in current {self.peak_calculation_minutes}-min slot yet")
        
        return current_peak_kw, current_slot_start, len(slot_readings)
