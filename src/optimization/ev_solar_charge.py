"""EV Solar Charge Controller.

Manages EV charging based on available solar power production, with automatic
phase switching between single-phase and three-phase charging.
"""
import logging
from math import sqrt
from typing import Optional

from ..config import CONFIG

logger = logging.getLogger(__name__)

_VOLTAGE_SINGLE_PHASE = 230.0           # V (line-to-neutral)
_VOLTAGE_THREE_PHASE = 400.0 * sqrt(3)  # I = P / (√3 × 400V) ≈ 692.8 V


def _get_solar_charge_config() -> dict:
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

    Only operates on EV devices with ``solar_charge_only=True``.
    """

    def __init__(self, get_state_func, devices_instance):
        self.get_state = get_state_func
        self.devices = devices_instance

    async def run_all(self, ev_devices, battery_devices=None):
        """Run controller for every EV device with ``solar_charge_only=True``."""
        for ev_device in ev_devices:
            if ev_device.solar_charge_only:
                await self.run(ev_device, battery_devices=battery_devices)

    async def run(self, ev_device, battery_devices=None):
        """Run the solar charge controller for a single EV device."""
        solar_cfg = _get_solar_charge_config()
        device_name = ev_device.name
        logger.info(f"☀️ {device_name}: Running solar charge controller")

        try:
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

            total_production = sum(production)
            total_consumption = sum(consumption)

            ev_load_watts = await self._get_ev_load_watts(ev_device)
            effective_production = total_production + ev_load_watts

            battery_discharge_watts = await self._get_battery_discharge_watts(battery_devices)
            effective_consumption = total_consumption + battery_discharge_watts

            total_surplus = max(0.0, effective_production - effective_consumption)

            logger.info(
                f"☀️ {device_name}: "
                f"Production={total_production:.0f}W "
                f"(L1={production[0]:.0f}, L2={production[1]:.0f}, L3={production[2]:.0f})  "
                f"EV load={ev_load_watts:.0f}W  "
                f"Effective production={effective_production:.0f}W  "
                f"Consumption={total_consumption:.0f}W "
                f"(L1={consumption[0]:.0f}, L2={consumption[1]:.0f}, L3={consumption[2]:.0f})  "
                f"Battery discharge={battery_discharge_watts:.0f}W  "
                f"Effective consumption={effective_consumption:.0f}W  "
                f"Surplus={total_surplus:.0f}W"
            )

            min_power = solar_cfg['minimum_ev_charging_power']
            phase_threshold = solar_cfg['phase_switch_threshold_power']
            ev_currently_charging = await self._is_ev_charging(ev_device)

            if total_surplus < min_power:
                logger.info(
                    f"☀️ {device_name}: Surplus {total_surplus:.0f}W < minimum {min_power:.0f}W – stopping EV charger"
                )
                if ev_currently_charging:
                    await self.devices.execute_device_action(
                        device_name=device_name,
                        actions=ev_device.stop.model_dump(exclude_none=True),
                        action_label="stop",
                    )
                return

            use_three_phase = total_surplus >= phase_threshold
            if use_three_phase:
                phase_voltage = _VOLTAGE_THREE_PHASE
                three_phase, single_phase = 1, 0
                logger.info(f"☀️ {device_name}: {total_surplus:.0f}W ≥ {phase_threshold:.0f}W → THREE-phase charging")
            else:
                phase_voltage = _VOLTAGE_SINGLE_PHASE
                three_phase, single_phase = 0, 1
                logger.info(f"☀️ {device_name}: {total_surplus:.0f}W < {phase_threshold:.0f}W → SINGLE-phase charging")

            limit_amps = total_surplus / phase_voltage
            context = {
                "limit_watts": total_surplus,
                "limit_amps": limit_amps,
                "three_phase": three_phase,
                "single_phase": single_phase,
            }

            logger.info(
                f"☀️ {device_name}: Applying limit {total_surplus:.0f}W / {limit_amps:.1f}A "
                f"({'3-phase' if use_three_phase else '1-phase'})"
            )

            load_mgmt = ev_device.load_management
            if not load_mgmt or not load_mgmt.apply_limit_actions:
                logger.warning(f"☀️ {device_name}: No load_management or apply_limit_actions configured")
                return

            apply_actions = load_mgmt.apply_limit_actions

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

            if apply_actions.apply_limit:
                await self.devices.execute_device_action(
                    device_name=device_name,
                    actions=apply_actions.apply_limit.model_dump(exclude_none=True),
                    action_label=f"solar_limit_{int(total_surplus)}W",
                    context=context,
                )

        except Exception as e:
            logger.error(f"☀️ {device_name}: Unhandled error in solar charge controller: {e}", exc_info=True)

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

    async def _is_ev_charging(self, ev_device) -> bool:
        """Return True if the EV charger is currently drawing power."""
        load_mgmt = ev_device.load_management
        if not load_mgmt or not load_mgmt.instantaneous_load_entity:
            return False

        raw = await self._read_watts(load_mgmt.instantaneous_load_entity, load_mgmt.instantaneous_load_entity_unit)
        if raw is None:
            logger.warning(
                f"☀️ {ev_device.name}: Cannot read load from "
                f"{load_mgmt.instantaneous_load_entity} – assuming not charging"
            )
            return False

        threshold = CONFIG.get('options', {}).get('load_watcher_threshold_power', 10.0)
        return raw < -threshold if load_mgmt.charge_sign == 'negative' else raw > threshold

    async def _get_ev_load_watts(self, ev_device) -> float:
        """Return current EV charger power draw in watts (absolute value)."""
        load_mgmt = ev_device.load_management
        if not load_mgmt or not load_mgmt.instantaneous_load_entity:
            return 0.0
        raw = await self._read_watts(load_mgmt.instantaneous_load_entity, load_mgmt.instantaneous_load_entity_unit)
        return abs(raw) if raw is not None else 0.0

    async def _get_battery_discharge_watts(self, battery_devices) -> float:
        """Return total power currently discharged by all batteries in watts."""
        if not battery_devices:
            return 0.0

        total_discharge = 0.0
        for bat_device in battery_devices:
            load_mgmt = bat_device.load_management
            if not load_mgmt or not load_mgmt.instantaneous_load_entity:
                continue

            raw = await self._read_watts(load_mgmt.instantaneous_load_entity, load_mgmt.instantaneous_load_entity_unit)
            if raw is None:
                logger.warning(
                    f"☀️ Battery {bat_device.name}: Cannot read load from "
                    f"{load_mgmt.instantaneous_load_entity} – ignoring for surplus calculation"
                )
                continue

            discharge = max(0.0, raw if load_mgmt.charge_sign == 'negative' else -raw)
            if discharge > 0:
                logger.debug(f"☀️ Battery {bat_device.name}: discharging {discharge:.0f}W")
            total_discharge += discharge

        return total_discharge
