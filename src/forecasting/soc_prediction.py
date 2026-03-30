"""Battery SOC trajectory prediction.

Provides a slot-by-slot battery state-of-charge simulation that can be used
both during optimization planning and for generating forward-looking SOC
forecasts to display in the UI.
"""
import logging

logger = logging.getLogger(__name__)


def compute_soc_trajectory(
    charge_slots: set[int],
    discharge_slots: set[int],
    current_soc: float,
    battery_capacity_kwh: float,
    battery_charge_speed_kw: float,
    min_soc_percent: float,
    max_soc_percent: float,
    slot_minutes: int,
    start_slot: int,
    total_slots: int,
    usage_by_slot: dict | None = None,
    solar_by_slot: dict | None = None,
    solar_only_slots: set[int] | None = None,
) -> list[tuple[int, float]]:
    """Simulate battery SOC slot-by-slot over a range of slots.

    Iterates over every slot in [start_slot, total_slots] (inclusive) and
    applies charging, discharging, or solar-only passive charging as appropriate.

    Args:
        charge_slots: Set of slot indices scheduled for grid charging.
        discharge_slots: Set of slot indices scheduled for discharge (covers both
            regular and deep discharge).
        current_soc: Initial battery state of charge in percent (0–100).
        battery_capacity_kwh: Battery capacity in kWh.
        battery_charge_speed_kw: Maximum charge (and reference discharge) speed in kW.
        min_soc_percent: Minimum allowed SOC in percent.
        max_soc_percent: Maximum allowed SOC in percent.
        slot_minutes: Duration of each time slot in minutes.
        start_slot: First slot index to include in the output.
        total_slots: Total number of slots in the horizon (exclusive upper bound for
            SOC updates; an output record is produced for slot ``total_slots`` but no
            update is applied after it).
        usage_by_slot: Mapping of slot_idx → kWh of household consumption per slot.
        solar_by_slot: Mapping of slot_idx → kWh of solar production per slot.
        solar_only_slots: Set of slot indices where passive solar charging is active.
            Excess solar (production minus consumption) charges the battery up to the
            charge speed and max SOC cap.

    Returns:
        List of ``(slot_idx, soc_percent)`` tuples for every slot in
        ``[start_slot, total_slots]``.
    """
    slot_hours = slot_minutes / 60
    charge_energy_per_slot = battery_charge_speed_kw * slot_hours
    _usage = usage_by_slot or {}
    _solar = solar_by_slot or {}
    _solar_only = solar_only_slots or set()

    soc = float(current_soc)
    result: list[tuple[int, float]] = []

    for slot_idx in range(start_slot, total_slots + 1):
        result.append((slot_idx, round(soc, 1)))

        if slot_idx >= total_slots:
            break

        if slot_idx in charge_slots:
            headroom = battery_capacity_kwh * max(0.0, max_soc_percent - soc) / 100
            energy = min(charge_energy_per_slot, headroom)
            soc = min(max_soc_percent, soc + (energy / battery_capacity_kwh) * 100)

        elif slot_idx in discharge_slots:
            available = battery_capacity_kwh * max(0.0, soc - min_soc_percent) / 100
            gross_usage = _usage.get(slot_idx, 0.0)
            solar_offset = _solar.get(slot_idx, 0.0)
            net_usage = max(0.0, gross_usage - solar_offset)
            discharge_energy = min(net_usage, available)
            soc = max(min_soc_percent, soc - (discharge_energy / battery_capacity_kwh) * 100)

        elif slot_idx in _solar_only:
            # Passive solar charging: excess solar (beyond consumption) charges the battery
            gross_usage = _usage.get(slot_idx, 0.0)
            solar = _solar.get(slot_idx, 0.0)
            excess_solar = max(0.0, solar - gross_usage)
            headroom = battery_capacity_kwh * max(0.0, max_soc_percent - soc) / 100
            energy = min(excess_solar, charge_energy_per_slot, headroom)
            soc = min(max_soc_percent, soc + (energy / battery_capacity_kwh) * 100)

    return result
