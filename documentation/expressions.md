# Expressions in Device Configuration

Device configuration `value` and `payload` fields support mathematical expressions. This lets you perform calculations (e.g. unit conversions, clamping, rounding) directly in your configuration without changing code.

**Syntax:** Wrap expressions in curly brackets `{}`. Text outside brackets is kept as-is. Variables inside brackets do **not** need their own brackets.

```python
"value": "{limit_watts / 230}"          # pure expression
"value": "{round(limit_watts / 230, 1)}"  # with function
"value": "{limit_watts}W"               # expression + literal suffix
"payload": "Power: {limit_watts}W, Current: {round(limit_watts / 230, 1)}A"  # multiple
```

## Available Variables

| Variable | Description | Example value |
|----------|-------------|---------------|
| `limit_watts` | Power limit in watts | `3450` |
| `limit_amps` | Current limit in amps | `15.217` |
| `three_phase` | `1` if three-phase mode, `0` otherwise | `0` |
| `single_phase` | `1` if single-phase mode, `0` otherwise | `1` |

## Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `+` | Addition | `{limit_watts + 100}` |
| `-` | Subtraction | `{limit_watts - 100}` |
| `*` | Multiplication | `{limit_watts * 0.9}` |
| `/` | Division | `{limit_watts / 230}` |
| `//` | Floor division | `{limit_watts // 100}` |
| `%` | Modulo | `{limit_watts % 1000}` |
| `**` | Power | `{limit_watts ** 2}` |

## Functions

| Function | Description | Example |
|----------|-------------|---------|
| `round(x, n)` | Round to `n` decimals | `{round(limit_amps, 1)}` → `15.2` |
| `int(x)` | Convert to integer | `{int(limit_watts)}` → `3450` |
| `float(x)` | Convert to float | `{float(limit_watts)}` → `3450.0` |
| `abs(x)` | Absolute value | `{abs(-limit_watts)}` → `3450` |
| `min(a, b)` | Minimum of two values | `{min(limit_watts, 3000)}` → `3000` |
| `max(a, b)` | Maximum of two values | `{max(limit_watts, 2000)}` → `3450` |
| `sqrt(x)` | Square root | `{sqrt(limit_watts)}` |

## Common Patterns

### Watts to amps (230 V single-phase)
```python
"value": "{round(limit_watts / 230, 1)}"
```

### Integer conversion
```python
"value": "{int(limit_watts)}"
```

### Apply efficiency factor
```python
"value": "{limit_watts * 0.85}"
```

### Clamp between min and max
```python
"value": "{max(500, min(limit_watts, 5000))}"
```

### Convert to percentage of rated capacity
```python
"value": "{round(limit_watts / 5000 * 100)}"
```

### Value with unit suffix
```python
"value": "{limit_watts}W"
"payload": "{round(limit_watts / 230, 1)}A"
```

### Multiple expressions in one string
```python
"payload": "Power: {limit_watts}W, Current: {round(limit_watts / 230, 1)}A"
```

## Use-Case Examples

### ⚡ EV Charger – watts to amps
```json
{
  "service": "number/set_value",
  "entity_id": "number.ev_charger_max_charging_current",
  "value": "{round(limit_watts / 230, 1)}"
}
```

### 🔋 Battery – apply efficiency
```json
{
  "service": "number/set_value",
  "entity_id": "number.battery_charge_limit",
  "value": "{round(limit_watts * 0.9)}"
}
```

### 📡 MQTT – integer payload with unit
```json
{
  "topic": "ev_charger/set_limit",
  "payload": "{int(limit_watts)}W"
}
```

### 📡 MQTT – composite JSON payload
```json
{
  "topic": "device/config",
  "payload": "{\"power\": {limit_watts}, \"current\": {round(limit_watts / 230, 1)}, \"three_phase\": {three_phase}}"
}
```

## Safety

- Expressions are evaluated using Python's `ast` module – no arbitrary code execution is possible.
- Only numeric literals, arithmetic operators, and the whitelisted functions listed above are allowed.
- Invalid or unsafe expressions fall back to the original string value; errors are logged.
- Division by zero and other runtime errors are caught silently.

## Implementation

The `evaluate_expression()` function in `src/utils.py`:
1. Finds all `{...}` blocks in the string.
2. Substitutes variable names with their current values.
3. Parses each expression into an Abstract Syntax Tree (AST).
4. Evaluates only safe mathematical nodes.
5. Returns the computed result or the original string on failure.
