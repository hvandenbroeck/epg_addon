"""
Device Configuration using Pydantic BaseSettings

This module defines device configurations for Home Assistant integration.
Devices are configured with unique names and types (wp, hw, battery, ev).
Multiple devices of the same type can be configured.

Configuration is loaded from environment variables or config files.
"""

from typing import Dict, List, Optional, Literal, Any, Union
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict, JsonConfigSettingsSource, PydanticBaseSettingsSource
import json
import os


# Device Types
DeviceType = Literal["wp", "hw", "battery", "ev"]


# Action Models
class MQTTAction(BaseModel):
    """MQTT action configuration."""
    topic: str
    topic_get: Optional[str] = None
    payload: Union[str, int, float]
    payload_check: Optional[Union[str, int, float]] = None


class EntityAction(BaseModel):
    """Entity service call action configuration."""
    service: str
    entity_id: str
    value: Optional[Union[str, int, float]] = None
    option: Optional[str] = None
    value_check: Optional[Union[str, int, float]] = None
    state_attribute: Optional[str] = None


class ActionSet(BaseModel):
    """Set of actions (MQTT and/or entity actions)."""
    mqtt: List[MQTTAction] = Field(default_factory=list)
    entity: List[EntityAction] = Field(default_factory=list)


class EntityCondition(BaseModel):
    """A single comparison against a Home Assistant entity's state or attribute."""
    entity_id: str = Field(..., description="Home Assistant entity to read")
    state_attribute: Optional[str] = Field(default=None, description="Read this attribute instead of the entity state")
    operator: Literal["<", "<=", ">", ">=", "==", "!=", "in", "not_in"] = Field(..., description="Comparison operator")
    value: Union[str, int, float, List[Union[str, int, float]]] = Field(..., description="Value to compare against; use a list for 'in'/'not_in'")


class ConditionGroup(BaseModel):
    """A flat group of entity conditions combined with a single boolean operator (no nesting)."""
    logic: Literal["and", "or"] = Field(default="and", description="How to combine the conditions")
    conditions: List[EntityCondition] = Field(default_factory=list, description="Leaf conditions to evaluate")


class LoadManagementActions(BaseModel):
    """Load management specific actions."""
    switch_to_single_phase: Optional[ActionSet] = None
    switch_to_three_phase: Optional[ActionSet] = None
    apply_limit: Optional[ActionSet] = None


class LoadManagement(BaseModel):
    """Load management configuration for a device."""
    instantaneous_load_entity: str
    instantaneous_load_entity_unit: str = "W"
    load_priority: int = 999
    load_limiter_entity: str
    load_maximum_watts: Union[str, int, float]
    charge_sign: Literal["positive", "negative"] = "positive"
    automated_phase_switching: bool = False
    apply_limit_actions: LoadManagementActions = Field(default_factory=LoadManagementActions)


class Device(BaseModel):
    """Individual device configuration."""
    name: str = Field(..., description="Unique device name")
    type: DeviceType = Field(..., description="Device type")
    enable_load_management: bool = False
    load_management: Optional[LoadManagement] = None
    # Generic start/stop actions (used by wp, hw, ev)
    start: ActionSet = Field(default_factory=ActionSet)
    stop: ActionSet = Field(default_factory=ActionSet)
    # Battery-specific actions (only used when type='battery')
    charge_start: Optional[ActionSet] = None
    charge_stop: Optional[ActionSet] = None
    discharge_start: Optional[ActionSet] = None
    discharge_stop: Optional[ActionSet] = None
    solar_only_start: Optional[ActionSet] = None
    solar_only_stop: Optional[ActionSet] = None
    block_grid_export_start: Optional[ActionSet] = None
    block_grid_export_stop: Optional[ActionSet] = None
    grid_export_switch_entity: Optional[str] = Field(
        default=None,
        description="Switch entity whose OFF state means grid export is blocked and PV is being "
                    "curtailed. Used to exclude curtailed hours from solar-production training so "
                    "the forecast is not misled by suppressed output. If unset, falls back to the "
                    "entity in block_grid_export_stop.",
    )
    price_based_solar_grid_export: bool = Field(default=False, description="Block solar grid export during negative-price slots")
    grid_export_block_threshold: float = Field(default=0.0, description="Block grid export when price is below this threshold (€/kWh). Default 0 blocks only at negative prices.")
    # Battery-specific configuration (only used when type='battery')
    battery_soc_entity: Optional[str] = Field(default=None, description="Home Assistant entity for battery state of charge (%)")
    battery_capacity_kwh: Optional[float] = Field(default=None, description="Battery capacity in kWh")
    battery_charge_speed_kw: Optional[float] = Field(default=None, description="Battery charge speed in kW")
    battery_min_soc_percent: Optional[float] = Field(default=20.0, description="Minimum battery SOC in percent")
    battery_max_soc_percent: Optional[float] = Field(default=80.0, description="Maximum battery SOC in percent")
    # WP and HW optimization parameters (only used when type='wp' or type='hw')
    block_hours: Optional[float] = Field(default=None, description="Minimum runtime when turned on (hours)")
    min_gap_hours: Optional[float] = Field(default=None, description="Minimum gap between runs (hours)")
    max_gap_hours: Optional[float] = Field(default=None, description="Maximum gap between runs (hours)")
    # WP runtime calculation sensors (only used when type='wp')
    inside_temp_sensor: Optional[str] = Field(default=None, description="Inside temperature sensor entity ID")
    outside_temp_sensor: Optional[str] = Field(default=None, description="Outside temperature sensor entity ID")
    heatpump_status_sensor: Optional[str] = Field(default=None, description="Heat pump on/off status sensor entity ID")
    # WP temperature-based optimization disable threshold (only used when type='wp')
    disable_optimization_above_avg_temp: Optional[float] = Field(default=None, description="Disable WP optimization when 48h average outside temperature exceeds this value (°C). None means always optimize.")
    # EV-specific options (only used when type='ev')
    solar_charge_only: bool = Field(default=False, description="When True, the EV charger is controlled by solar surplus only; price-based scheduling and the load watcher are bypassed")
    ev_min_current_limit: float = Field(default=6.0, description="Minimum EV charging current in Amps")
    ev_max_current_limit: float = Field(default=16.0, description="Maximum EV charging current in Amps")
    ev_voltage_entity_l1: Optional[str] = Field(default=None, description="HA entity for L1 phase voltage (V). Defaults to 230 V if unset.")
    ev_voltage_entity_l2: Optional[str] = Field(default=None, description="HA entity for L2 phase voltage (V). Defaults to 230 V if unset.")
    ev_voltage_entity_l3: Optional[str] = Field(default=None, description="HA entity for L3 phase voltage (V). Defaults to 230 V if unset.")
    ev_single_phase_line: Literal["l1", "l2", "l3"] = Field(default="l1", description="Physical phase the charger uses when in single-phase mode. Determines which phase's voltage backs the 1-phase power calculation and which per-phase production/consumption reading is checked for the phase-balance switch.")
    grid_export_unblock_condition: Optional[ConditionGroup] = Field(
        default=None,
        description="EV-ready override for price-based grid-export blocking. When this condition is TRUE "
                    "(e.g. the charger is connected and the car is below its target SOC), price-based grid-export "
                    "blocking is overridden and export is force-unblocked so the inverter runs at full production "
                    "for the EV. Evaluated with certainty only: if any referenced entity is unavailable/unknown, "
                    "the EV counts as not-ready and normal price-based blocking stays in effect. None disables it.",
    )
    # EV solar-charge tuning (only used when type='ev' and solar_charge_only=True)
    solar_round_down: bool = Field(default=False, description="Round the charge limit DOWN to the level at/below the surplus (no grid import to round) instead of UP to the level above it.")
    solar_start_margin: float = Field(default=200.0, description="Extra surplus (W) above the minimum required before a solar charging session starts.")
    solar_stop_debounce: int = Field(default=3, description="Consecutive control cycles the surplus must stay below the minimum before the session stops (anti-flap).")
    solar_battery_soc_full: float = Field(default=0.0, description="Strict battery-first lockout: the EV will not start until every battery with a SOC entity reaches this percent, and stops if SOC later drops below (this - hysteresis). 0 disables (the battery still keeps priority via the surplus calculation).")
    solar_battery_soc_hysteresis: float = Field(default=5.0, description="Resume band (%) below 'full' before a stopped EV resumes (used with solar_battery_soc_full).")
    solar_block_battery_discharge: bool = Field(default=False, description="When enabled, battery discharge is blocked (via discharge_stop) while the EV is actively charging, and re-enabled (via discharge_start) when charging stops — but only if discharge was active when charging began.")
    solar_phase_balance_switching: bool = Field(default=False, description="While charging single-phase, detect the EV's phase importing from the grid while the other two phases are exporting or idle (a sign the inverter can't rebalance across phases), and switch early to 3-phase to use the spare solar on those phases.")
    solar_phase_balance_import_threshold: float = Field(default=100.0, description="Minimum import (W) on the EV's phase to count as 'starved' for the phase-balance switch.")
    solar_phase_balance_export_margin: float = Field(default=0.0, description="The other two phases must each be at or above this net export (W) - i.e. not importing - to count as having spare solar for the phase-balance switch.")
    solar_phase_balance_debounce: int = Field(default=2, description="Consecutive control cycles the phase imbalance must persist before forcing an early switch to 3-phase (anti-flap).")

class DevicesConfig(BaseSettings):
    """Main devices configuration."""
    model_config = SettingsConfigDict(
        env_prefix='DEVICES_',
        env_nested_delimiter='__',
        extra='ignore'
    )
    
    devices: List[Device] = Field(default_factory=list)
    
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Customize settings sources to include JSON file."""
        return (
            init_settings,
            JsonConfigSettingsSource(settings_cls, json_file='/data/options.json'),
            env_settings,
            file_secret_settings,
        )
    
    def get_device_by_name(self, name: str) -> Optional[Device]:
        """Get a device by its unique name."""
        for device in self.devices:
            if device.name == name:
                return device
        return None
    
    def get_devices_by_type(self, device_type: DeviceType) -> List[Device]:
        """Get all devices of a specific type."""
        return [d for d in self.devices if d.type == device_type]


# Load default configuration from file if it exists
def load_default_config() -> DevicesConfig:
    """Load default device configuration from config file or create empty config."""
    config_path = "/data/options.json"
    
    # Try to load from file
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                data = json.load(f)
                if 'devices' in data:
                    return DevicesConfig(devices=data['devices'])
        except Exception as e:
            print(f"Warning: Could not load devices config from {config_path}: {e}")
    
    # Create default config with example devices (backward compatible)
    default_devices = [
        Device(
            name="wp",
            type="wp",
            #inside_temp_sensor="sensor.ebusd_700_z2roomtemp",
            outside_temp_sensor="sensor.ebusd_700_displayedoutsidetemp",
            disable_optimization_above_avg_temp=10.0,
            #heatpump_status_sensor="sensor.ebusd_700_hc2pumpstatus_2",
            enable_load_management=False,
            start=ActionSet(mqtt=[
                MQTTAction(topic="ebusd/700/z2sfmode/set", topic_get="ebusd/700/z2sfmode/get", payload="veto"),
                MQTTAction(topic="ebusd/700/z2quickvetotemp/set", topic_get="ebusd/700/z2quickvetotemp/get", payload="21")
            ]),
            stop=ActionSet(mqtt=[
                MQTTAction(topic="ebusd/700/z2sfmode/set", topic_get="ebusd/700/z2sfmode/get", payload="auto"),
                MQTTAction(topic="ebusd/700/z2quickvetotemp/set", topic_get="ebusd/700/z2quickvetotemp/get", payload="20")
            ])
        ),
        Device(
            name="hw",
            type="hw",
            enable_load_management=False,
            start=ActionSet(mqtt=[
                MQTTAction(topic="ebusd/700/HwcTempDesired/set", topic_get="ebusd/700/HwcTempDesired/get", payload="60")
            ]),
            stop=ActionSet(mqtt=[
                MQTTAction(topic="ebusd/700/HwcTempDesired/set", topic_get="ebusd/700/HwcTempDesired/get", payload="50")
            ])
        ),
        Device(
            name="battery",
            type="battery",
            enable_load_management=True,
            battery_soc_entity="sensor.deye_battery_soc",
            #battery_soc_entity="input_number.battery_soc_simulation",  # Use helper instead of real sensor
            battery_capacity_kwh=14.3,
            battery_charge_speed_kw=3.5,
            battery_min_soc_percent=10.0,
            battery_max_soc_percent=50.0,
            load_management=LoadManagement(
                instantaneous_load_entity="sensor.deye_battery_power",
                instantaneous_load_entity_unit="W",
                load_priority=1,
                load_limiter_entity="select.device_load_limit",
                load_maximum_watts="3500",
                charge_sign="negative",
                automated_phase_switching=False,
                apply_limit_actions=LoadManagementActions(
                    apply_limit=ActionSet(entity=[
                        EntityAction(
                            service="number/set_value",
                            entity_id="number.deye_battery_max_charge_current",
                            value="{int(round((limit_watts/51.2), 0))}"
                        )
                    ])
                )
            ),
            charge_start=ActionSet(entity=[
                EntityAction(service="number/set_value", entity_id="number.deye_prog1_capacity", value=50),
                EntityAction(service="number/set_value", entity_id="number.deye_prog2_capacity", value=50),
                EntityAction(service="number/set_value", entity_id="number.deye_prog3_capacity", value=50),
                EntityAction(service="number/set_value", entity_id="number.deye_prog4_capacity", value=50),
                EntityAction(service="number/set_value", entity_id="number.deye_prog5_capacity", value=50),
                EntityAction(service="number/set_value", entity_id="number.deye_prog6_capacity", value=50),
                EntityAction(service="select/select_option", entity_id="select.deye_prog1_charge", option="Allow Grid"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog2_charge", option="Allow Grid"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog3_charge", option="Allow Grid"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog4_charge", option="Allow Grid"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog5_charge", option="Allow Grid"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog6_charge", option="Allow Grid"),
            ]),
            charge_stop=ActionSet(entity=[
                EntityAction(service="select/select_option", entity_id="select.deye_prog1_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog2_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog3_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog4_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog5_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog6_charge", option="No Grid or Gen"),
            ]),
            discharge_start=ActionSet(entity=[
                EntityAction(service="number/set_value", entity_id="number.deye_prog1_capacity", value=10),
                EntityAction(service="number/set_value", entity_id="number.deye_prog2_capacity", value=10),
                EntityAction(service="number/set_value", entity_id="number.deye_prog3_capacity", value=10),
                EntityAction(service="number/set_value", entity_id="number.deye_prog4_capacity", value=10),
                EntityAction(service="number/set_value", entity_id="number.deye_prog5_capacity", value=10),
                EntityAction(service="number/set_value", entity_id="number.deye_prog6_capacity", value=10),
                EntityAction(service="select/select_option", entity_id="select.deye_prog1_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog2_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog3_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog4_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog5_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog6_charge", option="No Grid or Gen"),
            ]),
            discharge_stop=ActionSet(entity=[
                EntityAction(service="number/set_value", entity_id="number.deye_prog1_capacity", value=80),
                EntityAction(service="number/set_value", entity_id="number.deye_prog2_capacity", value=80),
                EntityAction(service="number/set_value", entity_id="number.deye_prog3_capacity", value=80),
                EntityAction(service="number/set_value", entity_id="number.deye_prog4_capacity", value=80),
                EntityAction(service="number/set_value", entity_id="number.deye_prog5_capacity", value=80),
                EntityAction(service="number/set_value", entity_id="number.deye_prog6_capacity", value=80),
            ]),
            solar_only_start=ActionSet(entity=[
                EntityAction(service="number/set_value", entity_id="number.deye_prog1_capacity", value=15),
                EntityAction(service="number/set_value", entity_id="number.deye_prog2_capacity", value=15),
                EntityAction(service="number/set_value", entity_id="number.deye_prog3_capacity", value=15),
                EntityAction(service="number/set_value", entity_id="number.deye_prog4_capacity", value=15),
                EntityAction(service="number/set_value", entity_id="number.deye_prog5_capacity", value=15),
                EntityAction(service="number/set_value", entity_id="number.deye_prog6_capacity", value=15),
                EntityAction(service="select/select_option", entity_id="select.deye_prog1_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog2_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog3_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog4_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog5_charge", option="No Grid or Gen"),
                EntityAction(service="select/select_option", entity_id="select.deye_prog6_charge", option="No Grid or Gen"),
            ]),
            solar_only_stop=ActionSet(entity=[
                EntityAction(service="number/set_value", entity_id="number.deye_prog1_capacity", value=50),
                EntityAction(service="number/set_value", entity_id="number.deye_prog2_capacity", value=50),
                EntityAction(service="number/set_value", entity_id="number.deye_prog3_capacity", value=50),
                EntityAction(service="number/set_value", entity_id="number.deye_prog4_capacity", value=50),
                EntityAction(service="number/set_value", entity_id="number.deye_prog5_capacity", value=50),
                EntityAction(service="number/set_value", entity_id="number.deye_prog6_capacity", value=50),
            ]),
            block_grid_export_start=ActionSet(entity=[
                EntityAction(service="switch/turn_off", entity_id="switch.deye_solar_export"),
                
            ]),
            block_grid_export_stop=ActionSet(entity=[
                EntityAction(service="switch/turn_on", entity_id="switch.deye_solar_export"),
            ]),
            price_based_solar_grid_export=True,
            grid_export_block_threshold=0.015
        ),
        Device(
            name="ev",
            type="ev",
            solar_charge_only=True,
            enable_load_management=True,
            # EV solar-charge tuning (all optional; defaults shown for illustration)
            solar_round_down=False,
            solar_start_margin=200.0,
            solar_stop_debounce=3,
            solar_battery_soc_full=0.0,
            solar_battery_soc_hysteresis=5.0,
            solar_block_battery_discharge=True,
            ev_min_current_limit=6.0,
            ev_max_current_limit=16.0,
            ev_voltage_entity_l1="sensor.peblar_ev_charger_spanning_fase_1",
            ev_voltage_entity_l2="sensor.peblar_ev_charger_spanning_fase_2",
            ev_voltage_entity_l3="sensor.peblar_ev_charger_spanning_fase_3",
            ev_single_phase_line="l2",
            solar_phase_balance_switching=True,
            solar_phase_balance_import_threshold=100.0,
            solar_phase_balance_export_margin=0.0,
            solar_phase_balance_debounce=2,
            # Unblock grid export while the car is below 79% AND the charger is charging or suspended.
            grid_export_unblock_condition=ConditionGroup(
                logic="and",
                conditions=[
                    EntityCondition(entity_id="sensor.id_7_tourer_pro_accu", operator="<", value=79),
                    EntityCondition(entity_id="sensor.peblar_ev_charger_status", operator="in", value=["charging", "suspended"]),
                ],
            ),
            load_management=LoadManagement(
                instantaneous_load_entity="sensor.peblar_ev_charger_vermogen",
                instantaneous_load_entity_unit="W",
                load_priority=2,
                load_limiter_entity="select.device_load_limit",
                load_maximum_watts="8000",
                charge_sign="positive",
                automated_phase_switching=True,
                apply_limit_actions=LoadManagementActions(
                    switch_to_single_phase=ActionSet(entity=[
                        EntityAction(service="switch/turn_on", entity_id="switch.peblar_ev_charger_dwing_enkelvoudige_fase_af")
                    ]),
                    switch_to_three_phase=ActionSet(entity=[
                        EntityAction(service="switch/turn_off", entity_id="switch.peblar_ev_charger_dwing_enkelvoudige_fase_af")
                    ]),
                    apply_limit=ActionSet(entity=[
                        EntityAction(
                            service="number/set_value",
                            entity_id="number.peblar_ev_charger_laadlimiet",
                            value="{int(round(limit_watts / (three_phase*400*sqrt(3)+single_phase*230), 0))}"
                        )
                    ])
                )
            ),
            start=ActionSet(entity=[
                EntityAction(service="switch/turn_on", entity_id="switch.peblar_ev_charger_opladen")
            ]),
            stop=ActionSet(entity=[
                EntityAction(service="switch/turn_off", entity_id="switch.peblar_ev_charger_opladen")
            ])
        )
    ]
    
    return DevicesConfig(devices=default_devices)


# Global instance
devices_config = load_default_config()