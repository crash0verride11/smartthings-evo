"""Tests for Samsung-account configuration-flow state transitions."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import PACKAGE, load_component_module
from pysmartthings import SmartThingsConnectionError


class FakeConfigFlow:
    """Minimal ConfigFlow API used by this integration."""

    source = "user"
    flow_id = "flow-id"

    def __init_subclass__(cls, *, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.domain = domain

    def async_abort(self, *, reason, description_placeholders=None):
        return {
            "type": "abort",
            "reason": reason,
            "description_placeholders": description_placeholders,
        }

    def async_show_menu(self, *, step_id, menu_options):
        return {
            "type": "menu",
            "step_id": step_id,
            "menu_options": menu_options,
        }

    def async_external_step(self, *, step_id, url):
        return {"type": "external", "step_id": step_id, "url": url}

    def async_external_step_done(self, *, next_step_id):
        return {"type": "external_done", "next_step_id": next_step_id}

    def async_show_form(
        self,
        *,
        step_id,
        data_schema=None,
        description_placeholders=None,
        errors=None,
    ):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "description_placeholders": description_placeholders,
            "errors": errors or {},
        }

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        self.unique_id_configured_called = True
        return None

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def _abort_if_unique_id_mismatch(self, *, reason):
        self.unique_id_mismatch_reason = reason
        return None

    def _get_reauth_entry(self):
        return object()

    def async_update_reload_and_abort(self, entry, *, data):
        return {"type": "reauth", "entry": entry, "data": data}


@pytest.fixture
def config_flow_module(monkeypatch, samsung_auth):
    """Load config_flow with a compact Home Assistant API stand-in."""
    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.SOURCE_REAUTH = "reauth"
    config_entries.ConfigFlow = FakeConfigFlow
    config_entries.ConfigFlowResult = dict

    constants = ModuleType("homeassistant.const")
    constants.CONF_TOKEN = "token"

    oauth = ModuleType("homeassistant.helpers.config_entry_oauth2_flow")
    oauth._encode_jwt = lambda hass, payload: "encoded-routing-state"
    aiohttp_client = ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: hass.session

    helpers = ModuleType("homeassistant.helpers")
    helpers.config_entry_oauth2_flow = oauth

    monkeypatch.setitem(sys.modules, "homeassistant", ModuleType("homeassistant"))
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries)
    monkeypatch.setitem(sys.modules, "homeassistant.const", constants)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.config_entry_oauth2_flow", oauth
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.aiohttp_client", aiohttp_client
    )

    component_constants = ModuleType(f"{PACKAGE}.const")
    component_constants.CONF_LOCATION_ID = "location_id"
    component_constants.DOMAIN = "smartthings"
    monkeypatch.setitem(sys.modules, f"{PACKAGE}.const", component_constants)
    monkeypatch.setitem(sys.modules, f"{PACKAGE}.samsung_auth", samsung_auth)

    return load_component_module("config_flow")


def _flow(module, *, cloud=True):
    flow = module.SmartThingsConfigFlow()
    components = {"cloud"} if cloud else set()
    flow.hass = SimpleNamespace(
        config=SimpleNamespace(components=components),
        session=object(),
    )
    return flow


@pytest.mark.asyncio
async def test_user_step_requires_home_assistant_cloud(config_flow_module):
    result = await _flow(config_flow_module, cloud=False).async_step_user()

    assert result["type"] == "abort"
    assert result["reason"] == "cloud_not_enabled"


@pytest.mark.asyncio
async def test_user_step_offers_both_authentication_methods(config_flow_module):
    result = await _flow(config_flow_module).async_step_user()

    assert result == {
        "type": "menu",
        "step_id": "pick_method",
        "menu_options": ["auth_extension", "auth_manual"],
    }


@pytest.mark.asyncio
async def test_extension_step_builds_sentinel_handoff(config_flow_module, monkeypatch):
    auth = SimpleNamespace(authorize_url=lambda: "https://login.example/path?x=1")
    monkeypatch.setattr(
        config_flow_module.samsung_auth,
        "bootstrap",
        AsyncMock(return_value=auth),
    )
    flow = _flow(config_flow_module)

    result = await flow.async_step_auth_extension()
    url = urlparse(result["url"])

    assert result["type"] == "external"
    assert result["step_id"] == "auth_extension"
    assert result["url"].startswith(config_flow_module.SENTINEL_BASE + "#")
    # The handoff rides in the fragment so state_HA is never sent over the network.
    assert url.query == ""
    assert parse_qs(url.fragment) == {
        "state_HA": ["encoded-routing-state"],
        "signin_url": ["https://login.example/path?x=1"],
    }
    assert flow._auth is auth


@pytest.mark.asyncio
async def test_extension_callback_resumes_finish_step(config_flow_module):
    flow = _flow(config_flow_module)
    callback = {"code": "encrypted-code", "state": "routing-state"}

    result = await flow.async_step_auth_extension(callback)

    assert result == {"type": "external_done", "next_step_id": "finish"}
    assert flow._external_data == callback


@pytest.mark.asyncio
async def test_finish_rejects_missing_callback_code(config_flow_module):
    flow = _flow(config_flow_module)
    flow._external_data = {"error": "access_denied"}
    flow._auth = SimpleNamespace()

    result = await flow.async_step_finish()

    assert result["type"] == "abort"
    assert result["reason"] == "oauth_error"


@pytest.mark.asyncio
async def test_finish_rejects_missing_auth_session(config_flow_module):
    flow = _flow(config_flow_module)
    flow._external_data = {"code": "encrypted-code"}

    result = await flow.async_step_finish()

    assert result["type"] == "abort"
    assert result["reason"] == "oauth_error"


@pytest.mark.asyncio
async def test_finish_maps_token_exchange_failure_to_abort(
    config_flow_module, monkeypatch
):
    flow = _flow(config_flow_module)
    flow._external_data = {"code": "encrypted-code"}
    flow._auth = SimpleNamespace()
    monkeypatch.setattr(
        config_flow_module.samsung_auth,
        "resolve_callback_code",
        AsyncMock(
            side_effect=config_flow_module.samsung_auth.SamsungAuthError(
                "invalid_grant"
            )
        ),
    )

    result = await flow.async_step_finish()

    assert result["type"] == "abort"
    assert result["reason"] == "token_exchange_failed"


@pytest.mark.asyncio
async def test_finish_maps_unexpected_exchange_failure_to_abort(
    config_flow_module, monkeypatch
):
    flow = _flow(config_flow_module)
    flow._external_data = {"code": "encrypted-code"}
    flow._auth = SimpleNamespace()
    monkeypatch.setattr(
        config_flow_module.samsung_auth,
        "resolve_callback_code",
        AsyncMock(side_effect=ValueError("malformed callback")),
    )

    result = await flow.async_step_finish()

    assert result["type"] == "abort"
    assert result["reason"] == "token_exchange_failed"


@pytest.mark.asyncio
async def test_manual_step_returns_form_after_callback_failure(
    config_flow_module, monkeypatch
):
    first_auth = SimpleNamespace(authorize_url=lambda: "https://old.example")
    replacement_auth = SimpleNamespace(authorize_url=lambda: "https://new.example")
    flow = _flow(config_flow_module)
    flow._auth = first_auth
    monkeypatch.setattr(
        config_flow_module.samsung_auth,
        "resolve_callback",
        AsyncMock(
            side_effect=config_flow_module.samsung_auth.SamsungAuthError(
                "invalid_callback"
            )
        ),
    )
    monkeypatch.setattr(
        config_flow_module.samsung_auth,
        "bootstrap",
        AsyncMock(return_value=replacement_auth),
    )

    result = await flow.async_step_auth_manual({"callback_url": "invalid"})

    assert result["type"] == "form"
    assert result["errors"] == {"base": "token_exchange_failed"}
    assert result["description_placeholders"] == {
        "authorize_url": "https://new.example"
    }
    assert flow._auth is replacement_auth


@pytest.mark.asyncio
async def test_manual_step_maps_unexpected_callback_failure(
    config_flow_module, monkeypatch
):
    replacement_auth = SimpleNamespace(authorize_url=lambda: "https://new.example")
    flow = _flow(config_flow_module)
    flow._auth = SimpleNamespace()
    monkeypatch.setattr(
        config_flow_module.samsung_auth,
        "resolve_callback",
        AsyncMock(side_effect=ValueError("malformed callback")),
    )
    monkeypatch.setattr(
        config_flow_module.samsung_auth,
        "bootstrap",
        AsyncMock(return_value=replacement_auth),
    )

    result = await flow.async_step_auth_manual({"callback_url": "invalid"})

    assert result["type"] == "form"
    assert result["errors"] == {"base": "unknown"}


@pytest.mark.asyncio
async def test_finish_creates_entry_for_first_location(config_flow_module, monkeypatch):
    location = SimpleNamespace(location_id="location-1", name="Home")
    client = SimpleNamespace(
        authenticate=lambda token: setattr(client, "token", token),
        get_locations=AsyncMock(return_value=[location]),
    )
    monkeypatch.setattr(config_flow_module, "SmartThings", lambda session: client)
    monkeypatch.setattr(
        config_flow_module.samsung_auth,
        "normalize_token",
        lambda osp, previous: {
            "access_token": "redacted",
            "osp_host": previous["osp_host"],
        },
    )
    flow = _flow(config_flow_module)
    flow._auth = SimpleNamespace(osp_host="regional.example")

    result = await flow._async_finish({"access_token": "raw"})

    assert result == {
        "type": "create_entry",
        "title": "Home",
        "data": {
            "token": {
                "access_token": "redacted",
                "osp_host": "regional.example",
            },
            "location_id": "location-1",
        },
    }
    assert flow.unique_id == "location-1"
    assert client.token == "redacted"


@pytest.mark.asyncio
async def test_finish_aborts_when_no_locations_exist(config_flow_module, monkeypatch):
    client = SimpleNamespace(
        authenticate=lambda token: None,
        get_locations=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(config_flow_module, "SmartThings", lambda session: client)
    flow = _flow(config_flow_module)
    flow._auth = SimpleNamespace(osp_host="regional.example")

    result = await flow._async_finish({"access_token": "redacted"})

    assert result["type"] == "abort"
    assert result["reason"] == "no_locations"


@pytest.mark.asyncio
async def test_finish_aborts_on_smartthings_connection_error(
    config_flow_module, monkeypatch
):
    client = SimpleNamespace(
        authenticate=lambda token: None,
        get_locations=AsyncMock(side_effect=SmartThingsConnectionError("unavailable")),
    )
    monkeypatch.setattr(config_flow_module, "SmartThings", lambda session: client)
    flow = _flow(config_flow_module)
    flow._auth = SimpleNamespace(osp_host="regional.example")

    result = await flow._async_finish({"access_token": "redacted"})

    assert result["type"] == "abort"
    assert result["reason"] == "cannot_connect"


@pytest.mark.asyncio
async def test_finish_checks_for_duplicate_config_entry(config_flow_module, monkeypatch):
    location = SimpleNamespace(location_id="location-1", name="Home")
    client = SimpleNamespace(
        authenticate=lambda token: None,
        get_locations=AsyncMock(return_value=[location]),
    )
    monkeypatch.setattr(config_flow_module, "SmartThings", lambda session: client)
    flow = _flow(config_flow_module)
    flow._auth = SimpleNamespace(osp_host="regional.example")

    result = await flow._async_finish({"access_token": "redacted"})

    assert result["type"] == "create_entry"
    assert flow.unique_id_configured_called is True


@pytest.mark.asyncio
async def test_finish_checks_reauth_account_mismatch(config_flow_module, monkeypatch):
    location = SimpleNamespace(location_id="location-1", name="Home")
    client = SimpleNamespace(
        authenticate=lambda token: None,
        get_locations=AsyncMock(return_value=[location]),
    )
    monkeypatch.setattr(config_flow_module, "SmartThings", lambda session: client)
    flow = _flow(config_flow_module)
    flow.source = config_flow_module.SOURCE_REAUTH
    flow._auth = SimpleNamespace(osp_host="regional.example")

    result = await flow._async_finish({"access_token": "redacted"})

    assert result["type"] == "reauth"
    assert flow.unique_id_mismatch_reason == "reauth_account_mismatch"


@pytest.mark.asyncio
async def test_finish_updates_existing_entry_during_reauth(
    config_flow_module, monkeypatch
):
    location = SimpleNamespace(location_id="location-1", name="Home")
    client = SimpleNamespace(
        authenticate=lambda token: None,
        get_locations=AsyncMock(return_value=[location]),
    )
    monkeypatch.setattr(config_flow_module, "SmartThings", lambda session: client)
    flow = _flow(config_flow_module)
    flow.source = config_flow_module.SOURCE_REAUTH
    flow._auth = SimpleNamespace(osp_host="regional.example")

    result = await flow._async_finish({"access_token": "redacted"})

    assert result["type"] == "reauth"
    assert result["data"]["location_id"] == "location-1"


@pytest.mark.asyncio
async def test_reauth_confirmation_returns_to_method_picker(config_flow_module):
    flow = _flow(config_flow_module)

    confirmation = await flow.async_step_reauth_confirm()
    result = await flow.async_step_reauth_confirm({})

    assert confirmation["type"] == "form"
    assert confirmation["step_id"] == "reauth_confirm"
    assert result == {
        "type": "menu",
        "step_id": "pick_method",
        "menu_options": ["auth_extension", "auth_manual"],
    }


@pytest.mark.asyncio
async def test_reauth_step_shows_confirmation_form(config_flow_module):
    flow = _flow(config_flow_module)

    result = await flow.async_step_reauth({"access_token": "redacted"})

    assert result == {
        "type": "form",
        "step_id": "reauth_confirm",
        "data_schema": None,
        "description_placeholders": None,
        "errors": {},
    }


@pytest.mark.asyncio
async def test_token_refresh_persists_new_token(config_flow_module, monkeypatch):
    auth = config_flow_module.samsung_auth
    update_token = AsyncMock()
    manager = auth.SamsungTokenManager(
        object(),
        {
            "access_token": "expired",
            "refresh_token": "old-refresh",
            "expires_at": 0,
        },
        update_token,
    )
    monkeypatch.setattr(
        auth,
        "refresh_token",
        AsyncMock(
            return_value={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }
        ),
    )

    result = await manager.async_ensure_valid_token()

    assert result == "new-access"
    update_token.assert_awaited_once()
    assert update_token.await_args.args[0]["refresh_token"] == "new-refresh"
