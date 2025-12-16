# Real-World Configuration Examples

**Note:** Only content within curly brackets `{}` is evaluated as an expression. Variables within brackets don't need their own brackets.

## State Verification & Retry

The addon automatically verifies device states and retries failed commands:
- **Post-action**: Checks every 30s for 3 minutes after each command
- **Periodic**: Checks all devices every 5 minutes

**Verification fields:**
- MQTT: `topic_get` (read topic), `payload_check` (expected value)
- Entity: `value_check` (expected state), `state_attribute` (check attribute instead of state)
- Switch services (`turn_on`/`turn_off`) auto-infer expected state ("on"/"off")

## Example 1: Battery Charger with Efficiency Calculation

If your battery charger has 90% efficiency, you can automatically account for this:

```python
"bat_charge": {
    "enable_load_management": True,
    "load_management": {
        "apply_limit_actions": {
            "entity": [
                {
                    "service": "number/set_value",
                    "entity_id": "number.battery_charge_limit",
                    "value": "{round(limit_watts * 0.9)}"  # Apply 90% efficiency
                }
            ]
        }
    }
}
```

## Example 2: EV Charger with Switch Verification

Convert power limit (watts) to current (amps). Switch verification is automatic:

```python
"ev": {
    "enable_load_management": True,
    "load_management": {
        "apply_limit_actions": {
            "entity": [
                {
                    "service": "number/set_value",
                    "entity_id": "number.ev_charger_max_charging_current",
                    "value": "{round(limit_watts / 230, 1)}"
                }
            ]
        }
    },
    "start": {
        "entity": [
            {
                "service": "switch/turn_on",
                "entity_id": "switch.ev_charger"
                # Auto-verifies state = "on"
            }
        ]
    },
    "stop": {
        "entity": [
            {
                "service": "switch/turn_off",
                "entity_id": "switch.ev_charger"
                # Auto-verifies state = "off"
            }
        ]
    }
}
```

## Example 3: Heat Pump with MQTT Verification

Control via MQTT with explicit verification topics:

```python
"wp": {
    "start": {
        "mqtt": [
            {
                "topic": "ebusd/700/z2sfmode/set",
                "topic_get": "ebusd/700/z2sfmode/get",  # Topic to verify state
                "payload": "veto"
            },
            {
                "topic": "ebusd/700/z2quickvetotemp/set",
                "topic_get": "ebusd/700/z2quickvetotemp/get",
                "payload": "22"
            }
        ]
    },
    "stop": {
        "mqtt": [
            {
                "topic": "ebusd/700/z2sfmode/set",
                "topic_get": "ebusd/700/z2sfmode/get",
                "payload": "auto"
            },
            {
                "topic": "ebusd/700/z2quickvetotemp/set",
                "topic_get": "ebusd/700/z2quickvetotemp/get",
                "payload": "20"
            }
        ]
    }
}
```

## Example 4: Select Entity with Verification

For select entities, verification uses the `option` value automatically:

```python
"bat_charge": {
    "start": {
        "entity": [
            {
                "service": "select/select_option",
                "entity_id": "select.deye_prog1_charge",
                "option": "Allow Grid"
                # Auto-verifies state = "Allow Grid"
            }
        ]
    },
    "stop": {
        "entity": [
            {
                "service": "select/select_option",
                "entity_id": "select.deye_prog1_charge",
                "option": "No Grid or Gen"
            }
        ]
    }
}
```

## Example 5: Number Entity with Custom Verification

Use `value_check` when the returned state differs from the set value:

```python
"device": {
    "start": {
        "entity": [
            {
                "service": "number/set_value",
                "entity_id": "number.device_capacity",
                "value": 80,
                "value_check": "80.0"  # If device returns string with decimal
            }
        ]
    }
}
```

## Example 6: Device with Minimum and Maximum Limits

Ensure the calculated value stays within device limits:

```python
"device": {
    "enable_load_management": True,
    "load_management": {
        "apply_limit_actions": {
            "entity": [
                {
                    "service": "number/set_value",
                    "entity_id": "number.device_power_limit",
                    # Clamp between 500W minimum and 5000W maximum
                    "value": "{max(500, min(limit_watts, 5000))}"
                }
            ]
        }
    }
}
```

## Example 7: Percentage-Based Control

Convert absolute watts to percentage of device capacity:

```python
"device": {
    "enable_load_management": True,
    "load_management": {
        "apply_limit_actions": {
            "entity": [
                {
                    "service": "number/set_value",
                    "entity_id": "number.device_power_percentage",
                    # Convert to percentage of 5000W max capacity
                    "value": "{round(limit_watts / 5000 * 100)}"
                }
            ]
        }
    }
}
```

## Example 8: Combined MQTT Payload with JSON

Create complex payloads with calculations:

```python
"device": {
    "enable_load_management": True,
    "load_management": {
        "apply_limit_actions": {
            "mqtt": [
                {
                    "topic": "device/config",
                    "payload": '{"power": {limit_watts}, "current": {round(limit_watts / 230, 1)}, "three_phase": {three_phase}}'
                }
            ]
        }
    }
}
```

Note: JSON payloads work naturally with the new syntax.

## Tips and Best Practices

1. **Always round current values**: EV chargers and devices typically expect 1 decimal place
   ```python
   "value": "{round(limit_watts / 230, 1)}"
   ```

2. **Use int() for whole numbers**: Some devices don't accept decimal values
   ```python
   "value": "{int(limit_watts)}"
   ```

3. **Apply safety margins**: Leave some headroom for device protection
   ```python
   "value": "{limit_watts * 0.95}"  # 95% of calculated limit
   ```

4. **Add units for clarity**: Include unit suffixes in MQTT payloads
   ```python
   "payload": "{limit_watts}W"
   "payload": "Power: {limit_watts}W, Current: {round(limit_watts / 230, 1)}A"
   ```

5. **Test expressions first**: Use the test script to verify calculations
   ```bash
   python3 test_expressions.py
   ```

6. **Keep expressions simple**: Complex logic should be in code, not config
