# ✅ Expression Evaluation Implementation Complete

## What Was Implemented

Mathematical expression support has been added to all `value` and `payload` fields in device configurations. You can now perform calculations directly in your device configuration instead of hardcoding values.

## Key Features

✅ **Safe Expression Evaluation** - Uses Python's AST module (no arbitrary code execution)  
✅ **Placeholder Substitution** - Replace `{limit_watts}`, `{limit_amps}`, etc. with actual values  
✅ **Mathematical Operators** - `+`, `-`, `*`, `/`, `//`, `%`, `**`  
✅ **Safe Functions** - `round()`, `int()`, `float()`, `abs()`, `min()`, `max()`  
✅ **Backward Compatible** - All existing configs continue to work  
✅ **Error Handling** - Invalid expressions fall back to original values  

## Files Modified

1. **src/utils.py** - Added `evaluate_expression()` function
2. **src/devices.py** - Updated to evaluate expressions in actions
3. **src/load_watcher/limit_applier.py** - Simplified to use expression evaluator
4. **src/devices_config.py** - Updated EV charger config with example expressions

## Documentation Created

- `EXPRESSION_EXAMPLES.md` - Comprehensive guide with examples
- `CONFIGURATION_EXAMPLES.md` - Real-world configuration patterns
- `EXPRESSION_QUICK_REF.md` - Quick reference table
- `IMPLEMENTATION_SUMMARY.md` - Technical implementation details
- `test_expressions.py` - Test script to verify functionality

## Quick Example

**New Syntax:** Only content within `{}` is evaluated. Variables don't need their own brackets.

**Before:**
```python
"value": "{limit_watts}"
```

**Now you can:**
```python
"value": "{round(limit_watts / 230, 1)}"  # Convert W to A, rounded
"value": "{limit_watts * 0.85}"           # Apply 85% efficiency
"value": "{max(500, min(limit_watts, 5000))}"  # Clamp between limits
"value": "{limit_watts}W"                 # Add unit suffix
"payload": "Power: {limit_watts}W, Current: {round(limit_watts / 230, 1)}A"  # Multiple expressions
```

## How to Use

### 1. In Device Configuration (devices_config.py)

```python
"apply_limit_actions": {
    "entity": [
        {
            "service": "number/set_value",
            "entity_id": "number.device_current",
            "value": "{round(limit_watts / 230, 1)}"  # Expression here!
        }
    ],
    "mqtt": [
        {
            "topic": "device/power",
            "payload": "{int(limit_watts)}W"  # With unit suffix!
        }
    ]
}
```

### 2. Test Your Expressions

```bash
python3 test_expressions.py
```

Output:
```
Expression: {limit_watts / 230}       => 15.0 (float)
Expression: {round(limit_amps, 1)}    => 15.2 (float)
Expression: {int(limit_watts)}        => 3450 (int)
Expression: {limit_watts}W            => 3450W (str)
```

## Available Variables

When limit applier runs, these variables are available within `{}`:
- `limit_watts` - Power limit in watts (e.g., 3450)
- `limit_amps` - Current limit in amps (e.g., 15.217)
- `three_phase` - 1 if three-phase, 0 otherwise
- `single_phase` - 1 if single-phase, 0 otherwise

**Note:** Variables within brackets don't need their own brackets. Use `{limit_watts}` not `{{limit_watts}}`.

## Common Use Cases

### Convert Watts to Amps
```python
"value": "{round(limit_watts / 230, 1)}"
```

### Apply Efficiency Factor
```python
"value": "{limit_watts * 0.9}"  # 90% efficiency
```

### Ensure Minimum Value
```python
"value": "{max(500, limit_watts)}"  # At least 500W
```

### Integer Conversion with Unit
```python
"payload": "{int(limit_watts)}W"
```

### Multiple Values in One String
```python
"payload": "Power: {limit_watts}W, Current: {round(limit_watts / 230, 1)}A"
```

## Safety

✅ Only safe mathematical operations allowed  
✅ No file access, network access, or system calls  
✅ Invalid expressions return original value  
✅ All errors are logged for debugging  

## Testing

All Python files compile without errors:
```bash
✅ src/utils.py
✅ src/devices.py  
✅ src/load_watcher/limit_applier.py
✅ src/devices_config.py
```

Expression evaluation test passed:
```bash
✅ 3450W / 230V = 15.0A
```

## Next Steps

1. Update your device configurations with expressions as needed
2. Test with `python3 test_expressions.py` to verify calculations
3. Refer to `EXPRESSION_QUICK_REF.md` for syntax reference
4. Check `CONFIGURATION_EXAMPLES.md` for real-world patterns

## Support

For expression syntax help, see: `EXPRESSION_QUICK_REF.md`  
For configuration examples, see: `CONFIGURATION_EXAMPLES.md`  
For implementation details, see: `IMPLEMENTATION_SUMMARY.md`
