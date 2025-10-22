from datetime import datetime, timedelta
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

def ensure_list(value):
    """Ensure value is a list."""
    if isinstance(value, list):
        return value
    elif isinstance(value, dict):
        return [value]
    elif value is None:
        return []
    else:
        logger.warning(f"⚠️ Unexpected config entry: {value}")
        return []

def slot_to_time(index, slot_minutes):
    """Convert slot index to time string."""
    hours = (index * slot_minutes) // 60
    minutes = (index * slot_minutes) % 60
    return f"{hours:02d}:{minutes:02d}"

def get_block_len(device):
    """Get block length in minutes for each device type."""
    if device == "wp":
        return 2 * 60
    if device == "hw":
        return 1 * 60
    if device == "bat_charge":
        return 15
    if device == "bat_discharge":
        return 1 * 60
    return 15

from datetime import datetime, timedelta

def slots_to_iso_ranges(times, device, target_date):
    """Return list of (start_iso, stop_iso) for each time slot without merging."""
    if not times:
        return []

    block_len = get_block_len(device)

    # parse and sort times robustly (so result is in chronological order)
    def _to_dt(t):
        hour, minute = map(int, t.split(":"))
        return datetime.combine(target_date, datetime.min.time()) + timedelta(hours=hour, minutes=minute)

    slots = sorted(times, key=_to_dt)

    ranges = []
    for t in slots:
        start = _to_dt(t)
        stop = start + timedelta(minutes=block_len)
        ranges.append({ "device": device,
                        "start": start.isoformat(),
                        "stop": stop.isoformat() })

    return ranges


from datetime import datetime
from collections import defaultdict

def merge_sequential_timeslots(timeslot_lists):
    # Step 1: Group all timeslots by device (for merging only)
    device_slots = defaultdict(list)
    for timeslots in timeslot_lists:
        for slot in timeslots:
            device_slots[slot['device']].append(slot)
    
    # Step 2: For each device, merge sequential timeslots
    merged = []
    for device, slots in device_slots.items():
        # Sort slots by start time
        sorted_slots = sorted(slots, key=lambda x: x['start'])
        for slot in sorted_slots:
            start = datetime.fromisoformat(slot['start'])
            stop = datetime.fromisoformat(slot['stop'])
            if not merged or merged[-1]['device'] != device or start != datetime.fromisoformat(merged[-1]['stop']):
                merged.append({'device': device, 'start': slot['start'], 'stop': slot['stop']})
            else:
                # Merge if sequential and same device
                merged[-1]['stop'] = slot['stop']
    return merged

# Example usage:
input_data = [[{"device": "wp", "start": "2025-10-20T00:45:00", "stop": "2025-10-20T02:45:00"}, {"device": "wp", "start": "2025-10-20T02:45:00", "stop": "2025-10-20T04:45:00"}, {"device": "wp", "start": "2025-10-20T12:15:00", "stop": "2025-10-20T14:15:00"}, {"device": "wp", "start": "2025-10-20T21:30:00", "stop": "2025-10-20T23:30:00"}], [{"device": "hw", "start": "2025-10-20T03:30:00", "stop": "2025-10-20T04:30:00"}, {"device": "hw", "start": "2025-10-20T21:30:00", "stop": "2025-10-20T22:30:00"}], [{"device": "bat", "start": "2025-10-20T00:45:00", "stop": "2025-10-20T01:00:00"}, {"device": "bat", "start": "2025-10-20T02:45:00", "stop": "2025-10-20T03:00:00"}, {"device": "bat", "start": "2025-10-20T03:15:00", "stop": "2025-10-20T03:30:00"}, {"device": "bat", "start": "2025-10-20T03:30:00", "stop": "2025-10-20T03:45:00"}, {"device": "bat", "start": "2025-10-20T04:00:00", "stop": "2025-10-20T04:15:00"}, {"device": "bat", "start": "2025-10-20T04:15:00", "stop": "2025-10-20T04:30:00"}, {"device": "bat", "start": "2025-10-20T17:00:00", "stop": "2025-10-20T17:15:00"}, {"device": "bat", "start": "2025-10-20T21:30:00", "stop": "2025-10-20T21:45:00"}, {"device": "bat", "start": "2025-10-20T21:45:00", "stop": "2025-10-20T22:00:00"}, {"device": "bat", "start": "2025-10-20T23:45:00", "stop": "2025-10-21T00:00:00"}], [{"device": "wp", "start": "2025-10-21T00:15:00", "stop": "2025-10-21T02:15:00"}, {"device": "wp", "start": "2025-10-21T02:15:00", "stop": "2025-10-21T04:15:00"}, {"device": "wp", "start": "2025-10-21T04:15:00", "stop": "2025-10-21T06:15:00"}, {"device": "wp", "start": "2025-10-21T13:15:00", "stop": "2025-10-21T15:15:00"}], [{"device": "hw", "start": "2025-10-21T03:30:00", "stop": "2025-10-21T04:30:00"}, {"device": "hw", "start": "2025-10-21T13:30:00", "stop": "2025-10-21T14:30:00"}], [{"device": "bat", "start": "2025-10-21T01:45:00", "stop": "2025-10-21T02:00:00"}, {"device": "bat", "start": "2025-10-21T02:30:00", "stop": "2025-10-21T02:45:00"}, {"device": "bat", "start": "2025-10-21T02:45:00", "stop": "2025-10-21T03:00:00"}, {"device": "bat", "start": "2025-10-21T03:00:00", "stop": "2025-10-21T03:15:00"}, {"device": "bat", "start": "2025-10-21T03:15:00", "stop": "2025-10-21T03:30:00"}, {"device": "bat", "start": "2025-10-21T03:30:00", "stop": "2025-10-21T03:45:00"}, {"device": "bat", "start": "2025-10-21T03:45:00", "stop": "2025-10-21T04:00:00"}, {"device": "bat", "start": "2025-10-21T04:00:00", "stop": "2025-10-21T04:15:00"}, {"device": "bat", "start": "2025-10-21T04:15:00", "stop": "2025-10-21T04:30:00"}, {"device": "bat", "start": "2025-10-21T05:00:00", "stop": "2025-10-21T05:15:00"}]]

result = merge_sequential_timeslots(input_data)
for slot in result:
    print(slot)