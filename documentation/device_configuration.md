# Device Configuration

Configuration is loaded from `/data/options.json`. See `DEVICES_CONFIG_EXAMPLE.json` in the repository root for a complete example.

## Configuration Structure

```json
{
  "devices": [
    {
      "name": "wp",
      "type": "wp",
      "enable_load_management": false,
      "start": { "mqtt": [...], "entity": [...] },
      "stop":  { "mqtt": [...], "entity": [...] }
    }
  ]
}
```

Each device has a **unique name** and a **device type**.

## Device Types

| Type | Description |
|------|-------------|
| `wp` | Heat pump |
| `hw` | Hot water |
| `battery` | Battery (requires separate `charge_start` / `charge_stop` / `discharge_start` / `discharge_stop` actions) |
| `ev` | Electric vehicle charger |

## Device Fields

### Required

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique device identifier |
| `type` | string | Device type: `wp`, `hw`, `battery`, or `ev` |

### Optional – General

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `start` | ActionSet | `{}` | Actions to execute when starting the device |
| `stop` | ActionSet | `{}` | Actions to execute when stopping the device |
| `enable_load_management` | boolean | `false` | Enable dynamic power limiting |
| `load_management` | LoadManagement | `null` | Load management settings (required if `enable_load_management` is `true`) |
| `block_hours` | number | device default | Minimum duration (hours) for a scheduled run |
| `min_gap_hours` | number | device default | Minimum gap between runs (hours) |
| `max_gap_hours` | number | device default | Maximum gap between runs (hours) |

### Optional – Battery Only

| Field | Type | Description |
|-------|------|-------------|
| `charge_start` | ActionSet | Actions to start battery charging |
| `charge_stop` | ActionSet | Actions to stop battery charging |
| `discharge_start` | ActionSet | Actions to start battery discharging |
| `discharge_stop` | ActionSet | Actions to stop battery discharging |

### Optional – Heat Pump Runtime Calculation

| Field | Type | Description |
|-------|------|-------------|
| `inside_temp_sensor` | string | Entity ID of the inside temperature sensor |
| `outside_temp_sensor` | string | Entity ID of the outside temperature sensor |
| `heatpump_status_sensor` | string | Entity ID of the heat pump status sensor (1=on, 0=off) |

See [Heat Pump Runtime](heat_pump_runtime.md) for details.

## Action Sets

An `ActionSet` has two optional lists:

```json
{
  "mqtt":   [ <MQTTAction>, ... ],
  "entity": [ <EntityAction>, ... ]
}
```

### MQTT Action Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `topic` | string | ✓ | MQTT topic to publish to |
| `payload` | string / number | ✓ | Value to publish (supports [expressions](expressions.md)) |
| `topic_get` | string | | Read-back topic for state verification |
| `payload_check` | string / number | | Expected value when reading back |

### Entity Action Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `service` | string | ✓ | Home Assistant service, e.g. `switch/turn_on` |
| `entity_id` | string | ✓ | Target entity ID |
| `value` | string / number | | Value for `number/set_value` (supports [expressions](expressions.md)) |
| `option` | string | | Option for `select/select_option` |
| `value_check` | string / number | | Expected state after service call |
| `state_attribute` | string | | Check this attribute instead of the main state |

Switch services (`turn_on` / `turn_off`) automatically infer the expected state (`"on"` / `"off"`), so `value_check` is not needed for those.

## Load Management Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `instantaneous_load_entity` | string | ✓ | Sensor entity providing current power draw (W) |
| `load_priority` | integer | ✓ | Lower number = higher priority |
| `load_maximum_watts` | string / number | ✓ | Maximum allowed power draw (W) |
| `charge_sign` | string | | `"positive"` (default) or `"negative"` – sign convention for the power sensor |
| `apply_limit_actions` | ActionSet | | Actions to execute when a new power limit is set |

## Multiple Devices of the Same Type

You can configure any number of devices of the same type by giving each a unique name:

```json
{
  "devices": [
    {"name": "ev1", "type": "ev", "...": "..."},
    {"name": "ev2", "type": "ev", "...": "..."}
  ]
}
```

## Accessing Configuration in Code

```python
from src.devices_config import devices_config

# Get a single device by name
device = devices_config.get_device_by_name("ev1")

# Get all devices of a given type
ev_devices = devices_config.get_devices_by_type("ev")

# Iterate over all devices
for device in devices_config.devices:
    print(f"{device.name} ({device.type})")
```

### Optional – EV Solar Charge Controller

When `solar_charge_config` is provided on an EV device the addon periodically
reads per-phase solar production and house consumption from Home Assistant,
calculates the net solar surplus, and adjusts the EV charging current to
consume exactly that surplus. Automatic phase switching (single ↔ three phase)
is performed via the device's existing `load_management.apply_limit_actions`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `production_phase_l1_entity` | string | `null` | HA entity for solar production phase L1 (W) |
| `production_phase_l2_entity` | string | `null` | HA entity for solar production phase L2 (W) |
| `production_phase_l3_entity` | string | `null` | HA entity for solar production phase L3 (W) |
| `consumption_phase_l1_entity` | string | `null` | HA entity for house consumption phase L1 excluding the EV (W) |
| `consumption_phase_l2_entity` | string | `null` | HA entity for house consumption phase L2 excluding the EV (W) |
| `consumption_phase_l3_entity` | string | `null` | HA entity for house consumption phase L3 excluding the EV (W) |
| `phase_switch_threshold_power` | number | `4000` | Surplus (W) at or above which three-phase charging is used |
| `minimum_ev_charging_power` | number | `1380` | Minimum surplus (W) needed to adjust the charge limit (≈ 6 A × 230 V) |

Example:

```json
{
  "name": "ev1",
  "type": "ev",
  "enable_load_management": true,
  "load_management": { "...": "..." },
  "solar_charge_config": {
    "production_phase_l1_entity": "sensor.power_production_phase_l1",
    "production_phase_l2_entity": "sensor.power_production_phase_l2",
    "production_phase_l3_entity": "sensor.power_production_phase_l3",
    "consumption_phase_l1_entity": "sensor.power_consumption_phase_l1",
    "consumption_phase_l2_entity": "sensor.power_consumption_phase_l2",
    "consumption_phase_l3_entity": "sensor.power_consumption_phase_l3",
    "phase_switch_threshold_power": 4000,
    "minimum_ev_charging_power": 1380
  }
}
```

Omit the phases you don't need; missing phases default to **0 W**.



| Problem | Fix |
|---------|-----|
| Configuration not loading | Check `/data/options.json` exists and is valid JSON |
| Device not found | Verify the device name matches exactly (case-sensitive) |
| Type validation error | Check field types match the model; enum values must be `"positive"` or `"negative"` |
| Start/stop action not executing | Verify `service`, `entity_id`, or MQTT `topic` values are correct |

## Further Reading

- [Quick Start](quick_start.md) – Minimal working configuration
- [Configuration Examples](configuration_examples.md) – Real-world patterns
- [Expressions](expressions.md) – Dynamic value calculations in `value` / `payload` fields
