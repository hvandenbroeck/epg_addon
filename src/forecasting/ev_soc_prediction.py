"""EV State of Charge (SOC) prediction for deadline-charging mode.

Simulates the EV's SOC forward over the optimization horizon given a set of planned
charge slots, mirroring forecasting/battery_soc_prediction.py's walk-forward pattern but
simplified to the charge-only case (no discharge/solar offset — the EV is not a house
battery and deadline-mode charging never discharges).
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def predict_ev_soc(
    charge_times: list[str],
    slot_minutes: int,
    horizon_start: datetime,
    horizon_end: datetime,
    current_soc: float,
    ev_battery_capacity_kwh: float,
    charge_power_kw: float,
    device_name: str = "ev",
) -> list[dict]:
    """Simulate EV SOC over the optimization horizon.

    Returns a list of ``{'timestamp': <ISO string>, 'soc_percent': <float>}`` dicts, one
    entry per slot from the current slot through the last slot before horizon_end.
    """
    slot_hours = slot_minutes / 60
    energy_per_slot = charge_power_kw * slot_hours

    def time_to_slot_idx(time_str: str) -> int:
        hour, minute = map(int, time_str.split(":"))
        return (hour * 60 + minute) // slot_minutes

    charge_slots = {time_to_slot_idx(t) for t in (charge_times or [])}

    horizon_start_naive = horizon_start.replace(tzinfo=None)
    horizon_end_naive = horizon_end.replace(tzinfo=None)
    now = datetime.now().replace(tzinfo=None)

    current_slot_idx = max(0, int((now - horizon_start_naive).total_seconds() / 60 / slot_minutes))
    total_slots = int((horizon_end_naive - horizon_start_naive).total_seconds() / 60 / slot_minutes)

    soc = current_soc
    results: list[dict] = []

    for slot_idx in range(current_slot_idx, total_slots):
        slot_time = horizon_start_naive + timedelta(minutes=slot_idx * slot_minutes)
        results.append({
            "timestamp": slot_time.isoformat(),
            "soc_percent": round(soc, 1),
        })

        if slot_idx in charge_slots:
            headroom = ev_battery_capacity_kwh * (100.0 - soc) / 100
            energy = min(energy_per_slot, max(0.0, headroom))
            soc += (energy / ev_battery_capacity_kwh) * 100

        soc = max(0.0, min(100.0, soc))

    if results:
        logger.info(
            f"🚗 {device_name}: EV SOC prediction — {len(results)} slots, "
            f"start {current_soc:.1f}% → end {results[-1]['soc_percent']:.1f}%"
        )

    return results
