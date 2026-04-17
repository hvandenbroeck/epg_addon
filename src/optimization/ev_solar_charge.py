"""EV Solar Charge Controller.

Manages EV charging based on available solar power production.
Adjusts the charging current dynamically to maximise solar self-consumption,
with automatic phase switching between single-phase and three-phase charging.
"""
import logging
from datetime import datetime
from math import sqrt
from typing import Optional

logger = logging.getLogger(__name__)

# Voltage constants
_VOLTAGE_SINGLE_PHASE = 230.0          # V (line-to-neutral)
_VOLTAGE_THREE_PHASE = 400.0 * sqrt(3)  # V (line-to-line, ≈ 692.8 V)


class EvSolarChargeController:
    """Controls EV charging current based on available solar surplus.

    For every configured EV device the controller:
    1. Reads solar production per phase (L1/L2/L3) from Home Assistant.
    2. Reads house consumption per phase (L1/L2/L3, excluding the EV itself).
    3. Computes the net solar surplus per phase and sums them.
    4. If the surplus is below ``minimum_ev_charging_power`` nothing is done.
    5. Otherwise selects single-phase or three-phase mode based on
       ``phase_switch_threshold_power`` and applies the resulting current limit
       via the device's existing ``load_management.apply_limit_actions``.
    """

    def __init__(self, get_state_func, devices_instance, db=None):
        """Initialise the solar charge controller.

        Args:
            get_state_func: Async callable ``(entity_id) -> dict | None`` that
                returns the Home Assistant entity state.
            devices_instance: ``Devices`` instance used to execute actions.
            db: Optional TinyDB database for state persistence.
        """
        self.get_state = get_state_func
        self.devices = devices_instance
        self.db = db

    async def run_all(self, ev_devices):
        """Run the controller for every EV device that has a solar charge config.

        Args:
            ev_devices: Iterable of ``Device`` objects with ``type == 'ev'``.
        """
        for ev_device in ev_devices:
            if ev_device.solar_charge_config:
                await self.run(ev_device)

    async def run(self, ev_device):
        """Run the solar charge controller for a single EV device.

        Args:
            ev_device: ``Device`` configuration object for the EV charger.
        """
        solar_cfg = ev_device.solar_charge_config
        if not solar_cfg:
            return

        device_name = ev_device.name
        logger.info(f"☀️ {device_name}: Running solar charge controller")

        try:
            # --- Read power values from Home Assistant ---
            production = await self._get_phase_power(
                solar_cfg.production_phase_l1_entity,
                solar_cfg.production_phase_l2_entity,
                solar_cfg.production_phase_l3_entity,
                "production",
            )
            consumption = await self._get_phase_power(
                solar_cfg.consumption_phase_l1_entity,
                solar_cfg.consumption_phase_l2_entity,
                solar_cfg.consumption_phase_l3_entity,
                "consumption",
            )

            # --- Calculate net surplus per phase (floor at 0) ---
            surplus = tuple(max(0.0, p - c) for p, c in zip(production, consumption))
            total_surplus = sum(surplus)

            logger.info(
                f"☀️ {device_name}: "
                f"Production={sum(production):.0f}W "
                f"(L1={production[0]:.0f}, L2={production[1]:.0f}, L3={production[2]:.0f})  "
                f"Consumption={sum(consumption):.0f}W "
                f"(L1={consumption[0]:.0f}, L2={consumption[1]:.0f}, L3={consumption[2]:.0f})  "
                f"Surplus={total_surplus:.0f}W"
            )

            min_power = solar_cfg.minimum_ev_charging_power
            phase_threshold = solar_cfg.phase_switch_threshold_power

            if total_surplus < min_power:
                logger.info(
                    f"☀️ {device_name}: Solar surplus {total_surplus:.0f}W is below the "
                    f"minimum {min_power:.0f}W – charge limit not adjusted"
                )
                self._save_state(device_name, {
                    "total_surplus_watts": total_surplus,
                    "action": "skipped_below_minimum",
                    "updated_at": datetime.now().isoformat(),
                })
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

            # --- Apply the charge current limit ---
            if apply_actions.apply_limit:
                await self.devices.execute_device_action(
                    device_name=device_name,
                    actions=apply_actions.apply_limit.model_dump(exclude_none=True),
                    action_label=f"solar_limit_{int(limit_watts)}W",
                    context=context,
                )

            self._save_state(device_name, {
                "total_surplus_watts": total_surplus,
                "limit_watts": limit_watts,
                "limit_amps": limit_amps,
                "three_phase": three_phase,
                "single_phase": single_phase,
                "action": "limit_applied",
                "updated_at": datetime.now().isoformat(),
            })

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
                    values.append(float(state["state"]))
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

    def _save_state(self, device_name: str, state: dict) -> None:
        """Persist the latest controller state to TinyDB (best-effort).

        Args:
            device_name: EV device name used as part of the document key.
            state: Arbitrary dict to upsert into the database.
        """
        if not self.db:
            return
        try:
            from tinydb import Query

            query = Query()
            doc_id = f"solar_charge_{device_name}"
            self.db.upsert(
                {"id": doc_id, **state},
                query.id == doc_id,
            )
        except Exception as e:
            logger.warning(
                f"  ⚠️ Could not save solar charge state for {device_name}: {e}"
            )
