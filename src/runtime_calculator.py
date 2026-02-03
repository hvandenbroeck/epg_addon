"""Heat Pump Daily Runtime Calculator.

This module calculates expected daily runtime for heat pumps based on historical data.
It analyzes the relationship between inside temperature, outside temperature, and 
heat pump operation to estimate how long the heat pump needs to run daily.

The calculation considers:
- Inside temperature changes when heat pump is running
- Outside temperature (lower = longer runtime needed)
- Historical heat pump on/off states
- Last 10 days of data
"""

import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import csv
from pathlib import Path

logger = logging.getLogger(__name__)


class RuntimeCalculator:
    """Calculates expected daily runtime for heat pumps based on historical data."""

    def __init__(self):
        """Initialize the runtime calculator."""
        pass

    def load_history_from_csv(self, csv_path: str) -> Dict[str, List[Tuple[datetime, float]]]:
        """Load sensor history from CSV file.
        
        Args:
            csv_path: Path to CSV file with columns: entity_id, state, last_changed
            
        Returns:
            Dictionary mapping entity_id to list of (timestamp, value) tuples
        """
        history = {}
        
        try:
            with open(csv_path, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    entity_id = row['entity_id']
                    state = row['state']
                    timestamp_str = row['last_changed']
                    
                    # Parse timestamp
                    timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    
                    # Parse state value
                    try:
                        value = float(state)
                    except ValueError:
                        logger.warning(f"Could not parse state value '{state}' for {entity_id}")
                        continue
                    
                    if entity_id not in history:
                        history[entity_id] = []
                    history[entity_id].append((timestamp, value))
            
            # Sort each entity's history by timestamp
            for entity_id in history:
                history[entity_id].sort(key=lambda x: x[0])
            
            logger.info(f"Loaded history for {len(history)} sensors from {csv_path}")
            return history
            
        except Exception as e:
            logger.error(f"Error loading history from {csv_path}: {e}")
            return {}

    def load_history_from_ha(
        self, 
        ha_client,
        inside_temp_sensor: str,
        outside_temp_sensor: str,
        heatpump_status_sensor: str,
        days_back: int = 10
    ) -> Dict[str, List[Tuple[datetime, float]]]:
        """Load sensor history from Home Assistant.
        
        Args:
            ha_client: Home Assistant client instance
            inside_temp_sensor: Inside temperature sensor entity ID
            outside_temp_sensor: Outside temperature sensor entity ID
            heatpump_status_sensor: Heat pump status sensor entity ID
            days_back: Number of days of history to load
            
        Returns:
            Dictionary mapping entity_id to list of (timestamp, value) tuples
        """
        # TODO: Implement Home Assistant history API integration
        logger.warning("Home Assistant history loading not yet implemented, using CSV fallback")
        return {}

    def calculate_daily_runtime(
        self,
        history: Dict[str, List[Tuple[datetime, float]]],
        inside_temp_sensor: str,
        outside_temp_sensor: str,
        heatpump_status_sensor: str,
        days_back: int = 10
    ) -> Optional[float]:
        """Calculate expected daily runtime based on historical data.
        
        The calculation analyzes the last N days to determine:
        1. How long the heat pump ran each day
        2. The relationship between outside temperature and runtime
        3. The average daily runtime weighted by outside temperature
        
        Args:
            history: Dictionary of sensor histories
            inside_temp_sensor: Inside temperature sensor entity ID
            outside_temp_sensor: Outside temperature sensor entity ID  
            heatpump_status_sensor: Heat pump status sensor entity ID
            days_back: Number of days to analyze (default 10)
            
        Returns:
            Expected daily runtime in hours, or None if calculation fails
        """
        if heatpump_status_sensor not in history:
            logger.error(f"Heat pump status sensor {heatpump_status_sensor} not found in history")
            return None
        
        if outside_temp_sensor not in history:
            logger.error(f"Outside temperature sensor {outside_temp_sensor} not found in history")
            return None
        
        pump_history = history[heatpump_status_sensor]
        outside_temp_history = history[outside_temp_sensor]
        
        if not pump_history or not outside_temp_history:
            logger.error("Insufficient history data for runtime calculation")
            return None
        
        # Calculate daily runtimes from pump status history
        daily_runtimes = self._calculate_daily_runtimes_from_status(pump_history, days_back)
        
        if not daily_runtimes:
            logger.warning("No daily runtimes calculated from history")
            return None
        
        # Calculate average daily outside temperature for each day
        daily_avg_temps = self._calculate_daily_avg_temps(outside_temp_history, days_back)
        
        # Calculate weighted average runtime
        # Lower outside temps typically require longer runtime
        total_runtime = 0.0
        total_weight = 0.0
        
        for day_str, runtime_hours in daily_runtimes.items():
            if day_str in daily_avg_temps:
                avg_temp = daily_avg_temps[day_str]
                # Weight by inverse of temperature (lower temp = higher weight)
                # Add offset to avoid division by zero and to handle negative temps
                temp_weight = 1.0 / (avg_temp + 20.0) if avg_temp > -20 else 0.1
                total_runtime += runtime_hours * temp_weight
                total_weight += temp_weight
        
        if total_weight == 0:
            # Fallback to simple average
            avg_runtime = sum(daily_runtimes.values()) / len(daily_runtimes)
            logger.info(f"Calculated average daily runtime: {avg_runtime:.2f} hours (simple average)")
            return avg_runtime
        
        weighted_avg_runtime = total_runtime / total_weight
        logger.info(f"Calculated weighted average daily runtime: {weighted_avg_runtime:.2f} hours "
                   f"(based on {len(daily_runtimes)} days)")
        
        return weighted_avg_runtime

    def _calculate_daily_runtimes_from_status(
        self,
        pump_history: List[Tuple[datetime, float]],
        days_back: int
    ) -> Dict[str, float]:
        """Calculate runtime in hours for each day from pump status history.
        
        Args:
            pump_history: List of (timestamp, status) tuples where status is 1=on, 0=off
            days_back: Number of days to analyze
            
        Returns:
            Dictionary mapping date string (YYYY-MM-DD) to runtime in hours
        """
        if not pump_history:
            return {}
        
        # Get date range
        latest_timestamp = pump_history[-1][0]
        earliest_date = latest_timestamp - timedelta(days=days_back)
        
        # Filter to relevant date range
        filtered_history = [(ts, status) for ts, status in pump_history if ts >= earliest_date]
        
        if not filtered_history:
            return {}
        
        # Calculate runtime per day
        daily_runtimes = {}
        
        for i in range(len(filtered_history)):
            timestamp, status = filtered_history[i]
            date_str = timestamp.date().isoformat()
            
            # Calculate duration until next status change (or end of history)
            if i < len(filtered_history) - 1:
                next_timestamp = filtered_history[i + 1][0]
                duration_seconds = (next_timestamp - timestamp).total_seconds()
            else:
                # For last entry, assume it holds for 1 hour (or until end of day)
                duration_seconds = 3600
            
            # Add runtime if pump was on (status == 1)
            if status == 1:
                duration_hours = duration_seconds / 3600.0
                if date_str not in daily_runtimes:
                    daily_runtimes[date_str] = 0.0
                daily_runtimes[date_str] += duration_hours
        
        logger.debug(f"Calculated runtimes for {len(daily_runtimes)} days: {daily_runtimes}")
        return daily_runtimes

    def _calculate_daily_avg_temps(
        self,
        temp_history: List[Tuple[datetime, float]],
        days_back: int
    ) -> Dict[str, float]:
        """Calculate average temperature for each day.
        
        Args:
            temp_history: List of (timestamp, temperature) tuples
            days_back: Number of days to analyze
            
        Returns:
            Dictionary mapping date string (YYYY-MM-DD) to average temperature
        """
        if not temp_history:
            return {}
        
        # Get date range
        latest_timestamp = temp_history[-1][0]
        earliest_date = latest_timestamp - timedelta(days=days_back)
        
        # Filter to relevant date range
        filtered_history = [(ts, temp) for ts, temp in temp_history if ts >= earliest_date]
        
        if not filtered_history:
            return {}
        
        # Calculate average per day
        daily_temps = {}
        daily_counts = {}
        
        for timestamp, temp in filtered_history:
            date_str = timestamp.date().isoformat()
            if date_str not in daily_temps:
                daily_temps[date_str] = 0.0
                daily_counts[date_str] = 0
            daily_temps[date_str] += temp
            daily_counts[date_str] += 1
        
        # Calculate averages
        daily_avg_temps = {
            date_str: daily_temps[date_str] / daily_counts[date_str]
            for date_str in daily_temps
        }
        
        logger.debug(f"Calculated average temps for {len(daily_avg_temps)} days")
        return daily_avg_temps
