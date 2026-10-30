"""Config flow to configure SmartThings via the Samsung-account (OSP) login.

No developer OAuth client is required: we mimic the SmartThings Android app's Samsung-Account
web sign-in (PKCE public client). The user opens the Samsung login URL, signs in, and the
browser is redirected to a ``sasdk://`` callback which the capture extension forwards (or the
user pastes). We decrypt it and exchange the code for a SmartThings bearer token.
"""

from collections.abc import Mapping
import logging
from typing import Any
from urllib.parse import quote

from pysmartthings import SmartThings, SmartThingsConnectionError
import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_TOKEN
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import samsung_auth
from .const import CONF_LOCATION_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_CALLBACK_URL = "callback_url"

# Where the capture extension routes the (encrypted) callback back to HA. HA's standard
# OAuth callback view validates the routing JWT (state_HA) and resumes this flow.
HA_OAUTH_REDIRECT = "https://my.home-assistant.io/redirect/oauth"

# Sentinel the capture extension intercepts to hand off state_HA + the Samsung login URL.
# Both the generic OAuth Assist for HA extension and the bundled extensions/chrome-extension/ match
# it. The parameters ride in the URL FRAGMENT, which browsers never transmit — so even
# when the extension is absent and this URL is navigated for real, state_HA stays local
# (IANA does not resolve example.com subdomains; at most a DNS lookup escapes). The https
# form is used, rather than OAuth Assist's equally supported custom-scheme form
# ("ha-oauth-assist-initiate://start/smartthings"), because users paste this URL between
# browsers and omniboxes turn a pasted custom scheme into a web search — transmitting the
# whole URL, fragment included, to the search engine (and the bundled extension only
# matches the https form). The path segment is the OAuth Assist profile id and must match
# exactly — on a mismatch either extension ignores the sentinel.
SENTINEL_BASE = "https://ha-oauth-assist.example.com/initiate/smartthings"


class SmartThingsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle configuration of SmartThings via Samsung-account login."""

    VERSION = 3
    MINOR_VERSION = 4

    def __init__(self) -> None:
        """Initialise the flow."""
        self._auth: samsung_auth.SamsungAuthSession | None = None
        self._external_data: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Check the cloud integration is set up, then pick a sign-in method."""
        if "cloud" not in self.hass.config.components:
            return self.async_abort(
                reason="cloud_not_enabled",
                description_placeholders={"default_config": "default_config"},
            )
        return await self.async_step_pick_method()

    async def async_step_pick_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the automatic (extension) sign-in or the manual copy/paste fallback."""
        return self.async_show_menu(
            step_id="pick_method",
            menu_options=["auth_extension", "auth_manual"],
        )

    async def async_step_auth_extension(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Automatic path: drive the browser (via the capture extension) through login.

        First entry emits the ``ha-oauth-assist.example.com`` sentinel carrying ``state_HA``
        (HA's routing JWT) and the Samsung ``signin_url`` in the URL fragment; the extension
        opens the login and, on the ``sasdk://`` callback, routes back through
        ``my.home-assistant.io`` to HA's OAuth callback view — which resumes this same step
        with ``{code, state}``.
        """
        if user_input is not None:
            # Resumed from HA's OAuth callback view via the extension + my.home-assistant.io.
            self._external_data = user_input
            return self.async_external_step_done(next_step_id="finish")

        self._auth = await samsung_auth.bootstrap(async_get_clientsession(self.hass))
        state_ha = config_entry_oauth2_flow._encode_jwt(  # noqa: SLF001 - reuse HA's callback JWT
            self.hass,
            {"flow_id": self.flow_id, "redirect_uri": HA_OAUTH_REDIRECT},
        )
        # Fragment, not query string: a fragment is never transmitted, so state_HA
        # stays local even if no extension intercepts the sentinel.
        sentinel = (
            f"{SENTINEL_BASE}#state_HA={quote(state_ha, safe='')}"
            f"&signin_url={quote(self._auth.authorize_url(), safe='')}"
        )
        return self.async_external_step(step_id="auth_extension", url=sentinel)

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Exchange the code delivered by the automatic path, then create the entry."""
        assert self._external_data is not None
        if self._external_data.get("error") or not (
            code := self._external_data.get("code")
        ):
            return self.async_abort(reason="oauth_error")
        if self._auth is None:
            return self.async_abort(reason="oauth_error")

        session = async_get_clientsession(self.hass)
        # This runs inside HA's OAuth callback request (external_step_done chains straight
        # into this step), so ANY uncaught exception surfaces to the browser as a 500.
        # Catch broadly and abort with the cause logged.
        try:
            osp = await samsung_auth.resolve_callback_code(session, self._auth, code)
        except samsung_auth.SamsungAuthError as err:
            _LOGGER.warning("Samsung token exchange failed: %s", err)
            return self.async_abort(reason="token_exchange_failed")
        except Exception:  # noqa: BLE001 - never let this 500 the callback view
            _LOGGER.exception("Unexpected error resolving the Samsung callback (auto path)")
            return self.async_abort(reason="token_exchange_failed")
        try:
            return await self._async_finish(osp)
        except Exception:  # noqa: BLE001 - never let this 500 the callback view
            _LOGGER.exception("Unexpected error finishing the SmartThings entry (auto path)")
            return self.async_abort(reason="cannot_connect")

    async def async_step_auth_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual fallback: show the Samsung login URL and accept a pasted sasdk:// callback."""
        session = async_get_clientsession(self.hass)
        errors: dict[str, str] = {}

        if user_input is not None and self._auth is not None:
            try:
                osp = await samsung_auth.resolve_callback(
                    session, self._auth, user_input[CONF_CALLBACK_URL]
                )
            except samsung_auth.SamsungAuthError as err:
                _LOGGER.warning("Samsung token exchange failed: %s", err)
                errors["base"] = "token_exchange_failed"
            except Exception:  # noqa: BLE001 - surface any parse/crypto error as a form error
                _LOGGER.exception("Unexpected error resolving the Samsung callback")
                errors["base"] = "unknown"
            else:
                return await self._async_finish(osp)

        # First display, or retry after an error: bootstrap a fresh login (fresh 300s code).
        self._auth = await samsung_auth.bootstrap(session)
        return self.async_show_form(
            step_id="auth_manual",
            data_schema=vol.Schema({vol.Required(CONF_CALLBACK_URL): str}),
            description_placeholders={"authorize_url": self._auth.authorize_url()},
            errors=errors,
        )

    async def _async_finish(self, osp: dict) -> ConfigFlowResult:
        """Validate the token against SmartThings and create/update the entry."""
        assert self._auth is not None
        token = samsung_auth.normalize_token(osp, {"osp_host": self._auth.osp_host})

        client = SmartThings(session=async_get_clientsession(self.hass))
        client.authenticate(token["access_token"])
        try:
            locations = await client.get_locations()
        except SmartThingsConnectionError:
            return self.async_abort(reason="cannot_connect")
        if not locations:
            return self.async_abort(reason="no_locations")

        location = locations[0]
        # Use the location id as the unique id (stable across app/installed-app changes).
        await self.async_set_unique_id(location.location_id)
        data = {CONF_TOKEN: token, CONF_LOCATION_ID: location.location_id}

        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="reauth_account_mismatch")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=data
            )

        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=location.name, data=data)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth by restarting the Samsung login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauth dialog, then run the login flow."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_pick_method()
