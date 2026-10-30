"""Support for SmartThings Cloud."""

from typing import Any, override

from pysmartthings import (
    Attribute,
    Capability,
    Command,
    ComponentStatus,
    DeviceEvent,
    DeviceHealthEvent,
    SmartThings,
)
from pysmartthings.models import HealthStatus

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util

from . import FullDevice
from .assumed_state import AssumedValue, resolve_attribute_value
from .const import DOMAIN, MAIN, SIGNAL_SMARTTHINGS_UPDATE


class SmartThingsEntity(Entity):
    """Defines a SmartThings entity."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        client: SmartThings,
        device: FullDevice,
        capabilities: set[Capability],
        *,
        component: str = MAIN,
    ) -> None:
        """Initialize the instance."""
        self.client = client
        self.capabilities = capabilities
        self.component = component
        self._internal_state: ComponentStatus = {
            capability: device.status[component][capability]
            for capability in capabilities
            if capability in device.status[component]
        }
        self.device = device
        self._attr_unique_id = f"{device.device.device_id}_{component}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device.device_id)},
        )
        self._attr_available = device.online

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe to REST-polling updates for this device."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_SMARTTHINGS_UPDATE.format(self.device.device.device_id),
                self._handle_coordinator_update,
            )
        )
        self._update_attr()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle a fresh poll: rebuild internal state from the (in-place) updated device."""
        self._internal_state = {
            capability: self.device.status[self.component][capability]
            for capability in self.capabilities
            if capability in self.device.status[self.component]
        }
        self._attr_available = self.device.online
        self._update_attr()
        self.async_write_ha_state()

    def _availability_handler(self, event: DeviceHealthEvent) -> None:
        # Retained for the (currently unused) websocket path; not called under REST polling.
        self._attr_available = event.status != HealthStatus.OFFLINE
        self.async_write_ha_state()

    def _update_handler(self, event: DeviceEvent) -> None:
        # Retained so subclass overrides (event.py, light.py) keep a valid super() call. Not
        # invoked under REST polling (momentary events like button presses aren't pollable).
        self._internal_state[event.capability][event.attribute].value = event.value
        self._internal_state[event.capability][event.attribute].data = event.data
        self._handle_update()

    def supports_capability(self, capability: Capability) -> bool:
        """Test if device supports a capability."""
        return capability in self.device.status[self.component]

    def get_attribute_value(self, capability: Capability, attribute: Attribute) -> Any:
        """Get the value of a device attribute, preferring a recent command's result."""
        return resolve_attribute_value(
            self.device.assumed,
            (self.component, capability, attribute),
            self._internal_state[capability][attribute],
            dt_util.utcnow(),
        )

    def _update_attr(self) -> None:
        """Update the attributes."""

    def _handle_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attr()
        self.async_write_ha_state()

    async def execute_device_command(
        self,
        capability: Capability,
        command: Command,
        argument: int | str | list[Any] | dict[str, Any] | None = None,
        *,
        assume: dict[tuple[Capability, Attribute], Any] | None = None,
    ) -> None:
        """Execute a command on the device.

        ``assume`` maps (capability, attribute) to the value the command should produce.
        It's shown immediately, instead of the UI snapping back until the next poll.
        """
        kwargs = {}
        if argument is not None:
            kwargs["argument"] = argument
        device_id = self.device.device.device_id
        # Stamp before sending: any device report newer than this postdates the command.
        sent_at = dt_util.utcnow()
        await self.client.execute_device_command(
            device_id, capability, command, self.component, **kwargs
        )
        if assume:
            for (assumed_capability, attribute), value in assume.items():
                self.device.assumed[(self.component, assumed_capability, attribute)] = (
                    AssumedValue(value, sent_at)
                )
            # Device-wide signal so sibling entities reading the same attribute update too.
            async_dispatcher_send(
                self.hass, SIGNAL_SMARTTHINGS_UPDATE.format(device_id)
            )
        if (entry := self.platform.config_entry) is not None:
            entry.runtime_data.coordinator.async_schedule_device_refresh(device_id)
