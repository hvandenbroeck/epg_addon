"""EV Solar Charge Controller.

Manages EV charging based on available solar power production, with automatic
phase switching between single-phase and three-phase charging.

Design goals (see the long comment in :meth:`EvSolarChargeController.run`):

* Route excess solar to the EV by rounding the limit to the charge level just above
  the surplus (default, uses all excess; the grid draw is the dynamic gap to the
  level below) or, optionally (``ev_solar_round_down``), just below it (never imports
  to round, exports the small remainder instead).
* Never overshoot on start: pin the limit immediately instead of letting the
  charger sit at its power-on default (typically the hardware maximum), and grow
  the current gently (one level per control cycle).
* Don't flap: a short dip in surplus (a passing cloud, or the transient caused by
  the charger itself changing steps) must not stop the session. Stopping is
  debounced over several cycles.
* Give the battery priority without starving the EV: the surplus is the export
  overflow (PV minus house minus battery charging), so the battery always charges
  first and the EV only uses solar the battery can't absorb. Battery *discharge* is
  treated as a deficit, so the EV steps down rather than pulling the battery flat.
  (An optional strict SOC lockout exists but is disabled by default.)
* Keep the phase honest: the charger's ACTUAL phase is read each cycle and the limit
  is always applied with the matching phase command, so a charger stuck in the wrong
  phase can't make a current limit draw ~3x the expected power. After dropping to
  single phase, switching back up to three phase (and any limit change that would
  require it) is held off for ``phase_switch_delay_minutes`` (shared with the load
  watcher) to avoid chatter.
* Cope with blocked grid export (negative-price slots): when the inverter is told
  not to export, its production meter no longer reflects the real surplus, so the
  surplus folds in battery-charge power instead.
"""
import logging
from datetime import datetime
from typing import Optional

from ..config import CONFIG
from .ev_charger import EvCharger

logger = logging.getLogger(__name__)


def _get_solar_charge_config() -> dict:
    opts = CONFIG.get('options', {})
    return {
        'production_l1': opts.get('production_phase_l1_entity'),
        'production_l2': opts.get('production_phase_l2_entity'),
        'production_l3': opts.get('production_phase_l3_entity'),
        'consumption_l1': opts.get('consumption_phase_l1_entity'),
        'consumption_l2': opts.get('consumption_phase_l2_entity'),
        'consumption_l3': opts.get('consumption_phase_l3_entity'),
        'minimum_ev_charging_power': opts.get('minimum_charging_power', 1380.0),
        # Minutes to wait after dropping to single phase before switching back up to
        # three phase, preventing phase chatter near the boundary. Shared (global)
        # with the load watcher's phase switching.
        'phase_switch_delay_minutes': float(opts.get('phase_switch_delay_minutes', 5)),
    }
    # NOTE: the per-EV behaviour knobs (round_down, start_margin, stop_debounce,
    # battery_soc_full/hysteresis) live on the EV *device* config, not here - see the
    # ``solar_*`` fields on ``Device`` and ``_device_tuning`` below.


class EvSolarChargeController:
    """Controls EV charging current based on available solar surplus.

    Only operates on EV devices with ``solar_charge_only=True``.
    """

    def __init__(self, get_state_func, devices_instance):
        self.get_state = get_state_func
        self.devices = devices_instance
        self.ev_charger = EvCharger(get_state_func, devices_instance)
        # Per-device runtime state:
        #   {'three_phase': bool, 'amps': float, 'below_min': int,
        #    'last_single_switch': Optional[str ISO timestamp]}
        self._device_state: dict[str, dict] = {}

    @staticmethod
    def _device_tuning(ev_device) -> dict:
        """Per-EV solar-charge tuning, read from the device config (with defaults)."""
        return {
            'round_down': bool(getattr(ev_device, 'solar_round_down', False)),
            'start_margin': float(getattr(ev_device, 'solar_start_margin', 200.0)),
            'stop_debounce': int(getattr(ev_device, 'solar_stop_debounce', 3)),
            'battery_soc_full': float(getattr(ev_device, 'solar_battery_soc_full', 0.0)),
            'battery_soc_hysteresis': float(getattr(ev_device, 'solar_battery_soc_hysteresis', 5.0)),
        }

    async def run_all(self, ev_devices, battery_devices=None):
        """Run controller for every EV device with ``solar_charge_only=True``."""
        for ev_device in ev_devices:
            if ev_device.solar_charge_only:
                await self.run(ev_device, battery_devices=battery_devices)

    async def run(self, ev_device, battery_devices=None):
        """Run the solar charge controller for a single EV device.

        Algorithm (runs once per control cycle):

        1. Measure net per-phase grid flow (production = export, consumption =
           import), the EV's own draw, and battery charge/discharge, and reconstruct
           the **surplus** the EV may consume::

               surplus = (production - consumption) + ev_load - battery_discharge

           Adding back ``ev_load`` makes the figure independent of how much the EV is
           already drawing; subtracting ``battery_discharge`` ensures we never grow the
           EV into power that is coming out of the house battery.
        2. Choose the **target level**: by default the smallest level at or ABOVE the
           surplus, so every watt of excess solar is used (the grid drawn is just the
           dynamic gap to the level below). With ``ev_solar_round_down`` set, the
           largest level at or BELOW the surplus instead.
        3. Move toward that level **gently**: at most one level **up** per cycle, but
           step **down** as far as needed in one cycle to back off quickly. Switching
           back up to three phase is held off for ``phase_switch_delay_minutes`` after
           a drop to single phase.
        4. Start/stop with hysteresis: require ``surplus >= min_power + start_margin``
           to start, and only stop after the surplus has stayed below the minimum for
           ``stop_debounce`` consecutive cycles.

        When grid export is blocked (negative-price slots) the production meter is
        unreliable, so the surplus folds in battery-charge power; the same rounding
        then lets the EV soak up curtailed PV that the meters can't directly see.
        """
        cfg = _get_solar_charge_config()
        tuning = self._device_tuning(ev_device)
        device_name = ev_device.name
        logger.info(f"☀️ {device_name}: Running solar charge controller")

        try:
            load_mgmt = ev_device.load_management
            if not load_mgmt or not load_mgmt.apply_limit_actions:
                logger.warning(f"☀️ {device_name}: No load_management or apply_limit_actions configured")
                return

            r = await self._read_inputs(ev_device, battery_devices, cfg)
            self._log_inputs(device_name, r)

            surplus = r['surplus']
            voltages = r['voltages']
            min_amps = ev_device.ev_min_current_limit
            max_amps = ev_device.ev_max_current_limit
            phase_switching = load_mgmt.automated_phase_switching

            # Lowest power the charger can draw, the first step above it (the natural
            # granularity at the bottom, reused as the start/stop floor deadband), and
            # the effective minimum we insist on being able to sustain.
            min_level_power = EvCharger.compute_power(False, min_amps, voltages)
            min_step = self._step_above(False, min_amps, voltages, min_amps, max_amps, phase_switching)
            effective_min = max(cfg['minimum_ev_charging_power'], min_level_power)

            # Battery-first priority (no-op unless a SOC entity is configured and the
            # optional lockout is enabled). ``battery_full`` gates starting,
            # ``battery_ok`` (a hysteresis band below full) gates continuing.
            soc_full = tuning['battery_soc_full']
            soc_resume = max(0.0, soc_full - tuning['battery_soc_hysteresis'])
            min_soc = r['min_soc']
            battery_full = (min_soc is None) or (soc_full <= 0) or (min_soc >= soc_full)
            battery_ok = (min_soc is None) or (soc_full <= 0) or (min_soc >= soc_resume)

            st = self._device_state.get(device_name) or {}
            is_three_phase = st.get('three_phase', False)
            current_amps = st.get('amps', min_amps)
            below_min = st.get('below_min', 0)
            last_single_switch = st.get('last_single_switch')

            # ----------------------------------------------------------------
            # Not currently charging -> decide whether to start.
            # ----------------------------------------------------------------
            if not r['ev_charging']:
                if not battery_full:
                    logger.info(
                        f"☀️ {device_name}: Battery at {min_soc:.0f}% "
                        f"(< {soc_full:.0f}% full) - charging the house battery first, EV stays off"
                    )
                    self._device_state.pop(device_name, None)
                    return

                if r['export_blocked']:
                    # Can't see surplus directly; start as long as we're within one
                    # charge step of the minimum (negative-price slots are daytime).
                    start_ok = surplus >= effective_min - min_step
                else:
                    start_ok = surplus >= effective_min + tuning['start_margin']

                if not start_ok:
                    logger.info(
                        f"☀️ {device_name}: Surplus {surplus:.0f}W below start threshold "
                        f"({effective_min + tuning['start_margin']:.0f}W) - charger stays off"
                    )
                    self._device_state.pop(device_name, None)
                    return

                logger.info(f"☀️ {device_name}: Starting EV charger (surplus {surplus:.0f}W)")
                await self.devices.execute_device_action(
                    device_name=device_name,
                    actions=ev_device.start.model_dump(exclude_none=True),
                    action_label="start",
                )
                # Pin the limit immediately (the level just above or below the surplus,
                # per round_down) so the charger never sits at its power-on default.
                # Force the phase command (pass the OPPOSITE phase as "current") so the
                # charger is explicitly put into the intended phase - never assume it is
                # already there, or a stale 3-phase state would make a "16 A" limit draw
                # ~3x the expected power.
                init_three, init_amps = self._target_level(
                    surplus, tuning['round_down'], voltages, min_amps, max_amps, phase_switching
                )
                applied = await self.ev_charger.set_level(
                    ev_device, init_three, init_amps, current_three_phase=(not init_three)
                )
                if applied is not None:
                    init_three, init_amps = applied
                self._save_state(device_name, init_three, init_amps, 0, None)
                logger.info(
                    f"☀️ {device_name}: Started at "
                    f"{'3-phase' if init_three else '1-phase'} {init_amps:.0f}A "
                    f"({EvCharger.compute_power(init_three, init_amps, voltages):.0f}W)"
                )
                return

            # ----------------------------------------------------------------
            # Currently charging -> adjust the limit.
            # ----------------------------------------------------------------
            # Battery-first: if the battery slipped below the resume band, stop the EV
            # so the solar refills the battery before charging resumes.
            if not battery_ok:
                logger.info(
                    f"☀️ {device_name}: Battery fell to {min_soc:.0f}% "
                    f"(< {soc_resume:.0f}%) - stopping EV to recharge the house battery first"
                )
                await self._stop(ev_device)
                self._device_state.pop(device_name, None)
                return

            # Reconcile our phase belief with the charger's ACTUAL phase, so a charger
            # left in (or reverted to) the wrong phase can't corrupt the power figures
            # (a "16 A" limit applied in three-phase draws ~3x what we expect).
            actual_three = await self._read_actual_three_phase(ev_device)
            if actual_three is not None and actual_three != is_three_phase:
                logger.info(
                    f"☀️ {device_name}: Phase mismatch - charger is "
                    f"{'3' if actual_three else '1'}-phase but state said "
                    f"{'3' if is_three_phase else '1'}-phase; correcting to actual"
                )
                is_three_phase = actual_three

            # Can we sustain even the minimum level on solar? Allow up to one charge
            # step of grid draw at the bottom (the dynamic floor deadband) before we
            # start counting toward a stop.
            if surplus < effective_min - min_step:
                below_min += 1
                if below_min >= tuning['stop_debounce']:
                    logger.info(
                        f"☀️ {device_name}: Surplus {surplus:.0f}W below minimum "
                        f"({effective_min:.0f}W) for {below_min} cycles - stopping EV charger"
                    )
                    await self._stop(ev_device)
                    self._device_state.pop(device_name, None)
                    return

                # Not yet - hold at the minimum, waiting to see if it recovers (anti-flap).
                if is_three_phase or current_amps > min_amps:
                    if is_three_phase:
                        last_single_switch = datetime.now().isoformat()  # dropping to 1-phase
                    applied = await self.ev_charger.set_level(
                        ev_device, False, min_amps, current_three_phase=is_three_phase
                    )
                    if applied is not None:
                        is_three_phase, current_amps = applied
                logger.info(
                    f"☀️ {device_name}: Surplus low ({surplus:.0f}W) - holding at minimum "
                    f"({below_min}/{tuning['stop_debounce']} before stop)"
                )
                self._save_state(device_name, is_three_phase, current_amps, below_min, last_single_switch)
                return

            # Surplus is healthy -> reset the stop debounce and track the limit.
            is_three_phase, current_amps, last_single_switch = await self._adjust_level(
                ev_device, r, cfg, tuning, is_three_phase, current_amps, last_single_switch
            )
            logger.info(
                f"☀️ {device_name}: Limit settled at "
                f"{'3-phase' if is_three_phase else '1-phase'} {current_amps:.0f}A "
                f"({EvCharger.compute_power(is_three_phase, current_amps, voltages):.0f}W)"
            )
            self._save_state(device_name, is_three_phase, current_amps, 0, last_single_switch)

        except Exception as e:
            logger.error(f"☀️ {device_name}: Unhandled error in solar charge controller: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Cycle helpers
    # ------------------------------------------------------------------

    async def _read_inputs(self, ev_device, battery_devices, cfg) -> dict:
        """Gather all meter/sensor readings for one cycle and derive the surplus."""
        production = await self._get_phase_power(
            cfg['production_l1'], cfg['production_l2'], cfg['production_l3'], "production"
        )
        consumption = await self._get_phase_power(
            cfg['consumption_l1'], cfg['consumption_l2'], cfg['consumption_l3'], "consumption"
        )
        total_production = sum(production)
        total_consumption = sum(consumption)
        net_grid = total_production - total_consumption  # >0 export, <0 import

        ev_load, ev_charging = await self._read_ev_load(ev_device)
        bat_charge, bat_discharge, min_soc, export_blocked = await self._read_battery_state(battery_devices)

        # Surplus available to the EV. ``net_grid`` is the export overflow (PV minus
        # house minus battery charging), so the battery keeps priority automatically.
        surplus = net_grid + ev_load - bat_discharge
        if export_blocked:
            # The export meter is muted by inverter curtailment; battery-charge power
            # is the visible proxy for the spare PV, so fold it in.
            surplus += bat_charge

        voltages = await self.ev_charger.get_phase_voltages(ev_device)

        return {
            'production': production, 'consumption': consumption,
            'total_production': total_production, 'total_consumption': total_consumption,
            'net_grid': net_grid, 'ev_load': ev_load, 'ev_charging': ev_charging,
            'bat_charge': bat_charge, 'bat_discharge': bat_discharge,
            'min_soc': min_soc, 'export_blocked': export_blocked,
            'surplus': surplus, 'voltages': voltages,
        }

    def _log_inputs(self, device_name, r) -> None:
        p, c = r['production'], r['consumption']
        soc_text = 'n/a' if r['min_soc'] is None else f"{r['min_soc']:.0f}%"
        logger.info(
            f"☀️ {device_name}: "
            f"Production={r['total_production']:.0f}W (L1={p[0]:.0f}, L2={p[1]:.0f}, L3={p[2]:.0f})  "
            f"Consumption={r['total_consumption']:.0f}W (L1={c[0]:.0f}, L2={c[1]:.0f}, L3={c[2]:.0f})  "
            f"Net grid={r['net_grid']:.0f}W  EV load={r['ev_load']:.0f}W  "
            f"Battery charge={r['bat_charge']:.0f}W discharge={r['bat_discharge']:.0f}W  "
            f"Export blocked={r['export_blocked']}  "
            f"Battery SOC={soc_text}  "
            f"Surplus={r['surplus']:.0f}W"
        )

    async def _adjust_level(self, ev_device, r, cfg, tuning, is_three_phase, current_amps, last_single_switch):
        """Move the charge limit toward the target for the current surplus.

        At most one level **up** per cycle (and a single->three phase switch is held
        off for ``phase_switch_delay_minutes`` after a drop to single phase); steps
        **down** as far as needed in one cycle. Each applied step also re-asserts the
        phase (``set_*_level_power`` issues the switch action whenever the step's phase
        differs from the current one). Returns the new
        ``(is_three_phase, current_amps, last_single_switch)``.
        """
        voltages = r['voltages']
        surplus = r['surplus']
        min_amps = ev_device.ev_min_current_limit
        max_amps = ev_device.ev_max_current_limit
        phase_switching = ev_device.load_management.automated_phase_switching

        target_three, target_amps = self._target_level(
            surplus, tuning['round_down'], voltages, min_amps, max_amps, phase_switching
        )
        target_power = EvCharger.compute_power(target_three, target_amps, voltages)
        current_power = EvCharger.compute_power(is_three_phase, current_amps, voltages)

        if current_power > target_power:
            # Above target: back off as far as needed in one cycle (always safe).
            while current_power > target_power:
                prev_three = is_three_phase
                new_state = await self.ev_charger.set_lower_level_power(
                    ev_device, is_three_phase, current_amps
                )
                if new_state is None:
                    break  # already at minimum
                is_three_phase, current_amps = new_state
                if prev_three and not is_three_phase:
                    last_single_switch = datetime.now().isoformat()  # dropped to 1-phase
                current_power = EvCharger.compute_power(is_three_phase, current_amps, voltages)
        elif current_power < target_power:
            # Below target: climb one level. If that single step would switch us back
            # up to three phase, honour the dwell since the last drop to single phase.
            peek = EvCharger._next_higher_state(
                is_three_phase, current_amps, min_amps, max_amps, phase_switching
            )
            is_phase_up = peek is not None and (not is_three_phase) and peek[0]
            if is_phase_up and last_single_switch:
                mins = (datetime.now() - datetime.fromisoformat(last_single_switch)).total_seconds() / 60.0
                if mins < cfg['phase_switch_delay_minutes']:
                    logger.info(
                        f"☀️ {ev_device.name}: ⏳ Phase-switch delay active "
                        f"({mins:.1f}/{cfg['phase_switch_delay_minutes']:.0f} min) - "
                        f"staying 1-phase {current_amps:.0f}A instead of switching to 3-phase"
                    )
                    return is_three_phase, current_amps, last_single_switch
            new_state = await self.ev_charger.set_higher_level_power(
                ev_device, is_three_phase, current_amps
            )
            if new_state is not None:
                is_three_phase, current_amps = new_state

        return is_three_phase, current_amps, last_single_switch

    async def _stop(self, ev_device) -> None:
        await self.devices.execute_device_action(
            device_name=ev_device.name,
            actions=ev_device.stop.model_dump(exclude_none=True),
            action_label="stop",
        )

    def _save_state(self, device_name, three_phase, amps, below_min, last_single_switch) -> None:
        self._device_state[device_name] = {
            'three_phase': three_phase,
            'amps': amps,
            'below_min': below_min,
            'last_single_switch': last_single_switch,
        }

    # ------------------------------------------------------------------
    # Level selection (pure helpers)
    # ------------------------------------------------------------------

    def _ideal_level(self, target_power, voltages, min_amps, max_amps, phase_switching) -> tuple:
        """Highest (phase, amps) level whose power is <= ``target_power`` (never below min)."""
        is_three_phase, amps = False, min_amps
        while True:
            nxt = EvCharger._next_higher_state(is_three_phase, amps, min_amps, max_amps, phase_switching)
            if nxt is None:
                break
            n_three, n_amps = nxt
            if EvCharger.compute_power(n_three, n_amps, voltages) > target_power:
                break
            is_three_phase, amps = n_three, n_amps
        return is_three_phase, amps

    def _step_above(self, is_three_phase, amps, voltages, min_amps, max_amps, phase_switching) -> float:
        """Power gap (W) from this level to the next level up, or 0.0 if at maximum."""
        nxt = EvCharger._next_higher_state(is_three_phase, amps, min_amps, max_amps, phase_switching)
        if nxt is None:
            return 0.0
        current = EvCharger.compute_power(is_three_phase, amps, voltages)
        return EvCharger.compute_power(nxt[0], nxt[1], voltages) - current

    def _level_at_least(self, surplus, voltages, min_amps, max_amps, phase_switching):
        """Smallest (phase, amps) level whose power is >= ``surplus``, or None if none."""
        is_three_phase, amps = False, min_amps
        if EvCharger.compute_power(is_three_phase, amps, voltages) >= surplus:
            return (is_three_phase, amps)
        while True:
            nxt = EvCharger._next_higher_state(is_three_phase, amps, min_amps, max_amps, phase_switching)
            if nxt is None:
                return None
            n_three, n_amps = nxt
            if EvCharger.compute_power(n_three, n_amps, voltages) >= surplus:
                return nxt
            is_three_phase, amps = n_three, n_amps

    def _target_level(self, surplus, round_down, voltages, min_amps, max_amps, phase_switching) -> tuple:
        """Pick the charge level for the given ``surplus``.

        ``round_down`` False (default): the smallest level at or ABOVE the surplus, so
        all excess solar is used (drawing the gap to the level below from the grid).
        ``round_down`` True: the largest level at or BELOW the surplus, so the EV never
        imports just to take a step (it exports the small remainder instead).
        """
        if round_down:
            return self._ideal_level(surplus, voltages, min_amps, max_amps, phase_switching)
        up = self._level_at_least(surplus, voltages, min_amps, max_amps, phase_switching)
        if up is None:
            # Surplus exceeds even the maximum level -> use the maximum.
            return self._ideal_level(surplus, voltages, min_amps, max_amps, phase_switching)
        return up

    # ------------------------------------------------------------------
    # HA reading helpers
    # ------------------------------------------------------------------

    async def _read_watts(self, entity_id: Optional[str], default_unit: str = "W") -> Optional[float]:
        """Read a HA entity state as watts. Returns None if unavailable or unparseable."""
        if not entity_id:
            return None
        state = await self.get_state(entity_id)
        if not state or state.get("state") in ("unavailable", "unknown", None):
            return None
        try:
            raw = float(state["state"])
            unit = state.get("attributes", {}).get("unit_of_measurement", default_unit)
            if unit.lower() == "kw":
                raw *= 1000.0
            return raw
        except (ValueError, TypeError):
            return None

    async def _get_phase_power(self, l1_entity, l2_entity, l3_entity, name) -> tuple:
        """Fetch per-phase power readings (W). Missing/unavailable phases default to 0.0."""
        results = []
        for entity_id, phase in [(l1_entity, "L1"), (l2_entity, "L2"), (l3_entity, "L3")]:
            watts = await self._read_watts(entity_id)
            if watts is None and entity_id:
                logger.warning(f"  ⚠️ No valid state for {name} {phase} ({entity_id})")
            results.append(watts if watts is not None else 0.0)
        return tuple(results)

    async def _read_ev_load(self, ev_device) -> tuple:
        """Read the EV charger's current power draw in one HA call.

        Returns (load_watts, is_charging) where load_watts is always non-negative.
        """
        load_mgmt = ev_device.load_management
        if not load_mgmt or not load_mgmt.instantaneous_load_entity:
            return 0.0, False

        raw = await self._read_watts(load_mgmt.instantaneous_load_entity, load_mgmt.instantaneous_load_entity_unit)
        if raw is None:
            logger.warning(
                f"☀️ {ev_device.name}: Cannot read load from "
                f"{load_mgmt.instantaneous_load_entity} - assuming not charging"
            )
            return 0.0, False

        threshold = CONFIG.get('options', {}).get('load_watcher_threshold_power', 10.0)
        is_charging = raw < -threshold if load_mgmt.charge_sign == 'negative' else raw > threshold
        return abs(raw), is_charging

    async def _read_actual_three_phase(self, ev_device) -> Optional[bool]:
        """Read the charger's ACTUAL phase from its force-single-phase switch.

        Uses the entity toggled by the ``switch_to_single_phase`` action (turned ON to
        force single phase). Returns True for three-phase, False for single-phase, or
        None when it can't be determined (no such action/entity, or unavailable state)
        - in which case the caller keeps its internal belief.
        """
        load_mgmt = getattr(ev_device, 'load_management', None)
        actions = getattr(load_mgmt, 'apply_limit_actions', None) if load_mgmt else None
        single_action = getattr(actions, 'switch_to_single_phase', None) if actions else None
        entities = getattr(single_action, 'entity', None) if single_action else None
        if not entities:
            return None
        entity_id = getattr(entities[0], 'entity_id', None)
        if not entity_id:
            return None
        state = await self.get_state(entity_id)
        if not state or state.get("state") in ("unavailable", "unknown", None):
            return None
        # Switch ON  -> single phase forced -> three_phase = False
        # Switch OFF -> three phase
        return str(state.get("state")).lower() not in ("on", "true", "1")

    async def _read_battery_state(self, battery_devices):
        """Read all battery devices in a single pass (each entity read exactly once).

        Returns ``(charge_w, discharge_w, min_soc, export_blocked)`` aggregated across
        every battery: total charge and discharge power (W), the lowest configured SOC
        (% or None), and whether grid export is currently blocked on any device.

        ``charge_sign == 'negative'`` means a positive raw power reading is discharge
        and a negative reading is charge; ``'positive'`` is the opposite. Export is
        considered blocked when a device's ``block_grid_export_stop`` switch (the one
        that *enables* export) reads ``off`` - the inverter is then curtailing, so the
        production meter no longer reflects the true surplus.
        """
        charge = discharge = 0.0
        socs = []
        export_blocked = False
        if not battery_devices:
            return charge, discharge, None, export_blocked

        for bat in battery_devices:
            load_mgmt = bat.load_management
            if load_mgmt and load_mgmt.instantaneous_load_entity:
                raw = await self._read_watts(
                    load_mgmt.instantaneous_load_entity, load_mgmt.instantaneous_load_entity_unit
                )
                if raw is None:
                    logger.warning(
                        f"☀️ Battery {bat.name}: Cannot read load from "
                        f"{load_mgmt.instantaneous_load_entity} - ignoring for surplus calculation"
                    )
                elif load_mgmt.charge_sign == 'negative':
                    discharge += max(0.0, raw)
                    charge += max(0.0, -raw)
                else:
                    discharge += max(0.0, -raw)
                    charge += max(0.0, raw)

            soc_entity = getattr(bat, 'battery_soc_entity', None)
            if soc_entity:
                state = await self.get_state(soc_entity)
                if state and state.get("state") not in ("unavailable", "unknown", None):
                    try:
                        socs.append(float(state["state"]))
                    except (ValueError, TypeError):
                        pass

            enable_action = getattr(bat, 'block_grid_export_stop', None)
            if enable_action and getattr(enable_action, 'entity', None):
                for entity_action in enable_action.entity:
                    entity_id = getattr(entity_action, 'entity_id', None)
                    if not entity_id:
                        continue
                    state = await self.get_state(entity_id)
                    if state and str(state.get("state")).lower() == "off":
                        export_blocked = True

        return charge, discharge, (min(socs) if socs else None), export_blocked
