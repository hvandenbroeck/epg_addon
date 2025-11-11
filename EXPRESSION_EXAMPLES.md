# Expression Support in Device Configuration

The device configuration now supports mathematical expressions in `value` and `payload` fields. This allows you to perform calculations on placeholder values before they are sent to devices.

**New Syntax:** Only content within curly brackets `{}` is evaluated as an expression. Variables within brackets don't need their own brackets. Text outside brackets is kept as literal text.

## Supported Features

### Variables (use within `{}` without additional brackets)
- `limit_watts` - Power limit in watts
- `limit_amps` - Power limit in amps
- `three_phase` - 1 if three-phase mode, 0 otherwise
- `single_phase` - 1 if single-phase mode, 0 otherwise

### Mathematical Operations
- Basic arithmetic: `+`, `-`, `*`, `/`, `//` (floor division), `%` (modulo), `**` (power)
- Functions: `round()`, `int()`, `float()`, `abs()`, `min()`, `max()`

## Examples

### Example 1: Convert Watts to Amps
```python
"value": "{limit_watts / 230}"
# If limit_watts = 3450, result = 15.0
```

### Example 2: Round to 1 Decimal Place
```python
"value": "{round(limit_amps, 1)}"
# If limit_amps = 15.217, result = 15.2
```

### Example 3: Apply Efficiency Factor
```python
"value": "{limit_watts * 0.85}"
# If limit_watts = 1000, result = 850.0
```

### Example 4: Convert to Integer
```python
"value": "{int(limit_watts / 230)}"
# If limit_watts = 3450, result = 15
```

### Example 5: Minimum Value
```python
"value": "{min(limit_watts, 3000)}"
# If limit_watts = 3500, result = 3000
```

### Example 6: Complex Expression
```python
"value": "{round(limit_watts / 230 * 0.9, 1)}"
# If limit_watts = 3450, result = 13.5 (3450/230*0.9 = 13.5)
```

### Example 7: With Unit Suffix
```python
"value": "{limit_watts}W"
# If limit_watts = 3450, result = "3450W"
```

### Example 8: Multiple Expressions
```python
"payload": "Power: {limit_watts}W, Current: {round(limit_watts / 230, 1)}A"
# If limit_watts = 3450, result = "Power: 3450W, Current: 15.0A"
```

## Usage in Device Configuration

### MQTT Action with Expression
```python
"mqtt": [
    {
        "topic": "device/set_current",
        "payload": "{round(limit_watts / 230, 1)}"
    }
]
```

### Entity Action with Expression and Unit
```python
"entity": [
    {
        "service": "number/set_value",
        "entity_id": "number.device_charge_limit",
        "value": "{limit_watts * 0.8}"
    }
]
```

### Real-World Example: EV Charger
```python
"ev": {
    "enable_load_management": True,
    "load_management": {
        "apply_limit_actions": {
            "entity": [
                {
                    "service": "number/set_value",
                    "entity_id": "number.ev_charger_max_charging_current",
                    "value": "{round(limit_watts / 230, 1)}"  # Convert W to A and round
                }
            ],
            "mqtt": [
                {
                    "topic": "ev_charger/set_limit",
                    "payload": "{int(limit_watts)}"  # Send as integer
                },
                {
                    "topic": "ev_charger/status",
                    "payload": "Charging at {limit_watts}W ({round(limit_watts / 230, 1)}A)"
                }
            ]
        }
    }
}
```

## Safety Notes

- Expressions are evaluated using Python's `ast` module with only safe operations allowed
- Invalid expressions will fall back to the original string value
- Division by zero and other errors are caught and logged
- Only numeric literals, arithmetic operations, and whitelisted functions are allowed
- No arbitrary code execution is possible

## Implementation Details

The expression evaluation is handled by the `evaluate_expression()` function in `src/utils.py`. It:
1. Finds all expressions within curly brackets `{}`
2. Replaces variable names with actual values within each expression
3. Parses each expression into an Abstract Syntax Tree (AST)
4. Evaluates only safe mathematical operations
5. Returns the computed result or the original value if evaluation fails
6. Supports mixing literal text with expressions in the same string
