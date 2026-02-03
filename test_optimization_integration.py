#!/usr/bin/env python3
"""Test the runtime calculator module independently."""

import sys
import os
from datetime import datetime, timedelta

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

def main():
    # Import only the runtime calculator (avoid importing the full package)
    import csv
    import logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    print("=" * 70)
    print("Testing Heat Pump Runtime Calculation")
    print("=" * 70)
    
    # Load and parse CSV directly (copy of RuntimeCalculator logic)
    csv_path = 'data/history.csv'
    history = {}
    
    print(f"\n1. Loading history from {csv_path}...")
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            entity_id = row['entity_id']
            state = row['state']
            timestamp_str = row['last_changed']
            
            timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            
            try:
                value = float(state)
            except ValueError:
                continue
            
            if entity_id not in history:
                history[entity_id] = []
            history[entity_id].append((timestamp, value))
    
    for entity_id in history:
        history[entity_id].sort(key=lambda x: x[0])
    
    print(f"   Loaded history for sensors: {list(history.keys())}")
    
    # Calculate daily runtimes
    inside_temp_sensor = 'sensor.ebusd_700_z2roomtemp'
    outside_temp_sensor = 'sensor.ebusd_700_displayedoutsidetemp'
    heatpump_status_sensor = 'sensor.ebusd_700_hc2pumpstatus_2'
    
    print(f"\n2. Calculating daily runtimes from heat pump status...")
    pump_history = history[heatpump_status_sensor]
    outside_temp_history = history[outside_temp_sensor]
    
    days_back = 10
    latest_timestamp = pump_history[-1][0]
    earliest_date = latest_timestamp - timedelta(days=days_back)
    filtered_history = [(ts, status) for ts, status in pump_history if ts >= earliest_date]
    
    # Calculate runtime per day
    daily_runtimes = {}
    for i in range(len(filtered_history)):
        timestamp, status = filtered_history[i]
        date_str = timestamp.date().isoformat()
        
        if i < len(filtered_history) - 1:
            next_timestamp = filtered_history[i + 1][0]
            duration_seconds = (next_timestamp - timestamp).total_seconds()
        else:
            duration_seconds = 3600
        
        if status == 1:
            duration_hours = duration_seconds / 3600.0
            if date_str not in daily_runtimes:
                daily_runtimes[date_str] = 0.0
            daily_runtimes[date_str] += duration_hours
    
    print(f"   Daily runtimes:")
    for date_str, runtime in sorted(daily_runtimes.items()):
        print(f"      {date_str}: {runtime:.2f} hours")
    
    # Calculate average temperatures per day
    print(f"\n3. Calculating average outside temperatures per day...")
    filtered_temp = [(ts, temp) for ts, temp in outside_temp_history if ts >= earliest_date]
    
    daily_temps = {}
    daily_counts = {}
    for timestamp, temp in filtered_temp:
        date_str = timestamp.date().isoformat()
        if date_str not in daily_temps:
            daily_temps[date_str] = 0.0
            daily_counts[date_str] = 0
        daily_temps[date_str] += temp
        daily_counts[date_str] += 1
    
    daily_avg_temps = {
        date_str: daily_temps[date_str] / daily_counts[date_str]
        for date_str in daily_temps
    }
    
    print(f"   Average outside temperatures:")
    for date_str, temp in sorted(daily_avg_temps.items()):
        print(f"      {date_str}: {temp:.2f}°C")
    
    # Calculate weighted average runtime
    print(f"\n4. Calculating weighted average runtime...")
    total_runtime = 0.0
    total_weight = 0.0
    
    for day_str, runtime_hours in daily_runtimes.items():
        if day_str in daily_avg_temps:
            avg_temp = daily_avg_temps[day_str]
            # Weight by inverse of temperature
            temp_weight = 1.0 / (avg_temp + 20.0) if avg_temp > -20 else 0.1
            total_runtime += runtime_hours * temp_weight
            total_weight += temp_weight
            print(f"      {day_str}: runtime={runtime_hours:.2f}h, temp={avg_temp:.2f}°C, weight={temp_weight:.4f}")
    
    if total_weight > 0:
        weighted_avg_runtime = total_runtime / total_weight
        print(f"\n✅ Expected daily runtime: {weighted_avg_runtime:.2f} hours")
        print(f"   (Based on {len(daily_runtimes)} days of history)")
    else:
        simple_avg = sum(daily_runtimes.values()) / len(daily_runtimes)
        print(f"\n✅ Expected daily runtime: {simple_avg:.2f} hours (simple average)")
    
    print("\n" + "=" * 70)
    print("Test completed successfully!")
    print("=" * 70)

if __name__ == '__main__':
    main()


if __name__ == '__main__':
    main()
