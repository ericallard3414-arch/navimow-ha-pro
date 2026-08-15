"""Sensor platform for Navimow integration."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NavimowCoordinator


@dataclass(frozen=True, kw_only=True)
class NavimowSensorEntityDescription(SensorEntityDescription):
    """Describes Navimow sensor entity."""

    value_fn: Callable[[NavimowCoordinator], Any]
    attrs_fn: Callable[[NavimowCoordinator], dict[str, Any] | None] | None = None
    exists_fn: Callable[[NavimowCoordinator], bool] | None = None


def _private(coordinator: NavimowCoordinator) -> dict[str, Any]:
    return (coordinator.data or {}).get("private_telemetry") or {}


def _mapped_area(coordinator: NavimowCoordinator) -> float | None:
    geometry = coordinator.get_map_geometry() or {}
    total = 0.0
    found = False
    for zone in geometry.get("zones") or []:
        points = zone.get("points") if isinstance(zone, dict) else None
        if not isinstance(points, list) or len(points) < 3:
            continue
        area2 = 0.0
        for first, second in zip(points, points[1:] + points[:1]):
            area2 += float(first[0]) * float(second[1]) - float(second[0]) * float(first[1])
        total += abs(area2) / 2.0
        found = True
    return round(total, 2) if found else None


def _state_value(coordinator: NavimowCoordinator) -> str | None:
    state = coordinator.get_device_state()
    value = getattr(state, "state", None) if state else None
    return str(getattr(value, "value", value)) if value is not None else None


def _schedule_summary(coordinator: NavimowCoordinator) -> str:
    schedule = _private(coordinator).get("schedule") or []
    days = [
        str(day.get("weekday"))[:3]
        for day in schedule
        if day.get("enabled") and day.get("periods")
    ]
    return ", ".join(days) if days else "Off"


def _schedule_attributes(coordinator: NavimowCoordinator) -> dict[str, Any]:
    return {
        "days": _private(coordinator).get("schedule") or [],
        "zones": [
            {"id": zone_id, "name": name or f"Zone {zone_id}"}
            for zone_id, name in coordinator.get_discovered_zones().items()
        ],
    }


SENSOR_DESCRIPTIONS: tuple[NavimowSensorEntityDescription, ...] = (
    NavimowSensorEntityDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: (
            state.battery if (state := coordinator.get_device_state()) else None
        ),
    ),
    NavimowSensorEntityDescription(
        key="mowing_progress",
        name="Mowing progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: _location_value(
            coordinator, "mowingPercentage"
        ),
    ),
    NavimowSensorEntityDescription(
        key="position_x",
        name="Position X",
        native_unit_of_measurement="m",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: _location_value(coordinator, "postureX"),
    ),
    NavimowSensorEntityDescription(
        key="position_y",
        name="Position Y",
        native_unit_of_measurement="m",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: _location_value(coordinator, "postureY"),
    ),
    NavimowSensorEntityDescription(
        key="heading",
        name="Heading",
        native_unit_of_measurement="°",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: _heading_value(coordinator),
    ),
    NavimowSensorEntityDescription(
        key="target_zone",
        name="Target zone",
        value_fn=lambda coordinator: _zone_value(coordinator, "targetZone"),
    ),
    NavimowSensorEntityDescription(
        key="mowing_zone",
        name="Current mowing zone",
        value_fn=lambda coordinator: _zone_value(
            coordinator, "currentMowBoundary"
        ),
    ),
    NavimowSensorEntityDescription(
        key="route_progress_raw",
        name="Route progress raw",
        value_fn=lambda coordinator: _location_value(
            coordinator, "currentMowProgress"
        ),
    ),
    NavimowSensorEntityDescription(
        key="coverage",
        name="Coverage",
        icon="mdi:grid",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: (_private(c).get("coverage") or {}).get("percentage"),
        attrs_fn=lambda c: _private(c).get("coverage"),
        exists_fn=lambda c: bool((_private(c).get("coverage") or {}).get("zones")),
    ),
    NavimowSensorEntityDescription(
        key="session_area",
        name="Session area",
        icon="mdi:texture-box",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: _private(c).get("session_area"),
        exists_fn=lambda c: _private(c).get("session_area") is not None,
    ),
    NavimowSensorEntityDescription(
        key="weekly_area",
        name="Area this week",
        icon="mdi:calendar-week",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: _private(c).get("weekly_area"),
        exists_fn=lambda c: _private(c).get("weekly_area") is not None,
    ),
    NavimowSensorEntityDescription(
        key="total_area",
        name="Total area",
        icon="mdi:ruler-square",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_mapped_area,
        exists_fn=lambda c: _mapped_area(c) is not None,
    ),
    NavimowSensorEntityDescription(
        key="schedule",
        name="Schedule",
        icon="mdi:calendar-clock",
        value_fn=_schedule_summary,
        attrs_fn=_schedule_attributes,
        exists_fn=lambda c: bool(_private(c).get("set_list_available")),
    ),
    NavimowSensorEntityDescription(
        key="status",
        name="Status",
        icon="mdi:robot-mower",
        value_fn=_state_value,
    ),
    NavimowSensorEntityDescription(
        key="problem",
        name="Problem",
        icon="mdi:check-circle",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: _private(c).get("problem"),
        attrs_fn=lambda c: {"raw_error": _private(c).get("error")},
        exists_fn=lambda c: "problem" in _private(c),
    ),
    NavimowSensorEntityDescription(
        key="online",
        name="Online",
        icon="mdi:monitor-check",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: "Connected" if _private(c).get("online") else "Disconnected",
        exists_fn=lambda c: _private(c).get("online") is not None,
    ),
    NavimowSensorEntityDescription(
        key="state_code",
        name="State code",
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: _private(c).get("state_code"),
        exists_fn=lambda c: _private(c).get("state_code") is not None,
    ),
    NavimowSensorEntityDescription(
        key="wifi_signal",
        name="Wi-Fi signal",
        icon="mdi:wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: _private(c).get("wifi_signal"),
        exists_fn=lambda c: _private(c).get("wifi_signal") is not None,
    ),
    NavimowSensorEntityDescription(
        key="blades_life",
        name="Blades life",
        icon="mdi:saw-blade",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: ((_private(c).get("maintenance") or {}).get("blades") or {}).get("percentage"),
        attrs_fn=lambda c: ((_private(c).get("maintenance") or {}).get("blades") or {}),
        exists_fn=lambda c: (((_private(c).get("maintenance") or {}).get("blades") or {}).get("percentage") is not None),
    ),
    NavimowSensorEntityDescription(
        key="chassis_life",
        name="Chassis life",
        icon="mdi:car-wrench",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: ((_private(c).get("maintenance") or {}).get("chassis") or {}).get("percentage"),
        attrs_fn=lambda c: ((_private(c).get("maintenance") or {}).get("chassis") or {}),
        exists_fn=lambda c: (((_private(c).get("maintenance") or {}).get("chassis") or {}).get("percentage") is not None),
    ),
)


def _location_value(coordinator: NavimowCoordinator, key: str) -> Any:
    location = coordinator.get_location() or {}
    return location.get(key)


def _zone_value(coordinator: NavimowCoordinator, key: str) -> str | None:
    """Return a friendly zone name while preserving unknown partition IDs."""
    return coordinator.get_zone_label(_location_value(coordinator, key))


def _heading_value(coordinator: NavimowCoordinator) -> float | None:
    value = _location_value(coordinator, "postureTheta")
    try:
        radians = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(radians):
        return None
    return round(math.degrees(radians) % 360, 1)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navimow sensors from a config entry."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    devices = data["devices"]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]

    entities: list[NavimowSensor] = []
    for device in devices:
        coordinator = coordinators[device.id]
        for description in SENSOR_DESCRIPTIONS:
            if description.exists_fn is not None and not description.exists_fn(coordinator):
                continue
            entities.append(
                NavimowSensor(
                    coordinator=coordinator,
                    entity_description=description,
                )
            )
    async_add_entities(entities)


class NavimowSensor(CoordinatorEntity[NavimowCoordinator], SensorEntity):
    """Representation of a Navimow sensor."""

    entity_description: NavimowSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NavimowCoordinator,
        entity_description: NavimowSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description

        device = coordinator.device
        self._attr_unique_id = f"{DOMAIN}_{device.id}_{entity_description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Navimow",
            model=device.model or "Unknown",
            sw_version=device.firmware_version or None,
            serial_number=device.serial_number or device.id,
        )

    @property
    def available(self) -> bool:
        if self.coordinator.get_device_state() is not None:
            return True
        return super().available

    @property
    def native_value(self) -> Any:
        """Return sensor value from coordinator."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the redacted raw location payload for model discovery."""
        if self.entity_description.attrs_fn is not None:
            return self.entity_description.attrs_fn(self.coordinator)
        if self.entity_description.key not in {
            "mowing_progress",
            "target_zone",
            "mowing_zone",
        }:
            return None
        location = self.coordinator.get_location() or {}
        attributes = {
            key: value
            for key, value in location.items()
            if key.lower() not in {"token", "access_token", "refresh_token", "password"}
        }
        zone_key = {
            "target_zone": "targetZone",
            "mowing_zone": "currentMowBoundary",
        }.get(self.entity_description.key)
        if zone_key:
            raw_partition_id = location.get(zone_key)
            attributes["partition_id"] = raw_partition_id
            attributes["zone_name"] = self.coordinator.get_zone_label(
                raw_partition_id
            )
        return attributes
