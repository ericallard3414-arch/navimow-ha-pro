"""DataUpdateCoordinator for Navimow integration."""
import asyncio
import logging
import math
import json
import time
import re
import io
import zipfile
import urllib.request
import hashlib
from urllib.parse import urlsplit
from urllib.parse import parse_qs, unquote_plus
from pathlib import Path
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.storage import Store

from mower_sdk.api import MowerAPI
from mower_sdk.models import (
    Device,
    DeviceAttributesMessage,
    DeviceStateMessage,
    DeviceStatus,
)
from mower_sdk.sdk import NavimowSDK

from .const import (
    DOMAIN,
    HTTP_FALLBACK_MIN_INTERVAL,
    MQTT_STALE_SECONDS,
    UPDATE_INTERVAL,
)
from .api import NavimowCloudClient, NavimowError

_LOGGER = logging.getLogger(__name__)


class NavimowCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Navimow data updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        sdk: NavimowSDK,
        api: MowerAPI,
        device: Device,
        oauth_session: config_entry_oauth2_flow.OAuth2Session | None = None,
        discovery_payloads: Any | None = None,
        private_client: NavimowCloudClient | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.sdk = sdk
        self.api = api
        self.device = device
        self.oauth_session = oauth_session
        self._discovery_payloads = discovery_payloads
        self._private_client = private_client
        self.data: dict[str, Any] = {}
        self._last_state: DeviceStateMessage | None = None
        self._last_attributes: DeviceAttributesMessage | None = None
        self._last_mqtt_update: float | None = None
        self._last_http_fetch: float | None = None
        self._last_data_source: str | None = None
        self._location: dict[str, Any] | None = None
        self._trail: list[list[float]] = []  # [x, y, partition_id] in schema v3
        # Exact swept paths downloaded from Navimow's official trail API.
        # Kept separately from the high-frequency live trail so the camera can
        # render server-authoritative geometry without sacrificing a live pose.
        self._official_trail_groups: list[dict[str, Any]] = []
        self._official_trail_signature: dict[int, tuple[int | None, int | None, float | None]] = {}
        self._last_official_trail_fetch: float = 0.0
        # Fresh-session guard for official server trail. After a reset mowing
        # command, old server path data can remain available briefly. Hide it
        # until path-info-time reports a new startTime for the commanded zone.
        self._official_session_waiting: bool = False
        self._official_session_zone_ids: set[int] = set()
        self._official_session_baseline_start: dict[int, int | None] = {}
        # Persist the last authoritative per-zone completion percentage.
        # Navimow can temporarily report 0/empty progress after docking even
        # though the unfinished job remains resumable.  Keeping the last
        # server value makes the dashboard resume logic stable across idle/
        # charging transitions and Home Assistant restarts.
        self._zone_progress_cache: dict[int, float] = {}
        # Zones from the most recent dashboard mowing request.  This lets the
        # live map distinguish blade-on lawn coverage from simple transit.
        self._commanded_zone_ids: list[int] = []
        # Short rolling pose buffer used to backfill the first in-zone metres
        # once Navimow confirms the active mowing boundary. This avoids losing
        # the beginning of the mowing trail without ever painting transit.
        self._recent_pose_samples: list[tuple[float, float, float]] = []
        self._trail_gate_active: bool = False
        self._trail_gate_boundary: int | None = None
        # Trail format v2: only blade-on live mowing samples are rendered.
        # Older releases persisted generic movement/travel points, so those
        # legacy caches must be discarded once after upgrading.
        self._trail_schema_version: int = 3
        self._mqtt_topics: dict[str, Any] = {}
        self._command_discovery_events: list[dict[str, Any]] = []
        self._location_messages_by_type: dict[str, Any] = {}
        self._last_location_update: float | None = None
        self._last_location_recovery: float | None = None
        self._location_recovery_count = 0
        self._last_saved = 0.0
        self._discovered_zone_ids: set[int] = set()
        # Set only after a successful private-cloud map-detail response.  Once
        # present, polygon-backed map partitions are the source of truth and
        # transient telemetry sentinel values (notably 0 while docked) must
        # never become selectable zones.
        self._authoritative_zone_ids: set[int] | None = None
        self._zone_names: dict[int, str] = {}
        self._selected_zone_id: int | None = None
        self._map_geometry: dict[str, Any] | None = None
        self._terrain_image: bytes | None = None
        self._terrain_metadata: dict[str, Any] | None = None
        self._last_experimental_zone_command: dict[str, Any] | None = None
        self._private_telemetry: dict[str, Any] = {}
        self._private_slow_raw: dict[str, Any] = {}
        self._private_poll_cycle = 0
        # Keep user-requested configuration values authoritative while the
        # Navimow cloud is still returning its previous set-list snapshot.
        # This prevents unrelated setting writes from temporarily rolling
        # numbers (especially cutting height) back to stale values.
        # value, deadline, pre-write value, accept_external_change
        self._pending_setting_values: dict[str, tuple[Any, float, Any, bool]] = {}
        self._fast_settings_task: asyncio.Task | None = None
        self._fast_location_task: asyncio.Task | None = None
        self._fast_location_deadline: float = 0.0
        self._fast_location_last_signature: tuple[Any, ...] | None = None
        self._fast_location_stale_count: int = 0
        # Weather/task-delay messages can be extremely brief.  Keep the last
        # positive delay event latched so the dashboard can still explain why
        # the mower returned to the dock after the live taskDelay field clears.
        self._last_task_delay: Any | None = None
        self._last_task_delay_monotonic: float = 0.0
        self._last_task_delay_epoch_ms: int | None = None
        self._interruption_notice: dict[str, Any] | None = None
        # A user-requested RETURN HOME must never be re-labelled as a weather
        # interruption just because an older taskDelay is still latched.
        self._manual_return_home_until: float = 0.0
        self._store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}.{device.id}.map"
        )

    async def async_setup(self) -> None:
        """Register callbacks from SDK."""
        saved = await self._store.async_load()
        if isinstance(saved, dict):
            # Load saved polygons before migrating schema-v2 trail points so
            # each existing mowing point can be assigned to its lawn.
            self._map_geometry = self._validate_map_geometry(saved.get("map_geometry"))
            saved_trail_schema = self._integer(saved.get("trail_schema_version")) or 1
            raw_trail = saved.get("trail")
            if saved_trail_schema >= 2 and isinstance(raw_trail, list):
                # Schema v2 already contained blade-on mowing points, but did
                # not tag them with their partition.  Schema v3 stores the
                # partition with every point so starting a different lawn can
                # never erase another lawn's completed/partial work.
                migrated: list[list[float]] = []
                for item in raw_trail[-12000:]:
                    if not isinstance(item, (list, tuple)) or len(item) < 2:
                        continue
                    x = self._finite(item[0])
                    y = self._finite(item[1])
                    if x is None or y is None:
                        continue
                    zone_id = self._integer(item[2]) if len(item) >= 3 else None
                    if zone_id is None:
                        zone_id = self._zone_id_for_point(float(x), float(y))
                    if zone_id is not None:
                        migrated.append([round(float(x), 3), round(float(y), 3), int(zone_id)])
                self._trail = migrated[-12000:]
            elif saved_trail_schema < 2:
                # Schema v1 could include transit movement. Never import it as
                # cut coverage.
                self._trail = []
                self._official_trail_groups = []
            raw_official = saved.get("official_trail_groups")
            if saved_trail_schema >= 2 and isinstance(raw_official, list):
                self._official_trail_groups = self._validate_official_trail_groups(raw_official)
            raw_zones = saved.get("zones")
            if isinstance(raw_zones, dict):
                for raw_id, raw_name in raw_zones.items():
                    zone_id = self._integer(raw_id)
                    if zone_id is None:
                        continue
                    self._discovered_zone_ids.add(zone_id)
                    if isinstance(raw_name, str) and raw_name.strip():
                        self._zone_names[zone_id] = raw_name.strip()
            selected_zone_id = self._integer(saved.get("selected_zone_id"))
            if selected_zone_id is not None:
                self._selected_zone_id = selected_zone_id
                self._discovered_zone_ids.add(selected_zone_id)
            raw_commanded = saved.get("commanded_zone_ids")
            if isinstance(raw_commanded, list):
                self._commanded_zone_ids = [
                    zone_id
                    for item in raw_commanded
                    if (zone_id := self._integer(item)) is not None
                ]
            raw_progress = saved.get("zone_progress_cache")
            if isinstance(raw_progress, dict):
                for raw_id, raw_pct in raw_progress.items():
                    zone_id = self._integer(raw_id)
                    pct = self._finite(raw_pct)
                    if zone_id is not None and pct is not None:
                        self._zone_progress_cache[zone_id] = max(0.0, min(100.0, float(pct)))
            self._map_geometry = self._validate_map_geometry(
                saved.get("map_geometry")
            )
        if self._map_geometry is None:
            await self._async_import_map_file()
        if self._private_client is not None:
            await self._async_refresh_cloud_geometry()
            await self._async_refresh_cloud_terrain()
            await self._async_refresh_private_telemetry(force_slow=True)
            self._fast_settings_task = self.hass.async_create_task(
                self._async_fast_settings_refresh()
            )
        await self._async_import_terrain_files()
        if self._discover_zones_from_payload(self._discovery_payloads):
            await self._async_save_store()
        self.sdk.on_state(self._handle_state)
        self.sdk.on_attributes(self._handle_attributes)

    def _build_data(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "state": self._last_state,
            "attributes": self._last_attributes,
            "location": self._location,
            "trail_schema_version": self._trail_schema_version,
            "trail": self._trail,
            "official_trail_groups": self._official_trail_groups,
            "map_geometry": self._map_geometry,
            "private_telemetry": self._private_telemetry,
            "zones": {
                "discovered_ids": sorted(self._discovered_zone_ids),
                "names": {
                    str(zone_id): self._zone_names[zone_id]
                    for zone_id in sorted(self._zone_names)
                },
                "selected_id": self._selected_zone_id,
            },
            "meta": {
                "last_data_source": self._last_data_source,
                "last_mqtt_update_monotonic": self._last_mqtt_update,
                "last_http_fetch_monotonic": self._last_http_fetch,
                "last_task_delay": self._last_task_delay,
                "last_task_delay_age_s": (
                    round(time.monotonic() - self._last_task_delay_monotonic, 1)
                    if self._last_task_delay_monotonic else None
                ),
                "last_task_delay_epoch_ms": self._last_task_delay_epoch_ms,
                "interruption_notice": self._interruption_notice,
            },
        }

    def _device_status_to_state(self, status: DeviceStatus) -> DeviceStateMessage:
        error: dict[str, Any] | None = None
        if status.error_code and status.error_code.value != "none":
            error = {
                "code": status.error_code.value,
                "message": status.error_message,
            }
        return DeviceStateMessage(
            device_id=status.device_id,
            timestamp=status.timestamp,
            state=status.status.value,
            battery=status.battery,
            signal_strength=status.signal_strength,
            position=status.position,
            error=error,
            metrics=None,
        )

    async def _async_ensure_valid_token(self) -> str | None:
        if not self.oauth_session:
            return None
        try:
            token: dict[str, Any] | None
            if hasattr(self.oauth_session, "async_ensure_token_valid"):
                await self.oauth_session.async_ensure_token_valid()
                token = self.oauth_session.token
            elif hasattr(self.oauth_session, "async_get_valid_token"):
                token = await self.oauth_session.async_get_valid_token()
            else:
                token = self.oauth_session.token
        except ConfigEntryAuthFailed:
            # 确定性认证失败（refresh_token 缺失或被服务端拒绝）→ 直接上报，让 HA 引导用户重新认证
            raise
        except Exception as err:
            # 瞬态错误（网络超时、DNS 等）→ 不立即触发重新认证流程。
            # 尝试沿用缓存中的 access_token；若缓存也不可用才升级为认证失败。
            _LOGGER.warning(
                "Token refresh failed (likely transient), falling back to cached token: %s", err
            )
            cached = getattr(self.oauth_session, "token", None)
            if cached and cached.get("access_token"):
                token = cached
            else:
                raise ConfigEntryAuthFailed(
                    f"Token refresh failed and no cached token available: {err}"
                ) from err
        if not token or not token.get("access_token"):
            raise ConfigEntryAuthFailed("No access token after refresh")
        access_token = token["access_token"]
        self.api.set_token(access_token)
        return access_token

    async def _async_update_data(self) -> dict[str, Any]:
        # 每次 update 都主动刷新 token，确保 api._token 与 oauth_session 保持同步。
        # 若仅在 HTTP fallback 时刷新，MQTT 正常推数据期间 token 长期不更新，
        # 过期后用户下发指令会立即收到 CODE_OAUTH_INFO_ILLEGAL。
        try:
            await self._async_ensure_valid_token()
        except ConfigEntryAuthFailed:
            raise

        cached_state = self.sdk.get_cached_state(self.device.id)
        if cached_state is not None:
            self._last_state = cached_state
            self._last_data_source = "mqtt_cache"

        cached_attrs = self.sdk.get_cached_attributes(self.device.id)
        if cached_attrs is not None:
            self._last_attributes = cached_attrs

        now = time.monotonic()
        is_mqtt_stale = (
            self._last_mqtt_update is None
            or now - self._last_mqtt_update > MQTT_STALE_SECONDS
        )
        can_http_fetch = (
            self._last_http_fetch is None
            or now - self._last_http_fetch > HTTP_FALLBACK_MIN_INTERVAL
        )
        if is_mqtt_stale and can_http_fetch:
            try:
                status = await self.api.async_get_device_status(self.device.id)
                self._last_state = self._device_status_to_state(status)
                self._last_http_fetch = now
                self._last_data_source = "http_fallback"
            except ConfigEntryAuthFailed:
                raise
            except Exception as err:
                _LOGGER.warning(
                    "HTTP fallback failed for device %s: %s", self.device.id, err
                )

        if self._private_client is not None:
            await self._async_refresh_private_telemetry()

        _LOGGER.debug(
            "Coordinator update: device=%s source=%s mqtt_ts=%s http_ts=%s",
            self.device.id,
            self._last_data_source,
            self._last_mqtt_update,
            self._last_http_fetch,
        )
        self.data = self._build_data()
        return self.data

    @staticmethod
    def _private_find(value: Any, *keys: str) -> Any:
        """Return the first matching value in a private-cloud payload."""
        if isinstance(value, dict):
            for key in keys:
                if key in value and value[key] is not None:
                    return value[key]
            for item in value.values():
                found = NavimowCoordinator._private_find(item, *keys)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = NavimowCoordinator._private_find(item, *keys)
                if found is not None:
                    return found
        return None

    def _coverage_start_times(self, coverage: Any) -> dict[int, int | None]:
        """Return partition -> server session startTime from path-info-time."""
        result: dict[int, int | None] = {}
        if not isinstance(coverage, list):
            return result
        for item in coverage:
            if not isinstance(item, dict):
                continue
            zone_id = self._integer(item.get("partitionId"))
            if zone_id is None:
                continue
            result[zone_id] = self._integer(item.get("startTime"))
        return result

    def _official_session_accepts(self, coverage: Any) -> bool:
        """Release official trail only after the server exposes the new job."""
        if not self._official_session_waiting:
            return True
        starts = self._coverage_start_times(coverage)
        for zone_id in self._official_session_zone_ids:
            current = starts.get(zone_id)
            baseline = self._official_session_baseline_start.get(zone_id)
            if current is not None and (baseline is None or current > baseline):
                self._official_session_waiting = False
                _LOGGER.info(
                    "Official trail fresh session detected: zone=%s start=%s baseline=%s",
                    zone_id, current, baseline,
                )
                return True
        return False

    async def _async_fast_settings_refresh(self) -> None:
        """Refresh mutable cloud settings without accelerating heavy telemetry."""
        client = self._private_client
        serial = str(getattr(self.device, "serial_number", None) or self.device.id or "")
        if client is None or not serial:
            return
        try:
            while True:
                await asyncio.sleep(5.0)
                try:
                    set_list = await self.hass.async_add_executor_job(
                        client.set_list, serial
                    )
                except (NavimowError, OSError, ValueError, json.JSONDecodeError) as err:
                    _LOGGER.debug(
                        "Fast private settings refresh failed for %s: %s",
                        serial,
                        err,
                    )
                    continue
                raw = dict(self._private_slow_raw)
                raw["set_list"] = set_list
                self._private_slow_raw = raw
                normalized = self._normalize_private_telemetry(raw)
                new_settings = normalized.get("settings") or {}
                old_settings = (self._private_telemetry or {}).get("settings") or {}
                if new_settings != old_settings:
                    telemetry = dict(self._private_telemetry or {})
                    telemetry["settings"] = new_settings
                    self._private_telemetry = telemetry
                    self.data = self._build_data()
                    self.async_set_updated_data(self.data)
        except asyncio.CancelledError:
            raise
        finally:
            self._fast_settings_task = None

    def stop_background_tasks(self) -> None:
        """Cancel coordinator-owned polling and bootstrap tasks."""
        for task in (self._fast_settings_task, self._fast_location_task):
            if task is not None and not task.done():
                task.cancel()

    async def _async_refresh_private_telemetry(self, force_slow: bool = False) -> None:
        """Fetch and normalize private read-only mower telemetry."""
        client = self._private_client
        serial = str(getattr(self.device, "serial_number", None) or self.device.id or "")
        if client is None or not serial:
            return
        self._private_poll_cycle += 1
        slow = force_slow or not self._private_slow_raw or self._private_poll_cycle % 20 == 0

        def _fetch() -> dict[str, Any]:
            raw = dict(self._private_slow_raw)
            auth_list = client.auth_list() if slow or "auth_list" not in raw else raw["auth_list"]
            raw["auth_list"] = auth_list
            auth_item = next(
                (
                    item for item in auth_list
                    if isinstance(item, dict)
                    and str(item.get("vehicle_sn") or item.get("sn") or "") == serial
                ),
                auth_list[0] if isinstance(auth_list, list) and auth_list else {},
            )
            vehicle_type = self._integer(
                self._private_find(auth_item, "vehicle_type", "vehicleType", "type")
            ) or 0
            raw["index2"] = client.index2(serial)
            try:
                raw["location"] = client.location(serial, vehicle_type)
            except NavimowError:
                raw.setdefault("location", {})
            try:
                raw["coverage"] = client.path_info_time(serial)
            except NavimowError:
                raw.setdefault("coverage", [])

            # Official Android app flow: coverage/time metadata first, then
            # fetch Zstd path geometry for the partitions that need it.  On
            # startup/slow refresh fetch every known partition once; while a
            # zone is in progress, refresh only that incomplete partition.
            coverage_items = raw.get("coverage") if isinstance(raw.get("coverage"), list) else []
            known_ids: list[int] = []
            incomplete_ids: list[int] = []
            for item in coverage_items:
                if not isinstance(item, dict):
                    continue
                zone_id = self._integer(item.get("partitionId"))
                if zone_id is None:
                    continue
                if zone_id not in known_ids:
                    known_ids.append(zone_id)
                try:
                    percentage = float(item.get("partitionPercentage"))
                except (TypeError, ValueError):
                    percentage = 100.0
                if percentage < 100.0 and zone_id not in incomplete_ids:
                    incomplete_ids.append(zone_id)

            session_ready = self._official_session_accepts(coverage_items)
            if self._official_session_waiting and not session_ready:
                fetch_ids = []
            elif self._official_session_zone_ids:
                fetch_ids = [z for z in known_ids if z in self._official_session_zone_ids]
            else:
                fetch_ids = known_ids if (slow or not self._official_trail_groups) else incomplete_ids
            if fetch_ids:
                try:
                    raw["official_path_data"] = client.path_info_data(serial, fetch_ids)
                    raw["official_path_ids"] = fetch_ids
                except (NavimowError, OSError, ValueError):
                    pass
            # Settings are mutable from both Home Assistant and the official
            # Navimow app. Fetch them on every coordinator cycle so app-originated
            # changes appear promptly and a successful HA write cannot later be
            # overwritten by an old slow-cache snapshot.
            try:
                raw["set_list"] = client.set_list(serial)
            except NavimowError:
                raw.setdefault("set_list", {})

            if slow:
                getters = {
                    "maintenance": lambda: client.maintenance(serial),
                    "device_info": lambda: client.device_info(serial),
                    "today_plan": lambda: client.today_plan(serial, vehicle_type),
                }
                for key, getter in getters.items():
                    try:
                        raw[key] = getter()
                    except NavimowError:
                        raw.setdefault(key, {})
            return raw

        try:
            raw = await self.hass.async_add_executor_job(_fetch)
        except (NavimowError, OSError, ValueError, json.JSONDecodeError) as err:
            _LOGGER.warning("Private telemetry refresh failed for %s: %s", serial, err)
            return
        if slow:
            # Do not retain the potentially large path blob in the slow-cache.
            self._private_slow_raw = {
                key: value for key, value in raw.items()
                if key not in {"official_path_data", "official_path_ids"}
            }
        progress_changed = self._update_zone_progress_cache(raw.get("coverage"))
        trail_changed = False
        if "official_path_data" in raw:
            trail_changed = self._merge_official_trail_payload(
                raw.get("official_path_data"),
                raw.get("official_path_ids") or [],
            )
            if trail_changed:
                self._last_official_trail_fetch = time.time()
        if progress_changed or trail_changed:
            await self._async_save_store()
        self._private_telemetry = self._normalize_private_telemetry(raw)
        private_location = raw.get("location")
        if isinstance(private_location, (dict, list)):
            self.handle_location(private_location, source="private_location")

    def _normalize_private_telemetry(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Create a stable, model-independent read-only telemetry snapshot."""
        index2 = raw.get("index2") or {}
        location = raw.get("location") or {}
        auth_list = raw.get("auth_list") or []
        auth = auth_list[0] if isinstance(auth_list, list) and auth_list else {}
        coverage_items = raw.get("coverage") if isinstance(raw.get("coverage"), list) else []

        def number(value: Any) -> float | None:
            try:
                result = float(value)
            except (TypeError, ValueError):
                return None
            return result if math.isfinite(result) else None

        total = finished = 0.0
        coverage_zones: list[dict[str, Any]] = []
        for item in coverage_items:
            if not isinstance(item, dict):
                continue
            area = number(item.get("area")) or 0.0
            done = number(item.get("finishedArea")) or 0.0
            zone_id = self._integer(item.get("partitionId"))
            pct = number(item.get("partitionPercentage"))
            total += area
            finished += done
            coverage_zones.append({
                "id": zone_id,
                "name": self.get_zone_label(zone_id),
                "area": round(area, 2),
                "finished_area": round(done, 2),
                "percentage": round(pct, 1) if pct is not None else None,
            })
        coverage_pct = round(100 * finished / total, 1) if total > 0 else None

        maintenance = raw.get("maintenance") or {}
        set_list = raw.get("set_list") or {}
        device_info = raw.get("device_info") or {}

        # X-series mowers report the robot-side height as hexadecimal text
        # (for example "41" means 0x41 = 65 mm). Other models/cloud snapshots
        # use ordinary decimal values. Resolve that ambiguity against the
        # mower's authoritative supported-height list instead of guessing.
        raw_height_values = self._private_find(device_info, "mowingHeightList")
        cutting_height_values = (
            sorted({
                height
                for value in raw_height_values
                if (height := self._integer(value)) is not None
            })
            if isinstance(raw_height_values, list)
            else []
        )
        raw_cutting_height = self._private_find(set_list, "height")
        cutting_height = self._integer(raw_cutting_height)
        if isinstance(raw_cutting_height, str) and cutting_height_values:
            try:
                hex_height = int(raw_cutting_height.strip(), 16)
            except ValueError:
                hex_height = None
            if (
                cutting_height not in cutting_height_values
                and hex_height in cutting_height_values
            ):
                cutting_height = hex_height

        def boolean(value: Any) -> bool | None:
            if value is None:
                return None
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"1", "01", "true", "on", "yes"}:
                return True
            if text in {"0", "00", "false", "off", "no", ""}:
                return False
            return None

        setting_specs = {
            "schedule_enabled": ("startPlan", "start_plan"),
            "night_mow": ("nightMowSwitch", "night_mow_switch"),
            "rain_sensor": ("rainSensor", "rain_sensor"),
            "rain_detection": ("rainDetectionSwitch", "rain_detection_switch"),
            "sound": ("soundSwitch", "sound_switch"),
            "power_saving": ("lowPowerSet", "low_power_set"),
            "child_lock": ("childLock", "child_lock"),
            "lift_alarm": ("liftSwitch", "lift_switch"),
            "mowing_cycle": ("mowingCycle", "mowing_cycle"),
            "frost_delay": ("frostSwitch", "frost_switch"),
            "snow_delay": ("snowSwitch", "snow_switch"),
            "strong_wind_delay": ("stormSwitch", "storm_switch"),
            "high_temp_delay": ("highTempSwitch", "high_temp_switch"),
            "efls": ("slamSwitch", "slam_switch"),
            "obstacle_avoidance": ("cptSwitch", "cpt_switch"),
            "traction_control": ("tractionControl", "traction_control"),
            "rain_forecast": ("weatherSwitch", "weather_switch"),
            "delay_on_rain": ("delayedPileSwitch", "delayed_pile_switch"),
        }
        settings = {
            name: boolean(self._private_find(set_list, *keys))
            for name, keys in setting_specs.items()
        }
        settings.update({
            "return_battery_level": self._integer(self._private_find(set_list, "returnBatteryLevel", "return_battery_level")),
            "charging_limit": self._integer(self._private_find(set_list, "chargingLimit", "charging_limit")),
            "rain_delay_wire": self._integer(self._private_find(set_list, "delayedPileSet", "delayed_pile_set")),
            "cutting_height": cutting_height,
            "night_light_level": self._integer(self._private_find(set_list, "nightLightLevel", "night_light_level")),
            "weather_sensitivity": self._integer(self._private_find(set_list, "weatherSensitivity", "weather_sensitivity")),
            # Captured from the official app: Precision/Standard/Efficient is stored
            # in MowerSettingBean ``mode`` (values 2/4/3 respectively).
            "work_mode": self._integer(self._private_find(set_list, "mode")),
        })
        # Navimow's set-list endpoint can lag a successful write by several
        # seconds.  While a value is pending, ignore that stale snapshot.  The
        # pending value is cleared as soon as the cloud reports the requested
        # value, or after a conservative timeout.
        now_pending = time.monotonic()
        for pending_key, pending in list(self._pending_setting_values.items()):
            pending_value, pending_until, previous_value, accept_external = pending
            current_value = settings.get(pending_key)
            if current_value == pending_value:
                # The cloud acknowledged our write.
                self._pending_setting_values.pop(pending_key, None)
            elif (
                accept_external
                and current_value is not None
                and current_value != previous_value
            ):
                # A different value than both the pre-write snapshot and our
                # requested value is a genuine external/app change, not stale
                # cloud data. Accept it immediately.
                self._pending_setting_values.pop(pending_key, None)
            elif now_pending < pending_until:
                settings[pending_key] = pending_value
            else:
                self._pending_setting_values.pop(pending_key, None)
        battery_config = self._private_find(device_info, "batteryConfig") or {}
        limits = {
            "return_battery_min": self._integer(self._private_find(battery_config, "returnBatteryLevelMin")) or 5,
            "return_battery_max": self._integer(self._private_find(battery_config, "returnBatteryLevelMax")) or 50,
            "charging_limit_min": self._integer(self._private_find(battery_config, "chargingLimitMin")) or 50,
            "charging_limit_max": self._integer(self._private_find(battery_config, "chargingLimitMax")) or 100,
        }
        limits["cutting_height_values"] = cutting_height_values
        def life(component: Any) -> dict[str, Any]:
            if not isinstance(component, dict):
                return {"percentage": None}
            hours = number(component.get("setTime"))
            used_min = number(component.get("usedTime"))
            pct = None
            if hours and used_min is not None:
                pct = round(max(0.0, 100.0 * (1.0 - used_min / (hours * 60))), 1)
            return {"percentage": pct, "interval_hours": hours, "used_minutes": used_min}

        state_code = self._private_find(index2, "vehicle_state", "vehicleState")
        network_status = self._integer(self._private_find(index2, "network_status", "networkStatus"))
        error = self._private_find(index2, "error_data", "errorData", "error_list")
        schedule = self._parse_private_schedule(set_list)
        return {
            "state_code": state_code,
            "online": None if network_status is None else network_status == 1,
            "wifi_signal": self._integer(self._private_find(index2, "network_signal_wifi", "networkSignalWifi")),
            "signal": self._integer(self._private_find(index2, "network_signal", "networkSignal")),
            "session_area": number(self._private_find(location, "subtotal_area", "subtotalArea")),
            "weekly_area": number(self._private_find(location, "mowing_week_area", "mowingWeekArea")),
            "coverage": {"percentage": coverage_pct, "total_area": round(total, 2), "finished_area": round(finished, 2), "zones": coverage_zones},
            "error": error,
            "problem": "OK" if not error else "Problem",
            "maintenance": {"blades": life(maintenance.get("knife")), "chassis": life(maintenance.get("chassis"))},
            "set_list_available": isinstance(raw.get("set_list"), dict) and bool(raw.get("set_list")),
            "settings": settings,
            "limits": limits,
            "vehicle_type": self._integer(self._private_find(auth, "vehicle_type", "vehicleType", "type")) or 0,
            "schedule_enabled": self._private_find(raw.get("set_list") or {}, "startPlan", "start_plan"),
            "schedule": schedule,
            "today_plan": self._sanitize_mqtt(raw.get("today_plan") or {}),
            "device_info": self._sanitize_mqtt(raw.get("device_info") or {}),
            "auth_battery": self._integer(self._private_find(auth, "soc", "battery")),
        }

    @staticmethod
    def _official_point(value: Any) -> list[float] | None:
        """Normalize one official trail point without guessing unrelated arrays."""
        try:
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                x = float(value[0])
                y = float(value[1])
                if math.isfinite(x) and math.isfinite(y) and abs(x) < 10000 and abs(y) < 10000:
                    return [round(x, 4), round(y, 4)]
            if isinstance(value, dict):
                pairs = (
                    ("x", "y"),
                    ("postureX", "postureY"),
                    ("pointX", "pointY"),
                    ("longitudeX", "latitudeY"),
                )
                for x_key, y_key in pairs:
                    if x_key in value and y_key in value:
                        x = float(value[x_key])
                        y = float(value[y_key])
                        if math.isfinite(x) and math.isfinite(y) and abs(x) < 10000 and abs(y) < 10000:
                            return [round(x, 4), round(y, 4)]
        except (TypeError, ValueError, OverflowError):
            return None
        return None

    @classmethod
    def _extract_official_point_sets(cls, value: Any, *, hinted: bool = False) -> list[list[list[float]]]:
        """Find coordinate sequences inside one decompressed official path record."""
        groups: list[list[list[float]]] = []
        if isinstance(value, list):
            direct = [cls._official_point(item) for item in value]
            if hinted and len(value) >= 2 and all(point is not None for point in direct):
                clean: list[list[float]] = []
                for point in direct:
                    assert point is not None
                    if not clean or point != clean[-1]:
                        clean.append(point)
                if len(clean) >= 2:
                    return [clean]
            for item in value:
                groups.extend(cls._extract_official_point_sets(item, hinted=hinted))
            return groups
        if not isinstance(value, dict):
            return groups
        for key, child in value.items():
            lower = str(key).replace("_", "").lower()
            child_hinted = hinted or any(token in lower for token in ("path", "point", "track", "trail", "coord"))
            groups.extend(cls._extract_official_point_sets(child, hinted=child_hinted))
        return groups

    @classmethod
    def _parse_official_trail_payload(cls, payload: Any, requested_ids: list[int]) -> list[dict[str, Any]]:
        """Normalize the app's decompressed trail JSON into partitioned groups."""
        records: list[Any]
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            candidate = None
            for key in ("data", "list", "paths", "pathList", "workPathList", "records"):
                if isinstance(payload.get(key), list):
                    candidate = payload.get(key)
                    break
            records = candidate if isinstance(candidate, list) else [payload]
        else:
            return []

        output: list[dict[str, Any]] = []
        fallback_ids = [int(value) for value in requested_ids]
        for index, record in enumerate(records):
            zone_id = None
            if isinstance(record, dict):
                for key in ("partitionId", "partition_id", "partition", "zoneId", "zone_id", "id"):
                    try:
                        if key in record:
                            zone_id = int(record[key])
                            break
                    except (TypeError, ValueError):
                        pass
            if zone_id is None and len(records) == len(fallback_ids) and index < len(fallback_ids):
                zone_id = fallback_ids[index]
            point_sets = cls._extract_official_point_sets(record, hinted=False)
            for points in point_sets:
                if len(points) >= 2:
                    output.append({"partition_id": zone_id, "points": points})
        return cls._validate_official_trail_groups(output)

    @classmethod
    def _validate_official_trail_groups(cls, value: Any) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        if not isinstance(value, list):
            return output
        for item in value:
            if not isinstance(item, dict):
                continue
            try:
                zone_id = int(item.get("partition_id")) if item.get("partition_id") is not None else None
            except (TypeError, ValueError):
                zone_id = None
            points: list[list[float]] = []
            for raw_point in item.get("points") or []:
                point = cls._official_point(raw_point)
                if point is not None and (not points or point != points[-1]):
                    points.append(point)
            if len(points) >= 2:
                output.append({"partition_id": zone_id, "points": points[-20000:]})
        return output

    def _merge_official_trail_payload(self, payload: Any, requested_ids: list[int]) -> bool:
        parsed = self._parse_official_trail_payload(payload, requested_ids)
        if not parsed:
            _LOGGER.debug("Official Navimow trail payload decoded but no point groups were recognized")
            return False

        requested = {int(value) for value in requested_ids}
        retained = [
            group for group in self._official_trail_groups
            if group.get("partition_id") not in requested
        ]
        merged = self._validate_official_trail_groups(retained + parsed)
        if merged == self._official_trail_groups:
            return False
        self._official_trail_groups = merged
        _LOGGER.info(
            "Official Navimow trail updated: partitions=%s groups=%s points=%s",
            sorted({group.get("partition_id") for group in merged if group.get("partition_id") is not None}),
            len(merged),
            sum(len(group.get("points") or []) for group in merged),
        )
        return True

    def _update_zone_progress_cache(self, coverage: Any) -> bool:
        """Remember authoritative non-stale per-zone mowing progress."""
        if not isinstance(coverage, list):
            return False
        changed = False
        starts = self._coverage_start_times(coverage)
        for item in coverage:
            if not isinstance(item, dict):
                continue
            zone_id = self._integer(item.get("partitionId"))
            pct = self._finite(item.get("partitionPercentage"))
            if zone_id is None or pct is None:
                continue
            value = max(0.0, min(100.0, float(pct)))

            # During START FRESH the cloud may replay the previous session for
            # several polls.  Do not let that historical percentage repopulate
            # the resume UI until a genuinely newer startTime appears.
            if self._official_session_waiting and zone_id in self._official_session_zone_ids:
                baseline = self._official_session_baseline_start.get(zone_id)
                current = starts.get(zone_id)
                if baseline is not None and (current is None or current <= baseline):
                    continue

            # A transient 0 while the mower is docked/idle is not evidence that
            # an unfinished cloud job was erased.  Explicit START FRESH clears
            # the cache itself below, so passive zeroes are ignored when a
            # positive resumable value is already known.
            previous = self._zone_progress_cache.get(zone_id)
            if value == 0.0 and previous is not None and 0.0 < previous < 100.0:
                continue
            if previous != value:
                self._zone_progress_cache[zone_id] = value
                changed = True
        return changed

    def get_zone_progress_map(self) -> dict[int, float]:
        """Return stable authoritative server mowing progress by partition."""
        output: dict[int, float] = dict(self._zone_progress_cache)
        coverage = None
        if isinstance(self._private_telemetry, dict):
            coverage = self._private_telemetry.get("coverage")
        if coverage is None and isinstance(self._private_slow_raw, dict):
            coverage = self._private_slow_raw.get("coverage")
        if isinstance(coverage, list):
            starts = self._coverage_start_times(coverage)
            for item in coverage:
                if not isinstance(item, dict):
                    continue
                zone_id = self._integer(item.get("partitionId"))
                pct = self._finite(item.get("partitionPercentage"))
                if zone_id is None or pct is None:
                    continue
                if self._official_session_waiting and zone_id in self._official_session_zone_ids:
                    baseline = self._official_session_baseline_start.get(zone_id)
                    current = starts.get(zone_id)
                    if baseline is not None and (current is None or current <= baseline):
                        continue
                value = max(0.0, min(100.0, float(pct)))
                cached = output.get(zone_id)
                if value == 0.0 and cached is not None and 0.0 < cached < 100.0:
                    continue
                output[zone_id] = value
        return output

    def get_resumable_zone_ids(self) -> list[int]:
        """Return last-commanded zones that still have unfinished work."""
        progress = self.get_zone_progress_map()
        return [
            int(zone_id)
            for zone_id in self._commanded_zone_ids
            if 0.0 < progress.get(int(zone_id), 0.0) < 100.0
        ]

    def get_official_trail_groups(self) -> list[dict[str, Any]]:
        return [
            {"partition_id": group.get("partition_id"), "points": [list(point) for point in group.get("points") or []]}
            for group in self._official_trail_groups
        ]

    def _parse_private_schedule(self, set_list: Any) -> list[dict[str, Any]]:
        """Normalize workPlanV2 into UI-friendly weekday entries."""
        plan = self._private_find(set_list, "workPlanV2", "plan_v2", "plan")
        if not isinstance(plan, list):
            return []
        weekdays = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
        output: list[dict[str, Any]] = []
        for entry in plan:
            if not isinstance(entry, dict):
                continue
            day = self._integer(entry.get("day"))
            if day is None or not 1 <= day <= 7:
                continue
            periods: list[dict[str, Any]] = []
            for period in entry.get("period") or entry.get("periodList") or []:
                if isinstance(period, dict):
                    start = self._integer(period.get("start_time", period.get("startTime")))
                    end = self._integer(period.get("end_time", period.get("endTime")))
                    raw_ids = period.get("partition_ids", period.get("partitionIds")) or []
                elif isinstance(period, (list, tuple)) and len(period) >= 2:
                    start, end = self._integer(period[0]), self._integer(period[1])
                    raw_ids = []
                else:
                    continue
                if start is None or end is None:
                    continue
                zone_ids = [
                    zone_id
                    for item in raw_ids
                    if (zone_id := self._integer(item)) is not None
                ]
                periods.append(
                    {
                        "start_min": start * 15,
                        "end_min": end * 15,
                        "start_hhmm": f"{(start * 15 // 60) % 24:02d}:{start * 15 % 60:02d}",
                        "end_hhmm": f"{(end * 15 // 60) % 24:02d}:{end * 15 % 60:02d}",
                        "zone_ids": zone_ids,
                        "zone_names": [self.get_zone_label(zone_id) for zone_id in zone_ids]
                        or ["All zones"],
                    }
                )
            output.append(
                {
                    "day": day,
                    "weekday": weekdays[day - 1],
                    "enabled": bool(self._integer(entry.get("open")) or 0),
                    "periods": periods,
                }
            )
        return output

    def _set_pending_setting(self, key: str | None, value: Any, timeout: float = 180.0) -> None:
        """Optimistically hold config values until Navimow's set-list catches up.

        A write to *any* setting can make the cloud briefly return an older
        snapshot for unrelated settings.  Preserve the current snapshot for a
        short guard window, then hold the setting that was actually changed for
        longer until it is acknowledged.
        """
        if not key:
            return
        now = time.monotonic()
        telemetry = dict(self._private_telemetry or {})
        settings = dict(telemetry.get("settings") or {})
        for stable_key, stable_value in settings.items():
            if stable_value is not None and stable_key not in self._pending_setting_values:
                self._pending_setting_values[stable_key] = (
                    stable_value,
                    now + 20.0,
                    stable_value,
                    False,
                )
        previous_value = settings.get(key)
        self._pending_setting_values[key] = (
            value,
            now + timeout,
            previous_value,
            True,
        )
        settings[key] = value
        telemetry["settings"] = settings
        self._private_telemetry = telemetry
        self.data = self._build_data()
        self.async_set_updated_data(self.data)

    async def async_set_private_switch(
        self,
        *,
        write_key: str,
        on: bool,
        iot: bool,
        numeric: bool = False,
        robot_key: str | None = None,
        robot_numeric: bool = True,
        setting_key: str | None = None,
    ) -> None:
        """Apply one feature-detected setting using the official two-channel write."""
        client = self._private_client
        if client is None:
            raise ValueError("Private Navimow control is unavailable")
        serial = str(getattr(self.device, "serial_number", None) or self.device.id or "")
        vehicle_type = self._integer(self._private_telemetry.get("vehicle_type")) or 0

        def _write() -> None:
            if iot:
                robot_value: Any = (1 if on else 0) if robot_numeric else ("1" if on else "0")
                client.send_setting_device(serial, {robot_key or write_key: robot_value})
                client.set_iot_bool(serial, vehicle_type, write_key, on, numeric)
            else:
                client.set_bool_setting(serial, write_key, on)

        await self.hass.async_add_executor_job(_write)
        self._set_pending_setting(setting_key, bool(on))

    async def async_set_private_number(
        self, *, write_key: str, displayed_value: float, scale: int = 1, cloud_hex: bool = False, setting_key: str | None = None
    ) -> None:
        """Apply a numeric setting with its official robot/cloud encodings."""
        client = self._private_client
        if client is None:
            raise ValueError("Private Navimow control is unavailable")
        serial = str(getattr(self.device, "serial_number", None) or self.device.id or "")
        vehicle_type = self._integer(self._private_telemetry.get("vehicle_type")) or 0
        wire = int(round(displayed_value)) * scale
        robot_value = f"{wire:02X}"

        if setting_key == "cutting_height":
            # Home Assistant converts imperial display values back to mm, which
            # can produce values such as 66.04 for 2.6 in. The mower accepts
            # only the discrete values advertised by mowingHeightList, so snap
            # to the nearest supported native height before writing.
            height_values = (
                (self._private_telemetry.get("limits") or {}).get(
                    "cutting_height_values"
                )
                or []
            )
            valid_heights = [
                height
                for value in height_values
                if (height := self._integer(value)) is not None
            ]
            if valid_heights:
                wire = min(valid_heights, key=lambda height: abs(height - wire))

            # The official app uses different wire formats by mower family.
            #
            # i-series (confirmed with an i215):
            #   /vehicle/set/send          {"height": 65}
            #   /vehicle/set/save-set-data {"height": "65"}
            #
            # The official app uses different wire formats by mower family.
            #
            # i-series (confirmed with an i215):
            #   /vehicle/set/send          {"height": 65}
            #   /vehicle/set/save-set-data {"height": "65"}
            #
            # X-series keeps the hexadecimal encoding used by its settings
            # protocol. Do not share the i-series conversion with X models.
            model = str(getattr(self.device, "model", None) or "").strip().lower()
            is_i_series = bool(re.match(r"^i\d", model))
            if is_i_series:
                robot_value = wire
                cloud_value: Any = str(wire)
            else:
                robot_value = f"{wire:02X}"
                cloud_value = f"{wire:02X}" if cloud_hex else wire
        else:
            cloud_value = f"{wire:02X}" if cloud_hex else wire

        def _write() -> None:
            # Match the official Save workflow: apply the robot command first;
            # persist the cloud setting only after that command succeeds.
            client.send_setting_device(serial, {write_key: robot_value})
            client.save_setting_iot(
                serial, vehicle_type, {write_key: cloud_value}
            )

        await self.hass.async_add_executor_job(_write)
        self._set_pending_setting(setting_key, wire)

    async def async_set_private_select(
        self, *, write_key: str, value: int, robot_numeric: bool, cloud_string: bool = False, setting_key: str | None = None
    ) -> None:
        """Apply an enumerated setting using the official two-channel write."""
        client = self._private_client
        if client is None:
            raise ValueError("Private Navimow control is unavailable")
        serial = str(getattr(self.device, "serial_number", None) or self.device.id or "")
        vehicle_type = self._integer(self._private_telemetry.get("vehicle_type")) or 0

        def _write() -> None:
            robot_value: Any = value if robot_numeric else f"{value:02d}"
            client.send_setting_device(serial, {write_key: robot_value})
            cloud_value: Any = str(value) if cloud_string else value
            client.save_setting_iot(serial, vehicle_type, {write_key: cloud_value})

        await self.hass.async_add_executor_job(_write)
        self._set_pending_setting(setting_key, value)

    async def async_set_day_schedule(
        self, *, day: int, enabled: bool, periods: list[dict[str, Any]]
    ) -> None:
        """Write one weekday schedule and refresh the authoritative cloud plan."""
        client = self._private_client
        if client is None:
            raise ValueError("Private Navimow schedule control is unavailable")
        serial = str(getattr(self.device, "serial_number", None) or self.device.id or "")
        vehicle_type = self._integer(self._private_telemetry.get("vehicle_type")) or 0
        await self.hass.async_add_executor_job(
            client.set_day_schedule,
            serial,
            vehicle_type,
            day,
            enabled,
            periods,
        )
        await self._async_refresh_private_telemetry(force_slow=True)
        self.async_set_updated_data(self._build_data())

    @staticmethod
    def _task_delay_is_active(value: Any) -> bool:
        """Return True for a real Navimow task-delay payload, not its clear value."""
        if value is None or value is False:
            return False
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() not in {"", "0", "00", "none", "null", "false", "off"}
        if isinstance(value, dict):
            return any(NavimowCoordinator._task_delay_is_active(v) for v in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(NavimowCoordinator._task_delay_is_active(v) for v in value)
        return True

    def _handle_state(self, state: DeviceStateMessage) -> None:
        if state.device_id != self.device.id:
            return
        _LOGGER.debug(
            "MQTT state received: device=%s state=%s battery=%s",
            state.device_id,
            state.state,
            state.battery,
        )
        self._last_mqtt_update = time.monotonic()
        self._last_data_source = "mqtt_push"
        self.hass.loop.call_soon_threadsafe(self._update_from_state, state)

    def _handle_attributes(self, attrs: DeviceAttributesMessage) -> None:
        if attrs.device_id != self.device.id:
            return
        _LOGGER.debug(
            "MQTT attributes received: device=%s keys=%d",
            attrs.device_id,
            len(getattr(attrs, "__dict__", {}) or {}),
        )
        self._last_mqtt_update = time.monotonic()
        self.hass.loop.call_soon_threadsafe(self._update_from_attributes, attrs)

    def _update_from_state(self, state: DeviceStateMessage) -> None:
        previous = self._last_state
        prev_raw = getattr(previous, "state", None) if previous else None
        prev_raw = getattr(prev_raw, "value", prev_raw)
        new_raw = getattr(state, "state", None)
        new_raw = getattr(new_raw, "value", new_raw)
        prev_state = str(prev_raw or "").strip().lower()
        new_state = str(new_raw or "").strip().lower()

        work_states = {"mowing", "working", "isworking", "ismowing", "paused", "ispaused"}
        base_states = {"returning", "returninghome", "docked", "charging", "ischarging", "idle", "isidle"}
        progress = self._finite((self._location or {}).get("mowingPercentage"))

        # Preserve an interruption notice across the return-to-base transition.
        # If a real taskDelay was seen recently, attach that exact reason.  If
        # not, only report that the job ended early; do not invent a weather cause.
        if prev_state in work_states and new_state in base_states and (progress is None or progress < 99.0):
            now = time.monotonic()
            # If RETURN HOME was explicitly requested by the user, this is a
            # normal manual dock action, not an interruption/weather event.
            if now < self._manual_return_home_until:
                self._interruption_notice = None
            else:
                recent_delay = self._last_task_delay_monotonic and (now - self._last_task_delay_monotonic) <= 900.0
                self._interruption_notice = {
                    "kind": "task_delay" if recent_delay else "early_return",
                    "reason": self._last_task_delay if recent_delay else None,
                    "observed_at_epoch_ms": int(time.time() * 1000),
                    "progress": progress,
                    "state": new_state,
                }
        elif new_state in work_states:
            # A new/resumed mowing run supersedes a previous return notice.
            self._interruption_notice = None

        # Navimow can leave the last moving pose cached for a while after the
        # mower has physically latched onto the charging station.  Once the
        # device state itself says DOCKED/CHARGING, the charging-pile geometry
        # is the authoritative position for the UI.  Snap the live pose to the
        # dock so the mower icon cannot remain stranded out on the lawn while
        # it is actually charging.  Do not do this for RETURNING -- we still
        # want to see the mower travel home in real time.
        if new_state in {"docked", "charging", "ischarging"}:
            dock = (self._map_geometry or {}).get("dock")
            if isinstance(dock, (list, tuple)) and len(dock) >= 2:
                dx = self._finite(dock[0])
                dy = self._finite(dock[1])
                if dx is not None and dy is not None:
                    merged_location = dict(self._location or {})
                    merged_location["postureX"] = dx
                    merged_location["postureY"] = dy
                    merged_location["position_source"] = "dock_state_snap"
                    self._location = merged_location
                    # A docked mower is no longer in an active mowing segment.
                    # Clear only the transient trail gate/buffer; preserve the
                    # completed mowing trail on the map.
                    self._trail_gate_active = False
                    self._trail_gate_boundary = None
                    self._recent_pose_samples = []

        self._last_state = state
        self._last_data_source = "mqtt_push"
        self.async_set_updated_data(self._build_data())

        # A pause/return/dock/charge transition is a persistence boundary.
        # Save the blade-on trail immediately instead of waiting for another
        # location sample or the periodic throttle.  This is especially
        # important when the mower reaches the charger and stops publishing
        # mowing positions for hours.
        if new_state in {"paused", "ispaused", "returning", "returninghome", "docked", "charging", "ischarging", "idle", "isidle"}:
            self._last_saved = time.monotonic()
            self.hass.async_create_task(self._async_save_store())

    def _update_from_attributes(self, attrs: DeviceAttributesMessage) -> None:
        self._last_attributes = attrs
        self.async_set_updated_data(self._build_data())

    def handle_raw_mqtt(self, topic: str, payload: Any) -> None:
        """Retain a bounded, redacted sample of model-specific MQTT data."""
        registry_changed = self._discover_zones_from_payload(payload)
        channel = "/".join(topic.strip("/").split("/")[-3:]) or topic
        # Reinsert an existing channel so dictionary order reflects recency.
        self._mqtt_topics.pop(channel, None)
        self._mqtt_topics[channel] = self._sanitize_mqtt(payload)
        # Preserve only the 50 most recently seen channels.
        while len(self._mqtt_topics) > 50:
            self._mqtt_topics.pop(next(iter(self._mqtt_topics)))
        # State and location are high-frequency telemetry. Preserve a rolling
        # history only for less common topics that may describe app commands.
        if not channel.endswith(("realtimeDate/state", "realtimeDate/location")):
            self._command_discovery_events.append(
                {
                    "observed_at_epoch_ms": int(time.time() * 1000),
                    "topic": channel,
                    "payload": self._sanitize_mqtt(payload),
                }
            )
            self._command_discovery_events = self._command_discovery_events[-100:]
        if registry_changed:
            self.hass.async_create_task(self._async_save_store())
            self.data = self._build_data()
            self.async_set_updated_data(self.data)

    def handle_location(self, payload: Any, *, source: str = "mqtt_location") -> None:
        """Merge pose, progress, zone, and delay messages from a location payload."""
        changed = False
        registry_changed = False
        found_message = False
        for sample in self._message_samples(payload):
            message_type = self._integer(sample.get("type"))
            if message_type in (1, 2, 3, 4):
                found_message = True
                self._location_messages_by_type[str(message_type)] = (
                    self._sanitize_mqtt(sample)
                )

            merged = dict(self._location or {})
            if message_type == 2:
                if "currentMowBoundary" in sample:
                    merged["currentMowBoundary"] = sample.get(
                        "currentMowBoundary"
                    )
                    if self._discover_zone(sample.get("currentMowBoundary")):
                        changed = True
                        registry_changed = True
                if "currentMowProgress" in sample:
                    raw_progress = self._finite(sample.get("currentMowProgress"))
                    if raw_progress is not None:
                        previous_progress = self._finite(
                            (self._location or {}).get("mowingPercentage")
                        )
                        next_progress = round(
                            min(max(raw_progress / 100.0, 0.0), 100.0), 1
                        )
                        # Do not clear the visible trail just because a
                        # docked/idle telemetry frame reports progress=0.
                        # Only an explicit START FRESH command is allowed to
                        # erase the current-session trail.
                        merged["currentMowProgress"] = raw_progress
                        merged["mowingPercentage"] = round(
                            min(max(raw_progress / 100.0, 0.0), 100.0), 1
                        )
                if "mapWorkPosition" in sample:
                    merged["mapWorkPosition"] = sample.get("mapWorkPosition")
                self._location = merged
            elif message_type == 3:
                partition_ids = sample.get("partitionIds")
                merged["partitionIds"] = partition_ids
                merged["targetZone"] = (
                    partition_ids[0]
                    if isinstance(partition_ids, list) and partition_ids
                    else None
                )
                if isinstance(partition_ids, list):
                    for partition_id in partition_ids:
                        if self._discover_zone(partition_id):
                            changed = True
                            registry_changed = True
                self._location = merged
            elif message_type == 4:
                raw_delay = sample.get("taskDelay")
                merged["taskDelay"] = raw_delay
                self._location = merged
                # Do not throw the reason away when Navimow clears taskDelay as
                # soon as the mower starts returning/charging.  The official
                # app keeps showing the one-time weather-delay explanation.
                if self._task_delay_is_active(raw_delay):
                    now_delay = time.monotonic()
                    # Ignore a stale type-4 delay replay immediately after an
                    # explicit manual RETURN HOME. A genuinely new weather
                    # delay will be accepted again after the short dock window.
                    if now_delay >= self._manual_return_home_until:
                        self._last_task_delay = self._sanitize_mqtt(raw_delay)
                        self._last_task_delay_monotonic = now_delay
                        self._last_task_delay_epoch_ms = int(time.time() * 1000)
                        self._interruption_notice = {
                            "kind": "task_delay",
                            "reason": self._last_task_delay,
                            "observed_at_epoch_ms": self._last_task_delay_epoch_ms,
                        }

            x = self._finite(sample.get("postureX"))
            y = self._finite(sample.get("postureY"))
            if x is None or y is None:
                position = sample.get("position")
                if isinstance(position, dict):
                    x = self._finite(position.get("x"))
                    y = self._finite(position.get("y"))
            if x is None or y is None:
                continue
            found_message = True

            merged.update(sample)
            self._location = merged
            self._location["postureX"] = x
            self._location["postureY"] = y

            # Retain a short pose history even while trail drawing is gated off.
            # Navimow can confirm currentMowBoundary a few seconds after the
            # mower has actually crossed into the lawn. When that confirmation
            # arrives we can backfill only the already-in-zone samples, which
            # restores the beginning of the trail without drawing the drive
            # from the dock/corridor.
            pose_now = time.monotonic()
            self._recent_pose_samples.append((pose_now, x, y))
            cutoff = pose_now - 25.0
            self._recent_pose_samples = [p for p in self._recent_pose_samples[-160:] if p[0] >= cutoff]

            # Only paint the mowing trail while the mower is physically inside
            # its active lawn polygon. Navimow reports pose continuously while
            # travelling from the dock to a selected zone (and between zones),
            # but the blades are not doing lawn coverage during that transit.
            # Using the active boundary + point-in-polygon test lets the mower
            # icon keep moving live without drawing an artificial travel line.
            gate_active = self._position_is_inside_active_mowing_zone(x, y)
            boundary_now = self._integer((self._location or {}).get("currentMowBoundary"))
            if gate_active:
                # On the first confirmed in-zone sample, backfill the contiguous
                # samples that were already inside this exact active polygon.
                # This fixes the small missing section at lawn entry caused by
                # currentMowBoundary arriving a few seconds late.
                if not self._trail_gate_active or self._trail_gate_boundary != boundary_now:
                    active_zone = self._active_zone_geometry(boundary_now)
                    if active_zone is not None:
                        points = active_zone.get("points") or []
                        buffered: list[tuple[float, float]] = []
                        for _ts, bx, by in reversed(self._recent_pose_samples):
                            if self._point_in_or_near_polygon(bx, by, points, margin=0.8):
                                buffered.append((bx, by))
                            elif buffered:
                                break
                        buffered.reverse()
                        for bx, by in buffered:
                            last = self._trail[-1] if self._trail else None
                            if last is None or math.hypot(bx - last[0], by - last[1]) >= 0.05:
                                if last is None or math.hypot(bx - last[0], by - last[1]) <= 50:
                                    self._trail.append([round(bx, 3), round(by, 3), int(boundary_now)])
                                    changed = True
                last = self._trail[-1] if self._trail else None
                if last is None or math.hypot(x - last[0], y - last[1]) >= 0.05:
                    if last is None or math.hypot(x - last[0], y - last[1]) <= 50:
                        self._trail.append([round(x, 3), round(y, 3), int(boundary_now)])
                        changed = True
                self._trail = self._trail[-12000:]
                self._trail_gate_active = True
                self._trail_gate_boundary = boundary_now
            else:
                self._trail_gate_active = False
                self._trail_gate_boundary = None

        if not found_message:
            return
        now_update = time.monotonic()
        if source == "mqtt_location":
            self._last_mqtt_update = now_update
        self._last_location_update = now_update
        self._last_data_source = source
        self.data = self._build_data()
        self.async_set_updated_data(self.data)
        now = time.monotonic()
        if registry_changed or (changed and now - self._last_saved >= 30):
            self._last_saved = now
            self.hass.async_create_task(self._async_save_store())

    @staticmethod
    def _point_in_polygon(x: float, y: float, points: list[Any]) -> bool:
        """Return True when a local XY point lies inside a zone polygon."""
        clean: list[tuple[float, float]] = []
        for point in points or []:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                clean.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError):
                continue
        if len(clean) < 3:
            return False

        inside = False
        j = len(clean) - 1
        for i, (xi, yi) in enumerate(clean):
            xj, yj = clean[j]
            # Standard ray-casting test. The tiny denominator fallback avoids a
            # division error on perfectly horizontal polygon edges.
            crosses = (yi > y) != (yj > y)
            if crosses:
                edge_x = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
                if x < edge_x:
                    inside = not inside
            j = i
        return inside

    @classmethod
    def _point_in_or_near_polygon(
        cls, x: float, y: float, points: list[Any], margin: float = 0.8
    ) -> bool:
        """Return True inside a polygon or within ``margin`` metres of its edge.

        Navimow's reported mower centre can briefly sit just outside the mapped
        lawn while perimeter mowing, even though the blades are still covering
        the edge.  Keeping a small tolerance around the *active* zone prevents
        those perimeter passes from getting holes without re-enabling transit
        trails between the dock and lawn.
        """
        clean: list[tuple[float, float]] = []
        for point in points or []:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                clean.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError):
                continue
        if len(clean) < 3:
            return False

        if cls._point_in_polygon(x, y, clean):
            return True

        tolerance = max(0.0, float(margin))
        if tolerance <= 0:
            return False

        # Distance from the mower pose to every polygon edge.  The coordinates
        # are Navimow local-map metres, so the tolerance is also in metres.
        px, py = float(x), float(y)
        for i, (ax, ay) in enumerate(clean):
            bx, by = clean[(i + 1) % len(clean)]
            dx, dy = bx - ax, by - ay
            denom = dx * dx + dy * dy
            if denom <= 1e-12:
                distance = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
                nearest_x = ax + t * dx
                nearest_y = ay + t * dy
                distance = math.hypot(px - nearest_x, py - nearest_y)
            if distance <= tolerance:
                return True
        return False

    def _active_zone_geometry(self, boundary: int | None) -> dict[str, Any] | None:
        """Return geometry for the exact active mowing boundary."""
        if boundary is None:
            return None
        geometry = self._map_geometry or {}
        for zone in geometry.get("zones") or []:
            if self._integer(zone.get("id")) == boundary:
                return zone
        return None

    def _zone_id_for_point(self, x: float, y: float) -> int | None:
        """Best-effort partition lookup for a stored mowing point."""
        geometry = self._map_geometry or {}
        # Exact polygon membership first.
        for zone in geometry.get("zones") or []:
            zone_id = self._integer(zone.get("id"))
            if zone_id is None:
                continue
            if self._point_in_polygon(float(x), float(y), zone.get("points") or []):
                return zone_id
        # Perimeter passes can place the mower centre slightly outside the lawn.
        for zone in geometry.get("zones") or []:
            zone_id = self._integer(zone.get("id"))
            if zone_id is None:
                continue
            if self._point_in_or_near_polygon(float(x), float(y), zone.get("points") or [], margin=0.8):
                return zone_id
        return None

    def _position_is_inside_active_mowing_zone(self, x: float, y: float) -> bool:
        """Return True only for blade-on movement inside the active lawn.

        A pose update by itself does *not* mean mowing.  Navimow publishes the
        mower position while leaving the dock, driving along corridors, and
        transferring between selected lawns.  The previous implementation had a
        permissive fallback to "any polygon"; that could paint those transit
        movements as a mowing trail.

        We now require all of the following:
        * a real mapped ``currentMowBoundary``;
        * that boundary to be one of the zones requested by the current job when
          that information is available;
        * the live XY pose to be inside, or only slightly outside, that exact
          polygon (for perimeter mowing); and
        * the mower to be clear of the charging-dock area.

        This intentionally prefers a short missing trail at the very beginning
        of a zone over drawing a false travel route across the map.
        """
        geometry = self._map_geometry or {}
        zones = geometry.get("zones") or []
        if not zones:
            return False

        # Position telemetry continues while Navimow is paused, returning home,
        # travelling from the dock, and manoeuvring between lawn sections.  A
        # currentMowBoundary alone therefore does not prove that the blades are
        # actually mowing.  Only collect new *live mowing trail* points while
        # the mower state itself is a running/mowing state.
        state_obj = self._last_state
        raw_state = getattr(state_obj, "state", None) if state_obj is not None else None
        raw_state = getattr(raw_state, "value", raw_state)
        state_name = str(raw_state or "").strip().lower()
        mowing_states = {
            "mowing",
            "working",
            "isworking",
            "ismowing",
            "running",
            "isrunning",
        }
        if state_name not in mowing_states:
            return False

        # Never paint the departure/arrival manoeuvre around the dock.  Some map
        # polygons include the base itself, which otherwise makes the first few
        # metres look like mowing even with strict polygon gating.
        dock = geometry.get("dock")
        if isinstance(dock, (list, tuple)) and len(dock) >= 2:
            try:
                if math.hypot(x - float(dock[0]), y - float(dock[1])) < 2.0:
                    return False
            except (TypeError, ValueError):
                pass

        location = self._location or {}
        boundary = self._integer(location.get("currentMowBoundary"))
        if boundary is None:
            return False

        active_zone = self._active_zone_geometry(boundary)
        if active_zone is None:
            return False

        # Prefer the exact zones sent by our mow command.  If mowing was started
        # outside HA, type-3 telemetry normally supplies the equivalent list.
        requested = {int(z) for z in self._commanded_zone_ids}
        if not requested:
            raw_ids = location.get("partitionIds")
            if isinstance(raw_ids, list):
                requested = {
                    zid for value in raw_ids
                    if (zid := self._integer(value)) is not None
                }
        if requested and boundary not in requested:
            return False

        # Allow a small edge tolerance for perimeter passes. The exact active
        # boundary/requested-zone checks above remain mandatory, so this does
        # not bring back the dock-to-zone or zone-to-zone transit trail.
        return self._point_in_or_near_polygon(
            x, y, active_zone.get("points") or [], margin=0.8
        )

    def _discover_zone(self, value: Any) -> bool:
        """Remember a mower partition ID without guessing its app name."""
        zone_id = self._integer(value)
        if (
            zone_id is None
            or zone_id < 0
            or (
                self._authoritative_zone_ids is not None
                and zone_id not in self._authoritative_zone_ids
            )
            or zone_id in self._discovered_zone_ids
        ):
            return False
        self._discovered_zone_ids.add(zone_id)
        _LOGGER.info(
            "Discovered mowing partition: device=%s partition_id=%s",
            self.device.id,
            zone_id,
        )
        return True

    def _discover_zones_from_payload(self, payload: Any) -> bool:
        """Learn partition IDs from any known cloud/MQTT payload shape.

        Navimow generations use different field names and nesting. This parser
        intentionally keys on partition-specific names rather than guessing a
        numerical sequence, so it works with sparse IDs such as 4, 6, 8, 12.
        """
        changed = False
        visited: set[int] = set()

        single_keys = {
            "partitionid",
            "partition_id",
            "currentmowboundary",
            "targetzone",
            "target_zone",
            "currentzone",
            "current_zone",
        }
        list_keys = {
            "partitionids",
            "partition_ids",
            "partitionlist",
            "partition_list",
            "zoneids",
            "zone_ids",
        }

        def add(value: Any) -> None:
            nonlocal changed
            if isinstance(value, bool):
                return
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    add(item)
                return
            if isinstance(value, str):
                text = unquote_plus(value).strip()
                if not text:
                    return
                try:
                    decoded = json.loads(text)
                except (TypeError, ValueError, json.JSONDecodeError):
                    decoded = None
                if isinstance(decoded, (list, dict)):
                    if isinstance(decoded, list):
                        add(decoded)
                    else:
                        walk(decoded)
                    return
                # Accept a plain integer or a compact comma-delimited list.
                for match in re.findall(r"(?<![A-Za-z0-9])-?\d+(?![A-Za-z0-9])", text):
                    if self._discover_zone(match):
                        changed = True
                return
            if self._discover_zone(value):
                changed = True

        def walk(value: Any) -> None:
            if isinstance(value, (dict, list, tuple, set)):
                marker = id(value)
                if marker in visited:
                    return
                visited.add(marker)
            if isinstance(value, dict):
                for raw_key, item in value.items():
                    key = str(raw_key).replace("-", "_").lower()
                    compact = key.replace("_", "")
                    if key in single_keys or compact in single_keys:
                        add(item)
                    elif key in list_keys or compact in list_keys:
                        add(item)
                    walk(item)
                return
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    walk(item)
                return
            if isinstance(value, str) and any(
                marker in value.lower()
                for marker in ("partitionid", "partitionlist", "targetzone")
            ):
                decoded = unquote_plus(value)
                try:
                    parsed = json.loads(decoded)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = None
                if parsed is not None:
                    walk(parsed)
                    return
                # Query-string request captures use partitionList=%5B...%5D.
                query = parse_qs(decoded, keep_blank_values=True)
                for raw_key, items in query.items():
                    key = raw_key.replace("_", "").lower()
                    if key in {"partitionid", "partitionids", "partitionlist", "targetzone"}:
                        add(items)

        walk(payload)
        return changed

    async def _async_save_store(self) -> None:
        """Persist map history and the device-specific zone registry."""
        await self._store.async_save(
            {
                # Persist the schema marker with the trail itself.  Without
                # this, the next HA/integration restart treats a perfectly
                # valid mowing-only trail as legacy travel data and clears it.
                "trail_schema_version": self._trail_schema_version,
                "trail": self._trail,
                "official_trail_groups": self._official_trail_groups,
                "zones": {
                    str(zone_id): self._zone_names.get(zone_id, "")
                    for zone_id in sorted(self._discovered_zone_ids)
                },
                "selected_zone_id": self._selected_zone_id,
                "commanded_zone_ids": list(self._commanded_zone_ids),
                "zone_progress_cache": {str(k): v for k, v in self._zone_progress_cache.items()},
                "map_geometry": self._map_geometry,
            }
        )

    async def _async_import_map_file(self) -> None:
        """Import a sanitized local-coordinate map once, then persist it."""
        path = Path(self.hass.config.path(f"{DOMAIN}_map.json"))
        if not path.is_file():
            return

        def _read() -> Any:
            with path.open(encoding="utf-8") as stream:
                return json.load(stream)

        try:
            geometry = self._validate_map_geometry(
                await self.hass.async_add_executor_job(_read)
            )
        except (OSError, ValueError, json.JSONDecodeError) as err:
            _LOGGER.warning("Could not import %s: %s", path, err)
            return
        if geometry is None:
            _LOGGER.warning("Ignored invalid map geometry in %s", path)
            return
        self._map_geometry = geometry
        authoritative_ids = {int(zone["id"]) for zone in geometry["zones"]}
        # The private map detail is authoritative. Telemetry can transiently
        # report internal/sentinel ids (commonly 0 while docked); do not expose
        # those as selectable mowing zones when they have no map boundary.
        self._discovered_zone_ids.intersection_update(authoritative_ids)
        self._zone_names = {
            zone_id: name
            for zone_id, name in self._zone_names.items()
            if zone_id in authoritative_ids
        }
        if (
            self._selected_zone_id is not None
            and self._selected_zone_id not in authoritative_ids
        ):
            self._selected_zone_id = None
        for zone in geometry["zones"]:
            zone_id = zone["id"]
            self._discovered_zone_ids.add(zone_id)
            if zone.get("name"):
                self._zone_names.setdefault(zone_id, zone["name"])
        await self._async_save_store()
        _LOGGER.info("Imported %d mapped zones from %s", len(geometry["zones"]), path)

    async def _async_import_terrain_files(self) -> None:
        """Load an optional Navimow LiDAR terrain WebP and XY transform."""
        directory = Path(self.hass.config.path(f"{DOMAIN}_terrain"))

        def _read() -> tuple[bytes, dict[str, Any]] | None:
            if not directory.is_dir():
                return None
            images = sorted(directory.glob("merge2d*.webp"))
            metadata_files = sorted(directory.glob("merge2d*.json"))
            if not images or not metadata_files:
                return None
            image_path = images[-1]
            metadata_path = metadata_files[-1]
            if image_path.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("terrain image exceeds 10 MiB")
            image = image_path.read_bytes()
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not image.startswith(b"RIFF") or image[8:12] != b"WEBP":
                raise ValueError("terrain image is not WebP")
            if not isinstance(metadata, dict):
                raise ValueError("terrain metadata is not an object")
            return image, metadata

        try:
            loaded = await self.hass.async_add_executor_job(_read)
        except (OSError, ValueError, json.JSONDecodeError) as err:
            _LOGGER.warning("Could not import LiDAR terrain from %s: %s", directory, err)
            return
        if loaded is None:
            return
        image, raw = loaded
        min_x = self._finite(raw.get("minX"))
        max_x = self._finite(raw.get("maxX"))
        min_y = self._finite(raw.get("minY"))
        max_y = self._finite(raw.get("maxY"))
        pixels_per_meter = self._finite(raw.get("pixel_per_meter"))
        width = self._integer(raw.get("width"))
        height = self._integer(raw.get("height"))
        if (
            None in (min_x, max_x, min_y, max_y, pixels_per_meter, width, height)
            or min_x >= max_x
            or min_y >= max_y
            or pixels_per_meter <= 0
            or width <= 0
            or height <= 0
        ):
            _LOGGER.warning("Ignored invalid LiDAR terrain transform in %s", directory)
            return
        self._terrain_image = image
        self._terrain_metadata = {
            "min_x": min_x,
            "max_x": max_x,
            "min_y": min_y,
            "max_y": max_y,
            "pixels_per_meter": pixels_per_meter,
            "width": width,
            "height": height,
        }
        _LOGGER.info(
            "Imported LiDAR terrain image: device=%s size=%sx%s scale=%s px/m",
            self.device.id,
            width,
            height,
            pixels_per_meter,
        )

    async def _async_refresh_cloud_terrain(self) -> None:
        """Fetch and safely cache the official LiDAR terrain bundle."""
        client = self._private_client
        serial = str(
            getattr(self.device, "serial_number", None) or self.device.id or ""
        )
        if client is None or not serial:
            return

        directory = Path(self.hass.config.path(f"{DOMAIN}_terrain"))

        def _find_value(value: Any, keys: set[str]) -> Any:
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).lower() in keys and item not in (None, ""):
                        return item
                for item in value.values():
                    found = _find_value(item, keys)
                    if found not in (None, ""):
                        return found
            elif isinstance(value, list):
                for item in value:
                    found = _find_value(item, keys)
                    if found not in (None, ""):
                        return found
            return None

        def _fetch_and_cache() -> bool:
            metadata = client.iot_file(serial, 2)
            download_url = _find_value(
                metadata, {"downloadurl", "download_url", "url", "fileurl"}
            )
            if not isinstance(download_url, str) or not download_url.startswith("https://"):
                raise ValueError("terrain response contained no HTTPS download URL")
            version = str(
                _find_value(metadata, {"version", "resourceversion", "fileversion"})
                or hashlib.sha256(
                    urlsplit(download_url).path.encode("utf-8")
                ).hexdigest()[:20]
            )
            marker = directory / ".cloud-version"
            if marker.is_file() and marker.read_text(encoding="utf-8").strip() == version:
                if list(directory.glob("merge2d*.webp")) and list(
                    directory.glob("merge2d*.json")
                ):
                    return False

            request = urllib.request.Request(
                download_url,
                headers={"User-Agent": "HomeAssistant Navimow HA Pro"},
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > 20 * 1024 * 1024:
                    raise ValueError("terrain archive exceeds 20 MiB")
                archive = response.read(20 * 1024 * 1024 + 1)
            if len(archive) > 20 * 1024 * 1024:
                raise ValueError("terrain archive exceeds 20 MiB")

            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                candidates = [
                    info
                    for info in bundle.infolist()
                    if not info.is_dir()
                    and info.filename.replace("\\", "/")
                    .rsplit("/", 1)[-1]
                    .lower()
                    .startswith("merge2d")
                    and Path(info.filename.replace("\\", "/")).suffix.lower()
                    in {".webp", ".json"}
                ]
                if sum(info.file_size for info in candidates) > 15 * 1024 * 1024:
                    raise ValueError("terrain contents exceed 15 MiB")
                image_info = next(
                    (item for item in candidates if item.filename.lower().endswith(".webp")),
                    None,
                )
                json_info = next(
                    (item for item in candidates if item.filename.lower().endswith(".json")),
                    None,
                )
                if image_info is None or json_info is None:
                    raise ValueError("terrain archive lacks merge2d WebP or JSON")
                image = bundle.read(image_info)
                transform = bundle.read(json_info)
            if not image.startswith(b"RIFF") or image[8:12] != b"WEBP":
                raise ValueError("downloaded terrain image is not WebP")
            parsed = json.loads(transform.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("downloaded terrain transform is invalid")

            directory.mkdir(parents=True, exist_ok=True)
            (directory / "merge2d_cloud.webp").write_bytes(image)
            (directory / "merge2d_cloud.json").write_bytes(transform)
            marker.write_text(version, encoding="utf-8")
            return True

        try:
            changed = await self.hass.async_add_executor_job(_fetch_and_cache)
        except (NavimowError, OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as err:
            _LOGGER.warning(
                "Automatic LiDAR terrain refresh failed for %s; keeping cached/local map: %s",
                serial,
                err,
            )
            return
        _LOGGER.info(
            "Automatic LiDAR terrain %s for %s",
            "updated" if changed else "already current",
            serial,
        )

    async def _async_refresh_cloud_geometry(self) -> None:
        """Discover all zone polygons from Navimow's private map detail."""
        client = self._private_client
        serial = str(
            getattr(self.device, "serial_number", None) or self.device.id or ""
        )
        if client is None or not serial:
            return

        def _first_mapping_list(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                items = [item for item in value if isinstance(item, dict)]
                if items:
                    return items
            if isinstance(value, dict):
                for key in ("list", "maps", "map_list", "mapList", "data"):
                    if key in value:
                        found = _first_mapping_list(value[key])
                        if found:
                            return found
                for item in value.values():
                    found = _first_mapping_list(item)
                    if found:
                        return found
            return []

        def _value(mapping: dict[str, Any], *keys: str) -> Any:
            for key in keys:
                if key in mapping and mapping[key] is not None:
                    return mapping[key]
            return None

        def _xy(value: Any) -> list[float] | None:
            if not isinstance(value, (list, tuple)) or len(value) < 2:
                return None
            x, y = self._finite(value[0]), self._finite(value[1])
            if x is None or y is None:
                return None
            return [round(x, 4), round(y, 4)]

        def _parse_detail(value: Any) -> dict[str, Any] | None:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (TypeError, ValueError):
                    return None
            if not isinstance(value, dict):
                return None
            detail: Any = _value(value, "map_detail", "mapDetail")
            if detail is None and "sub_maps" in value:
                detail = value
            if isinstance(detail, str):
                try:
                    detail = json.loads(detail)
                except (TypeError, ValueError):
                    return None
            if not isinstance(detail, dict):
                return None

            zones: list[dict[str, Any]] = []
            dock: list[float] | None = None
            for sub_map in detail.get("sub_maps") or detail.get("subMaps") or []:
                if not isinstance(sub_map, dict):
                    continue
                zone_id = self._integer(_value(sub_map, "id", "partitionId"))
                points: list[list[float]] = []
                for element in sub_map.get("elements") or []:
                    if not isinstance(element, dict):
                        continue
                    element_type = str(element.get("type") or "").upper()
                    if element_type == "BOUNDARY" and not points:
                        for raw_point in element.get("points") or []:
                            point = _xy(raw_point)
                            if point is not None:
                                points.append(point)
                    elif element_type == "CHARGING_PILE" and dock is None:
                        dock = _xy(element.get("position"))
                if zone_id is None or len(points) < 3:
                    continue
                zones.append(
                    {
                        "id": zone_id,
                        "name": str(
                            sub_map.get("name") or f"Partition {zone_id}"
                        )[:80],
                        "points": points,
                    }
                )
            if not zones:
                return None
            return {"version": 1, "zones": zones, "paths": [], "dock": dock}

        def _fetch_geometry() -> dict[str, Any] | None:
            maps = _first_mapping_list(client.map_list(serial))
            selected: dict[str, Any] | None = None
            for item in maps:
                if _value(item, "map_id", "mapId") is not None:
                    selected = item
                    break
            if selected is None:
                return None
            map_id = _value(selected, "map_id", "mapId")
            map_base_id = _value(selected, "map_base_id", "mapBaseId")
            if map_id is None or map_base_id is None:
                return None
            detail = client.map_detail_plain(
                serial, str(map_id), str(map_base_id)
            )
            return _parse_detail(detail)

        try:
            geometry = await self.hass.async_add_executor_job(_fetch_geometry)
        except (NavimowError, OSError, ValueError, json.JSONDecodeError) as err:
            _LOGGER.warning(
                "Automatic zone geometry refresh failed for %s; keeping cached geometry: %s",
                serial,
                err,
            )
            return
        geometry = self._validate_map_geometry(geometry)
        if geometry is None:
            _LOGGER.warning(
                "Automatic zone geometry returned no usable boundaries for %s",
                serial,
            )
            return

        old_count = len((self._map_geometry or {}).get("zones") or [])
        new_count = len(geometry["zones"])
        if old_count and new_count < old_count:
            _LOGGER.warning(
                "Automatic zone geometry had fewer zones for %s (%s < %s); keeping cached geometry",
                serial,
                new_count,
                old_count,
            )
            return

        self._map_geometry = geometry
        authoritative_ids = {int(zone["id"]) for zone in geometry["zones"]}
        self._authoritative_zone_ids = authoritative_ids
        self._discovered_zone_ids.intersection_update(authoritative_ids)
        self._zone_names = {
            zone_id: name
            for zone_id, name in self._zone_names.items()
            if zone_id in authoritative_ids
        }
        if (
            self._selected_zone_id is not None
            and self._selected_zone_id not in authoritative_ids
        ):
            self._selected_zone_id = None
        for zone in geometry["zones"]:
            zone_id = int(zone["id"])
            self._discovered_zone_ids.add(zone_id)
            cloud_name = str(zone.get("name") or "").strip()
            if cloud_name and zone_id not in self._zone_names:
                self._zone_names[zone_id] = cloud_name
        await self._async_save_store()
        _LOGGER.info(
            "Automatic zone geometry updated for %s: zones=%s",
            serial,
            [zone["id"] for zone in geometry["zones"]],
        )

    @classmethod
    def _validate_map_geometry(cls, value: Any) -> dict[str, Any] | None:
        """Accept only bounded, GPS-free local map geometry."""
        if not isinstance(value, dict) or not isinstance(value.get("zones"), list):
            return None
        zones: list[dict[str, Any]] = []
        for raw_zone in value["zones"][:120]:
            if not isinstance(raw_zone, dict):
                continue
            zone_id = cls._integer(raw_zone.get("id"))
            raw_points = raw_zone.get("points")
            if zone_id is None or not isinstance(raw_points, list):
                continue
            points: list[list[float]] = []
            for point in raw_points[:5000]:
                if not isinstance(point, list) or len(point) < 2:
                    continue
                x, y = cls._finite(point[0]), cls._finite(point[1])
                if x is not None and y is not None and abs(x) < 10000 and abs(y) < 10000:
                    points.append([round(x, 4), round(y, 4)])
            if len(points) < 3:
                continue
            zones.append({
                "id": zone_id,
                "name": str(raw_zone.get("name") or f"Partition {zone_id}")[:80],
                "points": points,
            })
        if not zones:
            return None

        paths: list[dict[str, Any]] = []
        for raw_path in value.get("paths", [])[:250]:
            if not isinstance(raw_path, dict) or not isinstance(raw_path.get("points"), list):
                continue
            points = []
            for point in raw_path["points"][:5000]:
                if isinstance(point, list) and len(point) >= 2:
                    x, y = cls._finite(point[0]), cls._finite(point[1])
                    if x is not None and y is not None:
                        points.append([round(x, 4), round(y, 4)])
            if len(points) >= 2:
                paths.append({"type": str(raw_path.get("type") or "path")[:40], "points": points})

        dock = value.get("dock")
        clean_dock = None
        if isinstance(dock, list) and len(dock) >= 2:
            x, y = cls._finite(dock[0]), cls._finite(dock[1])
            if x is not None and y is not None:
                clean_dock = [round(x, 4), round(y, 4)]
        return {"version": 1, "zones": zones, "paths": paths, "dock": clean_dock}

    def get_discovered_zones(self) -> dict[int, str]:
        """Return discovered partition IDs with optional friendly names."""
        return {
            zone_id: self._zone_names.get(zone_id, "")
            for zone_id in sorted(self._discovered_zone_ids)
        }

    def get_zone_label(self, value: Any) -> str | None:
        """Translate a raw partition ID through the persistent zone registry."""
        zone_id = self._integer(value)
        if zone_id is None:
            return None
        return self._zone_names.get(zone_id) or f"Partition {zone_id}"

    def get_selected_zone_id(self) -> int | None:
        """Return the prepared partition, falling back to the live target."""
        if self._selected_zone_id is not None:
            return self._selected_zone_id
        location = self._location or {}
        target = self._integer(location.get("targetZone"))
        return target if target in self._discovered_zone_ids else None

    async def async_select_zone(self, zone_id: int) -> None:
        """Prepare a discovered partition for a future verified start action."""
        if zone_id not in self._discovered_zone_ids:
            raise ValueError(f"Unknown partition ID: {zone_id}")
        self._selected_zone_id = zone_id
        await self._async_save_store()
        self.async_set_updated_data(self._build_data())

    def can_mow_prepared_zone(self) -> bool:
        """Return whether private control and a polygon-backed choice exist."""
        zone_id = self.get_selected_zone_id()
        return (
            self._private_client is not None
            and zone_id is not None
            and zone_id in self._discovered_zone_ids
            and (
                self._authoritative_zone_ids is None
                or zone_id in self._authoritative_zone_ids
            )
        )

    async def _async_refresh_private_location_only(self, *, wake: bool = False) -> bool:
        """Refresh the private pose endpoint and immediately feed the live map.

        Right after a job starts Navimow can keep returning the last dock pose for
        tens of seconds even though the mower is already moving.  The mobile app
        wakes the vehicle detail channel before consuming location updates, so
        during bootstrap we occasionally mirror that with a lightweight index2
        read when repeated location samples are stale.
        """
        client = self._private_client
        serial = str(getattr(self.device, "serial_number", None) or self.device.id or "")
        if client is None or not serial:
            return False
        vehicle_type = self._integer(self._private_telemetry.get("vehicle_type")) or 0

        def _fetch() -> Any:
            if wake:
                try:
                    client.index2(serial)
                except NavimowError:
                    pass
            return client.location(serial, vehicle_type)

        try:
            payload = await self.hass.async_add_executor_job(_fetch)
        except (NavimowError, OSError, ValueError, json.JSONDecodeError) as err:
            _LOGGER.debug("Fast private location refresh failed for %s: %s", serial, err)
            return False
        if not isinstance(payload, (dict, list)):
            return False

        # Track whether the server is recycling the same dock sample.  Repeated
        # stale signatures trigger an index2 wake on the next poll instead of
        # waiting for the normal 30 s coordinator cycle.
        sample = payload[0] if isinstance(payload, list) and payload else payload
        if isinstance(sample, dict):
            signature = (
                sample.get("report_time") or sample.get("reportTime"),
                sample.get("postureX"),
                sample.get("postureY"),
            )
            if signature == self._fast_location_last_signature:
                self._fast_location_stale_count += 1
            else:
                self._fast_location_last_signature = signature
                self._fast_location_stale_count = 0

        self.handle_location(payload, source="private_location")
        return True

    async def _async_fast_location_bootstrap(self) -> None:
        """Aggressively bridge the dead period between MOW and live MQTT pose."""
        started = time.monotonic()
        try:
            # Wake the private vehicle-detail channel immediately.  This is done
            # before the first location read, while the mow command may still be
            # propagating to the mower.
            await self._async_refresh_private_location_only(wake=True)
            while time.monotonic() < self._fast_location_deadline:
                elapsed = time.monotonic() - started
                # If the cloud keeps recycling the exact dock sample, force a
                # detail-channel wake every ~5 stale reads.
                wake = self._fast_location_stale_count >= 5
                if wake:
                    self._fast_location_stale_count = 0
                await self._async_refresh_private_location_only(wake=wake)

                # First 25 seconds are the critical leave-the-dock phase.  Poll
                # once per second there, then ease back to 2 seconds.
                await asyncio.sleep(1.0 if elapsed < 25.0 else 2.0)
        except asyncio.CancelledError:
            raise
        finally:
            self._fast_location_task = None
            self._fast_location_deadline = 0.0

    def _start_fast_location_bootstrap(self, duration: float = 120.0) -> None:
        """Start or extend the fast location bootstrap window."""
        if self._private_client is None:
            return
        self._fast_location_deadline = max(
            self._fast_location_deadline, time.monotonic() + max(10.0, duration)
        )
        if self._fast_location_task is not None and not self._fast_location_task.done():
            return
        self._fast_location_last_signature = None
        self._fast_location_stale_count = 0
        self._fast_location_task = self.hass.async_create_task(
            self._async_fast_location_bootstrap()
        )

    async def async_mow_zones(
        self, zone_ids: list[int], *, reset: bool = True, ordered: bool = True
    ) -> Any:
        """Start an authoritative one- or multi-zone mowing job.

        Partition ids are encoded exactly like the Navimow mobile app: each id
        is a little-endian uint16 concatenated into ``partitionIds``.

        ``partitionSetup`` uses two nibbles:
        - high nibble: 1 = continue existing progress, 2 = restart/clear progress
        - low nibble:  1 = mower chooses order, 2 = honor supplied zone order

        Therefore the common dashboard call ``reset=True, ordered=True`` sends
        0x22.  This method never falls back to an empty/all-zone command.
        """
        client = self._private_client
        if client is None:
            raise ValueError(
                "Private Navimow control is unavailable; enable the LiDAR/private cloud session"
            )

        zones = [int(zone_id) for zone_id in zone_ids]
        if not zones:
            raise ValueError("Choose at least one mowing zone")
        if len(set(zones)) != len(zones):
            raise ValueError("The mowing zone list contains duplicates")

        unknown = [z for z in zones if z not in self._discovered_zone_ids]
        if unknown:
            raise ValueError(f"Unknown partition ID(s): {unknown}")
        if self._authoritative_zone_ids is not None:
            missing = [z for z in zones if z not in self._authoritative_zone_ids]
            if missing:
                raise ValueError(f"Partition ID(s) have no map boundary: {missing}")

        state = self.get_device_state()
        raw_state = getattr(state, "state", None) if state else None
        raw_state = getattr(raw_state, "value", raw_state)
        normalized_state = str(raw_state or "unknown").strip().lower()
        if normalized_state not in {
            "docked",
            "idle",
            "charging",
            "ischarging",
            "isidle",
        }:
            raise ValueError(
                "Mow zones requires the mower to be docked, idle, or charging; "
                f"current state is {raw_state or 'unknown'}"
            )

        serial = str(
            getattr(self.device, "serial_number", None) or self.device.id or ""
        )
        if not serial:
            raise ValueError("The mower serial number is unavailable")

        partition_ids = "".join(
            f"{zone_id & 0xFF:02x}{(zone_id >> 8) & 0xFF:02x}" for zone_id in zones
        )
        partition_setup = (0x20 if reset else 0x10) | (0x02 if ordered else 0x01)
        self._commanded_zone_ids = list(zones)
        if reset:
            coverage_now = self._private_telemetry.get("coverage") if isinstance(self._private_telemetry, dict) else None
            if coverage_now is None:
                coverage_now = self._private_slow_raw.get("coverage") if isinstance(self._private_slow_raw, dict) else None
            starts = self._coverage_start_times(coverage_now)
            self._official_session_zone_ids = set(zones)
            self._official_session_baseline_start = {z: starts.get(z) for z in zones}
            self._official_session_waiting = True
            for zone_id in zones:
                self._zone_progress_cache[int(zone_id)] = 0.0
            # A fresh start invalidates server trail only for the lawns in
            # this command.  Preserve every other partition's history.
            reset_ids = {int(zone_id) for zone_id in zones}
            self._official_trail_groups = [
                group for group in self._official_trail_groups
                if self._integer(group.get("partition_id")) not in reset_ids
            ]
            for zone_id in reset_ids:
                self._official_trail_signature.pop(zone_id, None)

        # Begin waking/polling the pose channel *before* the mow request.  Waiting
        # for the command round-trip was wasting several valuable seconds while
        # the mower could already be leaving the dock.
        self._start_fast_location_bootstrap(120.0)

        response = await self.hass.async_add_executor_job(
            client.mow_zones,
            serial,
            partition_ids,
            partition_setup,
        )

        # Keep the fast window alive for two minutes after a successful start;
        # MQTT normally takes over well before this.
        self._start_fast_location_bootstrap(120.0)

        # START FRESH is partition-local.  Never erase work from lawns that
        # were not part of this command.  This mirrors the official app: a
        # partial Back Lawn remains visible if the user later starts Front Lawn.
        # A 100% completed lawn also follows this path automatically when it is
        # selected again (the UI does not offer Resume for 100%).
        if reset:
            reset_ids = {int(zone_id) for zone_id in zones}
            self._trail = [
                point for point in self._trail
                if not (
                    isinstance(point, (list, tuple))
                    and len(point) >= 3
                    and self._integer(point[2]) in reset_ids
                )
            ]
            self._official_trail_groups = [
                group for group in self._official_trail_groups
                if self._integer(group.get("partition_id")) not in reset_ids
            ]
            for zone_id in reset_ids:
                self._official_trail_signature.pop(zone_id, None)
            self._recent_pose_samples = []
            self._trail_gate_active = False
            self._trail_gate_boundary = None
            if self._location is not None:
                self._location["currentMowProgress"] = 0
                self._location["mowingPercentage"] = 0.0
        await self._async_save_store()
        self.async_set_updated_data(self._build_data())
        _LOGGER.info(
            "Started authoritative mowing zones: device=%s zones=%s encoded=%s setup=0x%02x",
            self.device.id,
            zones,
            partition_ids,
            partition_setup,
        )
        return response

    async def async_mow_prepared_zone(self) -> Any:
        """Clear progress and mow only the currently prepared partition."""
        zone_id = self.get_selected_zone_id()
        if zone_id is None:
            raise ValueError("Choose a Prepared mowing zone first")
        return await self.async_mow_zones([zone_id], reset=True, ordered=True)

    async def async_set_zone_name(self, zone_id: int, name: str) -> None:
        """Register a user-confirmed partition and assign its friendly name."""
        if zone_id < 0:
            raise ValueError("Partition ID cannot be negative")
        if (
            self._authoritative_zone_ids is not None
            and zone_id not in self._authoritative_zone_ids
        ):
            raise ValueError(f"Partition ID has no map boundary: {zone_id}")
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Zone name cannot be empty")
        if zone_id not in self._discovered_zone_ids:
            self._discovered_zone_ids.add(zone_id)
            _LOGGER.info(
                "Manually registered mowing partition: device=%s partition_id=%s",
                self.device.id,
                zone_id,
            )
        self._zone_names[zone_id] = clean_name
        await self._async_save_store()
        self.async_set_updated_data(self._build_data())

    @classmethod
    def _message_samples(cls, payload: Any):
        """Yield every nested dictionary from an MQTT location payload."""
        if isinstance(payload, list):
            for item in payload:
                yield from cls._message_samples(item)
            return
        if not isinstance(payload, dict):
            return

        yield payload

        for value in payload.values():
            if isinstance(value, (dict, list)):
                yield from cls._message_samples(value)

    @classmethod
    def _sanitize_mqtt(cls, value: Any, depth: int = 0) -> Any:
        """Redact secrets and bound payload size for downloadable diagnostics."""
        if depth >= 8:
            return "<max depth>"
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 100:
                    output["<truncated>"] = len(value) - 100
                    break
                lowered = str(key).lower()
                if any(
                    marker in lowered
                    for marker in (
                        "token",
                        "password",
                        "passwd",
                        "pwd",
                        "secret",
                        "authorization",
                        "username",
                    )
                ):
                    output[str(key)] = "**REDACTED**"
                else:
                    output[str(key)] = cls._sanitize_mqtt(item, depth + 1)
            return output
        if isinstance(value, list):
            output = [cls._sanitize_mqtt(item, depth + 1) for item in value[:100]]
            if len(value) > 100:
                output.append(f"<truncated {len(value) - 100} items>")
            return output
        if isinstance(value, str):
            return value if len(value) <= 1000 else f"{value[:1000]}<truncated>"
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:1000]

    @staticmethod
    def _finite(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _integer(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def get_device_state(self) -> DeviceStateMessage | None:
        return self.data.get("state")

    def get_device_attributes(self) -> DeviceAttributesMessage | None:
        return self.data.get("attributes")

    def get_device_info(self) -> Any | None:
        return self.data.get("device")

    def get_location(self) -> dict[str, Any] | None:
        return self.data.get("location")

    def get_trail(self) -> list[list[float]]:
        return self.data.get("trail") or []

    def get_map_geometry(self) -> dict[str, Any] | None:
        return self.data.get("map_geometry")

    def get_terrain_map(self) -> tuple[bytes, dict[str, Any]] | None:
        """Return the imported LiDAR WebP and sanitized local XY transform."""
        if self._terrain_image is None or self._terrain_metadata is None:
            return None
        return self._terrain_image, dict(self._terrain_metadata)

    def get_private_telemetry(self) -> dict[str, Any]:
        """Return the normalized, secret-free private telemetry snapshot."""
        return dict(self._private_telemetry)

    def mark_manual_return_home(self) -> None:
        """Mark an explicit user dock request and clear stale delay context."""
        self._manual_return_home_until = time.monotonic() + 90.0
        self._interruption_notice = None
        self._last_task_delay = None
        self._last_task_delay_monotonic = 0.0
        self._last_task_delay_epoch_ms = None
        if self._location is not None:
            self._location["taskDelay"] = None
        self.async_set_updated_data(self._build_data())

    def get_delay_context(self) -> dict[str, Any]:
        """Return the latched task-delay/interruption context for UI use."""
        return {
            "last_task_delay": self._last_task_delay,
            "last_task_delay_age_s": (
                round(time.monotonic() - self._last_task_delay_monotonic, 1)
                if self._last_task_delay_monotonic else None
            ),
            "last_task_delay_epoch_ms": self._last_task_delay_epoch_ms,
            "interruption_notice": dict(self._interruption_notice) if self._interruption_notice else None,
        }

    def get_mqtt_topics(self) -> dict[str, Any]:
        return dict(self._mqtt_topics)

    def get_location_messages_by_type(self) -> dict[str, Any]:
        return dict(self._location_messages_by_type)

    def get_command_discovery_events(self) -> list[dict[str, Any]]:
        """Return bounded, redacted passive MQTT discovery events."""
        return list(self._command_discovery_events)

    def record_experimental_zone_command(
        self,
        zone_id: int,
        zone_name: str,
        command_zone: str,
        response: Any,
        error: str | None = None,
    ) -> None:
        """Retain one redacted result from the explicitly requested experiment."""
        self._last_experimental_zone_command = {
            "observed_at_epoch_ms": int(time.time() * 1000),
            "request": {
                "endpoint": "/openapi/smarthome/sendCommands",
                "command": "action.devices.commands.StartStop",
                "params": {"on": True, "zone": command_zone},
                "partition_id": zone_id,
                "friendly_name": zone_name,
            },
            "response": self._sanitize_mqtt(response),
            "error": self._sanitize_mqtt(error),
        }

    def get_experimental_zone_command_diagnostics(self) -> dict[str, Any] | None:
        """Return the most recent redacted experimental command result."""
        return (
            dict(self._last_experimental_zone_command)
            if self._last_experimental_zone_command is not None
            else None
        )

    def get_last_location_update(self) -> float | None:
        return self._last_location_update

    def record_location_recovery(self) -> None:
        self._last_location_recovery = time.monotonic()
        self._location_recovery_count += 1

    def get_location_recovery_diagnostics(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "last_location_age_seconds": (
                round(now - self._last_location_update, 1)
                if self._last_location_update is not None
                else None
            ),
            "last_recovery_age_seconds": (
                round(now - self._last_location_recovery, 1)
                if self._last_location_recovery is not None
                else None
            ),
            "recovery_count": self._location_recovery_count,
        }
