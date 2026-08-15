"""Zone and feature-detected setting selectors for Navimow HA Pro."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NavimowCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the prepared-zone and supported settings selectors."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]
    entities: list[SelectEntity] = []
    for coordinator in coordinators.values():
        entities.append(NavimowPreparedZoneSelect(coordinator))
        settings = coordinator.get_private_telemetry().get("settings") or {}
        entities.extend(
            NavimowSettingSelect(coordinator, description)
            for description in SETTING_SELECTS
            if settings.get(description.key) is not None
        )
    async_add_entities(entities)


@dataclass(frozen=True)
class SettingSelect:
    """Describe one enumerated mower setting."""

    key: str
    name: str
    icon: str
    write_key: str
    options: dict[str, int]
    robot_numeric: bool
    cloud_string: bool = False


SETTING_SELECTS = (
    SettingSelect(
        "night_light_level",
        "Night light brightness",
        "mdi:brightness-6",
        "nightLightLevel",
        {"Dim": 0, "Very dim": 1},
        False,
    ),
    SettingSelect(
        "weather_sensitivity",
        "Rain sensitivity",
        "mdi:weather-partly-rainy",
        "weatherSensitivity",
        {"Drizzle": 0, "Light rain": 1, "Moderate": 2},
        True,
    ),
    SettingSelect(
        "work_mode",
        "Work mode",
        "mdi:speedometer",
        "mode",
        {
            "Precision Mowing": 2,
            "Standard Mowing": 4,
            "Efficient Mowing": 3,
        },
        True,
        True,
    ),
)


class NavimowPreparedZoneSelect(
    CoordinatorEntity[NavimowCoordinator], SelectEntity
):
    """Select a discovered partition without sending a mower command."""

    _attr_has_entity_name = True
    _attr_name = "Prepared mowing zone"
    _attr_icon = "mdi:map-marker-path"

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{DOMAIN}_{device.id}_prepared_zone"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Navimow",
            model=device.model or "Unknown",
            sw_version=device.firmware_version or None,
            serial_number=device.serial_number or device.id,
        )

    def _option_map(self) -> dict[str, int]:
        output: dict[str, int] = {}
        for zone_id, friendly_name in self.coordinator.get_discovered_zones().items():
            label = (
                f"{friendly_name} (partition {zone_id})"
                if friendly_name
                else f"Partition {zone_id}"
            )
            output[label] = zone_id
        return output

    @property
    def options(self) -> list[str]:
        """Return all partitions learned from live mower telemetry."""
        return list(self._option_map())

    @property
    def current_option(self) -> str | None:
        """Return the prepared partition label."""
        selected_id = self.coordinator.get_selected_zone_id()
        if selected_id is None:
            return None
        for label, zone_id in self._option_map().items():
            if zone_id == selected_id:
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        """Persist the prepared partition; this deliberately does not mow."""
        option_map = self._option_map()
        if option not in option_map:
            raise ValueError(f"Unknown zone option: {option}")
        await self.coordinator.async_select_zone(option_map[option])

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the exact, unambiguous device partition registry."""
        return {
            "selected_partition_id": self.coordinator.get_selected_zone_id(),
            "discovered_partitions": list(
                self.coordinator.get_discovered_zones()
            ),
            "command_enabled": self.coordinator.can_mow_prepared_zone(),
            "note": "Use the Mow prepared zone button to clear progress and mow this partition.",
        }


class NavimowSettingSelect(CoordinatorEntity[NavimowCoordinator], SelectEntity):
    """An enumerated setting supported by this mower model."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: NavimowCoordinator, description: SettingSelect
    ) -> None:
        super().__init__(coordinator)
        self.setting = description
        device = coordinator.device
        self._attr_name = description.name
        self._attr_icon = description.icon
        self._attr_options = list(description.options)
        self._reverse_options = {
            value: option for option, value in description.options.items()
        }
        self._attr_unique_id = f"{DOMAIN}_{device.id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Navimow",
            model=device.model or "Unknown",
            serial_number=device.serial_number or device.id,
        )

    @property
    def current_option(self) -> str | None:
        """Return the friendly option for the mower's numeric value."""
        value = (self.coordinator.get_private_telemetry().get("settings") or {}).get(
            self.setting.key
        )
        return self._reverse_options.get(value)

    async def async_select_option(self, option: str) -> None:
        """Apply and persist the selected mower setting."""
        if option not in self.setting.options:
            raise HomeAssistantError(f"Unknown {self.setting.name} option: {option}")
        try:
            await self.coordinator.async_set_private_select(
                write_key=self.setting.write_key,
                value=self.setting.options[option],
                robot_numeric=self.setting.robot_numeric,
                cloud_string=self.setting.cloud_string,
                setting_key=self.setting.key,
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
