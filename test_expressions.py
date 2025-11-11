#!/usr/bin/env python3
"""Test script to demonstrate expression evaluation functionality."""

import sys
sys.path.insert(0, '/root/addons/epg_addon/src')

from utils import evaluate_expression

def test_expressions():
    """Test various expression patterns."""
    
    print("=" * 60)
    print("Expression Evaluation Tests")
    print("=" * 60)
    
    # Test context
    context = {
        'limit_watts': 3450,
        'limit_amps': 15.217,
        'three_phase': 1,
        'single_phase': 0
    }
    
    test_cases = [
        # (expression, description)
        ("{limit_watts}", "Simple variable"),
        ("{limit_watts / 230}", "Convert watts to amps"),
        ("{round(limit_amps, 1)}", "Round to 1 decimal"),
        ("{int(limit_watts / 230)}", "Convert to integer"),
        ("{limit_watts * 0.85}", "Apply efficiency factor"),
        ("{round(limit_watts / 230 * 0.9, 1)}", "Complex calculation"),
        ("{min(limit_watts, 3000)}", "Minimum value"),
        ("{max(limit_watts, 2000)}", "Maximum value"),
        ("{abs(-limit_watts)}", "Absolute value"),
        ("{limit_watts // 100}", "Floor division"),
        ("{limit_watts % 1000}", "Modulo operation"),
        ("{three_phase}", "Boolean flag"),
        ("{limit_watts}W", "Value with unit suffix"),
        ("{round(limit_watts / 230, 1)}A", "Amps with unit"),
        ("Power: {limit_watts}W, Current: {round(limit_watts / 230, 1)}A", "Multiple expressions"),
        ("{max(500, limit_watts)}W minimum", "Expression with text"),
        ("Set to {int(limit_watts * 0.9)} (90% efficiency)", "Complex text with expression"),
        ("42", "Plain number"),
        ("hello", "Plain string (no expression)"),
    ]
    
    print(f"\nContext: {context}\n")
    
    for expr, desc in test_cases:
        result = evaluate_expression(expr, context)
        result_type = type(result).__name__
        print(f"Expression: {expr:40} => {result:>10} ({result_type})")
        print(f"  └─ {desc}")
        print()
    
    print("=" * 60)
    print("All tests completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    test_expressions()
