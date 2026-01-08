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
    # Battery-specific configuration (only used when type='battery')
    battery_soc_entity: Optional[str] = Field(default=None, description="Home Assistant entity for battery state of charge (%)")
    battery_capacity_kwh: Optional[float] = Field(default=None, description="Battery capacity in kWh")
    battery_charge_speed_kw: Optional[float] = Field(default=None, description="Battery charge speed in kW")
    battery_min_soc_percent: Optional[float] = Field(default=20.0, description="Minimum battery SOC in percent")
    battery_max_soc_percent: Optional[float] = Field(default=80.0, description="Maximum battery SOC in percent")


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
            enable_load_management=False,
            start=ActionSet(mqtt=[
                MQTTAction(topic="ebusd/700/z2sfmode/set", topic_get="ebusd/700/z2sfmode/get", payload="veto"),
                MQTTAction(topic="ebusd/700/z2quickvetotemp/set", topic_get="ebusd/700/z2quickvetotemp/get", payload="22")
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
            battery_max_soc_percent=90.0,
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
                EntityAction(service="number/set_value", entity_id="number.deye_prog1_capacity", value=90),
                EntityAction(service="number/set_value", entity_id="number.deye_prog2_capacity", value=90),
                EntityAction(service="number/set_value", entity_id="number.deye_prog3_capacity", value=90),
                EntityAction(service="number/set_value", entity_id="number.deye_prog4_capacity", value=90),
                EntityAction(service="number/set_value", entity_id="number.deye_prog5_capacity", value=90),
                EntityAction(service="number/set_value", entity_id="number.deye_prog6_capacity", value=90),
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
                EntityAction(service="number/set_value", entity_id="number.deye_prog1_capacity", value=90),
                EntityAction(service="number/set_value", entity_id="number.deye_prog2_capacity", value=90),
                EntityAction(service="number/set_value", entity_id="number.deye_prog3_capacity", value=90),
                EntityAction(service="number/set_value", entity_id="number.deye_prog4_capacity", value=90),
                EntityAction(service="number/set_value", entity_id="number.deye_prog5_capacity", value=90),
                EntityAction(service="number/set_value", entity_id="number.deye_prog6_capacity", value=90),
            ])
        ),
        Device(
            name="ev",
            type="ev",
            enable_load_management=True,
            load_management=LoadManagement(
                instantaneous_load_entity="sensor.peblar_ev_charger_vermogen",
                instantaneous_load_entity_unit="W",
                load_priority=2,
                load_limiter_entity="select.device_load_limit",
                load_maximum_watts="6000",
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