"""EV Charger helpers.

Fine-grained current-limit control for EV chargers, with automatic phase switching
at the boundaries (1-phase max → 3-phase min, and 3-phase min → 1-phase max).
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_VOLTAGE = 230.0


class EvCharger:
    """Helpers for stepping an EV charger's current limit up or down by one amp.

    Designed to mirror the constructor pattern of EvSolarChargeController:
    dependency-inject ``get_state_func`` (async, returns HA state dict or None)
    and ``devices_instance`` (``Devices``) for action execution.
    """

    def __init__(self, get_state_func, devices_instance):
        self.get_state = get_state_func
        self.devices = devices_instance

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _read_voltage(self, entity_id: Optional[str]) -> float:
        """Read a HA voltage entity.  Returns 230.0 if unset or unavailable."""
        if not entity_id:
            return _DEFAULT_VOLTAGE
        state = await self.get_state(entity_id)
        if not state or state.get("state") in ("unavailable", "unknown", None):
            logger.warning(f"⚡ Cannot read voltage from {entity_id} – using {_DEFAULT_VOLTAGE} V")
            return _DEFAULT_VOLTAGE
        try:
            return float(state["state"])
        except (ValueError, TypeError):
            logger.warning(f"⚡ Unparseable voltage from {entity_id} – using {_DEFAULT_VOLTAGE} V")
            return _DEFAULT_VOLTAGE

    async def get_phase_voltages(self, ev_device) -> tuple[float, float, float]:
        """Return (V_L1, V_L2, V_L3), reading from the device's configured entities."""
        v1 = await self._read_voltage(ev_device.ev_voltage_entity_l1)
        v2 = await self._read_voltage(ev_device.ev_voltage_entity_l2)
        v3 = await self._read_voltage(ev_device.ev_voltage_entity_l3)
        return (v1, v2, v3)

    @staticmethod
    def compute_power(is_three_phase: bool, amps: float, voltages: tuple[float, float, float]) -> float:
        """Return watts for the given phase mode and current.

        1-phase uses V_L1; 3-phase sums all three phase voltages × amps.
        """
        v1, v2, v3 = voltages
        if is_three_phase:
            return (v1 + v2 + v3) * amps
        return v1 * amps

    @staticmethod
    def _next_lower_state(
        is_three_phase: bool,
        current_amps: float,
        min_amps: float,
        max_amps: float,
        automated_phase_switching: bool,
    ) -> Optional[tuple[bool, float]]:
        """Return (new_is_three_phase, new_amps) one level lower, or None if at minimum."""
        if is_three_phase:
            if current_amps > min_amps:
                return (True, current_amps - 1)
            if automated_phase_switching:
                # Drop from 3-phase min to 1-phase max
                return (False, max_amps)
            return None
        else:
            if current_amps > min_amps:
                return (False, current_amps - 1)
            return None

    @staticmethod
    def _next_higher_state(
        is_three_phase: bool,
        current_amps: float,
        min_amps: float,
        max_amps: float,
        automated_phase_switching: bool,
    ) -> Optional[tuple[bool, float]]:
        """Return (new_is_three_phase, new_amps) one level higher, or None if at maximum."""
        if not is_three_phase:
            if current_amps < max_amps:
                return (False, current_amps + 1)
            if automated_phase_switching:
                # Jump from 1-phase max to 3-phase min
                return (True, min_amps)
            return None
        else:
            if current_amps < max_amps:
                return (True, current_amps + 1)
            return None

    # ------------------------------------------------------------------
    # Public get functions (pure – no HA calls)
    # ------------------------------------------------------------------

    def get_lower_level_power(
        self,
        is_three_phase: bool,
        current_amps: float,
        voltages: tuple[float, float, float],
        min_amps: float,
        max_amps: float,
        automated_phase_switching: bool,
    ) -> Optional[float]:
        """Return watts if the current limit is lowered by one level.

        Returns None when already at the minimum (no lower level exists).
        """
        next_state = self._next_lower_state(is_three_phase, current_amps, min_amps, max_amps, automated_phase_switching)
        if next_state is None:
            return None
        new_three_phase, new_amps = next_state
        return self.compute_power(new_three_phase, new_amps, voltages)

    def get_higher_level_power(
        self,
        is_three_phase: bool,
        current_amps: float,
        voltages: tuple[float, float, float],
        min_amps: float,
        max_amps: float,
        automated_phase_switching: bool,
    ) -> Optional[float]:
        """Return watts if the current limit is raised by one level.

        Returns None when already at the maximum (no higher level exists).
        """
        next_state = self._next_higher_state(is_three_phase, current_amps, min_amps, max_amps, automated_phase_switching)
        if next_state is None:
            return None
        new_three_phase, new_amps = next_state
        return self.compute_power(new_three_phase, new_amps, voltages)

    # ------------------------------------------------------------------
    # Public set functions (async – execute HA actions)
    # ------------------------------------------------------------------

    async def set_lower_level_power(
        self,
        ev_device,
        is_three_phase: bool,
        current_amps: float,
    ) -> Optional[tuple[bool, float]]:
        """Decrease the EV current limit by one level, switching phase if needed.

        Returns (new_is_three_phase, new_amps) if applied, None if already at minimum.
        """
        return await self._apply_level(ev_device, is_three_phase, current_amps, direction="lower")

    async def set_higher_level_power(
        self,
        ev_device,
        is_three_phase: bool,
        current_amps: float,
    ) -> Optional[tuple[bool, float]]:
        """Increase the EV current limit by one level, switching phase if needed.

        Returns (new_is_three_phase, new_amps) if applied, None if already at maximum.
        """
        return await self._apply_level(ev_device, is_three_phase, current_amps, direction="higher")

    async def _apply_level(self, ev_device, is_three_phase: bool, current_amps: float, direction: str) -> Optional[tuple[bool, float]]:
        device_name = ev_device.name
        min_amps = ev_device.ev_min_current_limit
        max_amps = ev_device.ev_max_current_limit
        load_mgmt = ev_device.load_management
        phase_switching = load_mgmt.automated_phase_switching if load_mgmt else False

        if direction == "lower":
            next_state = self._next_lower_state(is_three_phase, current_amps, min_amps, max_amps, phase_switching)
        else:
            next_state = self._next_higher_state(is_three_phase, current_amps, min_amps, max_amps, phase_switching)

        if next_state is None:
            boundary = "minimum" if direction == "lower" else "maximum"
            logger.warning(
                f"⚡ {device_name}: Already at {boundary} "
                f"({'3-phase' if is_three_phase else '1-phase'} {current_amps:.0f} A) – no action taken"
            )
            return None

        new_three_phase, new_amps = next_state
        voltages = await self.get_phase_voltages(ev_device)
        new_watts = self.compute_power(new_three_phase, new_amps, voltages)

        context = {
            "limit_amps": new_amps,
            "limit_watts": new_watts,
            "three_phase": 1 if new_three_phase else 0,
            "single_phase": 0 if new_three_phase else 1,
        }

        logger.info(
            f"⚡ {device_name}: {'↓' if direction == 'lower' else '↑'} "
            f"{'3-phase' if new_three_phase else '1-phase'} {new_amps:.0f} A ({new_watts:.0f} W)"
        )

        if load_mgmt and load_mgmt.apply_limit_actions:
            apply_actions = load_mgmt.apply_limit_actions

            # Execute phase switch action if phase changed
            if new_three_phase != is_three_phase and phase_switching:
                if new_three_phase and apply_actions.switch_to_three_phase:
                    await self.devices.execute_device_action(
                        device_name=device_name,
                        actions=apply_actions.switch_to_three_phase.model_dump(exclude_none=True),
                        action_label="switch_to_three_phase",
                        context=context,
                    )
                elif not new_three_phase and apply_actions.switch_to_single_phase:
                    await self.devices.execute_device_action(
                        device_name=device_name,
                        actions=apply_actions.switch_to_single_phase.model_dump(exclude_none=True),
                        action_label="switch_to_single_phase",
                        context=context,
                    )

            # Apply new current limit
            if apply_actions.apply_limit:
                await self.devices.execute_device_action(
                    device_name=device_name,
                    actions=apply_actions.apply_limit.model_dump(exclude_none=True),
                    action_label=f"set_limit_{int(new_amps)}A",
                    context=context,
                )
        else:
            logger.warning(f"⚡ {device_name}: No load_management or apply_limit_actions configured – limit not applied")

        return (new_three_phase, new_amps)
