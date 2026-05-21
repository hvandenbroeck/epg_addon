"""EV Solar Charge Controller.

Manages EV charging based on available solar power production, with automatic
phase switching between single-phase and three-phase charging.
"""
import logging
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
    }


class EvSolarChargeController:
    """Controls EV charging current based on available solar surplus.

    Only operates on EV devices with ``solar_charge_only=True``.
    """

    def __init__(self, get_state_func, devices_instance):
        self.get_state = get_state_func
        self.devices = devices_instance
        self.ev_charger = EvCharger(get_state_func, devices_instance)
        self._device_state: dict[str, Optional[tuple[bool, float]]] = {}

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

            ev_load_watts, ev_currently_charging = await self._read_ev_load(ev_device)
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
                self._device_state.pop(device_name, None)
                return

            load_mgmt = ev_device.load_management
            if not load_mgmt or not load_mgmt.apply_limit_actions:
                logger.warning(f"☀️ {device_name}: No load_management or apply_limit_actions configured")
                return

            min_amps = ev_device.ev_min_current_limit
            max_amps = ev_device.ev_max_current_limit
            phase_switching = load_mgmt.automated_phase_switching

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

            is_three_phase, current_amps = self._device_state.get(device_name) or (False, min_amps)
            voltages = await self.ev_charger.get_phase_voltages(ev_device)
            current_power = EvCharger.compute_power(is_three_phase, current_amps, voltages)

            if current_power < total_surplus:
                # Step up until we reach or exceed surplus (round up so the inverter
                # can always increase output when more solar is available).
                while True:
                    higher_power = self.ev_charger.get_higher_level_power(
                        is_three_phase, current_amps, voltages, min_amps, max_amps, phase_switching
                    )
                    if higher_power is None:
                        break  # Already at maximum
                    new_state = await self.ev_charger.set_higher_level_power(ev_device, is_three_phase, current_amps)
                    if new_state is None:
                        break
                    is_three_phase, current_amps = new_state
                    current_power = higher_power
                    if current_power >= total_surplus:
                        break  # First level at or above surplus – round-up target reached
            elif current_power > total_surplus:
                # Step down only while the next level would still be at or above surplus,
                # keeping the limit rounded up.
                while True:
                    lower_power = self.ev_charger.get_lower_level_power(
                        is_three_phase, current_amps, voltages, min_amps, max_amps, phase_switching
                    )
                    if lower_power is None or lower_power < total_surplus:
                        break  # At minimum or next step would drop below surplus – stay here
                    new_state = await self.ev_charger.set_lower_level_power(ev_device, is_three_phase, current_amps)
                    if new_state is None:
                        break
                    is_three_phase, current_amps = new_state
                    current_power = lower_power

            logger.info(
                f"☀️ {device_name}: Limit settled at "
                f"{'3-phase' if is_three_phase else '1-phase'} {current_amps:.0f}A ({current_power:.0f}W)"
            )
            self._device_state[device_name] = (is_three_phase, current_amps)

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

    async def _read_ev_load(self, ev_device) -> tuple[float, bool]:
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
                f"{load_mgmt.instantaneous_load_entity} – assuming not charging"
            )
            return 0.0, False

        threshold = CONFIG.get('options', {}).get('load_watcher_threshold_power', 10.0)
        is_charging = raw < -threshold if load_mgmt.charge_sign == 'negative' else raw > threshold
        return abs(raw), is_charging

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
