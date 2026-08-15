"""Button platform for verified Navimow private-cloud actions."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
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
    """Set up one selected-zone mow button per mower."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]
    async_add_entities(
        NavimowMowPreparedZoneButton(coordinator)
        for coordinator in coordinators.values()
    )


class NavimowMowPreparedZoneButton(
    CoordinatorEntity[NavimowCoordinator], ButtonEntity
):
    """Start only the partition chosen in Prepared mowing zone."""

    _attr_has_entity_name = True
    _attr_name = "Mow prepared zone"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{DOMAIN}_{device.id}_mow_prepared_zone"
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
        """Enable only when a valid mapped partition is prepared."""
        return super().available and self.coordinator.can_mow_prepared_zone()

    async def async_press(self) -> None:
        """Clear prior progress and start the selected partition."""
        try:
            await self.coordinator.async_mow_prepared_zone()
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
