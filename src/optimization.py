"""Backward-compatible re-exports from the optimization package.

This module maintains backward compatibility by re-exporting all optimization
functions from the new src/optimization/ package structure.

The actual implementations have been moved to:
- src/optimization/thermal.py: Heat pump and hot water optimization
- src/optimization/battery.py: Battery charge/discharge optimization
- src/optimization/ev.py: EV charging optimization
- src/optimization/battery_limiter.py: SOC-aware battery cycle limiting

For new code, prefer importing directly from src.optimization:
    from src.optimization import optimize_wp, optimize_battery, ...
"""

# Re-export all functions for backward compatibility
from .optimization import (
    optimize_thermal_device,
    optimize_wp,
    optimize_hw,
    optimize_battery,
    optimize_bat_discharge,
    optimize_ev,
    limit_battery_cycles,
)

__all__ = [
    'optimize_thermal_device',
    'optimize_wp',
    'optimize_hw',
    'optimize_battery',
    'optimize_bat_discharge',
    'optimize_ev',
    'limit_battery_cycles',
]