"""Optimization algorithms for energy devices.

This package contains optimization algorithms for different device types:
- thermal: Heat pump and hot water optimization (MILP-based)
- battery: Battery charge/discharge optimization (price threshold-based)
- ev_deadline: EV charging optimization (deadline-based cheapest-slot selection)
- battery_limiter: SOC-aware battery cycle limiting
"""

from .thermal import optimize_thermal_device, optimize_wp, optimize_hw
from .battery_lp import plan_battery_lp
from .thermal_preheat import plan_thermal_setpoints, build_comfort_floor
from .ev_deadline import plan_ev_deadline_charge, read_ev_deadline_inputs, EvDeadlinePlan

__all__ = [
    'optimize_thermal_device',
    'optimize_wp',
    'optimize_hw',
    'plan_battery_lp',
    'plan_thermal_setpoints',
    'build_comfort_floor',
    'plan_ev_deadline_charge',
    'read_ev_deadline_inputs',
    'EvDeadlinePlan',
]
