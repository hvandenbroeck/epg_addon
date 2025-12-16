# Device actions configuration
# 
# Verification fields:
# - For MQTT actions: 
#   - topic_get: MQTT topic to read current value (optional, derived from topic by replacing /set with /get if not specified)
#   - payload_check: Value to check against (optional, defaults to payload value)
# - For entity actions:
#   - value_check: Value to verify against entity state (optional, defaults to value/option)
#   - state_attribute: Attribute to check instead of state (optional)
#
device_actions = {
    "wp": {
        "enable_load_management": False,
        "start": {
            "mqtt": [
                {"topic": "ebusd/700/z2sfmode/set", "topic_get": "ebusd/700/z2sfmode/get", "payload": "veto"},
                {"topic": "ebusd/700/z2quickvetotemp/set", "topic_get": "ebusd/700/z2quickvetotemp/get", "payload": "22"}
            ]
        },
        "stop": {
            "mqtt": [
                {"topic": "ebusd/700/z2sfmode/set", "topic_get": "ebusd/700/z2sfmode/get", "payload": "auto"},
                {"topic": "ebusd/700/z2quickvetotemp/set", "topic_get": "ebusd/700/z2quickvetotemp/get", "payload": "20"}
            ]
        }
    },
    "hw": {
        "enable_load_management": False,
        "start": {
            "mqtt": [{"topic": "ebusd/700/HwcTempDesired/set", "topic_get": "ebusd/700/HwcTempDesired/get", "payload": "60"}]
        },
        "stop": {
            "mqtt": [{"topic": "ebusd/700/HwcTempDesired/set", "topic_get": "ebusd/700/HwcTempDesired/get", "payload": "50"}]
        }
    },
    "bat_charge": {
        "enable_load_management": True,
        "load_management": {
            "instantaneous_load_entity": "sensor.deye_battery_power",
            "instantaneous_load_entity_unit": "W",
            "load_priority": 1,
            "load_limiter_entity": "select.device_load_limit",
            "load_maximum_watts": "3500",
            "charge_sign": "negative",
            "automated_phase_switching": False,
            "apply_limit_actions": {
                "apply_limit": {
                    "entity": [
                        {
                            "service": "number/set_value",
                            "entity_id": "number.deye_battery_max_charge_current",
                            "value": "{int(round((limit_watts/51.2), 0))}"
                        }
                    ]
                }
            }
        },
        "start": {
            "entity": [
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog1_capacity",
                    "value": 80
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog2_capacity",
                    "value": 80
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog3_capacity",
                    "value": 80
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog4_capacity",
                    "value": 80
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog5_capacity",
                    "value": 80
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog6_capacity",
                    "value": 80
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog1_charge",
                    "option": "Allow Grid"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog2_charge",
                    "option": "Allow Grid"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog3_charge",
                    "option": "Allow Grid"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog4_charge",
                    "option": "Allow Grid"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog5_charge",
                    "option": "Allow Grid"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog6_charge",
                    "option": "Allow Grid"
                }
            ]
        },
        "stop": {
            "entity": [
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog1_charge",
                    "option": "No Grid or Gen"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog2_charge",
                    "option": "No Grid or Gen"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog3_charge",
                    "option": "No Grid or Gen"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog4_charge",
                    "option": "No Grid or Gen"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog5_charge",
                    "option": "No Grid or Gen"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog6_charge",
                    "option": "No Grid or Gen"
                }
            ]
        }
    },
    "bat_discharge": {
        "enable_load_management": False,
        "start": {
            "entity": [
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog1_capacity",
                    "value": 20
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog2_capacity",
                    "value": 20
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog3_capacity",
                    "value": 20
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog4_capacity",
                    "value": 20
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog5_capacity",
                    "value": 20
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog6_capacity",
                    "value": 20
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog1_charge",
                    "option": "No Grid or Gen"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog2_charge",
                    "option": "No Grid or Gen"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog3_charge",
                    "option": "No Grid or Gen"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog4_charge",
                    "option": "No Grid or Gen"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog5_charge",
                    "option": "No Grid or Gen"
                },
                {
                    "service": "select/select_option",
                    "entity_id": "select.deye_prog6_charge",
                    "option": "No Grid or Gen"
                }
            ]
        },
        "stop": {
            "entity": [
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog1_capacity",
                    "value": 80
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog2_capacity",
                    "value": 80
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog3_capacity",
                    "value": 80
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog4_capacity",
                    "value": 80
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog5_capacity",
                    "value": 80
                },
                {
                    "service": "number/set_value",
                    "entity_id": "number.deye_prog6_capacity",
                    "value": 80
                }
            ]
        }
    },
    "ev": {
        "enable_load_management": True,
        "load_management": {
            "instantaneous_load_entity": "sensor.peblar_ev_charger_vermogen",
            "instantaneous_load_entity_unit": "W",
            "load_priority": 2,
            "load_limiter_entity": "select.device_load_limit",
            "load_maximum_watts": "6000",
            "charge_sign": "positive",
            "automated_phase_switching": True,
            "apply_limit_actions": {
                "switch_to_single_phase": {
                    "entity": [
                        {
                            "service": "switch/turn_on",
                            "entity_id": "switch.peblar_ev_charger_dwing_enkelvoudige_fase_af"
                        }
                    ]
                },
                "switch_to_three_phase": {
                    "entity": [
                        {
                            "service": "switch/turn_off",
                            "entity_id": "switch.peblar_ev_charger_dwing_enkelvoudige_fase_af"
                        }
                    ]
                },
                "apply_limit": {
                    "entity": [
                        {
                            "service": "number/set_value",
                            "entity_id": "number.peblar_ev_charger_laadlimiet",
                            "value": "{int(round(limit_watts / (three_phase*400*sqrt(3)+single_phase*230), 0))}"
                        }
                    ]
                }
            }
        },
        "start": {
            "entity": [
                {
                    "service": "switch/turn_on",
                    "entity_id": "switch.peblar_ev_charger_opladen"
                }
            ]
        },
        "stop": {
            "entity": [
                {
                    "service": "switch/turn_off",
                    "entity_id": "switch.peblar_ev_charger_opladen"
                }
            ]
        }
    }
}