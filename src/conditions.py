"""Shared Home Assistant entity condition evaluator.

Evaluates a user-configured :class:`ConditionGroup` (a flat list of
:class:`EntityCondition` leaves combined by a single ``and``/``or``) against live HA
entity states, using an injected async ``get_state`` callable.

Safety rule (see the EV-ready override for price-based grid-export blocking): a leaf that
cannot be evaluated with certainty - the entity is unreadable, or its state/attribute is
``unavailable``/``unknown``/``None`` - resolves to ``None`` ("unknown"), which callers treat
as ``False``. So readiness is only ever asserted when every relevant leaf is definitely true.

The comparison logic mirrors the numeric-first / case-insensitive-string convention already
used in ``device_verifier.verify_entity_action`` and ``ev_solar_charge._is_discharge_active``.
"""
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# entity_id -> HA state dict (with 'state'/'attributes') or None
GetState = Callable[[str], Awaitable[Optional[dict]]]

_UNAVAILABLE = ("unavailable", "unknown", None)


def _as_float(value) -> Optional[float]:
    """Return ``value`` as a float, or None if it isn't numeric."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _compare(current, operator: str, target) -> bool:
    """Compare an entity's current value against a target using ``operator``.

    Numeric comparison is tried first (so "80.0" == 80); otherwise falls back to a
    case-insensitive string comparison. ``in``/``not_in`` test membership, coercing a
    scalar target to a single-element list.
    """
    if operator in ("in", "not_in"):
        targets = target if isinstance(target, list) else [target]
        # Match either numerically or as trimmed, case-insensitive strings.
        cur_f = _as_float(current)
        found = False
        for t in targets:
            t_f = _as_float(t)
            if cur_f is not None and t_f is not None:
                if cur_f == t_f:
                    found = True
                    break
            elif str(current).strip().lower() == str(t).strip().lower():
                found = True
                break
        return found if operator == "in" else not found

    if operator in ("==", "!="):
        cur_f, tgt_f = _as_float(current), _as_float(target)
        if cur_f is not None and tgt_f is not None:
            is_equal = cur_f == tgt_f
        else:
            is_equal = str(current).strip().lower() == str(target).strip().lower()
        return is_equal if operator == "==" else not is_equal

    # Ordering operators require numbers on both sides.
    cur_f, tgt_f = _as_float(current), _as_float(target)
    if cur_f is None or tgt_f is None:
        logger.warning(
            f"Condition operator '{operator}' needs numeric values, got current={current!r} "
            f"target={target!r} - treating as not satisfied"
        )
        return False
    if operator == "<":
        return cur_f < tgt_f
    if operator == "<=":
        return cur_f <= tgt_f
    if operator == ">":
        return cur_f > tgt_f
    if operator == ">=":
        return cur_f >= tgt_f
    logger.warning(f"Unknown condition operator '{operator}' - treating as not satisfied")
    return False


async def evaluate_entity_condition(cond, get_state: GetState) -> Optional[bool]:
    """Evaluate a single :class:`EntityCondition`.

    Returns True/False, or None when the value can't be read with certainty
    (entity missing, or state/attribute unavailable/unknown).
    """
    state = await get_state(cond.entity_id)
    if not state:
        return None  # unreadable -> unknown
    if cond.state_attribute:
        raw = state.get("attributes", {}).get(cond.state_attribute)
    else:
        raw = state.get("state")
    if raw in _UNAVAILABLE:
        return None  # unavailable/unknown -> unknown
    return _compare(raw, cond.operator, cond.value)


async def is_ev_ready(group, get_state: GetState) -> bool:
    """True ONLY if ``group`` evaluates with certainty to True.

    Any leaf that is unknown/unavailable/unreadable counts as False, so an ``and`` group
    short-circuits to "not ready". An empty/None group proves nothing and returns False.
    """
    if not group or not group.conditions:
        return False
    resolved = [(await evaluate_entity_condition(c, get_state)) or False for c in group.conditions]
    return all(resolved) if group.logic == "and" else any(resolved)


async def any_ev_ready(ev_devices, get_state: GetState) -> bool:
    """True if any EV device's ``grid_export_unblock_condition`` is certainly ready.

    When true, price-based grid-export blocking should be overridden (export unblocked) so
    the inverter runs at full production for the EV.
    """
    for ev in ev_devices or []:
        cond = getattr(ev, "grid_export_unblock_condition", None)
        if cond and cond.conditions and await is_ev_ready(cond, get_state):
            return True
    return False
