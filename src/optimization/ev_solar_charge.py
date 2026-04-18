"""EV Solar Charge Controller.

Manages EV charging based on available solar power production.
Adjusts the charging current dynamically to maximise solar self-consumption,
with automatic phase switching between single-phase and three-phase charging.

Configuration is read from ``CONFIG['options']`` (config.json):

    production_phase_l1_entity: sensor.power_production_phase_l1
    production_phase_l2_entity: sensor.power_production_phase_l2
    production_phase_l3_entity: sensor.power_production_phase_l3
    consumption_phase_l1_entity: sensor.power_consumption_phase_l1
    consumption_phase_l2_entity: sensor.power_consumption_phase_l2
    consumption_phase_l3_entity: sensor.power_consumption_phase_l3
    phase_switch_threshold_power: 4000          # shared with load watcher
    minimum_charging_power: 1380
"""
import logging
from math import sqrt
from typing import Optional

from ..config import CONFIG

logger = logging.getLogger(__name__)

# Voltage constants
_VOLTAGE_SINGLE_PHASE = 230.0          # V (line-to-neutral)
_VOLTAGE_THREE_PHASE = 400.0 * sqrt(3)  # divisor for I = P / (√3 × 400V), ≈ 692.8


def _get_solar_charge_config() -> dict:
    """Build the solar charge config dict from CONFIG['options']."""
    opts = CONFIG.get('options', {})
    return {
        'production_l1': opts.get('production_phase_l1_entity'),
        'production_l2': opts.get('production_phase_l2_entity'),
        'production_l3': opts.get('production_phase_l3_entity'),
        'consumption_l1': opts.get('consumption_phase_l1_entity'),
        'consumption_l2': opts.get('consumption_phase_l2_entity'),
        'consumption_l3': opts.get('consumption_phase_l3_entity'),
        'phase_switch_threshold_power': opts.get('phase_switch_threshold_power', 4000.0),
        'minimum_ev_charging_power': opts.get('minimum_charging_power', 1380.0),
    }


class EvSolarChargeController:
    """Controls EV charging current based on available solar surplus.

    Only operates on EV devices that have ``solar_charge_only=True`` set in
    their device configuration.  For each such device the controller:
    1. Reads solar production per phase (L1/L2/L3) from Home Assistant.
    2. Reads house consumption per phase (L1/L2/L3, excluding the EV itself).
    3. Computes the net solar surplus per phase and sums them.
    4. If the surplus is below ``minimum_charging_power`` nothing is done.
    5. Otherwise selects single-phase or three-phase mode based on
       ``phase_switch_threshold_power`` and applies the resulting current limit
       via the device's existing ``load_management.apply_limit_actions``.

    Global configuration is read from ``CONFIG['options']`` (config.json).
    """

    def __init__(self, get_state_func, devices_instance):
        """Initialise the solar charge controller.

        Args:
            get_state_func: Async callable ``(entity_id) -> dict | None`` that
                returns the Home Assistant entity state.
            devices_instance: ``Devices`` instance used to execute actions.
        """
        self.get_state = get_state_func
        self.devices = devices_instance

    async def run_all(self, ev_devices):
        """Run the controller for every EV device that has ``solar_charge_only=True``.

        Args:
            ev_devices: Iterable of ``Device`` objects with ``type == 'ev'``.
        """
        for ev_device in ev_devices:
            if not ev_device.solar_charge_only:
                continue
            await self.run(ev_device)

    async def run(self, ev_device):
        """Run the solar charge controller for a single EV device.

        Args:
            ev_device: ``Device`` configuration object for the EV charger.
        """
        solar_cfg = _get_solar_charge_config()
        device_name = ev_device.name
        logger.info(f"☀️ {device_name}: Running solar charge controller")

        try:
            # --- Read power values from Home Assistant ---
            production = await self._get_phase_power(
                solar_cfg['production_l1'],
                solar_cfg['production_l2'],
                solar_cfg['production_l3'],
                "production",
            )
            consumption = await self._get_phase_power(
                solar_cfg['consumption_l1'],
                solar_cfg['consumption_l2'],
                solar_cfg['consumption_l3'],
                "consumption",
            )

            # --- Calculate net surplus (total production minus total consumption, floor at 0) ---
            total_production = sum(production)
            total_consumption = sum(consumption)
            total_surplus = max(0.0, total_production - total_consumption)

            logger.info(
                f"☀️ {device_name}: "
                f"Production={total_production:.0f}W "
                f"(L1={production[0]:.0f}, L2={production[1]:.0f}, L3={production[2]:.0f})  "
                f"Consumption={total_consumption:.0f}W "
                f"(L1={consumption[0]:.0f}, L2={consumption[1]:.0f}, L3={consumption[2]:.0f})  "
                f"Surplus={total_surplus:.0f}W"
            )

            min_power = solar_cfg['minimum_ev_charging_power']
            phase_threshold = solar_cfg['phase_switch_threshold_power']

            # Check actual charger state via its instantaneous load sensor
            ev_currently_charging = await self._is_ev_charging(ev_device)

            if total_surplus < min_power:
                logger.info(
                    f"☀️ {device_name}: Solar surplus {total_surplus:.0f}W is below the "
                    f"minimum {min_power:.0f}W – stopping EV charger"
                )
                if ev_currently_charging:
                    logger.info(f"☀️ {device_name}: Stopping EV charger (was started by solar controller)")
                    await self.devices.execute_device_action(
                        device_name=device_name,
                        actions=ev_device.stop.model_dump(exclude_none=True),
                        action_label="stop",
                    )
                return

            # --- Determine phase mode ---
            use_three_phase = total_surplus >= phase_threshold
            if use_three_phase:
                phase_voltage = _VOLTAGE_THREE_PHASE
                three_phase, single_phase = 1, 0
                logger.info(
                    f"☀️ {device_name}: {total_surplus:.0f}W ≥ {phase_threshold:.0f}W "
                    f"→ THREE-phase charging"
                )
            else:
                phase_voltage = _VOLTAGE_SINGLE_PHASE
                three_phase, single_phase = 0, 1
                logger.info(
                    f"☀️ {device_name}: {total_surplus:.0f}W < {phase_threshold:.0f}W "
                    f"→ SINGLE-phase charging"
                )

            limit_watts = total_surplus
            limit_amps = limit_watts / phase_voltage

            logger.info(
                f"☀️ {device_name}: Applying limit {limit_watts:.0f}W / {limit_amps:.1f}A "
                f"({'3-phase' if use_three_phase else '1-phase'})"
            )

            # --- Validate load management configuration ---
            load_mgmt = ev_device.load_management
            if not load_mgmt or not load_mgmt.apply_limit_actions:
                logger.warning(
                    f"☀️ {device_name}: No load_management or apply_limit_actions "
                    f"configured – cannot apply solar limit"
                )
                return

            apply_actions = load_mgmt.apply_limit_actions

            # Context used when evaluating expressions in action payloads
            context = {
                "limit_watts": limit_watts,
                "limit_amps": limit_amps,
                "three_phase": three_phase,
                "single_phase": single_phase,
            }

            # --- Phase switching (only when automated_phase_switching is enabled) ---
            if load_mgmt.automated_phase_switching:
                if use_three_phase and apply_actions.switch_to_three_phase:
                    logger.info(f"☀️ {device_name}: Executing switch_to_three_phase action")
                    await self.devices.execute_device_action(
                        device_name=device_name,
                        actions=apply_actions.switch_to_three_phase.model_dump(exclude_none=True),
                        action_label="switch_to_three_phase",
                        context=context,
                    )
                elif not use_three_phase and apply_actions.switch_to_single_phase:
                    logger.info(f"☀️ {device_name}: Executing switch_to_single_phase action")
                    await self.devices.execute_device_action(
                        device_name=device_name,
                        actions=apply_actions.switch_to_single_phase.model_dump(exclude_none=True),
                        action_label="switch_to_single_phase",
                        context=context,
                    )

            # --- Start EV charger if not already running ---
            if not ev_currently_charging:
                logger.info(
                    f"☀️ {device_name}: Starting EV charger "
                    f"(surplus {total_surplus:.0f}W ≥ minimum {min_power:.0f}W)"
                )
                await self.devices.execute_device_action(
                    device_name=device_name,
                    actions=ev_device.start.model_dump(exclude_none=True),
                    action_label="start",
                )

            # --- Apply the charge current limit ---
            if apply_actions.apply_limit:
                await self.devices.execute_device_action(
                    device_name=device_name,
                    actions=apply_actions.apply_limit.model_dump(exclude_none=True),
                    action_label=f"solar_limit_{int(limit_watts)}W",
                    context=context,
                )

        except Exception as e:
            logger.error(
                f"☀️ {device_name}: Unhandled error in solar charge controller: {e}",
                exc_info=True,
            )

    async def _get_phase_power(
        self,
        l1_entity: Optional[str],
        l2_entity: Optional[str],
        l3_entity: Optional[str],
        name: str,
    ) -> tuple:
        """Fetch power readings for all three phases from Home Assistant.

        Missing, unavailable, or unparseable phases default to **0.0 W**.

        Args:
            l1_entity: HA entity ID for phase L1 (or ``None``).
            l2_entity: HA entity ID for phase L2 (or ``None``).
            l3_entity: HA entity ID for phase L3 (or ``None``).
            name: Descriptive label used in log messages (e.g. ``"production"``).

        Returns:
            Tuple ``(l1_watts, l2_watts, l3_watts)`` as floats.
        """
        values: list[float] = []
        for entity_id, phase in [
            (l1_entity, "L1"),
            (l2_entity, "L2"),
            (l3_entity, "L3"),
        ]:
            if not entity_id:
                values.append(0.0)
                continue

            state = await self.get_state(entity_id)
            if state and state.get("state") not in ["unavailable", "unknown", None]:
                try:
                    raw_value = float(state["state"])
                    unit = state.get("attributes", {}).get("unit_of_measurement", "W")
                    if unit.lower() == "kw":
                        raw_value *= 1000.0
                    values.append(raw_value)
                except (ValueError, TypeError):
                    logger.warning(
                        f"  ⚠️ Could not parse {name} {phase} from {entity_id}: "
                        f"{state.get('state')}"
                    )
                    values.append(0.0)
            else:
                logger.warning(
                    f"  ⚠️ No valid state for {name} {phase} ({entity_id})"
                )
                values.append(0.0)

        return tuple(values)

    async def _is_ev_charging(self, ev_device) -> bool:
        """Check whether the EV charger is currently drawing power.

        Reads the ``instantaneous_load_entity`` configured under
        ``load_management`` and applies the same ``charge_sign`` logic used
        by the load watcher's ``LimitCalculator``.

        Returns ``False`` if the entity is unavailable or not configured.
        """
        load_mgmt = ev_device.load_management
        if not load_mgmt or not load_mgmt.instantaneous_load_entity:
            return False

        threshold = CONFIG.get('options', {}).get('load_watcher_threshold_power', 10.0)
        state = await self.get_state(load_mgmt.instantaneous_load_entity)
        if not state or state.get('state') in ('unavailable', 'unknown', None):
            logger.warning(
                f"☀️ {ev_device.name}: Cannot read instantaneous load from "
                f"{load_mgmt.instantaneous_load_entity} – assuming not charging"
            )
            return False

        try:
            raw = float(state['state'])
            unit = state.get('attributes', {}).get(
                'unit_of_measurement',
                load_mgmt.instantaneous_load_entity_unit,
            )
            if unit.lower() == 'kw':
                raw *= 1000.0
        except (ValueError, TypeError):
            return False

        if load_mgmt.charge_sign == 'negative':
            return raw < -threshold
        return raw > threshold
