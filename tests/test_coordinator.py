"""Tests for the REST polling coordinator without a Home Assistant install."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import PACKAGE, load_component_module
from pysmartthings import (
    Category,
    SmartThingsAuthenticationFailedError,
    SmartThingsConnectionError,
)
from pysmartthings.models import HealthStatus


class ConfigEntryAuthFailed(Exception):
    """Home Assistant authentication failure stand-in."""


class UpdateFailed(Exception):
    """Home Assistant coordinator failure stand-in."""


class FakeDataUpdateCoordinator:
    """Small subset used by SmartThingsCoordinator."""

    def __init__(
        self,
        hass,
        logger,
        *,
        config_entry,
        name,
        update_interval,
    ):
        self.hass = hass
        self.logger = logger
        self.config_entry = config_entry
        self.name = name
        self.update_interval = update_interval
        self.data = None
        self.shutdown_called = False

    async def async_shutdown(self):
        self.shutdown_called = True

    def __class_getitem__(cls, item):
        return cls


@dataclass
class FakeFullDevice:
    """Mutable device record matching the integration's runtime shape."""

    device: object
    status: dict
    online: bool


@pytest.fixture
def coordinator_module(monkeypatch):
    """Load the coordinator with minimal Home Assistant module stand-ins."""
    signals = []
    timers = []

    homeassistant = ModuleType("homeassistant")
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.CALLBACK_TYPE = object
    core.callback = lambda func: func
    exceptions = ModuleType("homeassistant.exceptions")
    exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    helpers = ModuleType("homeassistant.helpers")
    dispatcher = ModuleType("homeassistant.helpers.dispatcher")
    dispatcher.async_dispatcher_send = lambda hass, signal: signals.append(signal)
    event = ModuleType("homeassistant.helpers.event")

    def async_call_later(hass, delay, action):
        cancel = MagicMock()
        timers.append(SimpleNamespace(delay=delay, action=action, cancel=cancel))
        return cancel

    event.async_call_later = async_call_later
    update_coordinator = ModuleType("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = FakeDataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed

    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exceptions)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.dispatcher", dispatcher)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", event)
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.update_coordinator", update_coordinator
    )

    constants = ModuleType(f"{PACKAGE}.const")
    constants.COMMAND_REFRESH_DELAY = 3
    constants.DEFAULT_SCAN_INTERVAL = 30
    constants.DOMAIN = "smartthings"
    constants.MAIN = "main"
    constants.SIGNAL_SMARTTHINGS_UPDATE = "smartthings_update_{}"
    monkeypatch.setitem(sys.modules, f"{PACKAGE}.const", constants)

    module = load_component_module("coordinator")
    module._test_signals = signals
    module._test_timers = timers
    return module


def _device(device_id, *, category=None):
    component = SimpleNamespace(manufacturer_category=category)
    return SimpleNamespace(device_id=device_id, components={"main": component})


@pytest.mark.asyncio
async def test_poll_updates_status_health_and_dispatches(coordinator_module):
    client = SimpleNamespace(
        get_device_status=AsyncMock(return_value={"raw": "status"}),
        get_device_health=AsyncMock(
            return_value=SimpleNamespace(state=HealthStatus.ONLINE)
        ),
    )
    full = FakeFullDevice(_device("device-1"), {"old": "status"}, False)

    def process_status(value):
        return {"processed": value}

    coordinator = coordinator_module.SmartThingsCoordinator(
        object(), object(), client, {"device-1": full}, process_status
    )

    result = await coordinator._async_update_data()

    assert result == {"device-1": full}
    assert full.status == {"processed": {"raw": "status"}}
    assert full.online is True
    client.get_device_status.assert_awaited_once_with("device-1")
    client.get_device_health.assert_awaited_once_with("device-1")
    assert coordinator_module._test_signals == ["smartthings_update_device-1"]


@pytest.mark.asyncio
async def test_poll_marks_offline_device_unavailable(coordinator_module):
    client = SimpleNamespace(
        get_device_status=AsyncMock(return_value={"raw": "status"}),
        get_device_health=AsyncMock(
            return_value=SimpleNamespace(state=HealthStatus.OFFLINE)
        ),
    )
    full = FakeFullDevice(_device("device-1"), {}, True)
    coordinator = coordinator_module.SmartThingsCoordinator(
        object(), object(), client, {"device-1": full}, lambda value: value
    )

    await coordinator._async_update_data()

    assert full.online is False


@pytest.mark.asyncio
async def test_tracker_skips_unsupported_status_requests(coordinator_module):
    client = SimpleNamespace(
        get_device_status=AsyncMock(),
        get_device_health=AsyncMock(),
    )
    full = FakeFullDevice(
        _device("tracker-1", category=Category.BLUETOOTH_TRACKER), {}, False
    )
    coordinator = coordinator_module.SmartThingsCoordinator(
        object(), object(), client, {"tracker-1": full}, lambda value: value
    )

    await coordinator._async_update_data()

    assert full.online is True
    client.get_device_status.assert_not_awaited()
    client.get_device_health.assert_not_awaited()
    assert coordinator_module._test_signals == ["smartthings_update_tracker-1"]


@pytest.mark.asyncio
async def test_authentication_failure_requests_reauthentication(coordinator_module):
    client = SimpleNamespace(
        get_device_status=AsyncMock(
            side_effect=SmartThingsAuthenticationFailedError("expired")
        ),
        get_device_health=AsyncMock(),
    )
    coordinator = coordinator_module.SmartThingsCoordinator(
        object(),
        object(),
        client,
        {"device-1": FakeFullDevice(_device("device-1"), {}, True)},
        lambda value: value,
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    assert coordinator_module._test_signals == []


@pytest.mark.asyncio
async def test_connection_failure_becomes_update_failed(coordinator_module):
    client = SimpleNamespace(
        get_device_status=AsyncMock(side_effect=SmartThingsConnectionError("offline")),
        get_device_health=AsyncMock(),
    )
    coordinator = coordinator_module.SmartThingsCoordinator(
        object(),
        object(),
        client,
        {"device-1": FakeFullDevice(_device("device-1"), {}, True)},
        lambda value: value,
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator_module._test_signals == []


def _online_client():
    return SimpleNamespace(
        get_device_status=AsyncMock(return_value={"raw": "status"}),
        get_device_health=AsyncMock(
            return_value=SimpleNamespace(state=HealthStatus.ONLINE)
        ),
    )


@pytest.mark.asyncio
async def test_refresh_device_polls_only_that_device(coordinator_module):
    client = _online_client()
    one = FakeFullDevice(_device("device-1"), {}, False)
    two = FakeFullDevice(_device("device-2"), {}, False)
    coordinator = coordinator_module.SmartThingsCoordinator(
        object(),
        object(),
        client,
        {"device-1": one, "device-2": two},
        lambda value: value,
    )

    await coordinator.async_refresh_device("device-2")

    client.get_device_status.assert_awaited_once_with("device-2")
    assert two.status == {"raw": "status"}
    assert two.online is True
    assert one.status == {}
    assert coordinator_module._test_signals == ["smartthings_update_device-2"]


@pytest.mark.asyncio
async def test_refresh_device_failure_is_swallowed(coordinator_module):
    client = SimpleNamespace(
        get_device_status=AsyncMock(side_effect=SmartThingsConnectionError("offline")),
        get_device_health=AsyncMock(),
    )
    full = FakeFullDevice(_device("device-1"), {"old": "status"}, True)
    coordinator = coordinator_module.SmartThingsCoordinator(
        object(), object(), client, {"device-1": full}, lambda value: value
    )

    await coordinator.async_refresh_device("device-1")

    assert full.status == {"old": "status"}
    assert coordinator_module._test_signals == []


@pytest.mark.asyncio
async def test_schedule_device_refresh_debounces_per_device(coordinator_module):
    client = _online_client()
    full = FakeFullDevice(_device("device-1"), {}, False)
    coordinator = coordinator_module.SmartThingsCoordinator(
        object(), object(), client, {"device-1": full}, lambda value: value
    )
    timers = coordinator_module._test_timers

    coordinator.async_schedule_device_refresh("device-1")
    coordinator.async_schedule_device_refresh("device-1")

    assert len(timers) == 2
    assert timers[0].delay == 3
    timers[0].cancel.assert_called_once()
    timers[1].cancel.assert_not_called()

    await timers[1].action(None)

    client.get_device_status.assert_awaited_once_with("device-1")
    assert coordinator_module._test_signals == ["smartthings_update_device-1"]


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_refreshes(coordinator_module):
    coordinator = coordinator_module.SmartThingsCoordinator(
        object(),
        object(),
        _online_client(),
        {"device-1": FakeFullDevice(_device("device-1"), {}, True)},
        lambda value: value,
    )
    coordinator.async_schedule_device_refresh("device-1")

    await coordinator.async_shutdown()

    coordinator_module._test_timers[0].cancel.assert_called_once()
    assert coordinator.shutdown_called is True
