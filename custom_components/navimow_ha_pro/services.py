"""Services for Navimow HA Pro.

- ``navimow_ha_pro.set_schedule`` writes one weekday's plan (enabled + one or more
  time periods, each optionally restricted to zones) via the proven
  save-set-data format.
This backs the graphical scheduler card and Home Assistant automations.
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_MOW = "mow"

# Navimow weekday numbering is 1=Sun .. 7=Sat.
_WEEKDAY_TO_NUM = {
    "sunday": 1,
    "monday": 2,
    "tuesday": 3,
    "wednesday": 4,
    "thursday": 5,
    "friday": 6,
    "saturday": 7,
}

_PERIOD_SCHEMA = vol.Schema(
    {
        vol.Required("start"): cv.string,  # "HH:MM"
        vol.Required("end"): cv.string,  # "HH:MM"
        vol.Optional("zones", default=list): vol.All(cv.ensure_list, [vol.Coerce(int)]),
    }
)

SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Required("day"): vol.In(list(_WEEKDAY_TO_NUM)),
        vol.Optional("enabled", default=True): cv.boolean,
        vol.Optional("periods", default=list): vol.All(cv.ensure_list, [_PERIOD_SCHEMA]),
    }
)

MOW_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        # Explicit partition ids to mow, in the desired mowing order.
        vol.Required("zones"): vol.All(cv.ensure_list, [vol.Coerce(int)]),
        # True = restart from scratch / clear progress; False = continue.
        vol.Optional("reset", default=True): cv.boolean,
    }
)

def _hhmm_to_min(value: str) -> int:
    parts = str(value).strip().split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ServiceValidationError(f"Invalid time '{value}' (use HH:MM)")
    return h * 60 + m


def async_setup_services(hass: HomeAssistant) -> None:
    """Register integration services once."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        return

    def _resolve_coordinator(call: ServiceCall):
        store = hass.data.get(DOMAIN) or {}
        coords = [
            coordinator
            for entry_data in store.values()
            if isinstance(entry_data, dict)
            for coordinator in (entry_data.get("coordinators") or {}).values()
        ]
        device_id = call.data.get("device_id")
        if device_id:
            ha_device = dr.async_get(hass).async_get(device_id)
            identifiers = ha_device.identifiers if ha_device else set()
            for coordinator in coords:
                device = coordinator.device
                if (DOMAIN, str(getattr(device, "id", ""))) in identifiers or device_id in {
                    str(getattr(device, "id", "")),
                    str(getattr(device, "serial_number", "")),
                    str(getattr(device, "name", "")),
                }:
                    return coordinator
            raise ServiceValidationError("device_id is not a Navimow HA Pro mower")
        if len(coords) == 1:
            return coords[0]
        raise ServiceValidationError(
            "Multiple Navimow mowers configured: pass device_id to choose one"
        )

    async def _set_schedule(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(call)
        day_num = _WEEKDAY_TO_NUM[call.data["day"]]
        enabled = call.data["enabled"]
        periods = []
        known_zones = set(coordinator.get_discovered_zones())
        for p in call.data.get("periods", []):
            start_min = _hhmm_to_min(p["start"])
            end_min = _hhmm_to_min(p["end"])
            # An end of "00:00" means end-of-day (24:00 = slot 96), never 0.
            if end_min == 0:
                end_min = 1440
            if start_min % 15 or end_min % 15:
                raise ServiceValidationError("Schedule times must use 15-minute increments")
            if end_min <= start_min:
                raise ServiceValidationError("Schedule end time must be after start time")
            zone_ids = list(p.get("zones") or [])
            unknown = set(zone_ids) - known_zones
            if unknown:
                raise ServiceValidationError(
                    f"Unknown mowing partition(s): {sorted(unknown)}"
                )
            periods.append(
                {
                    "start_min": start_min,
                    "end_min": end_min,
                    "zone_ids": zone_ids,
                }
            )
        if enabled and not periods:
            raise ServiceValidationError("An enabled schedule day needs at least one period")
        for first, second in zip(
            sorted(periods, key=lambda period: period["start_min"]),
            sorted(periods, key=lambda period: period["start_min"])[1:],
        ):
            if second["start_min"] < first["end_min"]:
                raise ServiceValidationError("Schedule periods cannot overlap")
        try:
            await coordinator.async_set_day_schedule(
                day=day_num, enabled=enabled, periods=periods
            )
        except Exception as err:  # noqa: BLE001 - surface a clean error to the UI
            raise HomeAssistantError(f"Navimow set_schedule failed: {err}") from err

    async def _mow(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(call)
        zones = [int(z) for z in call.data.get("zones") or []]
        if not zones:
            raise ServiceValidationError("Select at least one mowing zone")

        known_zones = set(coordinator.get_discovered_zones())
        unknown = [zone_id for zone_id in zones if zone_id not in known_zones]
        if unknown:
            raise ServiceValidationError(
                f"Unknown mowing partition(s): {unknown}; known zones: {sorted(known_zones)}"
            )
        if len(set(zones)) != len(zones):
            raise ServiceValidationError("Each mowing zone may only be selected once")

        try:
            await coordinator.async_mow_zones(
                zones, reset=call.data["reset"], ordered=True
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Navimow mow failed: {err}") from err

    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE, _set_schedule, schema=SET_SCHEDULE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_MOW, _mow, schema=MOW_SCHEMA)
