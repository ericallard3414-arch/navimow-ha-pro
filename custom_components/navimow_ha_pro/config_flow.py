"""Config flow for Navimow integration."""
from __future__ import annotations
import logging
import uuid
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_entry_oauth2_flow

from .auth import NavimowOAuth2Implementation
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
from .api import NavimowAuthError, NavimowCloudClient, PassportAuthError

_LOGGER = logging.getLogger(__name__)
_LOGGER.debug("Navimow config_flow module imported")


class NavimowOAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle a Navimow OAuth2 config flow."""

    DOMAIN = DOMAIN
    VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    @property
    def oauth2_implementation(self) -> NavimowOAuth2Implementation:
        """Return the OAuth2 implementation."""
        _LOGGER.debug(
            "Creating OAuth2 implementation for domain=%s, client_id_set=%s, client_secret_set=%s",
            DOMAIN,
            bool(CLIENT_ID),
            bool(CLIENT_SECRET),
        )
        implementation = NavimowOAuth2Implementation(
            self.hass, DOMAIN, CLIENT_ID, CLIENT_SECRET
        )
        # Ensure HA has the implementation registered before redirect/callback.
        config_entry_oauth2_flow.async_register_implementation(
            self.hass, DOMAIN, implementation
        )
        _LOGGER.debug("OAuth2 implementation registered for domain=%s", DOMAIN)
        return implementation

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initiated by the user."""
        _LOGGER.debug("Starting OAuth2 flow: source=%s", self.source)
        # 检查是否已经配置
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # 检查必要的配置
        if not CLIENT_ID or not CLIENT_SECRET:
            _LOGGER.error(
                "Missing OAuth2 client configuration: client_id_set=%s, client_secret_set=%s",
                bool(CLIENT_ID),
                bool(CLIENT_SECRET),
            )
            return self.async_abort(
                reason="missing_config",
                description_placeholders={
                    "error": "CLIENT_ID 或 CLIENT_SECRET 未配置，请在 const.py 中配置"
                },
            )

        # Ensure implementation is registered before authorize step.
        _LOGGER.debug("Registering OAuth2 implementation before authorize step")
        _ = self.oauth2_implementation
        # 仅一个 OAuth2 实现，直接进入授权步骤
        _LOGGER.debug("Proceeding to OAuth2 authorize step")
        return await super().async_step_user()

    async def async_step_oauth2_authorize(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ensure implementation exists before redirect."""
        _LOGGER.debug("Entering oauth2_authorize step")
        # Force register implementation in case HA missed it.
        _ = self.oauth2_implementation
        return await super().async_step_oauth2_authorize(user_input)

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Perform reauth upon an API authentication error."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Dialog that informs the user that reauth is required."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=None,
            )

        # 仅一个 OAuth2 实现，直接进入授权步骤
        return await super().async_step_user()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> FlowResult:
        """Create an entry for the flow, or update existing entry for reauth."""
        # HA 已经自动处理了 token 交换，data["token"] 已包含 token 信息
        # 如果是 reauth，HA 会自动更新 entry
        if self.source == config_entries.SOURCE_REAUTH:
            existing_entry = self.entry
            self.hass.config_entries.async_update_entry(
                existing_entry,
                data={
                    **existing_entry.data,
                    **data,  # 包含新的 token
                },
            )
            await self.hass.config_entries.async_reload(existing_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        self._pending_oauth_data = data
        return await self.async_step_private_login()

    async def async_step_private_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Offer maps and advanced controls during initial onboarding."""
        errors: dict[str, str] = {}
        if user_input is not None:
            enabled = bool(user_input.get(CONF_LIDAR_ENABLED))
            private_data: dict[str, Any] = {CONF_LIDAR_ENABLED: enabled}
            if enabled:
                email = str(user_input.get("email") or "").strip()
                password = str(user_input.get("password") or "")
                if not email or not password:
                    errors["base"] = "lidar_credentials_required"
                else:
                    device_id = uuid.uuid4().hex

                    def _login() -> dict[str, Any]:
                        client = NavimowCloudClient(device_id)
                        tokens = client.authenticate(email, password)
                        client.mower_login()
                        return {
                            CONF_LIDAR_ENABLED: True,
                            CONF_PRIVATE_ACCESS_TOKEN: tokens.access_token,
                            CONF_PRIVATE_REFRESH_TOKEN: tokens.refresh_token,
                            CONF_PRIVATE_UUID: tokens.uuid,
                            CONF_PRIVATE_UID: client.uid,
                            CONF_PRIVATE_DEVICE_ID: device_id,
                            CONF_PRIVATE_REGION: client.region,
                            CONF_PRIVATE_ACCOUNT_REGION: tokens.account_region,
                            CONF_PRIVATE_HOST: client.host,
                        }

                    try:
                        private_data = await self.hass.async_add_executor_job(_login)
                    except (PassportAuthError, NavimowAuthError):
                        errors["base"] = "lidar_invalid_auth"
                    except OSError:
                        errors["base"] = "cannot_connect"
                    except Exception:
                        _LOGGER.exception("Initial private-cloud setup failed")
                        errors["base"] = "unknown"

            if not errors:
                oauth_data = getattr(self, "_pending_oauth_data", {})
                return self.async_create_entry(
                    title="Navimow",
                    data={
                        "auth_implementation": DOMAIN,
                        **oauth_data,
                        "api_base_url": API_BASE_URL,
                        "mqtt_broker": MQTT_BROKER,
                        "mqtt_port": MQTT_PORT,
                        "mqtt_username": MQTT_USERNAME,
                        "mqtt_password": MQTT_PASSWORD,
                        **private_data,
                    },
                )

        return self.async_show_form(
            step_id="private_login",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LIDAR_ENABLED, default=True): bool,
                    vol.Optional("email"): str,
                    vol.Optional("password"): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return NavimowOptionsFlowHandler(config_entry)


class NavimowOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Navimow options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    @property
    def _current_private_config(self) -> dict[str, Any]:
        return {**self._config_entry.data, **self._config_entry.options}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            enabled = bool(user_input.get(CONF_LIDAR_ENABLED))
            if not enabled:
                return self.async_create_entry(
                    title="", data={CONF_LIDAR_ENABLED: False}
                )

            email = str(user_input.get("email") or "").strip()
            password = str(user_input.get("password") or "")
            if not email or not password:
                errors["base"] = "lidar_credentials_required"
            else:
                device_id = str(
                    self._current_private_config.get(CONF_PRIVATE_DEVICE_ID)
                    or uuid.uuid4().hex
                )

                def _login() -> dict[str, Any]:
                    client = NavimowCloudClient(device_id)
                    tokens = client.authenticate(email, password)
                    client.mower_login()
                    return {
                        CONF_LIDAR_ENABLED: True,
                        CONF_PRIVATE_ACCESS_TOKEN: tokens.access_token,
                        CONF_PRIVATE_REFRESH_TOKEN: tokens.refresh_token,
                        CONF_PRIVATE_UUID: tokens.uuid,
                        CONF_PRIVATE_UID: client.uid,
                        CONF_PRIVATE_DEVICE_ID: device_id,
                        CONF_PRIVATE_REGION: client.region,
                        CONF_PRIVATE_ACCOUNT_REGION: tokens.account_region,
                        CONF_PRIVATE_HOST: client.host,
                    }

                try:
                    private_options = await self.hass.async_add_executor_job(_login)
                except (PassportAuthError, NavimowAuthError):
                    errors["base"] = "lidar_invalid_auth"
                except OSError:
                    errors["base"] = "cannot_connect"
                except Exception:  # defensive: never expose token/password details
                    _LOGGER.exception("LiDAR private-session setup failed")
                    errors["base"] = "unknown"
                else:
                    return self.async_create_entry(title="", data=private_options)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LIDAR_ENABLED,
                        default=bool(
                            self._current_private_config.get(CONF_LIDAR_ENABLED, False)
                        ),
                    ): bool,
                    vol.Optional("email"): str,
                    vol.Optional("password"): str,
                }
            ),
            errors=errors,
        )
