"""Optimization algorithms for energy devices.

This package contains optimization algorithms for different device types:
- thermal: Heat pump and hot water optimization (MILP-based)
- battery: Battery charge/discharge optimization (price threshold-based)
- ev_deadline: EV charging optimization (deadline-based cheapest-slot selection)
- battery_limiter: SOC-aware battery cycle limiting
"""

from .thermal import optimize_thermal_device, optimize_wp, optimize_hw
from .battery import optimize_battery, optimize_bat_discharge
from .battery_limiter import limit_battery_cycles
from .ev_deadline import plan_ev_deadline_charge, read_ev_deadline_inputs

__all__ = [
    'optimize_thermal_device',
    'optimize_wp',
    'optimize_hw',
    'optimize_battery',
    'optimize_bat_discharge',
    'limit_battery_cycles',
    'plan_ev_deadline_charge',
    'read_ev_deadline_inputs',
]
