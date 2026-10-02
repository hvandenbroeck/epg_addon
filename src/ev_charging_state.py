"""Shared "is this EV charging right now?" detection.

One definition of *charging*, used by everything that needs to know:

* :class:`~src.devices.battery_discharge_guard.BatteryDischargeGuard` — decides whether to
  hold house-battery discharge off.
* :meth:`~src.optimizer.HeatpumpOptimizer.recalculate_ev_deadline_plans` and
  :meth:`~src.optimizer.HeatpumpOptimizer.refresh_ev_charging_state` — feed the ``ev_deadline``
  TinyDB doc, so the web UI, ``/api/ev_deadline`` and the Home Assistant card can say
  "charging now" instead of the (then misleading) "ready to charge".

Two signals, in order of preference:

1. ``ev_charging_condition`` — a user-configured :class:`ConditionGroup`, for a charger
   whose status entity is a better signal than its power meter (e.g. a Peblar reporting
   ``charging``). Evaluated with certainty only: unknown/unavailable counts as not charging.
2. ``load_management.instantaneous_load_entity`` — the charger's power meter, compared
   against the load watcher's ``load_watcher_threshold_power`` using the device's own
   ``charge_sign`` convention.

Unreadable always resolves to *not charging*: a dead sensor must release the battery rather
than pin it blocked, and the UI must not claim a session that may not exist. The measured
power is reported alongside the verdict (also when the condition drove it, whenever the
device has a power entity) so callers can display the live charger load.
"""
import logging
from typing import Optional

from .config import CONFIG
from .conditions import evaluate_condition_group
from .utils import read_entity_watts

logger = logging.getLogger(__name__)


def charging_threshold_w() -> float:
    """Power (W) above which a charger counts as actively charging."""
    return CONFIG.get('options', {}).get('load_watcher_threshold_power', 10.0)


async def read_ev_charge_power_w(get_state, ev_device) -> Optional[float]:
    """The EV charger's live load in watts (sign-normalised to positive = charging).

    None when the device has no ``instantaneous_load_entity`` or it can't be read.
    """
    load_mgmt = getattr(ev_device, 'load_management', None)
    entity_id = getattr(load_mgmt, 'instantaneous_load_entity', None) if load_mgmt else None
    if not entity_id:
        return None
    raw = await read_entity_watts(get_state, entity_id, load_mgmt.instantaneous_load_entity_unit)
    if raw is None:
        return None
    return -raw if load_mgmt.charge_sign == 'negative' else raw


async def get_ev_charging_state(get_state, ev_device, warn_when_undetectable: bool = False) -> dict:
    """Full charging picture for one EV device.

    Returns a dict with:
      ``charging`` — bool, the verdict (unreadable → False)
      ``source`` — ``'condition'``, ``'power'`` or ``'unavailable'`` (nothing to read)
      ``power_w`` — measured charger load in watts, or None when unreadable/not configured
      ``load_entity`` — the entity ``power_w`` came from, or None
      ``condition_configured`` — whether ``ev_charging_condition`` decided the verdict
      ``threshold_w`` — the power threshold used when ``source`` is ``'power'``

    ``warn_when_undetectable`` logs a warning when neither signal is available (the
    battery-discharge guard wants that; the UI refresh doesn't, it runs every few minutes).
    """
    load_mgmt = getattr(ev_device, 'load_management', None)
    load_entity = getattr(load_mgmt, 'instantaneous_load_entity', None) if load_mgmt else None
    power_w = await read_ev_charge_power_w(get_state, ev_device)
    threshold = charging_threshold_w()

    cond = getattr(ev_device, 'ev_charging_condition', None)
    if cond and cond.conditions:
        return {
            'charging': await evaluate_condition_group(cond, get_state),
            'source': 'condition',
            'power_w': power_w,
            'load_entity': load_entity,
            'condition_configured': True,
            'threshold_w': threshold,
        }

    if not load_entity:
        if warn_when_undetectable:
            logger.warning(
                f"🔌 {ev_device.name}: charging detection needs either ev_charging_condition "
                f"or load_management.instantaneous_load_entity - treating as not charging"
            )
        return {
            'charging': False,
            'source': 'unavailable',
            'power_w': None,
            'load_entity': None,
            'condition_configured': False,
            'threshold_w': threshold,
        }

    if power_w is None:
        if warn_when_undetectable:
            logger.warning(
                f"🔌 {ev_device.name}: cannot read charger power from {load_entity} - "
                f"treating as not charging"
            )
        return {
            'charging': False,
            'source': 'unavailable',
            'power_w': None,
            'load_entity': load_entity,
            'condition_configured': False,
            'threshold_w': threshold,
        }

    return {
        'charging': power_w > threshold,
        'source': 'power',
        'power_w': power_w,
        'load_entity': load_entity,
        'condition_configured': False,
        'threshold_w': threshold,
    }


async def is_ev_charging(get_state, ev_device, warn_when_undetectable: bool = False) -> bool:
    """Just the verdict from :func:`get_ev_charging_state`."""
    state = await get_ev_charging_state(get_state, ev_device, warn_when_undetectable)
    return state['charging']
