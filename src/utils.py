from datetime import datetime, timedelta
import logging
from collections import defaultdict
import ast
import re
import math

logger = logging.getLogger(__name__)

def ensure_list(value):
    """Ensure value is a list."""
    if isinstance(value, list):
        return value
    elif isinstance(value, dict):
        return [value]
    elif value is None:
        return []
    else:
        logger.warning(f"⚠️ Unexpected config entry: {value}")
        return []


def evaluate_expression(expr, context):
    """Evaluate a string expression with placeholder substitution and safe math evaluation.
    
    Only content within curly brackets is evaluated as expressions. Variables within brackets
    don't need their own brackets. Text outside brackets is kept as-is.
    
    Args:
        expr: Expression string (e.g., "{limit_watts / 230}" or "{max(500, limit_watts)}W")
        context: Dictionary of variable names to values for substitution
        
    Returns:
        Evaluated result (number, string, etc.) or original expression if evaluation fails
        
    Examples:
        >>> evaluate_expression("{limit_watts / 230}", {"limit_watts": 3450})
        15.0
        >>> evaluate_expression("{round(limit_amps, 1)}", {"limit_amps": 15.217})
        15.2
        >>> evaluate_expression("{limit_watts * 0.8}W", {"limit_watts": 1000})
        '800.0W'
        >>> evaluate_expression("{max(500, limit_watts)}", {"limit_watts": 3450})
        3450
        >>> evaluate_expression("{sqrt(limit_watts)}", {"limit_watts": 10000})
        100.0
    """
    if not isinstance(expr, str):
        return expr
    
    # Find all expressions within curly brackets
    # Pattern matches {expression} where expression can contain anything except unmatched braces
    pattern = r'\{([^{}]+)\}'
    
    # Check if there are any expressions to evaluate
    matches = re.findall(pattern, expr)
    if not matches:
        return expr
    
    # If the entire string is a single expression, return the evaluated result directly
    single_expr_match = re.fullmatch(pattern, expr)
    if single_expr_match:
        expression = single_expr_match.group(1)
        result = _evaluate_single_expression(expression, context)
        return result
    
    # Otherwise, replace each expression in the string
    def replacer(match):
        expression = match.group(1)
        result = _evaluate_single_expression(expression, context)
        return str(result)
    
    try:
        result = re.sub(pattern, replacer, expr)
        return result
    except Exception as e:
        logger.debug(f"Could not evaluate expression '{expr}': {e}")
        return expr


def _evaluate_single_expression(expression, context):
    """Evaluate a single expression with context variables.
    
    Args:
        expression: Expression string without curly brackets (e.g., "limit_watts / 230")
        context: Dictionary of variable names to values
        
    Returns:
        Evaluated result or original expression if evaluation fails
    """
    # Replace variable names with their values
    # We need to match whole words to avoid partial replacements
    for var_name, var_value in context.items():
        # Use word boundaries to match complete variable names
        expression = re.sub(r'\b' + re.escape(var_name) + r'\b', str(var_value), expression)
    
    # Try to safely evaluate the expression
    try:
        # Parse the expression into an AST
        node = ast.parse(expression, mode='eval')
        
        # Define safe functions
        safe_funcs = {
            'round': round,
            'int': int,
            'float': float,
            'abs': abs,
            'min': min,
            'max': max,
            'sqrt': math.sqrt,
        }
        
        # Evaluate the AST with only safe operations
        result = _eval_node(node.body, safe_funcs)
        return result
    except (ValueError, SyntaxError, TypeError, AttributeError, KeyError) as e:
        logger.debug(f"Could not evaluate expression '{expression}': {e}")
        # Return the expression as-is if it can't be evaluated
        return expression


def _eval_node(node, safe_funcs):
    """Recursively evaluate an AST node with only safe operations."""
    if isinstance(node, ast.Num):  # Python < 3.8
        return node.n
    elif isinstance(node, ast.Constant):  # Python >= 3.8
        return node.value
    elif isinstance(node, ast.BinOp):
        left = _eval_node(node.left, safe_funcs)
        right = _eval_node(node.right, safe_funcs)
        if isinstance(node.op, ast.Add):
            return left + right
        elif isinstance(node.op, ast.Sub):
            return left - right
        elif isinstance(node.op, ast.Mult):
            return left * right
        elif isinstance(node.op, ast.Div):
            return left / right
        elif isinstance(node.op, ast.FloorDiv):
            return left // right
        elif isinstance(node.op, ast.Mod):
            return left % right
        elif isinstance(node.op, ast.Pow):
            return left ** right
        else:
            raise TypeError(f"Unsupported binary operator: {node.op}")
    elif isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, safe_funcs)
        if isinstance(node.op, ast.UAdd):
            return +operand
        elif isinstance(node.op, ast.USub):
            return -operand
        else:
            raise TypeError(f"Unsupported unary operator: {node.op}")
    elif isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in safe_funcs:
            func = safe_funcs[node.func.id]
            args = [_eval_node(arg, safe_funcs) for arg in node.args]
            return func(*args)
        else:
            raise TypeError(f"Function call not allowed: {node.func}")
    else:
        raise TypeError(f"Unsupported node type: {type(node)}")


def slot_to_time(index, slot_minutes):
    """Convert slot index to time string."""
    hours = (index * slot_minutes) // 60
    minutes = (index * slot_minutes) % 60
    return f"{hours:02d}:{minutes:02d}"

def get_block_len(device):
    """Get block length in minutes for each device type."""
    if device == "wp":
        return 2 * 60
    if device == "hw":
        return 1 * 60
    if device == "bat_charge":
        return 15
    if device == "bat_discharge":
        return 1 * 60
    return 15

from datetime import datetime, timedelta

def slots_to_iso_ranges(times, device, target_date):
    """Return list of (start_iso, stop_iso) for each time slot without merging."""
    if not times:
        return []

    block_len = get_block_len(device)

    # parse and sort times robustly (so result is in chronological order)
    def _to_dt(t):
        hour, minute = map(int, t.split(":"))
        return datetime.combine(target_date, datetime.min.time()) + timedelta(hours=hour, minutes=minute)

    slots = sorted(times, key=_to_dt)

    ranges = []
    for t in slots:
        start = _to_dt(t)
        stop = start + timedelta(minutes=block_len)
        ranges.append({ "device": device,
                        "start": start.isoformat(),
                        "stop": stop.isoformat() })

    return ranges


from datetime import datetime
from collections import defaultdict

def merge_sequential_timeslots(timeslot_lists):
    # Step 1: Group all timeslots by device (for merging only)
    device_slots = defaultdict(list)
    for timeslots in timeslot_lists:
        for slot in timeslots:
            device_slots[slot['device']].append(slot)
    
    # Step 2: For each device, merge sequential timeslots
    merged = []
    for device, slots in device_slots.items():
        # Sort slots by start time
        sorted_slots = sorted(slots, key=lambda x: x['start'])
        for slot in sorted_slots:
            start = datetime.fromisoformat(slot['start'])
            stop = datetime.fromisoformat(slot['stop'])
            if not merged or merged[-1]['device'] != device or start != datetime.fromisoformat(merged[-1]['stop']):
                merged.append({'device': device, 'start': slot['start'], 'stop': slot['stop']})
            else:
                # Merge if sequential and same device
                merged[-1]['stop'] = slot['stop']
    return merged