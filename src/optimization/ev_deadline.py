"""EV deadline + target-SOC charging planner.

Given a target SOC and a deadline, pick the cheapest available price slots (before the
deadline) so charging accumulates enough energy to reach the target by then. This mirrors
the greedy cheapest-slot-first pattern in optimization/battery_limiter.py, simplified to a
charge-only, single energy-target case (no discharge, no SOC ceiling beyond "target met").
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

_UNAVAILABLE = ("unknown", "unavailable", None)


async def read_ev_deadline_inputs(get_state, ev_device, now: datetime) -> tuple[Optional[float], Optional[float], Optional[datetime]]:
    """Read current SOC, target SOC, and deadline datetime from HA entities.

    Returns (current_soc, target_soc, deadline_datetime). Any element is None if its
    backing entity is missing, unavailable, or unparseable this cycle.
    """
    current_soc = await _read_percent(get_state, ev_device.ev_soc_entity, "current SOC", ev_device.name)
    target_soc = await _read_percent(get_state, ev_device.ev_deadline_target_soc_entity, "target SOC", ev_device.name)
    deadline = await _read_deadline(get_state, ev_device.ev_deadline_target_time_entity, now, ev_device.name)
    return current_soc, target_soc, deadline


async def _read_percent(get_state, entity_id, label, device_name) -> Optional[float]:
    if not entity_id:
        logger.warning(f"⚠️ {device_name}: no entity configured for {label}")
        return None
    state = await get_state(entity_id)
    if not state or state.get('state') in _UNAVAILABLE:
        logger.warning(f"⚠️ {device_name}: {label} entity '{entity_id}' unavailable")
        return None
    try:
        return max(0.0, min(100.0, float(state['state'])))
    except (ValueError, TypeError):
        logger.warning(f"⚠️ {device_name}: {label} entity '{entity_id}' has non-numeric state {state.get('state')!r}")
        return None


async def _read_deadline(get_state, entity_id, now: datetime, device_name) -> Optional[datetime]:
    if not entity_id:
        logger.warning(f"⚠️ {device_name}: no entity configured for deadline target time")
        return None
    state = await get_state(entity_id)
    if not state or state.get('state') in _UNAVAILABLE:
        logger.warning(f"⚠️ {device_name}: deadline entity '{entity_id}' unavailable")
        return None
    raw = state['state']

    # input_datetime reports one of three formats depending on has_date/has_time.
    if len(raw) == 8 and raw.count(':') == 2:
        # "HH:MM:SS" -> recurring daily deadline, rolls to the next occurrence.
        try:
            time_part = datetime.strptime(raw, "%H:%M:%S").time()
        except ValueError:
            logger.warning(f"⚠️ {device_name}: could not parse time-only deadline '{raw}'")
            return None
        deadline = datetime.combine(now.date(), time_part)
        if deadline <= now:
            deadline += timedelta(days=1)
        return deadline

    if ' ' in raw:
        # "YYYY-MM-DD HH:MM:SS" -> absolute one-off deadline, does NOT auto-roll.
        try:
            deadline = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            logger.warning(f"⚠️ {device_name}: could not parse date+time deadline '{raw}'")
            return None
        if deadline <= now:
            logger.warning(
                f"⚠️ {device_name}: EV deadline {deadline} has passed; no further deadline-based "
                f"charging will be scheduled until the input_datetime is updated"
            )
            return None
        return deadline

    # "YYYY-MM-DD" date-only -> unsupported, no time component to target.
    logger.warning(f"⚠️ {device_name}: deadline entity '{entity_id}' has no time component (date-only) — unsupported")
    return None


def plan_ev_deadline_charge(
    prices: list[float],
    slot_minutes: int,
    horizon_start: datetime,
    current_soc: Optional[float],
    target_soc: Optional[float],
    deadline: Optional[datetime],
    ev_battery_capacity_kwh: float,
    charge_power_kw: float,
    device_name: str = "ev",
    previous_charge_times: Optional[list[str]] = None,
) -> list[str]:
    """Select cheapest slots so `device_name` reaches `target_soc`% by `deadline`.

    Time strings are "HH:MM" relative to horizon_start and may exceed 24h (rolling
    multi-day horizon), matching the convention used in optimization/battery_limiter.py.
    """
    previous_charge_times = previous_charge_times or []

    if current_soc is None or target_soc is None or deadline is None:
        logger.warning(
            f"⚠️ {device_name}: missing SOC/target/deadline input this cycle — keeping previous plan unchanged"
        )
        return sorted(previous_charge_times)

    def time_to_slot_idx(time_str):
        hour, minute = map(int, time_str.split(':'))
        return (hour * 60 + minute) // slot_minutes

    def slot_idx_to_time(slot_idx):
        total_minutes = slot_idx * slot_minutes
        return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"

    now = datetime.now().replace(tzinfo=None)
    current_slot_idx = max(0, int((now - horizon_start).total_seconds() / 60 / slot_minutes))

    prev_slots = {time_to_slot_idx(t) for t in previous_charge_times}
    # Only the slot currently in progress is protected from re-planning — cancelling it would
    # yank charging out mid-slot. Slots before it are already finished: scheduler.py skips them
    # ("Skipping past event"), so carrying them forward can never start charging, it only keeps
    # them in the plan forever. That matters because a slot picked under a transient bad input
    # (e.g. a briefly-nearby deadline triggering the "charge regardless of price" fallback below)
    # would otherwise stay in the schedule and on the Gantt indefinitely.
    in_progress_slots = {s for s in prev_slots if s == current_slot_idx}

    energy_needed_kwh = ev_battery_capacity_kwh * max(0.0, target_soc - current_soc) / 100
    if energy_needed_kwh <= 0:
        logger.info(
            f"🚗 {device_name}: current SOC {current_soc:.1f}% already >= target {target_soc:.1f}%, "
            f"no charging needed"
        )
        return sorted(slot_idx_to_time(s) for s in in_progress_slots)

    deadline_slot_idx = int((deadline - horizon_start).total_seconds() / 60 / slot_minutes)
    known_end_idx = min(deadline_slot_idx, len(prices))

    candidate_slots = [s for s in range(current_slot_idx, known_end_idx)]

    if not candidate_slots:
        logger.warning(
            f"⚠️ {device_name}: deadline {deadline} leaves no remaining slots (already passed or "
            f"imminent) — no new charging slots selected this cycle"
        )
        return sorted(slot_idx_to_time(s) for s in in_progress_slots)

    slot_hours = slot_minutes / 60
    energy_per_slot = charge_power_kw * slot_hours

    candidates_by_price = sorted(candidate_slots, key=lambda s: prices[s])
    selected = set()
    energy_acc = 0.0
    for s in candidates_by_price:
        if energy_acc >= energy_needed_kwh:
            break
        selected.add(s)
        energy_acc += energy_per_slot

    if energy_acc < energy_needed_kwh:
        if known_end_idx >= deadline_slot_idx:
            logger.warning(
                f"⚠️ {device_name}: cannot reach {target_soc:.1f}% by {deadline} even using all "
                f"{len(candidate_slots)} remaining slots (need {energy_needed_kwh:.2f} kWh, can "
                f"deliver {energy_acc:.2f} kWh) — charging every available slot regardless of price"
            )
            selected = set(candidate_slots)
        else:
            logger.info(
                f"🚗 {device_name}: deadline {deadline} is beyond the known price horizon (know "
                f"until slot {known_end_idx}, need until slot {deadline_slot_idx}) — planning "
                f"against {len(candidate_slots)} known slots only, will re-plan as more price "
                f"data arrives"
            )

    final_slots = in_progress_slots | selected
    result = sorted(slot_idx_to_time(s) for s in final_slots)

    if selected:
        avg_price = sum(prices[s] for s in selected) / len(selected)
        logger.info(
            f"🚗 {device_name}: selected {len(selected)} charge slot(s) (avg price {avg_price:.4f} "
            f"EUR/kWh, ~{energy_acc:.2f} kWh) to reach {target_soc:.1f}% by {deadline}"
        )

    return result
