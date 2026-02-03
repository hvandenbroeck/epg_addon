#!/usr/bin/env python3
"""Test the runtime calculator with sample CSV data."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from runtime_calculator import RuntimeCalculator

def main():
    calc = RuntimeCalculator()
    
    # Load history from CSV
    csv_path = 'data/history.csv'
    history = calc.load_history_from_csv(csv_path)
    
    print(f"Loaded history for sensors: {list(history.keys())}")
    
    # Calculate daily runtime
    inside_temp_sensor = 'sensor.ebusd_700_z2roomtemp'
    outside_temp_sensor = 'sensor.ebusd_700_displayedoutsidetemp'
    heatpump_status_sensor = 'sensor.ebusd_700_hc2pumpstatus_2'
    
    runtime = calc.calculate_daily_runtime(
        history=history,
        inside_temp_sensor=inside_temp_sensor,
        outside_temp_sensor=outside_temp_sensor,
        heatpump_status_sensor=heatpump_status_sensor,
        days_back=10
    )
    
    if runtime:
        print(f"\n✅ Calculated expected daily runtime: {runtime:.2f} hours")
    else:
        print("\n❌ Failed to calculate daily runtime")

if __name__ == '__main__':
    main()
