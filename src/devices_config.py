# Device actions configuration
device_actions = {
    "wp": {
        "start": {
            "mqtt": [
                {"topic": "ebusd/700/z2sfmode/set", "payload": "veto"},
                {"topic": "ebusd/700/z2quickvetotemp/set", "payload": "22"}
            ]
        },
        "stop": {
            "mqtt": [
                {"topic": "ebusd/700/z2sfmode/set", "payload": "auto"},
                {"topic": "ebusd/700/z2quickvetotemp/set", "payload": "20"}
            ]
        }
    },
    "hw": {
        "start": {
            "mqtt": [{"topic": "ebusd/700/HwcTempDesired/set", "payload": "60"}]
        },
        "stop": {
            "mqtt": [{"topic": "ebusd/700/HwcTempDesired/set", "payload": "50"}]
        }
    },
    "bat_charge": {
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
        "start": {
            "entity": [
                {
                    "service": "switch/turn_on",
                    "entity_id": "switch.ev_charger"
                }
            ]
        },
        "stop": {
            "entity": [
                {
                    "service": "switch/turn_off",
                    "entity_id": "switch.ev_charger"
                }
            ]
        }
    }
}