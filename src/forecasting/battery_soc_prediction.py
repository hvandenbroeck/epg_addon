"""Battery State of Charge (SOC) prediction.

Simulates the battery SOC over the optimization horizon based on scheduled
charge/discharge actions, predicted power usage, and predicted solar production.
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def predict_battery_soc(
    charge_times: list[str],
    discharge_times: list[str],
    slot_minutes: int,
    horizon_start: datetime,
    horizon_end: datetime,
    current_soc: float,
    battery_capacity_kwh: float,
    battery_charge_speed_kw: float,
    min_soc_percent: float,
    max_soc_percent: float,
    device_name: str = "battery",
    predicted_power_usage: list[dict] | None = None,
    predicted_solar: list[dict] | None = None,
) -> list[dict]:
    """Simulate battery SOC over the optimization horizon.

    Returns a list of dicts with 'timestamp' (ISO string) and 'soc_percent' (float),
    covering each time slot from the current time through horizon_end.

    The simulation starts with current_soc at the current time slot and projects
    the SOC forward based on scheduled charge/discharge actions and predicted
    power consumption and solar production.

    Args:
        charge_times: Charge slot start times in HH:MM format relative to horizon_start.
        discharge_times: Discharge slot start times in HH:MM format relative to horizon_start.
        slot_minutes: Duration of each time slot in minutes.
        horizon_start: Start datetime of the optimization horizon.
        horizon_end: End datetime of the optimization horizon.
        current_soc: Current battery state of charge in percent (0–100).
        battery_capacity_kwh: Total battery capacity in kWh.
        battery_charge_speed_kw: Battery charge/discharge speed in kW.
        min_soc_percent: Minimum allowed battery SOC in percent.
        max_soc_percent: Maximum allowed battery SOC in percent.
        device_name: Name used in log messages.
        predicted_power_usage: Per-slot power usage predictions as a list of dicts
            with 'timestamp' and 'predicted_kwh' keys.  The 'predicted_kwh' values
            are expected in the same unit as produced by the optimizer's
            ``_get_predicted_usage`` helper (kWh per slot).
        predicted_solar: Per-slot solar production predictions in the same format
            and unit as *predicted_power_usage*.

    Returns:
        List of ``{'timestamp': <ISO string>, 'soc_percent': <float>}`` dicts, one
        entry per slot from the current slot through the last slot before horizon_end.
    """
    slot_hours = slot_minutes / 60
    charge_energy_per_slot = battery_charge_speed_kw * slot_hours

    # Convert HH:MM strings to slot indices relative to horizon_start
    def time_to_slot_idx(time_str: str) -> int:
        hour, minute = map(int, time_str.split(":"))
        return (hour * 60 + minute) // slot_minutes

    charge_slots = {time_to_slot_idx(t) for t in (charge_times or [])}
    discharge_slots = {time_to_slot_idx(t) for t in (discharge_times or [])}

    # Build per-slot predicted usage and solar lookup (slot_idx → kWh per slot)
    horizon_start_naive = horizon_start.replace(tzinfo=None)

    def _pred_time_to_slot(pred_time_raw) -> int:
        t = pred_time_raw
        if isinstance(t, str):
            t = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if hasattr(t, "tzinfo") and t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        return int((t - horizon_start_naive).total_seconds() / 60 / slot_minutes)

    usage_by_slot: dict[int, float] = {}
    if predicted_power_usage:
        for pred in predicted_power_usage:
            slot_idx = _pred_time_to_slot(pred["timestamp"])
            if slot_idx >= 0:
                usage_by_slot[slot_idx] = float(pred["predicted_kwh"])

    solar_by_slot: dict[int, float] = {}
    if predicted_solar:
        for pred in predicted_solar:
            slot_idx = _pred_time_to_slot(pred["timestamp"])
            if slot_idx >= 0:
                solar_by_slot[slot_idx] = float(pred["predicted_kwh"])

    # Determine the first slot to simulate (the current slot)
    now = datetime.now().replace(tzinfo=None)
    horizon_end_naive = horizon_end.replace(tzinfo=None)
    current_slot_idx = max(0, int((now - horizon_start_naive).total_seconds() / 60 / slot_minutes))
    total_slots = int((horizon_end_naive - horizon_start_naive).total_seconds() / 60 / slot_minutes)

    logger.debug(
        f"🔋 {device_name}: starting SOC simulation — "
        f"current_soc={current_soc:.1f}%, capacity={battery_capacity_kwh} kWh, "
        f"charge_speed={battery_charge_speed_kw} kW, slot={slot_minutes} min, "
        f"slots={current_slot_idx}–{total_slots - 1}, "
        f"charge_slots={sorted(charge_slots)}, discharge_slots={sorted(discharge_slots)}"
    )

    # Walk forward slot-by-slot, updating the SOC
    soc = current_soc
    results: list[dict] = []

    for slot_idx in range(current_slot_idx, total_slots):
        slot_time = horizon_start_naive + timedelta(minutes=slot_idx * slot_minutes)
        results.append({
            "timestamp": slot_time.isoformat(),
            "soc_percent": round(soc, 1),
        })

        if slot_idx in charge_slots:
            headroom = battery_capacity_kwh * (max_soc_percent - soc) / 100
            energy = min(charge_energy_per_slot, max(0.0, headroom))
            soc += (energy / battery_capacity_kwh) * 100
            logger.debug(
                f"🔋 {device_name}: slot {slot_idx} ({slot_time.strftime('%H:%M')}) "
                f"CHARGE +{energy:.3f} kWh → SOC {soc:.1f}%"
            )
        elif slot_idx in discharge_slots:
            gross_usage = usage_by_slot.get(slot_idx, 0.0)
            solar_offset = solar_by_slot.get(slot_idx, 0.0)
            net_energy = gross_usage - solar_offset
            if net_energy < 0:
                # Excess solar charges the battery even during a discharge slot
                excess_solar = -net_energy
                headroom = battery_capacity_kwh * (max_soc_percent - soc) / 100
                charge_energy = min(excess_solar, max(0.0, headroom), charge_energy_per_slot)
                soc += (charge_energy / battery_capacity_kwh) * 100
                logger.debug(
                    f"🔋 {device_name}: slot {slot_idx} ({slot_time.strftime('%H:%M')}) "
                    f"DISCHARGE (excess solar +{charge_energy:.3f} kWh) → SOC {soc:.1f}%"
                )
            else:
                available = battery_capacity_kwh * (soc - min_soc_percent) / 100
                discharge_energy = min(net_energy, max(0.0, available))
                soc -= (discharge_energy / battery_capacity_kwh) * 100
                logger.debug(
                    f"🔋 {device_name}: slot {slot_idx} ({slot_time.strftime('%H:%M')}) "
                    f"DISCHARGE -{discharge_energy:.3f} kWh "
                    f"(usage={gross_usage:.3f}, solar={solar_offset:.3f}) → SOC {soc:.1f}%"
                )

        # Keep SOC within valid bounds
        clamped_soc = max(min_soc_percent, min(max_soc_percent, soc))
        if clamped_soc != soc:
            logger.debug(
                f"🔋 {device_name}: slot {slot_idx} SOC clamped {soc:.1f}% → {clamped_soc:.1f}%"
            )
        soc = clamped_soc

    if results:
        final_soc = results[-1]["soc_percent"]
        logger.info(
            f"🔋 {device_name}: SOC prediction — {len(results)} slots, "
            f"start {current_soc:.1f}% → end {final_soc:.1f}%"
        )

    return results
