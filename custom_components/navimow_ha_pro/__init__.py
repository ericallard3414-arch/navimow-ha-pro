"""The Navimow integration."""
import asyncio
import json
import logging
import os
import shutil
import time
from typing import Any
from urllib.parse import quote, urlparse

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth import NavimowOAuth2Implementation
from . import p101_crypto as private_crypto
from .const import (
    DOMAIN,
    CLIENT_ID,
    CLIENT_SECRET,
    API_BASE_URL,
    MQTT_BROKER,
    MQTT_PORT,
    MQTT_USERNAME,
    MQTT_PASSWORD,
    CONF_LIDAR_ENABLED,
    CONF_PRIVATE_ACCESS_TOKEN,
    CONF_PRIVATE_REFRESH_TOKEN,
    CONF_PRIVATE_UUID,
    CONF_PRIVATE_UID,
    CONF_PRIVATE_DEVICE_ID,
    CONF_PRIVATE_REGION,
    CONF_PRIVATE_ACCOUNT_REGION,
    CONF_PRIVATE_HOST,
)
from .api import NavimowCloudClient, Tokens
from .services import async_setup_services
_LOGGER = logging.getLogger(__name__)
_LOGGER.debug("Navimow module imported (__init__.py)")

PLATFORMS: list[Platform] = [
    Platform.LAWN_MOWER,
    Platform.SENSOR,
    Platform.CAMERA,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.TEXT,
]

_SCHEDULER_CARD = "navimow-scheduler-card.js"
_ZONE_DASHBOARD_CARD = "navimow-zone-dashboard-card.js"
_FRONTEND_CARD_VERSION = "0700b1"
_FRONTEND_KEY = f"{DOMAIN}_frontend_registered"


async def _async_register_scheduler_frontend(hass: HomeAssistant) -> None:
    """Copy and auto-load the dependency-free scheduler Lovelace card."""
    if hass.data.get(_FRONTEND_KEY):
        return
    destination_dir = hass.config.path("www", DOMAIN)
    cards = (_SCHEDULER_CARD, _ZONE_DASHBOARD_CARD)

    def _copy() -> None:
        os.makedirs(destination_dir, exist_ok=True)
        for card in cards:
            source = os.path.join(os.path.dirname(__file__), "www", card)
            destination = os.path.join(destination_dir, card)
            shutil.copyfile(source, destination)

    try:
        await hass.async_add_executor_job(_copy)
        from homeassistant.components.frontend import add_extra_js_url

        for card in cards:
            add_extra_js_url(
                hass,
                f"/local/{DOMAIN}/{card}?v={_FRONTEND_CARD_VERSION}",
            )
    except Exception:
        _LOGGER.warning("Could not register Navimow scheduler card", exc_info=True)
        return
    hass.data[_FRONTEND_KEY] = True


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Reload after LiDAR cloud options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the Navimow component."""
    hass.data.setdefault(DOMAIN, {})
    _LOGGER.debug("Navimow async_setup called, registering OAuth2 implementation")
    # Register OAuth2 implementation so config flow can find it.
    config_entry_oauth2_flow.async_register_implementation(
        hass,
        DOMAIN,
        NavimowOAuth2Implementation(
            hass,
            DOMAIN,
            CLIENT_ID,
            CLIENT_SECRET,
        ),
    )

    def _resolve_coordinator(call: ServiceCall):
        coordinators = []
        for entry_data in hass.data.get(DOMAIN, {}).values():
            if not isinstance(entry_data, dict):
                continue
            coordinators.extend(entry_data.get("coordinators", {}).values())

        mower_id = call.data.get("mower_id")
        if mower_id:
            coordinators = [
                coordinator
                for coordinator in coordinators
                if mower_id
                in {
                    coordinator.device.id,
                    coordinator.device.name,
                    coordinator.device.serial_number,
                }
            ]
        if len(coordinators) != 1:
            raise HomeAssistantError(
                "Specify mower_id when more than one Navimow mower is configured"
            )
        return coordinators[0]

    async def _async_set_zone_name(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(call)
        try:
            await coordinator.async_set_zone_name(
                int(call.data["partition_id"]), call.data["name"]
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    async def _async_start_prepared_zone_experimental(call: ServiceCall) -> None:
        """Send one deliberately gated zone-start experiment."""
        coordinator = _resolve_coordinator(call)
        if call.data.get("confirm_all_zone_risk") is not True:
            raise HomeAssistantError(
                "You must accept that the mower may ignore the zone and mow all zones"
            )

        state = coordinator.get_device_state()
        state_value = getattr(state, "state", None) if state else None
        state_value = getattr(state_value, "value", state_value)
        normalized_state = str(state_value or "unknown").lower()
        if normalized_state not in {
            "docked",
            "idle",
            "charging",
            "ischarging",
            "isidle",
        }:
            raise HomeAssistantError(
                "Experimental zone start requires the mower to be docked, idle, or charging; "
                f"current state is {state_value or 'unknown'}"
            )

        zone_id = coordinator.get_selected_zone_id()
        zones = coordinator.get_discovered_zones()
        zone_name = zones.get(zone_id) if zone_id is not None else None
        if zone_id is None:
            raise HomeAssistantError("Choose a Prepared mowing zone first")
        if not isinstance(zone_name, str) or not zone_name.strip():
            raise HomeAssistantError(
                f"Partition {zone_id} needs a friendly zone name before this experiment"
            )
        zone_name = zone_name.strip()
        command_zone = str(zone_id)
        request_data = {
            "commands": [
                {
                    "devices": [{"id": coordinator.device.id}],
                    "execution": {
                        "command": "action.devices.commands.StartStop",
                        "params": {"on": True, "zone": command_zone},
                    },
                }
            ]
        }

        response: Any = None
        try:
            await coordinator._async_ensure_valid_token()
            # Intentionally exactly one command request. There is no retry and
            # no fallback to the SDK's ordinary all-area start operation.
            response = await coordinator.api._async_request(
                "POST", "/openapi/smarthome/sendCommands", data=request_data
            )
            coordinator.record_experimental_zone_command(
                zone_id, zone_name, command_zone, response=response
            )
        except Exception as err:
            coordinator.record_experimental_zone_command(
                zone_id,
                zone_name,
                command_zone,
                response=response,
                error=str(err),
            )
            raise HomeAssistantError(
                f"Experimental zone command failed: {err}"
            ) from err

        if not isinstance(response, dict) or response.get("code") != 1:
            raise HomeAssistantError(
                f"Navimow rejected the experimental zone command: {response}"
            )
        payload = response.get("data", {}).get("payload", {})
        command_results = payload.get("commands", []) if isinstance(payload, dict) else []
        for result in command_results if isinstance(command_results, list) else []:
            if isinstance(result, dict) and str(result.get("status", "")).upper() == "ERROR":
                error_code = result.get("errorCode", "unknown error")
                raise HomeAssistantError(
                    f"Navimow rejected the experimental zone command: {error_code}"
                )

    hass.services.async_register(
        DOMAIN,
        "set_zone_name",
        _async_set_zone_name,
        schema=vol.Schema(
            {
                vol.Optional("mower_id"): cv.string,
                vol.Required("partition_id"): vol.Coerce(int),
                vol.Required("name"): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "start_prepared_zone_experimental",
        _async_start_prepared_zone_experimental,
        schema=vol.Schema(
            {
                vol.Optional("mower_id"): cv.string,
                vol.Required("confirm_all_zone_risk"): cv.boolean,
            }
        ),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Navimow from a config entry."""
    # 延迟导入 mower_sdk，避免在加载 config_flow 时触发依赖导入
    from mower_sdk.api import MowerAPI
    from mower_sdk.errors import MowerAPIError
    from mower_sdk.sdk import NavimowSDK
    
    from .coordinator import NavimowCoordinator
    
    hass.data.setdefault(DOMAIN, {})

    def _mask_secret(value: str | None) -> str:
        if not value:
            return "<empty>"
        if len(value) <= 4:
            return "*" * len(value)
        return f"{value[:2]}***{value[-2:]}"

    try:
        # 获取 OAuth2 实现
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
        if not isinstance(implementation, NavimowOAuth2Implementation):
            raise ConfigEntryAuthFailed("Invalid OAuth2 implementation")

        # 创建 OAuth2Session
        oauth_session = config_entry_oauth2_flow.OAuth2Session(
            hass, entry, implementation
        )

        token: dict[str, Any] | None = None
        if hasattr(oauth_session, "async_get_valid_token"):
            try:
                token = await oauth_session.async_get_valid_token()
            except AttributeError:
                token = None
        if not token and hasattr(oauth_session, "async_ensure_token_valid"):
            await oauth_session.async_ensure_token_valid()
            token = oauth_session.token
        if not token and hasattr(oauth_session, "async_get_access_token"):
            access_token_value = await oauth_session.async_get_access_token()
            token = {"access_token": access_token_value} if access_token_value else None
        if not token:
            # Final fallback for older HA versions storing token on the entry.
            token = entry.data.get("token")
        if not token:
            raise ConfigEntryAuthFailed("No valid token available")
        access_token = token.get("access_token")
        if not access_token:
            raise ConfigEntryAuthFailed("No access token in token data")

        # 创建 MowerAPI 实例
        api = MowerAPI(
            session=async_get_clientsession(hass),
            token=access_token,
            base_url=entry.data.get("api_base_url", API_BASE_URL),
        )

        # 发现设备
        try:
            devices = await api.async_get_devices()
            _LOGGER.info("Discovered %d Navimow device(s)", len(devices))
        except MowerAPIError as err:
            _LOGGER.error("Failed to discover devices: %s", err)
            raise ConfigEntryNotReady(f"Failed to discover devices: {err}") from err
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            _LOGGER.error("Authentication failed during device discovery: %s", err)
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err

        if not devices:
            _LOGGER.warning("No Navimow devices found")

        private_client: NavimowCloudClient | None = None
        private_config = {**entry.data, **entry.options}
        if private_config.get(CONF_LIDAR_ENABLED):
            private_tokens = Tokens(
                access_token=str(private_config.get(CONF_PRIVATE_ACCESS_TOKEN) or ""),
                refresh_token=str(private_config.get(CONF_PRIVATE_REFRESH_TOKEN) or ""),
                uuid=str(private_config.get(CONF_PRIVATE_UUID) or ""),
                region=str(private_config.get(CONF_PRIVATE_REGION) or "fra"),
                account_region=str(
                    private_config.get(CONF_PRIVATE_ACCOUNT_REGION) or ""
                ),
            )
            if private_tokens.access_token and private_tokens.refresh_token:
                private_client = NavimowCloudClient(
                    str(private_config.get(CONF_PRIVATE_DEVICE_ID) or ""),
                    tokens=private_tokens,
                    uid=str(private_config.get(CONF_PRIVATE_UID) or ""),
                    region=str(private_config.get(CONF_PRIVATE_REGION) or "fra"),
                    host=str(private_config.get(CONF_PRIVATE_HOST) or "") or None,
                )
            else:
                _LOGGER.warning(
                    "Automatic LiDAR terrain is enabled but its private session is incomplete; "
                    "open integration options and sign in again"
                )

        # Read-only capability discovery. The SDK parses authList into Device
        # records and discards the raw Google Smart Home traits. Preserve a
        # bounded/redacted copy so we can determine whether Navimow advertises
        # standard StartStop zones without sending any command.
        smart_home_capabilities: dict[str, Any] | None = None
        try:
            raw_auth_list = await api._async_request(
                "GET", "/openapi/smarthome/authList"
            )
            sanitized_auth_list = NavimowCoordinator._sanitize_mqtt(raw_auth_list)
            if isinstance(sanitized_auth_list, dict):
                smart_home_capabilities = sanitized_auth_list
            _LOGGER.info("Read-only smart-home capability discovery completed")
        except Exception as err:
            _LOGGER.warning(
                "Read-only smart-home capability discovery failed: %s", err
            )

        # One-time, read-only map route discovery. Navimow's official SDK does
        # not expose maps yet, while the former private integration used the
        # /map/index namespace. Probe a bounded set of plausible current and
        # compatibility routes with the OAuth session and retain only redacted,
        # size-limited results for diagnostics. No mower command is sent.
        map_discovery: dict[str, Any] = {}
        if devices:
            probe_device_id = str(devices[0].id)
            encoded_id = quote(probe_device_id, safe="")
            probes = (
                (
                    "private_map_list",
                    "POST",
                    "/map/index/map-list",
                    {"vehicle_sn": probe_device_id},
                ),
                (
                    "openapi_map_list_post",
                    "POST",
                    "/openapi/smarthome/mapList",
                    {"deviceId": probe_device_id},
                ),
                (
                    "openapi_map_list_get",
                    "GET",
                    f"/openapi/smarthome/mapList?deviceId={encoded_id}",
                    None,
                ),
                (
                    "openapi_device_map",
                    "POST",
                    "/openapi/smarthome/deviceMap",
                    {"deviceId": probe_device_id},
                ),
            )
            for label, method, path, request_data in probes:
                try:
                    if request_data is None:
                        response = await api._async_request(method, path)
                    else:
                        response = await api._async_request(
                            method, path, data=request_data
                        )
                    map_discovery[label] = {
                        "ok": True,
                        "response": NavimowCoordinator._sanitize_mqtt(response),
                    }
                except Exception as err:
                    map_discovery[label] = {
                        "ok": False,
                        "error_type": type(err).__name__,
                        "error": NavimowCoordinator._sanitize_mqtt(str(err)),
                    }

            # The plain compatibility request above is expected to reveal
            # whether the legacy route still exists. When it asks for envelope
            # field `k`, retry the same read-only operation using Navimow Pro's
            # p:101 transport while keeping the official OAuth bearer session.
            # Two bounded identity layouts are tried because regional clusters
            # differ on whether they require common app fields inside `data`.
            p101_variants = (
                (
                    "private_map_list_p101_minimal",
                    {"vehicle_sn": probe_device_id},
                ),
                (
                    "private_map_list_p101_oauth",
                    {
                        "vehicle_sn": probe_device_id,
                        "uid": "",
                        "device_id": probe_device_id,
                        "client_ver": "403000003",
                        "platform": "and",
                        "language": "en",
                        "systemVersion": "15",
                        "manufacturer": "HomeAssistant",
                        "access_token": access_token,
                    },
                ),
                (
                    "private_map_list_p101_token",
                    {
                        "vehicle_sn": probe_device_id,
                        "token": access_token,
                    },
                ),
                (
                    "private_map_list_p101_dual_token",
                    {
                        "vehicle_sn": probe_device_id,
                        "uid": "",
                        "device_id": probe_device_id,
                        "client_ver": "403000003",
                        "platform": "and",
                        "language": "en",
                        "systemVersion": "15",
                        "manufacturer": "HomeAssistant",
                        "token": access_token,
                        "access_token": access_token,
                    },
                ),
            )
            for label, business_data in p101_variants:
                try:
                    wire_response = await api._async_request(
                        "POST",
                        "/map/index/map-list",
                        data=private_crypto.pack(business_data),
                    )
                    try:
                        decoded_response = private_crypto.decode_response(
                            wire_response
                        )
                    except Exception as decode_err:
                        map_discovery[label] = {
                            "ok": False,
                            "stage": "decode",
                            "error_type": type(decode_err).__name__,
                            "error": NavimowCoordinator._sanitize_mqtt(
                                str(decode_err)
                            ),
                            "wire_response": NavimowCoordinator._sanitize_mqtt(
                                wire_response
                            ),
                        }
                    else:
                        map_discovery[label] = {
                            "ok": True,
                            "response": NavimowCoordinator._sanitize_mqtt(
                                decoded_response
                            ),
                        }
                except Exception as err:
                    map_discovery[label] = {
                        "ok": False,
                        "stage": "request",
                        "error_type": type(err).__name__,
                        "error": NavimowCoordinator._sanitize_mqtt(str(err)),
                    }
            _LOGGER.info("Read-only map route discovery completed")

        # 获取 MQTT 连接信息并创建 SDK
        try:
            mqtt_info = await api.async_get_mqtt_user_info()
        except MowerAPIError as err:
            _LOGGER.error("Failed to get MQTT info: %s", err)
            raise ConfigEntryNotReady(f"Failed to get MQTT info: {err}") from err

        mqtt_host = mqtt_info.get("mqttHost") or entry.data.get(
            "mqtt_broker", MQTT_BROKER
        )
        mqtt_url = mqtt_info.get("mqttUrl")
        mqtt_username = mqtt_info.get("userName") or entry.data.get(
            "mqtt_username", MQTT_USERNAME
        )
        mqtt_password = mqtt_info.get("pwdInfo") or entry.data.get(
            "mqtt_password", MQTT_PASSWORD
        )
        mqtt_port = 443 if mqtt_url else entry.data.get("mqtt_port", MQTT_PORT)
        ws_path = mqtt_url
        if mqtt_url:
            parsed = urlparse(mqtt_url)
            if parsed.scheme in ("ws", "wss") and parsed.hostname:
                if not mqtt_host:
                    mqtt_host = parsed.hostname
                if parsed.port:
                    mqtt_port = parsed.port
                ws_path = parsed.path or "/"
                if parsed.query:
                    ws_path = f"{ws_path}?{parsed.query}"
        auth_headers = {"Authorization": f"Bearer {access_token}"} if ws_path else None

        _LOGGER.info(
            "MQTT connection parameters: broker=%s port=%s mqtt_url=%s ws_path=%s username=%s password=%s auth_header=%s",
            mqtt_host,
            mqtt_port,
            mqtt_url,
            ws_path,
            _mask_secret(mqtt_username),
            _mask_secret(mqtt_password),
            "Bearer <masked>" if auth_headers else "<none>",
        )

        _mqtt_refresh_lock = asyncio.Lock()
        coordinators: dict[str, NavimowCoordinator] = {}
        # 用列表作为可变标志容器，使 async_unload_entry（不同函数作用域）可以修改它
        _unload_flag: list[bool] = [False]
        _planned_disconnect_until: list[float] = [0.0]

        def _attach_mqtt_debug_hooks(sdk: NavimowSDK, api: MowerAPI) -> None:
            mqtt = sdk._mqtt
            original_on_message = mqtt.on_message

            def _device_id_from_candidate_topic(topic: str) -> str | None:
                parts = topic.strip("/").split("/")
                if len(parts) >= 3 and parts[:2] == ["uplink", "vehicle"]:
                    return parts[2]
                if len(parts) >= 2 and parts[0] == "navimow":
                    return parts[1]
                return None

            def _attach_client_raw_hook() -> None:
                """Capture subscribed topics the SDK's downlink parser ignores."""
                client = mqtt.client
                if getattr(client, "_navimow_ha_pro_raw_hook", False):
                    return
                original_client_on_message = client.on_message

                def _on_raw_client_message(_client, _userdata, msg):
                    topic = str(getattr(msg, "topic", ""))
                    candidate_device_id = _device_id_from_candidate_topic(topic)
                    if candidate_device_id:
                        payload_bytes = getattr(msg, "payload", b"") or b""
                        try:
                            parsed_payload: Any = json.loads(
                                payload_bytes.decode("utf-8", errors="replace")
                            )
                        except (TypeError, ValueError):
                            parsed_payload = payload_bytes.decode(
                                "utf-8", errors="replace"
                            )
                        coordinator = coordinators.get(candidate_device_id)
                        if coordinator is not None:
                            hass.loop.call_soon_threadsafe(
                                coordinator.handle_raw_mqtt,
                                topic,
                                parsed_payload,
                            )
                    if original_client_on_message is not None:
                        original_client_on_message(_client, _userdata, msg)

                client.on_message = _on_raw_client_message
                setattr(client, "_navimow_ha_pro_raw_hook", True)

            def _subscribe_extended_topics() -> None:
                """Subscribe to the mower downlink tree now and on reconnect.

                The SDK starts connecting in its constructor path, so the first
                ready callback may happen before these hooks are attached. An
                immediate subscription covers that race; the ready callback
                covers all later reconnects. Newer mower generations do not
                necessarily use the legacy realtimeDate/location channel.
                """
                for device in devices:
                    topics = (
                        f"/downlink/vehicle/{device.id}/realtimeDate/location",
                        f"/downlink/vehicle/{device.id}/#",
                        f"/uplink/vehicle/{device.id}/#",
                        f"navimow/{device.id}/#",
                    )
                    for topic in topics:
                        result = mqtt.client.subscribe(topic)
                        _LOGGER.info(
                            "MQTT extended subscription: device=%s topic=%s result=%s",
                            device.id,
                            topic,
                            result,
                        )

            def _get_client_id() -> str:
                client_id_bytes = getattr(mqtt.client, "_client_id", b"")
                if isinstance(client_id_bytes, (bytes, bytearray)):
                    return client_id_bytes.decode("utf-8", errors="replace") or "<empty>"
                return str(client_id_bytes) if client_id_bytes else "<empty>"

            async def _on_connected() -> None:
                _LOGGER.info(
                    "MQTT connected callback: broker=%s port=%s ws_path=%s tls=%s client_id=%s",
                    mqtt.broker,
                    mqtt.port,
                    mqtt.ws_path,
                    mqtt._use_tls,
                    _get_client_id(),
                )

            async def _on_ready() -> None:
                _LOGGER.info(
                    "MQTT ready callback: subscribed to downlink topics on broker=%s port=%s client_id=%s",
                    mqtt.broker,
                    mqtt.port,
                    _get_client_id(),
                )
                # The official SDK subscribes only to known channels. The
                # wildcard lets us discover model-specific i-series channels.
                _attach_client_raw_hook()
                _subscribe_extended_topics()

            async def _on_disconnected() -> None:
                _LOGGER.debug(
                    "MQTT disconnected callback: broker=%s port=%s ws_path=%s tls=%s client_id=%s",
                    mqtt.broker,
                    mqtt.port,
                    mqtt.ws_path,
                    mqtt._use_tls,
                    _get_client_id(),
                )
                if _unload_flag[0]:
                    return
                if time.monotonic() < _planned_disconnect_until[0]:
                    _LOGGER.debug(
                        "MQTT credential refresh skipped for controlled location reconnect"
                    )
                    return
                # 若已有刷新在进行中，跳过本次——broker 批量断连会并发触发多次回调，
                # 只需执行一次凭据刷新即可，重复执行会导致 paho client 孤儿累积。
                if _mqtt_refresh_lock.locked():
                    _LOGGER.debug("MQTT credential refresh already in progress, skipping duplicate disconnect callback")
                    return
                async with _mqtt_refresh_lock:
                    if _unload_flag[0]:
                        return
                    # 断连后重新从服务端拉取 MQTT 凭据（userName/pwdInfo 与 OAuth token 绑定，
                    # token 刷新或过期后凭据会失效，直接用旧凭据重连会导致 CODE_OAUTH_INFO_ILLEGAL）
                    await _async_refresh_mqtt_credentials(sdk, api)

            async def _on_message(topic: str, payload: bytes, device_id: str) -> None:
                payload_text = (payload or b"").decode("utf-8", errors="replace")
                _LOGGER.debug(
                    "MQTT message received: topic=%s bytes=%d device=%s",
                    topic,
                    len(payload or b""),
                    device_id,
                )
                try:
                    parsed_payload: Any = json.loads(payload_text)
                except (TypeError, ValueError):
                    parsed_payload = payload_text
                coordinator = coordinators.get(device_id)
                if coordinator is not None:
                    coordinator.handle_raw_mqtt(topic, parsed_payload)
                    # This is intentionally attempted for every downlink topic.
                    # handle_location recursively recognizes coordinate-shaped
                    # dictionaries and ignores everything else.
                    coordinator.handle_location(parsed_payload)
                if original_on_message is not None:
                    await original_on_message(topic, payload, device_id)

            mqtt.on_connected = _on_connected
            mqtt.on_ready = _on_ready
            mqtt.on_disconnected = _on_disconnected
            mqtt.on_message = _on_message

            def _on_subscribe(_client, _userdata, mid, granted_qos, *args, **kwargs):
                _LOGGER.info(
                    "MQTT subscribed: mid=%s granted_qos=%s broker=%s port=%s client_id=%s",
                    mid,
                    granted_qos,
                    mqtt.broker,
                    mqtt.port,
                    _get_client_id(),
                )

            def _on_log(_client, _userdata, level, buf):
                _LOGGER.debug("MQTT client log: level=%s msg=%s", level, buf)

            mqtt.client.on_subscribe = _on_subscribe
            mqtt.client.on_log = _on_log

            _attach_client_raw_hook()

            # Cover the common case where sdk.connect() completed before the
            # callbacks above were attached.
            _subscribe_extended_topics()

        async def _probe_mqtt_status(sdk: NavimowSDK) -> None:
            await asyncio.sleep(5)
            _LOGGER.info("MQTT status probe (5s): connected=%s", sdk.is_connected)
            await asyncio.sleep(25)
            _LOGGER.info("MQTT status probe (30s): connected=%s", sdk.is_connected)

        async def _async_recover_location_stream(sdk: NavimowSDK) -> bool:
            """Perform one controlled reconnect so location delivery can recover."""
            if _mqtt_refresh_lock.locked() or _unload_flag[0]:
                return False
            async with _mqtt_refresh_lock:
                if _unload_flag[0]:
                    return False
                _planned_disconnect_until[0] = time.monotonic() + 15

                def _reconnect() -> None:
                    mqtt = sdk._mqtt
                    mqtt.disconnect()
                    mqtt.connect_async()

                try:
                    await hass.async_add_executor_job(_reconnect)
                except Exception as err:
                    _LOGGER.warning("MQTT location recovery reconnect failed: %s", err)
                    return False
                _LOGGER.info("MQTT controlled reconnect initiated for stale location stream")
                return True

        async def _location_watchdog(sdk: NavimowSDK) -> None:
            """Recover a missing location stream while a mower is active."""
            active_since: dict[str, float] = {}
            last_recovery = 0.0
            while not _unload_flag[0]:
                await asyncio.sleep(30)
                now = time.monotonic()
                stale_devices: list[NavimowCoordinator] = []
                for device_id, coordinator in coordinators.items():
                    state = coordinator.get_device_state()
                    state_value = getattr(state, "state", None) if state else None
                    state_value = getattr(state_value, "value", state_value)
                    is_active = str(state_value or "").lower() in {
                        "mowing",
                        "isrunning",
                        "mapping",
                        "ismapping",
                    }
                    if not is_active:
                        active_since.pop(device_id, None)
                        continue

                    started = active_since.setdefault(device_id, now)
                    last_location = coordinator.get_last_location_update()
                    stream_age = now - (last_location or started)
                    if stream_age >= 180:
                        stale_devices.append(coordinator)

                if not stale_devices or now - last_recovery < 300:
                    continue

                _LOGGER.warning(
                    "MQTT location stream stale for active mower(s): devices=%s action=reconnect",
                    ",".join(coordinator.device.id for coordinator in stale_devices),
                )
                if await _async_recover_location_stream(sdk):
                    last_recovery = now
                    for coordinator in stale_devices:
                        coordinator.record_location_recovery()

        async def _async_refresh_mqtt_credentials(sdk: NavimowSDK, api: MowerAPI) -> None:
            """Token 过期或 MQTT 断连后，重新获取 MQTT 凭据并更新 SDK。

            服务端下发的 userName/pwdInfo 与 OAuth token 绑定，token 刷新后需同步更新，
            否则 MQTT 重连时会收到 CODE_OAUTH_INFO_ILLEGAL。

            必须先刷新 OAuth token：MQTT 断连往往正是因为 token 过期触发的，
            此时 api._token 极可能也已失效，需先换新 token 再拉取 MQTT 凭据。
            """
            new_access_token: str | None = None
            new_auth_headers: dict[str, str] | None = None
            try:
                # 先刷新 OAuth token（oauth_session 来自外层闭包）
                if hasattr(oauth_session, "async_ensure_token_valid"):
                    await oauth_session.async_ensure_token_valid()
                    fresh_token = oauth_session.token
                elif hasattr(oauth_session, "async_get_valid_token"):
                    fresh_token = await oauth_session.async_get_valid_token()
                else:
                    fresh_token = oauth_session.token

                if fresh_token and fresh_token.get("access_token"):
                    new_access_token = fresh_token["access_token"]
                    api.set_token(new_access_token)
                    new_auth_headers = {"Authorization": f"Bearer {new_access_token}"}
            except Exception as err:
                _LOGGER.warning("Failed to refresh OAuth token before MQTT credential refresh: %s", err)

            try:
                new_mqtt_info = await api.async_get_mqtt_user_info()
            except Exception as err:
                _LOGGER.warning("Failed to refresh MQTT credentials: %s", err)
                return
            new_username = new_mqtt_info.get("userName")
            new_password = new_mqtt_info.get("pwdInfo")
            if new_auth_headers or new_username or new_password:
                # update_credentials 在断连时会调用 loop_stop()/tls_set()/load_default_certs()
                # 等阻塞 SSL 操作，必须在 executor 中执行，避免阻塞 HA 事件循环。
                # auth_headers 更新与 username/password 更新合并为一次 executor 调用。
                _new_auth_headers = new_auth_headers
                _new_username = new_username
                _new_password = new_password
                def _do_credential_update() -> None:
                    sdk.update_mqtt_credentials(
                        auth_headers=_new_auth_headers,
                        username=_new_username,
                        password=_new_password,
                    )
                await hass.async_add_executor_job(_do_credential_update)
                _LOGGER.info(
                    "MQTT credentials refreshed from server: username=%s",
                    _mask_secret(new_username),
                )

        def _create_sdk(api: MowerAPI) -> NavimowSDK:
            sdk = NavimowSDK(
                broker=mqtt_host,
                port=mqtt_port,
                username=mqtt_username,
                password=mqtt_password,
                ws_path=ws_path,
                auth_headers=auth_headers,
                loop=hass.loop,
                records=devices,
                # broker 每小时断连时，优先用 MQTT 协议层 keepalive（PINGREQ/PINGRESP）保活。
                keepalive_seconds=2400,  # 40 分钟
                reconnect_min_delay=1,
                reconnect_max_delay=60,
            )
            _LOGGER.info(
                "Invoking SDK MQTT connect: broker=%s port=%s ws_path=%s",
                mqtt_host,
                mqtt_port,
                ws_path,
            )
            sdk.connect()
            return sdk

        sdk = await hass.async_add_executor_job(_create_sdk, api)
        _attach_mqtt_debug_hooks(sdk, api)
        hass.async_create_task(_probe_mqtt_status(sdk))

        for device in devices:
            coordinator = NavimowCoordinator(
                hass=hass,
                sdk=sdk,
                api=api,
                device=device,
                oauth_session=oauth_session,
                discovery_payloads=map_discovery,
                private_client=private_client,
            )
            await coordinator.async_setup()
            await coordinator.async_config_entry_first_refresh()
            coordinators[device.id] = coordinator

        location_watchdog_task = hass.async_create_task(_location_watchdog(sdk))

        # 存储数据
        hass.data[DOMAIN][entry.entry_id] = {
            "sdk": sdk,
            "api": api,
            "devices": devices,
            "coordinators": coordinators,
            "oauth_session": oauth_session,
            "smart_home_capabilities": smart_home_capabilities,
            "map_discovery": map_discovery,
            "private_client": private_client,
            "unload_flag": _unload_flag,
            "location_watchdog_task": location_watchdog_task,
        }

        async_setup_services(hass)
        await _async_register_scheduler_frontend(hass)

        # 转发到平台
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))

        return True

    except ConfigEntryAuthFailed:
        raise
    except Exception as err:
        _LOGGER.exception("Error setting up Navimow integration: %s", err)
        raise ConfigEntryNotReady(f"Error setting up integration: {err}") from err


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # 清理数据
        if entry.entry_id in hass.data.get(DOMAIN, {}):
            data = hass.data[DOMAIN][entry.entry_id]
            # 标记正在卸载，阻止断连回调触发新的凭据刷新
            if "unload_flag" in data:
                data["unload_flag"][0] = True
            sdk = data.get("sdk")
            watchdog_task = data.get("location_watchdog_task")
            if watchdog_task:
                watchdog_task.cancel()
            if sdk:
                try:
                    sdk.disconnect()
                except Exception as err:
                    _LOGGER.warning("Error disconnecting MQTT: %s", err)

            hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
