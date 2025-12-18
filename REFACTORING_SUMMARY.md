# Devices Configuration Refactoring - Summary

## Overview
Successfully refactored the EPG Addon to use Pydantic BaseSettings for device configuration with support for multiple devices of the same type.

## Objectives Achieved ✅

1. ✅ Rewritten `devices_config.py` using Pydantic BaseSettings
2. ✅ Devices now have unique names AND types
3. ✅ Support for multiple devices of the same type (e.g., ev1, ev2)
4. ✅ Updated all dependent code to use new configuration system
5. ✅ Maintained backward compatibility via `device_actions` dict
6. ✅ Added Pydantic dependencies to Dockerfile

## Modified Files

### Core Configuration
- **src/devices_config.py** - Complete rewrite with Pydantic models
  - `DevicesConfig` - Main configuration class with BaseSettings
  - `Device` - Individual device model
  - `ActionSet` - MQTT and entity actions
  - `LoadManagement` - Load management configuration
  - Helper functions: `get_device_by_name()`, `get_devices_by_type()`

### Device Management
- **src/devices.py** - Enhanced with multi-device support
  - Added `get_device()` method
  - Added `get_devices_by_type()` method
  - Updated `execute_device_action()` to use device names

### Verification
- **src/device_verifier.py** - Updated imports and tracking

### Optimization
- **src/optimizer.py** - Refactored for multiple devices
  - Now iterates over all devices of each type
  - Dynamic device_block_minutes mapping
  - Updated state tracking per device name

### Load Management
- **src/load_watcher/limit_calculator.py** - Updated imports
- **src/load_watcher/limit_applier.py** - Updated imports

### Package Exports
- **src/__init__.py** - Export both `devices_config` and `device_actions`

### Dependencies
- **Dockerfile** - Added `pydantic` and `pydantic-settings`

## New Files Created

1. **DEVICES_CONFIG_EXAMPLE.json** - Complete example with multiple devices
   - Shows configuration for wp, hw, bat_charge, ev1, ev2
   - Demonstrates different device types and configurations

2. **DEVICES_CONFIG_MIGRATION.md** - Comprehensive migration guide
   - Overview of changes
   - Configuration structure
   - API usage examples
   - Troubleshooting guide

3. **example_device_config_usage.py** - Code examples
   - 10 practical examples of using the new API
   - Shows how to create, load, save, and validate configurations

## Key Features

### 1. Type Safety
```python
class Device(BaseModel):
    name: str  # Required, unique identifier
    type: DeviceType  # Enum: wp, hw, bat_charge, bat_discharge, ev
    enable_load_management: bool = False
    load_management: Optional[LoadManagement] = None
    start: ActionSet = Field(default_factory=ActionSet)
    stop: ActionSet = Field(default_factory=ActionSet)
```

### 2. Multiple Devices Per Type
```json
{
  "devices": [
    {"name": "ev1", "type": "ev", ...},
    {"name": "ev2", "type": "ev", ...},
    {"name": "ev3", "type": "ev", ...}
  ]
}
```

### 3. Configuration Access
```python
# By name
device = devices_config.get_device_by_name("ev1")

# By type
ev_devices = devices_config.get_devices_by_type("ev")

# Backward compatible
config = device_actions["ev1"]
```

### 4. Validation
Pydantic automatically validates:
- Required fields
- Field types
- Enum values
- Nested structures

## Configuration Location

Primary: `/data/options.json`

Format:
```json
{
  "devices": [
    {
      "name": "device_name",
      "type": "device_type",
      "enable_load_management": false,
      "start": { "mqtt": [...], "entity": [...] },
      "stop": { "mqtt": [...], "entity": [...] }
    }
  ]
}
```

## Backward Compatibility

- `device_actions` dict still available (auto-generated from Pydantic config)
- Existing code using `device_actions` continues to work
- Default configuration matches original setup

## Testing Checklist

- [ ] Configuration loads successfully
- [ ] Devices can be accessed by name
- [ ] Devices can be filtered by type
- [ ] Start/stop actions execute correctly
- [ ] Load management works with multiple devices
- [ ] Optimization runs for all configured devices
- [ ] State tracking works per device name
- [ ] Verification tracks all devices
- [ ] Backward compatible access via `device_actions` works

## Benefits

1. **Scalability** - Add unlimited devices without code changes
2. **Type Safety** - Catch configuration errors at load time
3. **Flexibility** - Easy to add/remove/modify devices
4. **Maintainability** - Clear models and validation
5. **Documentation** - Self-documenting with type hints

## Migration Path

For existing deployments:

1. Install updated container with Pydantic dependencies
2. Convert current configuration to new format in `/data/options.json`
3. Test device operations
4. Deploy to production

## Example: Adding New Device

Simply add to `/data/options.json`:

```json
{
  "name": "ev_garage",
  "type": "ev",
  "enable_load_management": true,
  "load_management": {
    "instantaneous_load_entity": "sensor.garage_ev_power",
    "load_priority": 5,
    "load_maximum_watts": "7400"
  },
  "start": {
    "entity": [{
      "service": "switch/turn_on",
      "entity_id": "switch.garage_ev_charging"
    }]
  },
  "stop": {
    "entity": [{
      "service": "switch/turn_off",
      "entity_id": "switch.garage_ev_charging"
    }]
  }
}
```

No code changes needed! 🎉

## Dependencies Added

```dockerfile
RUN pip install ... pydantic pydantic-settings
```

## Future Enhancements

Possible improvements:
- Configuration UI for easy device management
- Device templates for common setups
- Configuration validation endpoint
- Dynamic device loading/unloading
- Device groups for coordinated control

## Support

For issues or questions:
1. Check DEVICES_CONFIG_MIGRATION.md
2. Review example_device_config_usage.py
3. Validate configuration with Pydantic
4. Check logs for validation errors
