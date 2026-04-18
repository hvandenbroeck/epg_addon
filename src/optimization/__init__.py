"""Optimization algorithms for energy devices.

This package contains optimization algorithms for different device types:
- thermal: Heat pump and hot water optimization (MILP-based)
- battery: Battery charge/discharge optimization (price threshold-based)
- ev: EV charging optimization (simple threshold-based)
- ev_solar_charge: Solar-surplus-based EV charge controller
- battery_limiter: SOC-aware battery cycle limiting
"""

from .thermal import optimize_thermal_device, optimize_wp, optimize_hw
from .battery import optimize_battery, optimize_bat_discharge
from .ev import optimize_ev
from .ev_solar_charge import EvSolarChargeController
from .battery_limiter import limit_battery_cycles
from .battery_min_soc import categorize_slots_by_price

__all__ = [
    'optimize_thermal_device',
    'optimize_wp', 
    'optimize_hw',
    'optimize_battery',
    'optimize_bat_discharge',
    'optimize_ev',
    'EvSolarChargeController',
    'limit_battery_cycles',
    'categorize_slots_by_price',
]
