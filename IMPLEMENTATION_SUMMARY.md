# Expression Evaluation Implementation Summary

## Overview
Implemented support for mathematical expressions in device configuration `value` and `payload` fields using Python's `ast.literal_eval` approach with safe AST node evaluation.

## Changes Made

### 1. `src/utils.py`
- Added `import ast` and `import re`
- Added `evaluate_expression(expr, context)` function:
  - Replaces placeholders (e.g., `{limit_watts}`) with context values
  - Safely evaluates mathematical expressions using AST parsing
  - Supports basic arithmetic operators: `+`, `-`, `*`, `/`, `//`, `%`, `**`
  - Supports safe functions: `round()`, `int()`, `float()`, `abs()`, `min()`, `max()`, `sqrt()`
  - Returns evaluated result or original value if evaluation fails
- Added `_eval_node(node, safe_funcs)` helper function:
  - Recursively evaluates AST nodes with only safe operations
  - Prevents arbitrary code execution

### 2. `src/devices.py`
- Imported `evaluate_expression` from utils
- Updated `execute_device_action()` method:
  - Added optional `context` parameter for expression evaluation
  - Evaluates expressions in `payload` fields for MQTT actions
  - Evaluates expressions in `value` and `option` fields for entity actions
  - Passes context to expression evaluator

### 3. `src/load_watcher/limit_applier.py`
- Imported `evaluate_expression` from utils
- Updated `apply_device_limits()` method:
  - Creates context dictionary with limit values
  - Passes context directly to `execute_device_action()`
- Removed `_process_actions()` method (no longer needed)

### 4. `src/devices_config.py`
- Updated EV charger configuration to demonstrate expression usage:
  - Changed `"value": "{limit_amps}"` to `"value": "round({limit_watts} / 230, 1)"`
  - Changed `"payload": "{limit_watts}"` to `"payload": "int({limit_watts})"`

### 5. Documentation Files Created
- `EXPRESSION_EXAMPLES.md`: Comprehensive guide with examples
- `test_expressions.py`: Test script demonstrating functionality

## Usage Examples

### Convert Watts to Amps
```python
"value": "{limit_watts} / 230"
# 3450W → 15.0A
```

### Round to Specific Precision
```python
"value": "round({limit_amps}, 1)"
# 15.217A → 15.2A
```

### Convert to Integer
```python
"payload": "int({limit_watts})"
# 3450.5W → 3450
```

### Apply Efficiency Factor
```python
"value": "{limit_watts} * 0.85"
# 1000W → 850.0W
```

### Complex Expression
```python
"value": "round({limit_watts} / 230 * 0.9, 1)"
# 3450W → 13.5A (after conversion and efficiency)
```

### Square Root
```python
"value": "{sqrt(limit_watts)}"
# 10000W → 100.0
```

## Available Context Variables
- `limit_watts`: Power limit in watts
- `limit_amps`: Power limit in amps
- `three_phase`: 1 if three-phase mode, 0 otherwise
- `single_phase`: 1 if single-phase mode, 0 otherwise

## Safety Features
- Only safe mathematical operations allowed
- No arbitrary code execution possible
- Invalid expressions fall back to original value
- Errors are caught and logged
- Uses Python's AST module for secure parsing

## Testing
Run the test script to verify functionality:
```bash
python3 test_expressions.py
```

## Backward Compatibility
- Simple placeholders like `{limit_watts}` still work as before
- Plain values (numbers, strings) continue to work unchanged
- All existing configurations remain functional
