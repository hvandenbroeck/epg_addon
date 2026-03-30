"""Optimization algorithms for energy devices.

This package contains optimization algorithms for different device types:
- thermal: Heat pump and hot water optimization (MILP-based)
- battery: Battery charge/discharge optimization (price threshold-based)
- ev: EV charging optimization (simple threshold-based)
- battery_limiter: SOC-aware battery cycle limiting
"""

from .thermal import optimize_thermal_device, optimize_wp, optimize_hw
from .battery import optimize_battery, optimize_bat_discharge
from .ev import optimize_ev
from .battery_limiter import limit_battery_cycles

__all__ = [
    'optimize_thermal_device',
    'optimize_wp', 
    'optimize_hw',
    'optimize_battery',
    'optimize_bat_discharge',
    'optimize_ev',
    'limit_battery_cycles',
]
