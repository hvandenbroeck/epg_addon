# Expression Evaluation Quick Reference

## Syntax
```python
"value": "{expression}"        # For entity actions
"payload": "{expression}"      # For MQTT actions
"value": "{expression}W"       # With literal text suffix
"payload": "Power: {expr1}W, Current: {expr2}A"  # Multiple expressions
```

**Important:** Only content within `{}` is evaluated as an expression. Variables within brackets don't need their own brackets.

## Placeholders
| Placeholder | Description | Example Value |
|------------|-------------|---------------|
| `limit_watts` | Power limit in watts | 3450 |
| `limit_amps` | Current limit in amps | 15.217 |
| `three_phase` | Three-phase mode flag | 0 or 1 |
| `single_phase` | Single-phase mode flag | 0 or 1 |

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
| `round(x, n)` | Round to n decimals | `{round(limit_amps, 1)}` → 15.2 |
| `int(x)` | Convert to integer | `{int(limit_watts)}` → 3450 |
| `float(x)` | Convert to float | `{float(limit_watts)}` → 3450.0 |
| `abs(x)` | Absolute value | `{abs(-limit_watts)}` → 3450 |
| `min(a, b)` | Minimum value | `{min(limit_watts, 3000)}` → 3000 |
| `max(a, b)` | Maximum value | `{max(limit_watts, 2000)}` → 3450 |
| `sqrt(x)` | Square root | `{sqrt(limit_watts)}` → 100.0 |

## Common Patterns

### Watts to Amps (230V)
```python
"value": "{limit_watts / 230}"
```

### Watts to Amps, rounded
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

### Clamp between min/max
```python
"value": "{max(500, min(limit_watts, 5000))}"
```

### Convert to percentage
```python
"value": "{round(limit_watts / 5000 * 100)}"
```

### With unit suffix
```python
"value": "{limit_watts}W"
"payload": "{round(limit_watts / 230, 1)}A"
```

### Multiple expressions
```python
"payload": "Power: {limit_watts}W, Current: {round(limit_watts / 230, 1)}A"
```

## Examples by Use Case

### ⚡ EV Charger
```python
"value": "{round(limit_watts / 230, 1)}"  # Watts → Amps
"payload": "{int(limit_watts)}W"          # With unit
```

### 🔋 Battery
```python
"value": "{int(limit_watts * 0.9)}"       # Apply 90% efficiency
"payload": "{limit_watts}W at {round(limit_watts / 230, 1)}A"  # Descriptive
```

### 🌡️ Heat Pump
```python
"payload": "{round(20 + limit_watts / 1000 * 2, 1)}"  # Power → Temp
```

### 🔌 Generic Device
```python
"value": "{max(100, limit_watts)}"        # Minimum 100W
"payload": "Limit: {limit_watts}W"        # With label
```

## Notes
- Expressions are evaluated at runtime when actions are executed
- Only content within `{}` is evaluated; text outside is kept as-is
- Variables within brackets don't need their own brackets (use `limit_watts` not `{limit_watts}`)
- Invalid expressions fall back to the original string
- Only safe mathematical operations are allowed
- No arbitrary code execution is possible

### Square root
```python
"value": "{sqrt(limit_watts)}"
```
