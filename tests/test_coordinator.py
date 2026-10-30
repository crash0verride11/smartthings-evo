"""Tests for the REST polling coordinator without a Home Assistant install."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

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

    homeassistant = ModuleType("homeassistant")
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = object
    exceptions = ModuleType("homeassistant.exceptions")
    exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    helpers = ModuleType("homeassistant.helpers")
    dispatcher = ModuleType("homeassistant.helpers.dispatcher")
    dispatcher.async_dispatcher_send = lambda hass, signal: signals.append(signal)
    update_coordinator = ModuleType("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = FakeDataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed

    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exceptions)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.dispatcher", dispatcher)
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.update_coordinator", update_coordinator
    )

    constants = ModuleType(f"{PACKAGE}.const")
    constants.DEFAULT_SCAN_INTERVAL = 30
    constants.DOMAIN = "smartthings"
    constants.MAIN = "main"
    constants.SIGNAL_SMARTTHINGS_UPDATE = "smartthings_update_{}"
    monkeypatch.setitem(sys.modules, f"{PACKAGE}.const", constants)

    module = load_component_module("coordinator")
    module._test_signals = signals
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
