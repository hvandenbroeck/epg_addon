"""
Example: Programmatically creating device configurations using Pydantic models
"""

from src.devices_config import (
    Device, DevicesConfig, ActionSet, MQTTAction, EntityAction,
    LoadManagement, LoadManagementActions
)

# Example 1: Create a simple heat pump device
wp_device = Device(
    name="wp",
    type="wp",
    enable_load_management=False,
    start=ActionSet(mqtt=[
        MQTTAction(topic="ebusd/700/z2sfmode/set", payload="veto"),
        MQTTAction(topic="ebusd/700/z2quickvetotemp/set", payload="22")
    ]),
    stop=ActionSet(mqtt=[
        MQTTAction(topic="ebusd/700/z2sfmode/set", payload="auto"),
        MQTTAction(topic="ebusd/700/z2quickvetotemp/set", payload="20")
    ])
)

# Example 2: Create an EV charger with load management
ev1_device = Device(
    name="ev1",
    type="ev",
    enable_load_management=True,
    load_management=LoadManagement(
        instantaneous_load_entity="sensor.ev_charger_1_power",
        instantaneous_load_entity_unit="W",
        load_priority=2,
        load_limiter_entity="select.device_load_limit",
        load_maximum_watts="7400",
        charge_sign="positive",
        automated_phase_switching=True,
        apply_limit_actions=LoadManagementActions(
            switch_to_single_phase=ActionSet(entity=[
                EntityAction(
                    service="switch/turn_on",
                    entity_id="switch.ev_charger_1_force_single_phase"
                )
            ]),
            switch_to_three_phase=ActionSet(entity=[
                EntityAction(
                    service="switch/turn_off",
                    entity_id="switch.ev_charger_1_force_single_phase"
                )
            ]),
            apply_limit=ActionSet(entity=[
                EntityAction(
                    service="number/set_value",
                    entity_id="number.ev_charger_1_charge_limit",
                    value="{int(round(limit_watts / (three_phase*400*sqrt(3)+single_phase*230), 0))}"
                )
            ])
        )
    ),
    start=ActionSet(entity=[
        EntityAction(service="switch/turn_on", entity_id="switch.ev_charger_1_charging")
    ]),
    stop=ActionSet(entity=[
        EntityAction(service="switch/turn_off", entity_id="switch.ev_charger_1_charging")
    ])
)

# Example 3: Create a second EV charger (same type, different name)
ev2_device = Device(
    name="ev2",
    type="ev",
    enable_load_management=True,
    load_management=LoadManagement(
        instantaneous_load_entity="sensor.ev_charger_2_power",
        load_priority=3,
        load_maximum_watts="7400",
        charge_sign="positive",
    ),
    start=ActionSet(entity=[
        EntityAction(service="switch/turn_on", entity_id="switch.ev_charger_2_charging")
    ]),
    stop=ActionSet(entity=[
        EntityAction(service="switch/turn_off", entity_id="switch.ev_charger_2_charging")
    ])
)

# Example 4: Create a battery device with separate charge/discharge actions
battery_device = Device(
    name="battery",
    type="battery",
    enable_load_management=True,
    load_management=LoadManagement(
        instantaneous_load_entity="sensor.battery_power",
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
                    entity_id="number.battery_max_charge_current",
                    value="{int(round((limit_watts/51.2), 0))}"
                )
            ])
        )
    ),
    # Battery-specific actions for charging
    charge_start=ActionSet(entity=[
        EntityAction(service="select/select_option", entity_id="select.battery_charge_mode", option="Allow Grid")
    ]),
    charge_stop=ActionSet(entity=[
        EntityAction(service="select/select_option", entity_id="select.battery_charge_mode", option="No Grid")
    ]),
    # Battery-specific actions for discharging
    discharge_start=ActionSet(entity=[
        EntityAction(service="number/set_value", entity_id="number.battery_min_soc", value=10)
    ]),
    discharge_stop=ActionSet(entity=[
        EntityAction(service="number/set_value", entity_id="number.battery_min_soc", value=90)
    ])
)

# Example 5: Create a full configuration
config = DevicesConfig(devices=[wp_device, ev1_device, ev2_device, battery_device])

# Example 6: Access devices
print(f"Total devices: {len(config.devices)}")
print(f"EV devices: {len(config.get_devices_by_type('ev'))}")
print(f"Battery devices: {len(config.get_devices_by_type('battery'))}")

# Example 7: Get device by name
ev1 = config.get_device_by_name("ev1")
if ev1:
    print(f"Found {ev1.name} of type {ev1.type}")

# Example 8: Export to JSON
import json
config_json = json.dumps(
    {"devices": [d.model_dump(exclude_none=True) for d in config.devices]},
    indent=2
)
print("Configuration JSON:")
print(config_json)

# Example 9: Validate configuration
try:
    # This will raise ValidationError if configuration is invalid
    test_config = DevicesConfig(devices=[
        Device(name="invalid", type="wp", start=ActionSet(), stop=ActionSet())
    ])
    print("Configuration is valid!")
except Exception as e:
    print(f"Configuration error: {e}")

# Example 10: Load from JSON file
def load_config_from_file(filepath: str) -> DevicesConfig:
    """Load device configuration from JSON file."""
    with open(filepath, 'r') as f:
        data = json.load(f)
    return DevicesConfig(devices=data.get('devices', []))

# Example 11: Save to JSON file
def save_config_to_file(config: DevicesConfig, filepath: str):
    """Save device configuration to JSON file."""
    with open(filepath, 'w') as f:
        json.dump(
            {"devices": [d.model_dump(exclude_none=True) for d in config.devices]},
            f,
            indent=2
        )

# Example usage:
# config = load_config_from_file('/data/options.json')
# save_config_to_file(config, '/data/options.json')
