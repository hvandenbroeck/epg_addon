# Quick Start: Multi-Device Configuration

## 1. Configuration File Setup

Create or update `/data/options.json`:

```json
{
  "devices": [
    {
      "name": "wp",
      "type": "wp",
      "enable_load_management": false,
      "start": {
        "mqtt": [
          {"topic": "ebusd/700/z2sfmode/set", "payload": "veto"}
        ]
      },
      "stop": {
        "mqtt": [
          {"topic": "ebusd/700/z2sfmode/set", "payload": "auto"}
        ]
      }
    },
    {
      "name": "ev1",
      "type": "ev",
      "enable_load_management": true,
      "load_management": {
        "instantaneous_load_entity": "sensor.ev1_power",
        "load_priority": 2,
        "load_maximum_watts": "7400"
      },
      "start": {
        "entity": [
          {"service": "switch/turn_on", "entity_id": "switch.ev1_charging"}
        ]
      },
      "stop": {
        "entity": [
          {"service": "switch/turn_off", "entity_id": "switch.ev1_charging"}
        ]
      }
    }
  ]
}
```

## 2. Device Types

- `wp` - Heat Pump
- `hw` - Hot Water  
- `bat_charge` - Battery Charging
- `bat_discharge` - Battery Discharging
- `ev` - Electric Vehicle Charger

## 3. Basic Usage

```python
from src.devices_config import devices_config

# Get all EV devices
ev_devices = devices_config.get_devices_by_type("ev")

# Get specific device
device = devices_config.get_device_by_name("ev1")

# Access device properties
print(f"Device: {device.name}")
print(f"Type: {device.type}")
print(f"Load Management: {device.enable_load_management}")
```

## 4. Required Fields

Minimum configuration for a device:

```json
{
  "name": "unique_name",
  "type": "device_type",
  "start": {"mqtt": [], "entity": []},
  "stop": {"mqtt": [], "entity": []}
}
```

## 5. Load Management (Optional)

For devices with load management:

```json
{
  "enable_load_management": true,
  "load_management": {
    "instantaneous_load_entity": "sensor.device_power",
    "load_priority": 1,
    "load_maximum_watts": "3500",
    "charge_sign": "positive"
  }
}
```

## 6. Multiple Devices Same Type

Just give each a unique name:

```json
{
  "devices": [
    {"name": "ev1", "type": "ev", ...},
    {"name": "ev2", "type": "ev", ...},
    {"name": "ev_garage", "type": "ev", ...}
  ]
}
```

## 7. Actions

### MQTT Actions
```json
{
  "mqtt": [
    {
      "topic": "my/topic/set",
      "topic_get": "my/topic/get",
      "payload": "value"
    }
  ]
}
```

### Entity Actions
```json
{
  "entity": [
    {
      "service": "switch/turn_on",
      "entity_id": "switch.my_device"
    },
    {
      "service": "number/set_value",
      "entity_id": "number.my_setting",
      "value": 100
    }
  ]
}
```

## 8. Validation

Configuration is validated on load. Check logs for errors:

```
Warning: Could not load devices config from /data/options.json: <error details>
```

## 9. Default Behavior

If `/data/options.json` doesn't exist or is invalid, the system loads default configuration with:
- 1 wp device
- 1 hw device  
- 1 bat_charge device
- 1 bat_discharge device
- 1 ev device

## 10. Testing Your Configuration

```python
from src.devices_config import devices_config

# List all devices
for device in devices_config.devices:
    print(f"✓ {device.name} ({device.type})")

# Check specific type
ev_count = len(devices_config.get_devices_by_type("ev"))
print(f"Found {ev_count} EV devices")
```

## Common Issues

**Issue:** Configuration not loading
- **Fix:** Check JSON syntax, ensure file exists at `/data/options.json`

**Issue:** Device not found
- **Fix:** Verify device name is correct (case-sensitive)

**Issue:** Validation error
- **Fix:** Check all required fields are present and types are correct

## Next Steps

- See `DEVICES_CONFIG_EXAMPLE.json` for complete example
- Read `DEVICES_CONFIG_MIGRATION.md` for detailed documentation
- Check `example_device_config_usage.py` for code examples
