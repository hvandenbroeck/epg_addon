# Quick Start

## 1. Configuration File Setup

Create or update `/data/options.json` with your devices:

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

| Type | Description |
|------|-------------|
| `wp` | Heat pump |
| `hw` | Hot water |
| `battery` | Battery (with separate charge/discharge actions) |
| `ev` | Electric vehicle charger |

## 3. Required Fields

Minimum configuration for any device:

```json
{
  "name": "unique_name",
  "type": "device_type",
  "start": {"mqtt": [], "entity": []},
  "stop": {"mqtt": [], "entity": []}
}
```

## 4. Actions

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

## 5. Load Management (Optional)

For devices with dynamic power limiting:

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

## 6. Multiple Devices of the Same Type

Give each device a unique name:

```json
{
  "devices": [
    {"name": "ev1", "type": "ev", "...": "..."},
    {"name": "ev2", "type": "ev", "...": "..."},
    {"name": "ev_garage", "type": "ev", "...": "..."}
  ]
}
```

## 7. Default Behavior

If `/data/options.json` is missing or invalid, the addon loads a default configuration with one device of each type (`wp`, `hw`, `battery`, `ev`).

## 8. Validation

Configuration is validated on load. Check logs for errors:

```
Warning: Could not load devices config from /data/options.json: <error details>
```

## Common Issues

**Configuration not loading**
- Check JSON syntax and ensure the file exists at `/data/options.json`

**Device not found**
- Verify the device name is correct (case-sensitive)

**Validation error**
- Check that all required fields are present and values have the correct type

## Next Steps

- See [Device Configuration](device_configuration.md) for the complete configuration reference
- See [Configuration Examples](configuration_examples.md) for real-world patterns
- See [Expressions](expressions.md) for dynamic value calculations
- See [Heat Pump Runtime](heat_pump_runtime.md) for automatic runtime-aware scheduling
