# Multi-Device Configuration Migration Guide

## Overview

The device configuration has been rewritten to use **Pydantic BaseSettings**, providing:
- Type validation and safety
- Support for multiple devices of the same type
- Better configuration management
- Cleaner code structure

## Key Changes

### 1. Configuration Structure

**Before:** Devices were identified by type only (e.g., `"wp"`, `"hw"`, `"ev"`)

**After:** Each device has:
- **Unique name** (e.g., `"wp"`, `"ev1"`, `"ev2"`)
- **Device type** (e.g., `"wp"`, `"hw"`, `"battery"`, `"ev"`)

### 2. Configuration Location

Configuration is loaded from `/data/options.json` in the following format:

```json
{
  "devices": [
    {
      "name": "wp",
      "type": "wp",
      "enable_load_management": false,
      "start": { ... },
      "stop": { ... }
    },
    {
      "name": "ev1",
      "type": "ev",
      "enable_load_management": true,
      "load_management": { ... },
      "start": { ... },
      "stop": { ... }
    }
  ]
}
```

See `DEVICES_CONFIG_EXAMPLE.json` for a complete example.

### 3. Device Types

Supported device types:
- `wp` - Heat Pump (Water Pump)
- `hw` - Hot Water
- `battery` - Battery (with separate charge/discharge actions)
- `ev` - Electric Vehicle Charger

### 4. Multiple Devices

You can now configure **multiple devices of the same type**. For example, two EV chargers:

```json
{
  "devices": [
    {
      "name": "ev1",
      "type": "ev",
      "start": { "entity": [{ "entity_id": "switch.ev_charger_1_charging" }] },
      "stop": { "entity": [{ "entity_id": "switch.ev_charger_1_charging" }] }
    },
    {
      "name": "ev2",
      "type": "ev",
      "start": { "entity": [{ "entity_id": "switch.ev_charger_2_charging" }] },
      "stop": { "entity": [{ "entity_id": "switch.ev_charger_2_charging" }] }
    }
  ]
}
```

## Pydantic Models

### Device Model

```python
class Device(BaseModel):
    name: str  # Unique device identifier
    type: DeviceType  # wp, hw, battery, ev
    enable_load_management: bool = False
    load_management: Optional[LoadManagement] = None
    start: ActionSet = Field(default_factory=ActionSet)
    stop: ActionSet = Field(default_factory=ActionSet)
    # Battery-specific actions (only for type='battery')
    charge_start: Optional[ActionSet] = None
    charge_stop: Optional[ActionSet] = None
    discharge_start: Optional[ActionSet] = None
    discharge_stop: Optional[ActionSet] = None
```

### Action Models

```python
class MQTTAction(BaseModel):
    topic: str
    topic_get: Optional[str] = None
    payload: Union[str, int, float]
    payload_check: Optional[Union[str, int, float]] = None

class EntityAction(BaseModel):
    service: str
    entity_id: str
    value: Optional[Union[str, int, float]] = None
    option: Optional[str] = None
    value_check: Optional[Union[str, int, float]] = None
    state_attribute: Optional[str] = None

class ActionSet(BaseModel):
    mqtt: List[MQTTAction] = Field(default_factory=list)
    entity: List[EntityAction] = Field(default_factory=list)
```

## API Usage

### Accessing Device Configuration

```python
from src.devices_config import devices_config

# Get device by name
device = devices_config.get_device_by_name("ev1")

# Get all devices of a type
ev_devices = devices_config.get_devices_by_type("ev")

# Iterate over all devices
for device in devices_config.devices:
    print(f"{device.name} ({device.type})")
```

### Using the Devices Class

```python
from src.devices import Devices

devices = Devices(access_token)

# Get device object
device = devices.get_device("ev1")

# Get all devices by type
ev_devices = devices.get_devices_by_type("ev")

# Execute device action (using device name)
await devices.execute_device_action(
    device_name="ev1",
    actions=device.start.model_dump(),
    action_label="start"
)
```

## Direct Pydantic Model Usage

All code now uses Pydantic models directly—no dict conversion layer. This provides:
- Type safety throughout the application
- IDE autocompletion and type checking
- Cleaner, more maintainable code

## Migration Steps

1. **Update configuration file**: Convert your device configuration to the new format in `/data/options.json`
2. **Update device references**: Change device identifiers in your code from type-based to name-based
3. **Add dependencies**: Ensure `pydantic` and `pydantic-settings` are installed

## Dependencies

Added to `Dockerfile`:
- `pydantic`
- `pydantic-settings`

## Code Changes Summary

### Modified Files

1. **src/devices_config.py**: Complete rewrite using Pydantic models
2. **src/devices.py**: Added methods for multi-device support
3. **src/device_verifier.py**: Updated imports and device handling
4. **src/optimizer.py**: Updated to iterate over multiple devices per type
5. **src/load_watcher/limit_calculator.py**: Updated imports
6. **src/load_watcher/limit_applier.py**: Updated imports
7. **src/__init__.py**: Export both `devices_config` and `device_actions`
8. **Dockerfile**: Added pydantic dependencies

### New Files

- **DEVICES_CONFIG_EXAMPLE.json**: Example configuration with multiple devices
- **DEVICES_CONFIG_MIGRATION.md**: This migration guide

## Testing

After migration, test:
1. Configuration loading: Verify devices are loaded correctly
2. Device execution: Test start/stop actions for each device
3. Load management: Verify load limits are applied correctly
4. Optimization: Ensure optimization works for all configured devices
5. Multiple devices: Test with multiple devices of the same type

## Troubleshooting

### Configuration not loading

- Check `/data/options.json` exists and is valid JSON
- Verify all required fields are present
- Check logs for Pydantic validation errors

### Device not found

- Ensure device name matches exactly (case-sensitive)
- Verify device is in the `devices` list in configuration

### Type validation errors

- Check field types match the Pydantic model definitions
- Ensure enum values are correct (e.g., `"positive"` or `"negative"` for charge_sign)

## Example: Adding a Third EV Charger

```json
{
  "name": "ev3",
  "type": "ev",
  "enable_load_management": true,
  "load_management": {
    "instantaneous_load_entity": "sensor.ev_charger_3_power",
    "load_priority": 4,
    "load_maximum_watts": "7400"
  },
  "start": {
    "entity": [{
      "service": "switch/turn_on",
      "entity_id": "switch.ev_charger_3_charging"
    }]
  },
  "stop": {
    "entity": [{
      "service": "switch/turn_off",
      "entity_id": "switch.ev_charger_3_charging"
    }]
  }
}
```

Just add this to the `devices` array in your configuration!

## Benefits

1. **Type Safety**: Pydantic validates all configuration at load time
2. **Flexibility**: Easy to add/remove devices without code changes
3. **Scalability**: Support unlimited devices of each type
4. **Maintainability**: Cleaner code with proper models
5. **Documentation**: Self-documenting with type hints and field descriptions
