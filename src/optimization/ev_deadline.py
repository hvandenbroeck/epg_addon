"""EV deadline + target-SOC charging planner.

Given a target SOC and a deadline, pick the cheapest available price slots (before the
deadline) so charging accumulates enough energy to reach the target by then. This mirrors
the greedy cheapest-slot-first pattern in optimization/battery_limiter.py, simplified to a
charge-only, single energy-target case (no discharge, no SOC ceiling beyond "target met").

The planner is pure: it always computes the plan from the inputs it is given. Whether that
plan is actually *scheduled* is decided by the caller (``HeatpumpOptimizer
.recalculate_ev_deadline_plans``), which gates it on the EV's ``ev_ready_to_charge_condition``
so the provisional plan can still be shown in the UI while the car is not plugged in.
"""
import logging
from dataclasses import dataclass, field
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


@dataclass
class EvDeadlinePlan:
    """Result of one deadline-charging planning pass.

    ``charge_times`` is the complete plan (in-progress slot carried over + newly selected
    slots). ``in_progress_times`` is the subset that is currently running and must stay in
    the live schedule even when the plan is put on hold, so its stop action still fires.
    Times are "HH:MM" strings relative to horizon_start and may exceed 24h.
    """
    charge_times: list[str] = field(default_factory=list)
    in_progress_times: list[str] = field(default_factory=list)
    status: str = "planned"  # planned | target_reached | missing_inputs | no_slots | infeasible | partial_horizon
    message: str = ""
    energy_needed_kwh: float = 0.0
    energy_planned_kwh: float = 0.0
    avg_price: Optional[float] = None
    selected_count: int = 0


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
    lock_plan: bool = False,
) -> EvDeadlinePlan:
    """Select cheapest slots so `device_name` reaches `target_soc`% by `deadline`.

    Time strings are "HH:MM" relative to horizon_start and may exceed 24h (rolling
    multi-day horizon), matching the convention used in optimization/battery_limiter.py.

    ``lock_plan=True`` skips slot (re)selection entirely and just carries the previously
    committed plan forward unchanged (same as the "missing inputs" fallback below). Used by
    the 15-minute background recalculation so a car that is charging slower than predicted
    doesn't cause slots to be added outside of an explicit re-plan (daily optimization or the
    dashboard "Recalculate now" button) — see HeatpumpOptimizer.recalculate_ev_deadline_plans.
    """
    previous_charge_times = previous_charge_times or []

    def time_to_slot_idx(time_str):
        hour, minute = map(int, time_str.split(':'))
        return (hour * 60 + minute) // slot_minutes

    def slot_idx_to_time(slot_idx):
        total_minutes = slot_idx * slot_minutes
        return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"

    slot_hours = slot_minutes / 60
    energy_per_slot = charge_power_kw * slot_hours

    now = datetime.now().replace(tzinfo=None)
    current_slot_idx = max(0, int((now - horizon_start).total_seconds() / 60 / slot_minutes))

    prev_slots = {time_to_slot_idx(t) for t in previous_charge_times}
    # Only the slot currently in progress is protected from re-planning — cancelling it would
    # yank charging out mid-slot (and drop its stop action). Slots before it are already
    # finished: scheduler.py skips them ("Skipping past event"), so carrying them forward can
    # never start charging, it only keeps them in the plan forever. That matters because a slot
    # picked under a transient bad input (e.g. a briefly-nearby deadline triggering the "charge
    # regardless of price" fallback below) would otherwise stay in the schedule and on the
    # Gantt indefinitely.
    in_progress_slots = {s for s in prev_slots if s == current_slot_idx}
    in_progress_times = sorted(slot_idx_to_time(s) for s in in_progress_slots)

    def _hold(status, message):
        """Return a plan with no new slots: only the in-progress slot is kept."""
        return EvDeadlinePlan(
            charge_times=list(in_progress_times),
            in_progress_times=list(in_progress_times),
            status=status,
            message=message,
            energy_needed_kwh=0.0,
            energy_planned_kwh=len(in_progress_slots) * energy_per_slot,
        )

    if lock_plan:
        locked = sorted(previous_charge_times)
        energy_planned_kwh = len({s for s in prev_slots if s >= current_slot_idx}) * energy_per_slot
        energy_needed_kwh = 0.0
        if current_soc is not None and target_soc is not None:
            energy_needed_kwh = ev_battery_capacity_kwh * max(0.0, target_soc - current_soc) / 100
        logger.debug(
            f"🚗 {device_name}: plan locked, carrying forward {len(locked)} previously committed "
            f"slot(s) unchanged (no re-selection this cycle)"
        )
        return EvDeadlinePlan(
            charge_times=locked,
            in_progress_times=list(in_progress_times),
            status="locked",
            message=(
                f"Plan fixed at the last recalculation ({len(locked)} slot(s), about "
                f"{energy_planned_kwh:.1f} kWh planned); press \"Recalculate now\" to re-plan "
                "against the current SOC."
            ),
            energy_needed_kwh=energy_needed_kwh,
            energy_planned_kwh=energy_planned_kwh,
        )

    if current_soc is None or target_soc is None or deadline is None:
        logger.warning(
            f"⚠️ {device_name}: missing SOC/target/deadline input this cycle — keeping previous plan unchanged"
        )
        previous = sorted(previous_charge_times)
        return EvDeadlinePlan(
            charge_times=previous,
            in_progress_times=list(in_progress_times),
            status="missing_inputs",
            message="Current SOC, target SOC or deadline could not be read from Home Assistant; "
                    "previous plan kept unchanged.",
            energy_planned_kwh=len({s for s in prev_slots if s >= current_slot_idx}) * energy_per_slot,
        )

    energy_needed_kwh = ev_battery_capacity_kwh * max(0.0, target_soc - current_soc) / 100
    if energy_needed_kwh <= 0:
        logger.info(
            f"🚗 {device_name}: current SOC {current_soc:.1f}% already >= target {target_soc:.1f}%, "
            f"no charging needed"
        )
        return _hold(
            "target_reached",
            f"Current SOC {current_soc:.0f}% is already at or above the target of {target_soc:.0f}%; "
            "nothing to charge.",
        )

    deadline_slot_idx = int((deadline - horizon_start).total_seconds() / 60 / slot_minutes)
    known_end_idx = min(deadline_slot_idx, len(prices))

    candidate_slots = [s for s in range(current_slot_idx, known_end_idx)]

    if not candidate_slots:
        logger.warning(
            f"⚠️ {device_name}: deadline {deadline} leaves no remaining slots (already passed or "
            f"imminent) — no new charging slots selected this cycle"
        )
        plan = _hold(
            "no_slots",
            f"The deadline ({deadline:%a %d %b %H:%M}) leaves no remaining price slots to charge in.",
        )
        plan.energy_needed_kwh = energy_needed_kwh
        return plan

    candidates_by_price = sorted(candidate_slots, key=lambda s: prices[s])
    selected = set()
    energy_acc = 0.0
    for s in candidates_by_price:
        if energy_acc >= energy_needed_kwh:
            break
        selected.add(s)
        energy_acc += energy_per_slot

    status = "planned"
    message = ""
    if energy_acc < energy_needed_kwh:
        if known_end_idx >= deadline_slot_idx:
            logger.warning(
                f"⚠️ {device_name}: cannot reach {target_soc:.1f}% by {deadline} even using all "
                f"{len(candidate_slots)} remaining slots (need {energy_needed_kwh:.2f} kWh, can "
                f"deliver {energy_acc:.2f} kWh) — charging every available slot regardless of price"
            )
            selected = set(candidate_slots)
            status = "infeasible"
            message = (
                f"Not enough time left to reach {target_soc:.0f}% by {deadline:%a %d %b %H:%M} "
                f"(need {energy_needed_kwh:.1f} kWh, can deliver about {energy_acc:.1f} kWh); "
                "every remaining slot is used regardless of price."
            )
        else:
            logger.info(
                f"🚗 {device_name}: deadline {deadline} is beyond the known price horizon (know "
                f"until slot {known_end_idx}, need until slot {deadline_slot_idx}) — planning "
                f"against {len(candidate_slots)} known slots only, will re-plan as more price "
                f"data arrives"
            )
            status = "partial_horizon"
            message = (
                "The deadline lies beyond the known price horizon; planned against the known "
                "slots only and will be extended once tomorrow's prices are published."
            )

    final_slots = in_progress_slots | selected
    result = sorted(slot_idx_to_time(s) for s in final_slots)

    avg_price = None
    if selected:
        avg_price = sum(prices[s] for s in selected) / len(selected)
        logger.info(
            f"🚗 {device_name}: selected {len(selected)} charge slot(s) (avg price {avg_price:.4f} "
            f"EUR/kWh, ~{energy_acc:.2f} kWh) to reach {target_soc:.1f}% by {deadline}"
        )

    energy_planned_kwh = len(final_slots) * energy_per_slot
    if status == "planned":
        message = (
            f"{len(final_slots)} slot(s), about {energy_planned_kwh:.1f} kWh, to reach "
            f"{target_soc:.0f}% by {deadline:%a %d %b %H:%M}."
        )

    return EvDeadlinePlan(
        charge_times=result,
        in_progress_times=list(in_progress_times),
        status=status,
        message=message,
        energy_needed_kwh=energy_needed_kwh,
        energy_planned_kwh=energy_planned_kwh,
        avg_price=avg_price,
        selected_count=len(selected),
    )
