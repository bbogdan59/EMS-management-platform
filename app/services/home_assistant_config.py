"""Generate an explicit HA package, without discovery or actuator actions."""

import json

from app.services.home_assistant_service import context_topic, topic_root


def package_yaml(connection, mappings):
    automations = []
    for mapping in mappings:
        if mapping.kind == "occupancy":
            valid = "s in ['on', 'off']"
            value = "s == 'on'"
        elif mapping.kind.endswith("_status"):
            valid = "s in ['on', 'off', 'idle', 'heating', 'cooling', 'defrosting']"
            value = "s"
        else:
            valid = "is_number(s)"
            value = "s"
        quality = "declared" if mapping.kind == "flexibility" else "estimated"
        payload = (
            "{{ {'schema_version': 1, 'entity_id': entity, 'sample_id': observed, "
            "'observed_at': observed, 'source': 'home_assistant', "
            f"'kind': '{mapping.kind}', 'unit': '{mapping.unit}', "
            f"'quality': '{quality}' if ({valid}) else 'unknown', "
            f"'available': ({valid}), 'value': ({value}) if ({valid}) else none"
            "} | to_json }}"
        )
        automations.append(
            {
                "id": "ems_context_" + mapping.id.hex,
                "alias": "EMS context: " + mapping.entity_id,
                "mode": "restart",
                "triggers": [
                    {"trigger": "state", "entity_id": mapping.entity_id},
                    {"trigger": "time_pattern", "seconds": "/30"},
                    {"trigger": "homeassistant", "event": "start"},
                ],
                "variables": {
                    "entity": mapping.entity_id,
                    "s": "{{ states(entity) }}",
                    "observed": "{{ states[entity].last_reported.isoformat() if states[entity] is not none else utcnow().isoformat() }}",
                },
                "actions": [
                    {
                        "action": "mqtt.publish",
                        "data": {
                            "topic": context_topic(connection, mapping),
                            "qos": 1,
                            "retain": True,
                            "payload": payload,
                        },
                    }
                ],
            }
        )
    package = {"automation": automations}
    if connection.publish_enabled:
        package["mqtt"] = {
            "sensor": [
                {
                    "name": "EMS control mode",
                    "unique_id": "ems_mode_" + str(connection.station_id),
                    "state_topic": topic_root(connection) + "/ems/state",
                    "value_template": "{{ value_json.control_mode if value_json.control_mode is not none else 'unknown' }}",
                    "json_attributes_topic": topic_root(connection) + "/ems/state",
                    "json_attributes_template": "{{ {'recommendations': value_json.recommendations, 'expires_at': value_json.expires_at, 'capabilities': value_json.capabilities} | to_json }}",
                    "expire_after": 120,
                }
            ]
        }
    # JSON is a YAML subset; quoting generated templates avoids YAML injection/escaping bugs.
    return (
        "# EMS Home Assistant package · contract v1\n# Include under homeassistant.packages; replace this file after changing mappings.\n"
        + json.dumps(package, ensure_ascii=False, indent=2)
        + "\n"
    )
