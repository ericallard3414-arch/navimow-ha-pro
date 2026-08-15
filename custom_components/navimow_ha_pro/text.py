"""Editable friendly names for discovered Navimow mowing zones."""
from __future__ import annotations

from homeassistant.components.text import TextEntity, TextMode
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
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one editable name field for every authoritative map zone."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]
    async_add_entities(
        NavimowZoneNameText(coordinator, zone_id)
        for coordinator in coordinators.values()
        for zone_id in coordinator.get_discovered_zones()
    )


class NavimowZoneNameText(CoordinatorEntity[NavimowCoordinator], TextEntity):
    """Editable friendly name backed by the coordinator's persistent store."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:map-marker-edit"
    _attr_mode = TextMode.TEXT
    _attr_native_min = 1
    _attr_native_max = 80

    def __init__(self, coordinator: NavimowCoordinator, zone_id: int) -> None:
        super().__init__(coordinator)
        self.zone_id = zone_id
        device = coordinator.device
        self._attr_name = f"Zone {zone_id} name"
        self._attr_unique_id = f"{DOMAIN}_{device.id}_zone_{zone_id}_name"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Navimow",
            model=device.model or "Unknown",
            serial_number=device.serial_number or device.id,
        )

    @property
    def native_value(self) -> str:
        """Return the saved friendly name or the stable default label."""
        names = self.coordinator.get_discovered_zones()
        return names.get(self.zone_id) or f"Zone {self.zone_id}"

    async def async_set_value(self, value: str) -> None:
        """Persist a friendly name and publish it across all mower entities."""
        try:
            await self.coordinator.async_set_zone_name(self.zone_id, value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Keep the mower's immutable protocol identifier visible."""
        return {"partition_id": self.zone_id}
