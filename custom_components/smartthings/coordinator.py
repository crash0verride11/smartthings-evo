"""REST polling coordinator for SmartThings.

The Samsung-account (OSP) token has no ``installed_app_id`` / ``sse`` scope, so the realtime
websocket subscription isn't available. Instead we poll each device's status + health on an
interval, update the shared ``FullDevice`` objects in place, and notify entities via a
per-device dispatcher signal (entities keep the references they were built with, so no
coordinator plumbing is needed in the ~18 platform files).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING

from pysmartthings import (
    Category,
    ComponentStatus,
    SmartThings,
    SmartThingsAuthenticationFailedError,
    SmartThingsConnectionError,
    SmartThingsError,
)
from pysmartthings.models import HealthStatus

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    COMMAND_REFRESH_DELAY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAIN,
    SIGNAL_SMARTTHINGS_UPDATE,
)

if TYPE_CHECKING:
    from . import FullDevice, SmartThingsConfigEntry

_LOGGER = logging.getLogger(__name__)


class SmartThingsCoordinator(DataUpdateCoordinator[dict[str, "FullDevice"]]):
    """Polls SmartThings device status/health over REST and updates entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SmartThingsConfigEntry,
        client: SmartThings,
        devices: dict[str, FullDevice],
        process_status: Callable[[dict[str, ComponentStatus]], dict[str, ComponentStatus]],
    ) -> None:
        """Initialise the coordinator with the already-discovered devices."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.devices = devices
        self._process_status = process_status
        # We already have the initial status from setup; seed it so the first scheduled poll
        # is a refresh, not a cold start.
        self.data = devices
        self._pending_refreshes: dict[str, CALLBACK_TYPE] = {}

    async def _async_update_data(self) -> dict[str, FullDevice]:
        """Refresh status + health for every known device, in place."""
        for device_id, full in self.devices.items():
            try:
                await self._async_update_device(device_id, full)
            except SmartThingsAuthenticationFailedError as err:
                raise ConfigEntryAuthFailed from err
            except SmartThingsConnectionError as err:
                raise UpdateFailed(err) from err

            async_dispatcher_send(
                self.hass, SIGNAL_SMARTTHINGS_UPDATE.format(device_id)
            )
        return self.devices

    async def _async_update_device(self, device_id: str, full: FullDevice) -> None:
        """Fetch one device's status + health into its ``FullDevice``."""
        main = full.device.components.get(MAIN)
        if (
            main is not None
            and main.manufacturer_category is Category.BLUETOOTH_TRACKER
        ):
            # Trackers expose no queryable status; treat as online (matches setup).
            full.online = True
            return
        full.status = self._process_status(
            await self.client.get_device_status(device_id)
        )
        health = await self.client.get_device_health(device_id)
        full.online = health.state == HealthStatus.ONLINE

    @callback
    def async_schedule_device_refresh(self, device_id: str) -> None:
        """Re-poll one device shortly after a command, debounced per device.

        Confirms the command in seconds rather than at the next full poll, without
        re-fetching every device.
        """
        if (cancel := self._pending_refreshes.pop(device_id, None)) is not None:
            cancel()

        async def _refresh(_now: datetime) -> None:
            self._pending_refreshes.pop(device_id, None)
            await self.async_refresh_device(device_id)

        self._pending_refreshes[device_id] = async_call_later(
            self.hass, COMMAND_REFRESH_DELAY, _refresh
        )

    async def async_refresh_device(self, device_id: str) -> None:
        """Re-poll a single device outside the regular interval."""
        if (full := self.devices.get(device_id)) is None:
            return
        try:
            await self._async_update_device(device_id, full)
        except SmartThingsError as err:
            # Best effort: the scheduled poll surfaces persistent auth/connection failures.
            _LOGGER.debug("Post-command refresh of %s failed: %s", device_id, err)
            return
        async_dispatcher_send(self.hass, SIGNAL_SMARTTHINGS_UPDATE.format(device_id))

    async def async_shutdown(self) -> None:
        """Cancel pending post-command refreshes."""
        for cancel in self._pending_refreshes.values():
            cancel()
        self._pending_refreshes.clear()
        await super().async_shutdown()
