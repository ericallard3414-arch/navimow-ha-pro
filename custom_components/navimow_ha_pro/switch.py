"""Feature-detected private settings switches for Navimow HA Pro."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NavimowCoordinator


@dataclass(frozen=True)
class Setting:
    key: str
    translation_key: str
    icon: str
    write_key: str
    iot: bool = True
    numeric: bool = False
    robot_key: str | None = None
    robot_numeric: bool = True
    assumed: bool = False
    gate_key: str | None = None


SETTINGS = (
    Setting(
        "schedule_enabled",
        "schedule_enabled",
        "mdi:calendar-check",
        "startPlan",
        robot_numeric=False,
    ),
    Setting(
        "night_mow",
        "night_mow",
        "mdi:weather-night",
        "nightMowSwitch",
        numeric=True,
    ),
    Setting(
        "night_light",
        "night_light",
        "mdi:lightbulb-night-outline",
        "lightSwitch",
        numeric=True,
    ),
    Setting(
        "rain_sensor",
        "rain_sensor",
        "mdi:weather-rainy",
        "rainSensor",
        iot=False,
    ),
    Setting(
        "rain_detection",
        "rain_detection",
        "mdi:weather-pouring",
        "rainDetectionSwitch",
        iot=False,
    ),
    Setting(
        "sound",
        "sound",
        "mdi:volume-high",
        "soundSwitch",
        robot_numeric=False,
    ),
    Setting(
        "power_saving",
        "power_saving",
        "mdi:leaf",
        "lowPowerSet",
        numeric=True,
    ),
    Setting(
        "child_lock",
        "child_lock",
        "mdi:account-lock",
        "childLock",
    ),
    Setting(
        "lift_alarm",
        "lift_alarm",
        "mdi:alarm-light",
        "liftSwitch",
    ),
    Setting(
        "mowing_cycle",
        "mowing_cycle",
        "mdi:sync",
        "mowingCycle",
        robot_numeric=False,
    ),
    Setting(
        "frost_delay",
        "frost_delay",
        "mdi:snowflake-alert",
        "frostSwitch",
        numeric=True,
    ),
    Setting(
        "snow_delay",
        "snow_delay",
        "mdi:snowflake",
        "snowSwitch",
        numeric=True,
    ),
    Setting(
        "strong_wind_delay",
        "strong_wind_delay",
        "mdi:weather-windy",
        "stormSwitch",
        numeric=True,
    ),
    Setting(
        "high_temp_delay",
        "high_temp_delay",
        "mdi:thermometer-high",
        "highTempSwitch",
        numeric=True,
    ),
    Setting(
        "efls",
        "efls",
        "mdi:cctv",
        "slamSwitch",
        numeric=True,
    ),
    Setting(
        "obstacle_avoidance",
        "obstacle_avoidance",
        "mdi:eye-off-outline",
        "cptSwitch",
        numeric=True,
    ),
    Setting(
        "traction_control",
        "traction_control",
        "mdi:car-traction-control",
        "tractionControl",
        numeric=True,
        robot_key="tcsSwitch",
    ),
    Setting(
        "rain_forecast",
        "rain_forecast",
        "mdi:weather-cloudy-alert",
        "weatherSwitch",
        numeric=True,
    ),
    Setting(
        "delay_on_rain",
        "delay_on_rain",
        "mdi:timer-sand",
        "delayedPileSwitch",
        numeric=True,
    ),
    Setting(
        "animal_friendly",
        "animal_friendly",
        "mdi:paw",
        "animalProtection",
        numeric=True,
        assumed=True,
        gate_key="obstacle_avoidance",
    ),
    Setting(
        "edge_sense",
        "edge_sense",
        "mdi:vector-line",
        "edgeSense",
        numeric=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]

    entities = []

    for coordinator in coordinators.values():
        values = (
            coordinator.get_private_telemetry().get("settings") or {}
        )

        entities.extend(
            NavimowSettingSwitch(coordinator, setting)
            for setting in SETTINGS
            if values.get(setting.key) is not None
            or (
                setting.assumed
                and values.get(setting.gate_key or "") is not None
            )
        )

    async_add_entities(entities)


class NavimowSettingSwitch(
    CoordinatorEntity[NavimowCoordinator],
    SwitchEntity,
):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: NavimowCoordinator,
        setting: Setting,
    ) -> None:
        super().__init__(coordinator)

        self.setting = setting
        self._attr_assumed_state = setting.assumed
        self._optimistic: bool | None = None

        device = coordinator.device

        self._attr_translation_key = setting.translation_key
        self._attr_icon = setting.icon

        self._attr_unique_id = (
            f"{DOMAIN}_{device.id}_{setting.key}"
        )

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Navimow",
            model=device.model or "Unknown",
            serial_number=device.serial_number or device.id,
        )

    @property
    def is_on(self) -> bool | None:
        if self.setting.assumed:
            return self._optimistic

        return (
            self.coordinator
            .get_private_telemetry()
            .get("settings") or {}
        ).get(self.setting.key)

    async def _write(self, on: bool) -> None:
        try:
            await self.coordinator.async_set_private_switch(
                write_key=self.setting.write_key,
                on=on,
                iot=self.setting.iot,
                numeric=self.setting.numeric,
                robot_key=self.setting.robot_key,
                robot_numeric=self.setting.robot_numeric,
                setting_key=self.setting.key,
            )

            if self.setting.assumed:
                self._optimistic = on
                self.async_write_ha_state()

        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(False)