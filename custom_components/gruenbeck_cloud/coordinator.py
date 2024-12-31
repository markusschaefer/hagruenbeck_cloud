"""Coordinator for Grünbeck Cloud integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
from httpcore import URL
from pygruenbeck_cloud import PyGruenbeckCloud
from pygruenbeck_cloud.exceptions import (
    PyGruenbeckCloudConnectionClosedError,
    PyGruenbeckCloudError,
    PyGruenbeckCloudResponseStatusError,
    PyGruenbeckCloudUpdateParameterError,
)
from pygruenbeck_cloud.models import Device, DeviceRealtimeInfo
from pygruenbeck_cloud.const import (WEB_REQUESTS, PARAM_NAME_DEVICE_ID, PARAM_NAME_ACCESS_TOKEN)

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_DEVICE_ID,
    DOMAIN,
    SERVICE_PARAM_PARAMETER,
    SERVICE_PARAM_VALUE,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class GruenbeckCloudCoordinator(DataUpdateCoordinator[Device]):
    """Grünbeck Cloud Coordinator."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize Coordinator."""
        self.api = PyGruenbeckCloud(
            username=config_entry.data[CONF_USERNAME],
            password=config_entry.data[CONF_PASSWORD],
        )
        self.api.session = async_get_clientsession(hass)
        self.api.logger = _LOGGER
        self._device_id = config_entry.data[CONF_DEVICE_ID]

        self.unsub: CALLBACK_TYPE | None = None
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)

    async def disconnect(self) -> None:
        """Disconnect from API."""
        await self.api.disconnect()

    @callback
    def _listen_websocket(self) -> None:
        """Listen to WebSocket updates."""

        async def listen() -> None:
            """Listen for state changes."""
            try:
                await self.api.connect()
            except (PyGruenbeckCloudError, PyGruenbeckCloudResponseStatusError) as err:
                self.logger.error(err)
                if self.unsub:
                    self.unsub()
                    self.unsub = None
                return

            try:
                await self.api.listen(callback=self.async_set_updated_data)
            except (
                PyGruenbeckCloudConnectionClosedError,
                PyGruenbeckCloudResponseStatusError,
            ) as err:
                self.last_update_success = False
                self.logger.error(err)
            except PyGruenbeckCloudError as err:
                self.last_update_success = False
                self.async_update_listeners()
                self.logger.error(err)

            # Ensure we disconnect
            await self.api.disconnect()
            if self.unsub:
                self.unsub()
                self.unsub = None

        async def close_websocket(_: Event) -> None:
            """Close WebSocket connection."""
            self.unsub = None
            await self.api.disconnect()

        # Clean disconnect WebSocket on Home Assistant shutdown
        self.unsub = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, close_websocket
        )

        # Start listener
        self.config_entry.async_create_background_task(
            self.hass, listen(), "gruenbeck-cloud-listen"
        )

    async def service_get_device_salt_measurements(
        self, call: ServiceCall
    ) -> ServiceResponse:
        """Service to get Salt measurements."""
        device = await self.api.get_device_salt_measurements()
        if device.salt is None:
            return {"entries": []}

        return {
            "entries": [
                {
                    "date": item.date,
                    "value": item.value,
                }
                for item in device.salt
            ],
        }

    async def service_get_device_water_measurements(
        self, call: ServiceCall
    ) -> ServiceResponse:
        """Service to get Water measurements."""
        device = await self.api.get_device_water_measurements()
        if device.water is None:
            return {"entries": []}

        return {
            "entries": [
                {
                    "date": item.date,
                    "value": item.value,
                }
                for item in device.water
            ],
        }

    async def service_regenerate(self, call: ServiceCall) -> None:
        """Service to start manual regeneration."""
        await self.api.regenerate()

    async def service_change_settings(self, call: ServiceCall) -> None:
        """Service for update device settings."""
        data = {
            call.data[SERVICE_PARAM_PARAMETER]: call.data[SERVICE_PARAM_VALUE],
        }

        await self.update_device_infos_parameters(data)

    async def update_device_infos_parameters(self, data: dict[str, Any]) -> None:
        """Update Device parameters."""
        try:
            self.data = await self.api.update_device_infos_parameters(data)
        except PyGruenbeckCloudUpdateParameterError as err:
            raise HomeAssistantError(err) from err

    @callback
    def async_set_updated_data(self, data: Device) -> None:
        """Manually update data from WebSocket, avoid stopping refresh interval."""
        self.data = data
        self.last_update_success = True

        self.logger.debug(
            "Manually updated %s data",
            self.name,
        )
        self.async_update_listeners()

    async def _async_update_data(self) -> Device:
        """Update regularly data from API."""
        self.logger.debug(
            "Regularly updated %s data",
            self.name,
        )

        try:
            if not self.api.device:
                await self.api.set_device_from_id(self._device_id)
            await self.api.get_device_infos()
            device = await self.api.get_device_infos_parameters()

            # Start listening to websocket at first time
            if not self.api.connected and not self.unsub:
                self._listen_websocket()
            else:
                await self.api.enter_sd()
                #await self.api.refresh_sd()
                device.realtime = self.refresh_sd()

            self.logger.debug("=== real time data ===")
            self.logger.debug(self.api.device)
            return device
        except (
            Exception,
            IndexError,
            KeyError,
            PyGruenbeckCloudResponseStatusError,
        ) as err:
            raise UpdateFailed(f"Unable to get data from API: {err}") from err

    async def refresh_sd(self) -> DeviceRealtimeInfo:

        token = await self.api._get_web_access_token()

        scheme = WEB_REQUESTS["refresh_sd"]["scheme"]
        host = WEB_REQUESTS["refresh_sd"]["host"]
        port = WEB_REQUESTS["refresh_sd"]["port"]
        use_cookies = WEB_REQUESTS["refresh_sd"]["use_cookies"]

        headers = self.api._placeholder_to_values_dict(
            WEB_REQUESTS["refresh_sd"]["headers"],
            {
                PARAM_NAME_ACCESS_TOKEN: token,
            },
        )
        path = self.api._placeholder_to_values_str(
            WEB_REQUESTS["refresh_sd"]["path"],
            {PARAM_NAME_DEVICE_ID: self._device_id},
        )
        method = WEB_REQUESTS["refresh_sd"]["method"]
        data = WEB_REQUESTS["refresh_sd"]["data"]
        query = WEB_REQUESTS["refresh_sd"]["query_params"]

        url = URL.build(scheme=scheme, host=host, port=port, path=path, query=query)
        # @TODO - expected_status_codes and allow_redirects can also come from CONST!
        response = await self.api._http_request(
            url=url,
            headers=headers,
            method=method,
            data=data,
            expected_status_codes=[aiohttp.http.HTTPStatus.ACCEPTED, aiohttp.http.HTTPStatus.OK],
            use_cookies=use_cookies,
        )

        return DeviceRealtimeInfo.from_dict(response)