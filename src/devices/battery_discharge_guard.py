"""Battery-discharge guard: keep the house battery out of the EV's charging session.

Emptying the house battery into the car is close to always a loss — the energy makes a
second round trip through an inverter for a load that could just as well have taken the
same cheap import — so while an EV is charging each battery's ``discharge_stop`` action
is fired, and ``discharge_start`` fires again when the session ends.

The guard is deliberately **charge-mode agnostic**: it keys off the EV's *observed*
charging state rather than off whichever planner opened the slot, so it behaves
identically for solar-surplus charging, deadline/target-SOC charging, plain price-based
slots, and a session someone started by hand at the charger. Three call sites drive it:

* :class:`~src.devices.ev_solar_charge.EvSolarChargeController` calls
  :meth:`block`/:meth:`release` inline for its own devices, so the block lands in the
  same control cycle the charger starts.
* The scheduler routes EV start/stop through ``Devices.execute_ev_action``, so a
  deadline or price slot blocks discharge the moment the slot opens instead of at the
  next control cycle.
* :meth:`reconcile` sweeps every other EV once per control cycle, which both catches
  sessions nobody announced and re-asserts a block that another automation undid
  mid-session.

Restores are conservative and symmetric: only a battery that was *actually discharging*
when the block was taken is ever restored (discharge someone else had switched off stays
off), and a battery blocked for two EVs at once is restored only when the last of them
stops.
"""
import logging

from ..ev_charging_state import is_ev_charging

logger = logging.getLogger(__name__)


class BatteryDischargeGuard:
    """Blocks/restores house-battery discharge around EV charging sessions."""

    def __init__(self, get_state_func, devices_instance):
        self.get_state = get_state_func
        self.devices = devices_instance
        # EV device name -> names of the batteries whose discharge we stopped for it.
        # In-memory only: after a restart nothing is held, and the first reconcile
        # re-takes whatever block the live state still calls for.
        self._blocked: dict[str, set[str]] = {}

    @staticmethod
    def is_enabled(ev_device) -> bool:
        """True when this EV device asks for the guard (deprecated alias folded in by config)."""
        return bool(getattr(ev_device, 'block_battery_discharge_while_charging', False))

    def blocked_batteries(self) -> set:
        """Names of every battery currently held blocked, across all EVs.

        Used by the periodic device verifier so it doesn't "repair" a discharge slot the
        guard is deliberately holding off.
        """
        return {name for held in self._blocked.values() for name in held}

    # ------------------------------------------------------------------
    # Charging detection
    # ------------------------------------------------------------------

    async def is_ev_charging(self, ev_device) -> bool:
        """Is this EV drawing power right now?

        Delegates to :func:`src.ev_charging_state.is_ev_charging`, the single definition
        shared with the dashboard/UI status: ``ev_charging_condition`` when the user
        configured one, else the load-management power entity with the load watcher's
        threshold and sign convention. Unreadable counts as *not* charging, so a dead
        sensor releases the battery rather than pinning it blocked indefinitely.
        """
        return await is_ev_charging(self.get_state, ev_device, warn_when_undetectable=True)

    # ------------------------------------------------------------------
    # Block / release
    # ------------------------------------------------------------------

    async def block(self, ev_device, battery_devices) -> list:
        """Stop discharge on every battery currently discharging, on behalf of ``ev_device``.

        Idempotent, so it is safe to call every cycle: a battery already stopped is left
        untouched (no redundant command, and discharge that was off independently of us is
        never "restored" later), while one that started discharging mid-session is stopped
        and added to the restore set. Returns the full restore set for this EV.
        """
        if not self.is_enabled(ev_device):
            return []
        held = self._blocked.setdefault(ev_device.name, set())
        newly_blocked = []
        for bat in battery_devices or []:
            if not bat.discharge_stop:
                continue
            if not await self._is_discharge_active(bat):
                continue
            await self.devices.execute_device_action(
                device_name=bat.name,
                actions=bat.discharge_stop.model_dump(exclude_none=True),
                action_label="discharge_stop",
            )
            held.add(bat.name)
            newly_blocked.append(bat.name)
        if newly_blocked:
            logger.info(
                f"🔋 {ev_device.name}: blocked battery discharge while charging "
                f"({', '.join(sorted(newly_blocked))}; restore on stop: {sorted(held)})"
            )
        return sorted(held)

    async def release(self, ev_device, battery_devices) -> None:
        """Re-enable discharge on the batteries blocked for ``ev_device``.

        A no-op when nothing is held, so it can be called on any path that ends a session.
        A battery also held for another EV that is still charging stays blocked — it is
        restored when the last EV holding it releases.
        """
        held = self._blocked.pop(ev_device.name, set())
        if not held:
            return
        still_held = self.blocked_batteries()
        for bat in battery_devices or []:
            if bat.name not in held or not bat.discharge_start:
                continue
            if bat.name in still_held:
                logger.info(
                    f"🔋 {ev_device.name}: leaving {bat.name} discharge blocked - "
                    f"another EV is still charging"
                )
                continue
            await self.devices.execute_device_action(
                device_name=bat.name,
                actions=bat.discharge_start.model_dump(exclude_none=True),
                action_label="discharge_start",
            )
            logger.info(f"🔋 {ev_device.name}: restored battery discharge for {bat.name}")

    async def reconcile(self, ev_devices, battery_devices) -> None:
        """Block/release per observed charging state, for the EVs nobody else drives.

        ``solar_charge_only`` devices are skipped: EvSolarChargeController already blocks
        and releases inline on every cycle for those, and it knows about a session a beat
        before the power meter does — letting both drive the same device would flap the
        block around the moment a solar session ends.
        """
        if not battery_devices:
            return
        for ev in ev_devices or []:
            if not self.is_enabled(ev) or getattr(ev, 'solar_charge_only', False):
                continue
            if await self.is_ev_charging(ev):
                await self.block(ev, battery_devices)
            elif self._blocked.get(ev.name):
                logger.info(f"🔋 {ev.name}: EV no longer charging - releasing battery discharge block")
                await self.release(ev, battery_devices)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _is_discharge_active(self, bat_device) -> bool:
        """Return True if battery discharge is currently active (not already stopped).

        Checks each EntityAction in ``discharge_stop``: if the entity's current state
        already matches the action's target value, discharge was already stopped before we
        intervened. If any entity differs from its target, discharge counts as active.
        Falls back to True (assume active) when state is unreadable.
        """
        discharge_stop = getattr(bat_device, 'discharge_stop', None)
        if not discharge_stop:
            return False
        for entity_action in (discharge_stop.entity or []):
            entity_id = getattr(entity_action, 'entity_id', None)
            target = entity_action.value if entity_action.value is not None else entity_action.option
            if not entity_id or target is None:
                continue
            state = await self.get_state(entity_id)
            if not state or state.get('state') in ('unavailable', 'unknown', None):
                return True  # unreadable → assume active (safer)
            current = state.get('state', '')
            try:
                if float(current) != float(target):
                    return True
            except (ValueError, TypeError):
                if str(current).lower() != str(target).lower():
                    return True
            # This entity is already at its discharge_stop value → check the next
        # All checked entities are at their discharge_stop values → discharge was already off
        return False
