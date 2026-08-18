"""Feature-detected numeric settings for Navimow HA Pro."""
from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NavimowCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]
    entities = []
    for coordinator in coordinators.values():
        settings = coordinator.get_private_telemetry().get("settings") or {}
        if settings.get("return_battery_level") is not None:
            entities.append(NavimowSettingNumber(coordinator, "return_battery_level", "Return-to-dock battery", "mdi:battery-arrow-down", "returnBatteryLevel"))
        if settings.get("charging_limit") is not None:
            entities.append(NavimowSettingNumber(coordinator, "charging_limit", "Charging limit", "mdi:battery-charging-high", "chargingLimit"))
        if settings.get("rain_delay_wire") is not None:
            entities.append(NavimowSettingNumber(coordinator, "rain_delay_wire", "Rain delay time", "mdi:timer-pause", "delayedPileSet", scale=4, cloud_hex=True, unit="h"))
        if settings.get("cutting_height") is not None and (coordinator.get_private_telemetry().get("limits") or {}).get("cutting_height_values"):
            entities.append(NavimowSettingNumber(coordinator, "cutting_height", "Global cutting height", "mdi:grass", "height", unit=UnitOfLength.MILLIMETERS))
    async_add_entities(entities)


class NavimowSettingNumber(CoordinatorEntity[NavimowCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_step = 5

    def __init__(self, coordinator: NavimowCoordinator, key: str, name: str, icon: str, write_key: str, *, scale: int = 1, cloud_hex: bool = False, unit: str = PERCENTAGE) -> None:
        super().__init__(coordinator)
        self.key, self.write_key, self.scale, self.cloud_hex = key, write_key, scale, cloud_hex
        self._height_values: list[int] = []
        self._height_family: str | None = None
        device = coordinator.device
        limits = coordinator.get_private_telemetry().get("limits") or {}
        self._attr_name, self._attr_icon, self._attr_native_unit_of_measurement = name, icon, unit
        if key == "return_battery_level":
            self._attr_native_min_value, self._attr_native_max_value = limits.get("return_battery_min", 5), limits.get("return_battery_max", 50)
        elif key == "charging_limit":
            self._attr_native_min_value, self._attr_native_max_value = limits.get("charging_limit_min", 50), limits.get("charging_limit_max", 100)
        elif key == "cutting_height":
            self._attr_device_class = NumberDeviceClass.DISTANCE
            heights = sorted({
                int(value)
                for value in (limits.get("cutting_height_values") or [50, 100])
            })
            self._height_values = heights
            model = str(device.model or "").strip().lower()
            self._height_family = "quarter_inch" if model.startswith("x") else "five_mm"
            self._attr_native_min_value, self._attr_native_max_value = min(heights), max(heights)
            differences = [b - a for a, b in zip(heights, heights[1:]) if b > a]
            # X-series metric labels are rounded quarter-inch positions and
            # therefore alternate between 6 and 7 mm. A 1 mm HA step permits
            # every advertised value; the card uses the exact discrete list.
            self._attr_native_step = (
                differences[0]
                if differences and len(set(differences)) == 1
                else 1
            )
        else:
            self._attr_native_min_value, self._attr_native_max_value, self._attr_native_step, self._attr_mode = 1, 12, 1, NumberMode.BOX
        self._attr_unique_id = f"{DOMAIN}_{device.id}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device.id)}, name=device.name, manufacturer="Navimow", model=device.model or "Unknown", serial_number=device.serial_number or device.id)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        if self.key != "cutting_height":
            return {}
        return {
            "navimow_supported_values_mm": self._height_values,
            "navimow_height_family": self._height_family,
        }

    @property
    def native_value(self) -> float | None:
        value = (self.coordinator.get_private_telemetry().get("settings") or {}).get(self.key)
        return None if value is None else float(value) / self.scale

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.async_set_private_number(write_key=self.write_key, displayed_value=value, scale=self.scale, cloud_hex=self.cloud_hex, setting_key=self.key)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
