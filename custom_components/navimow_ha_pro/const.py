"""Constants for Navimow integration."""
from __future__ import annotations
from typing import Final

DOMAIN: Final = "navimow_ha_pro"
INTEGRATION_VERSION: Final = "0.6.15"

# Optional one-time private-cloud session used only for LiDAR terrain files.
CONF_LIDAR_ENABLED: Final = "lidar_cloud_enabled"
CONF_PRIVATE_ACCESS_TOKEN: Final = "private_access_token"
CONF_PRIVATE_REFRESH_TOKEN: Final = "private_refresh_token"
CONF_PRIVATE_UUID: Final = "private_uuid"
CONF_PRIVATE_UID: Final = "private_uid"
CONF_PRIVATE_DEVICE_ID: Final = "private_device_id"
CONF_PRIVATE_REGION: Final = "private_region"
CONF_PRIVATE_ACCOUNT_REGION: Final = "private_account_region"
CONF_PRIVATE_HOST: Final = "private_host"

# Private Navimow cloud routing.  The value returned by Passport is not always
# the value the mower login expects: for example, an Americas account reports
# ``ore`` while it is routed through the ``us`` Passport cluster and the FRA
# mower host.  Keep these aliases limited to host selection; the raw account
# region is preserved separately by api.passport.
DEFAULT_REGION: Final = "fra"
_REGION_ALIASES: Final = {"eu": "fra", "sea": "sg", "ore": "us"}
PASSPORT_HOSTS: Final = {
    "fra": ("api-passport-fra.willand.com", "api-passport-fra.ninebot.com"),
    "sg": ("api-passport-sg.willand.com", "api-passport-sg.ninebot.com"),
    "us": (
        "api-passport-us.ninebot.com",
        "api-passport-ore.ninebot.com",
        "api-passport-ore.willand.com",
    ),
    "bj": ("api-passport-bj.willand.com", "api-passport-bj.ninebot.com"),
}
MOWER_HOSTS: Final = {
    "fra": ("navimow-fra.ninebot.com", "navimow-fra.willand.com"),
    "sg": ("navimow-sg.willand.com", "navimow-sg.ninebot.com"),
    "bj": ("navimow-bj.ninebot.com", "navimow-bj.willand.com"),
    # Americas accounts currently use the FRA mower-cloud cluster.
    "us": ("navimow-fra.ninebot.com", "navimow-ore.willand.com"),
}
ALL_PASSPORT_HOSTS: Final = tuple(
    dict.fromkeys(
        [hosts[0] for hosts in PASSPORT_HOSTS.values()]
        + [host for hosts in PASSPORT_HOSTS.values() for host in hosts[1:]]
    )
)
_ALL_MOWER_HOSTS: Final = tuple(
    dict.fromkeys(host for hosts in MOWER_HOSTS.values() for host in hosts)
)


def canonical_region(region: str | None) -> str:
    """Return the canonical routing region for a vendor account-region code."""
    code = str(region or "").strip().lower()
    return _REGION_ALIASES.get(code, code) if code else DEFAULT_REGION


def passport_hosts(region: str | None) -> tuple[str, ...]:
    """Return Passport hosts for a routing region."""
    return PASSPORT_HOSTS.get(canonical_region(region)) or ALL_PASSPORT_HOSTS


def mower_hosts(region: str | None) -> tuple[str, ...]:
    """Return preferred mower hosts followed by safe regional fallbacks."""
    preferred = MOWER_HOSTS.get(canonical_region(region)) or ()
    return preferred + tuple(host for host in _ALL_MOWER_HOSTS if host not in preferred)

# OAuth2 Configuration
# 授权页面 URL（用户登录页面）
# 添加 channel=homeassistant 以便 HA 跳转回登录页时携带渠道信息
OAUTH2_AUTHORIZE: Final = (
    "https://navimow-h5-fra.willand.com/smartHome/login?channel=homeassistant"
)

# Token 交换端点
OAUTH2_TOKEN: Final = "https://navimow-fra.ninebot.com/openapi/oauth/getAccessToken"

# Token 刷新端点
OAUTH2_REFRESH: Final | None = None

# OAuth2 Client 配置
CLIENT_ID: Final = "homeassistant"
CLIENT_SECRET: Final = "57056e15-722e-42be-bbaa-b0cbfb208a52"

# API 配置
API_BASE_URL: Final = "https://navimow-fra.ninebot.com"

# MQTT 配置
# TODO: 需要提供实际的 MQTT broker 地址和端口
MQTT_BROKER: Final = "mqtt.navimow.com"
MQTT_PORT: Final = 1883
MQTT_USERNAME: Final | None = None
MQTT_PASSWORD: Final | None = None

# 更新间隔（秒）
UPDATE_INTERVAL: Final = 30

# MQTT 超时时间（秒），超过该时间未收到消息则走 HTTP 兜底
MQTT_STALE_SECONDS: Final = 300

# HTTP 兜底最小拉取间隔（秒），避免频繁请求
HTTP_FALLBACK_MIN_INTERVAL: Final = 3600

# MowerStatus 到 LawnMowerActivity 的映射
MOWER_STATUS_TO_ACTIVITY = {
    "idle": "docked",
    "mowing": "mowing",
    "paused": "paused",
    "docked": "docked",
    "charging": "docked",
    "returning": "returning",
    "error": "error",
    "unknown": "error",
}
