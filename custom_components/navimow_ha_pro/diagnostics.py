"""Redacted diagnostics for Navimow Complete."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .const import DOMAIN, INTEGRATION_VERSION

SECRET_KEYS = {"access_token", "refresh_token", "token", "password", "pwdinfo"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "**REDACTED**" if key.lower() in SECRET_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    data = hass.data[DOMAIN][entry.entry_id]
    output: dict[str, Any] = {
        "integration_version": INTEGRATION_VERSION,
        "entry": _redact(dict(entry.data)),
        "smart_home_capabilities": _redact(
            data.get("smart_home_capabilities")
        ),
        "map_discovery": _redact(data.get("map_discovery")),
        "devices": [],
    }
    for device in data.get("devices", []):
        coordinator = data["coordinators"].get(device.id)
        location = coordinator.get_location() if coordinator else None
        trail = coordinator.get_trail() if coordinator else []
        state = coordinator.get_device_state() if coordinator else None
        attributes = coordinator.get_device_attributes() if coordinator else None
        geometry = coordinator.get_map_geometry() if coordinator else None
        terrain = coordinator.get_terrain_map() if coordinator else None
        output["devices"].append(
            {
                "id": device.id,
                "name": device.name,
                "model": device.model,
                "firmware": device.firmware_version,
                "serial_present": bool(device.serial_number),
                "location": _redact(location),
                "state": _redact(state.to_dict() if state else None),
                "attributes": _redact(
                    attributes.to_dict() if attributes else None
                ),
                "trail_points": len(trail),
                "mqtt_connected": bool(data.get("sdk") and data["sdk"].is_connected),
                "mqtt_topics": _redact(
                    coordinator.get_mqtt_topics() if coordinator else {}
                ),
                "location_stream": (
                    coordinator.get_location_recovery_diagnostics()
                    if coordinator
                    else {}
                ),
                "location_messages_by_type": _redact(
                    coordinator.get_location_messages_by_type()
                    if coordinator
                    else {}
                ),
                "command_discovery_events": _redact(
                    coordinator.get_command_discovery_events()
                    if coordinator
                    else []
                ),
                "experimental_zone_command": _redact(
                    coordinator.get_experimental_zone_command_diagnostics()
                    if coordinator
                    else None
                ),
                "private_telemetry": _redact(
                    coordinator.get_private_telemetry() if coordinator else {}
                ),
                "zone_registry": (
                    {
                        "zones": coordinator.get_discovered_zones(),
                        "selected_id": coordinator.get_selected_zone_id(),
                    }
                    if coordinator
                    else {}
                ),
                "map_geometry_summary": (
                    {
                        "available": True,
                        "zone_count": len(geometry.get("zones", [])),
                        "zone_ids": [
                            zone.get("id")
                            for zone in geometry.get("zones", [])
                            if isinstance(zone, dict)
                        ],
                        "path_count": len(geometry.get("paths", [])),
                        "dock_available": geometry.get("dock") is not None,
                    }
                    if isinstance(geometry, dict)
                    else {
                        "available": False,
                        "zone_count": 0,
                        "zone_ids": [],
                        "path_count": 0,
                        "dock_available": False,
                    }
                ),
                "lidar_terrain_summary": (
                    {
                        "available": True,
                        "width": terrain[1].get("width"),
                        "height": terrain[1].get("height"),
                        "pixels_per_meter": terrain[1].get("pixels_per_meter"),
                    }
                    if terrain is not None
                    else {"available": False}
                ),
            }
        )
    return output
