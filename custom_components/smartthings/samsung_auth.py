"""Samsung-account (OSP) PKCE auth for the SmartThings integration.

This mirrors the official SmartThings Android app's Samsung-Account-SDK (SASDK) web sign-in
to obtain a SmartThings bearer token on the consumer (free) plane, with no developer OAuth
client and no client_secret. It is an async port of ``poc/samsung_st_auth.py`` (which proved
the full flow end-to-end against the live servers).

Flow:
  0. bootstrap()  -> whoareyou / getMyCountryZone / getEntryPoint (pkiPublicKey, hosts).
  1. build_authorize_url() -> the Samsung ANDROIDSDK signInGate URL with an encrypted svcParam
     (embeds a PKCE code_challenge + a random state).
  2. The browser (via the capture extension) returns
     ``sasdk://saccount.auth.com.samsung.android.oneconnect?code=<enc>&state=<enc>&...``.
  3. resolve_callback() -> AES/ECB-decrypt state (integrity) + code, then POST /auth/oauth2/token
     (grant_type=authorization_code, client_id=6iado3s6jc, code_verifier) -> access/refresh token.
  4. refresh_token() -> grant_type=refresh_token (non-interactive, ~2yr refresh).

Everything is reproducible off-device; only dep beyond HA core is ``cryptography``.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
import time
import urllib.parse

from aiohttp import ClientSession
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_der_public_key

# --------------------------------------------------------------------------- #
# Constants (from the decompiled Android app + live captures; see poc/ + CLAUDE.md)
# --------------------------------------------------------------------------- #

ACCOUNT_HOST = "https://account.samsung.com"
WHOAREYOU_HOST = "https://auth2.samsungosp.com"
DEFAULT_OSP_HOST = "us-auth2.samsungosp.com"

APP_ALIAS_ID = "ANDROIDSDK"            # web-gate appAlias: /accounts/ANDROIDSDK/...

# Single Android app identity (c40.a.f2232a); used for svcParam + the /token exchange.
CLIENT_SVCPARAM = "6iado3s6jc"
CLIENT_TOKEN = "6iado3s6jc"
# Universal SA-SDK client that passes the /authenticate allowlist (iOS-style path only).
CLIENT_AUTHENTICATE = "a2pvoj8e5q"

REDIRECT_URI = "sasdk://saccount.auth.com.samsung.android.oneconnect"
SCOPE = "iot.client mcs.client galaxystore.openapi"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
OSP_CLIENT_VERSION = "3.6.2024042301"

# pkiPublicKey is effectively static (server last-modified 2022); fetched at runtime with a
# hardcoded fallback. Used to RSA-wrap the svcParam AES key.
PKI_PUBLIC_KEY_FALLBACK = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCylnSE8ANPUNPmgYJGnApUrUPQiBmTY44Lw+fQbFO"
    "OslZZnuUasDFJuPU4287/LBQEpTtgPWLmjGftG/b2sj8eTH46mvhDtE8ijgZsMnGPMmhu/AljEvNOqU"
    "6nDZDtgGmL/pAdEBtsJ/VzClv8G9bV1kvczuZtg0gt3JTH+pagEwIDAQAB"
)


# --------------------------------------------------------------------------- #
# PKCE + svcParam crypto (CPU-only; tiny payloads — safe to call inline)
# --------------------------------------------------------------------------- #

def _b64u_np(raw: bytes) -> str:
    """URL-safe base64, no padding (the app's transport encoding for svcParam fields)."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def new_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge). challenge = b64url(sha256(verifier))."""
    verifier = _b64u_np(secrets.token_bytes(64))
    challenge = _b64u_np(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


def build_svc_param_vo(
    client_id: str,
    code_challenge: str,
    state: str,
    physical_address: str,
    device_unique_id: str,
    country_code: str = "US",
) -> dict[str, str]:
    """SvcParamVO with the @SerializedName keys from the decompiled SDK (serviceType 105)."""
    return {
        "clientId": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "responseEncryptionYNFlag": "Y",
        "responseEncryptionType": "1",
        "countryCode": country_code,
        "deviceType": "APP",
        "deviceInfo": "samsung|com.android.chrome",
        "deviceModelID": "SM-G991B",
        "deviceOSVersion": "14",
        "deviceUniqueID": device_unique_id,
        "devicePhysicalAddressText": physical_address,
    }


def encrypt_svc_param(vo: dict[str, str], pki_public_key_b64: str, chk_do_num: str = "1") -> str:
    """Reproduce Encryption.encrypt: RSA-wrapped AES key + AES-CBC(VO), capture-confirmed
    encoding (url-safe/no-pad transport, UPPERCASE hex IV, pretty-printed outer JSON)."""
    pub = load_der_public_key(base64.b64decode(pki_public_key_b64))
    aes_key = secrets.token_bytes(16)
    iv = secrets.token_bytes(16)

    plaintext = json.dumps(vo, separators=(",", ":")).encode("utf-8")
    enc = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).encryptor()
    ct = enc.update(_pkcs7_pad(plaintext)) + enc.finalize()

    rsa_ct = pub.encrypt(base64.b64encode(aes_key), padding.PKCS1v15())

    blob = {
        "svcEncIV": iv.hex().upper(),
        "chkDoNum": chk_do_num,
        "svcEncKY": _b64u_np(rsa_ct),
        "svcEncParam": _b64u_np(ct),
    }
    pretty = json.dumps(blob, indent=2, separators=(",", ": "))
    return urllib.parse.quote(base64.b64encode(pretty.encode()).decode())


def sasdk_decrypt(hex_ciphertext: str, key_str: str) -> str:
    """Reproduce Util.decrypt: AES/ECB/PKCS5 over hex-decoded ciphertext, key = first 16 bytes
    of ``key_str`` (ASCII). Used to decrypt the callback's state/code/*_server_url; the key is
    our original svcParam ``state``. ECB is required for interop (matches the app)."""
    key = key_str.encode("utf-8")[:16]
    ct = bytes.fromhex(hex_ciphertext)
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()  # noqa: S305 - matches the app
    pt = dec.update(ct) + dec.finalize()
    pad = pt[-1] if pt else 0
    if 1 <= pad <= 16:
        pt = pt[:-pad]
    return pt.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Flow session — holds the per-login state the callback needs to be resolved
# --------------------------------------------------------------------------- #

@dataclass
class SamsungAuthSession:
    """Per-login state. Persist (verifier, state) across the browser round-trip."""

    verifier: str
    state: str
    pki_public_key_b64: str = PKI_PUBLIC_KEY_FALLBACK
    chk_do_num: str = "1"
    sign_in_uri: str | None = None
    country_code: str = "US"
    osp_host: str = DEFAULT_OSP_HOST
    physical_address: str = field(default_factory=lambda: secrets.token_hex(16))
    device_unique_id: str = field(default_factory=lambda: secrets.token_hex(8))

    @classmethod
    def create(cls) -> SamsungAuthSession:
        verifier, _challenge = new_pkce()
        return cls(verifier=verifier, state=secrets.token_hex(32))

    def authorize_url(self) -> str:
        """Build the Samsung signInGate URL for this session (fresh challenge from verifier)."""
        challenge = _b64u_np(hashlib.sha256(self.verifier.encode("ascii")).digest())
        vo = build_svc_param_vo(
            CLIENT_SVCPARAM, challenge, self.state,
            self.physical_address, self.device_unique_id, self.country_code,
        )
        svc_param = encrypt_svc_param(vo, self.pki_public_key_b64, self.chk_do_num)
        base = self.sign_in_uri or f"{ACCOUNT_HOST}/accounts/{APP_ALIAS_ID}/signInGate"
        locale = urllib.parse.quote("en_US")
        return f"{base}?locale={locale}&svcParam={svc_param}&mode=C"


# --------------------------------------------------------------------------- #
# Async HTTP: bootstrap + token exchange/refresh
# --------------------------------------------------------------------------- #

def _osp_headers(appid: str) -> dict[str, str]:
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
        "x-osp-appid": appid,
        "x-osp-clientversion": OSP_CLIENT_VERSION,
        "x-app-id": appid,
        "Accept": "*/*",
    }


async def bootstrap(session: ClientSession) -> SamsungAuthSession:
    """Run the 3 pre-flight GETs and return a ready SamsungAuthSession.

    whoareyou (regional hosts) -> getMyCountryZone (country) -> getEntryPoint (pkiPublicKey).
    Falls back to sane defaults on any failure so a login can still be attempted.
    """
    auth = SamsungAuthSession.create()
    hdr = {"x-osp-appid": CLIENT_SVCPARAM, "User-Agent": USER_AGENT, "Accept": "*/*"}
    idm_host: str | None = None

    try:
        async with session.get(f"{WHOAREYOU_HOST}/v2/license/open/whoareyou", headers=hdr) as r:
            who = await r.json(content_type=None)
        auth.osp_host = who.get("api_server_url", auth.osp_host)
        idm_host = who.get("idm_server_url")
    except Exception:  # noqa: BLE001 - best-effort bootstrap
        pass

    try:
        url = f"https://{auth.osp_host}/v2/license/rule/getMyCountryZone"
        async with session.get(url, headers=hdr) as r:
            xml = await r.text()
        if m := re.search(r"<countryCode>([^<]+)</countryCode>", xml):
            auth.country_code = m.group(1)
    except Exception:  # noqa: BLE001
        pass

    try:
        host = f"https://{idm_host}" if idm_host else ACCOUNT_HOST
        async with session.get(
            f"{host}/accounts/{APP_ALIAS_ID}/getEntryPoint", headers=hdr
        ) as r:
            ep = await r.json(content_type=None)
        auth.pki_public_key_b64 = ep.get("pkiPublicKey", auth.pki_public_key_b64)
        auth.chk_do_num = ep.get("chkDoNum", "1")
        auth.sign_in_uri = ep.get("signInURI")
    except Exception:  # noqa: BLE001 - fall back to the pinned key
        pass

    return auth


class SamsungAuthError(Exception):
    """Raised when the Samsung OSP token exchange/refresh fails."""

    def __init__(self, error: str, description: str = "") -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description


async def _post_token(session: ClientSession, osp_base: str, fields: dict[str, str]) -> dict:
    async with session.post(
        f"{osp_base}/auth/oauth2/token",
        data=fields,
        headers=_osp_headers(CLIENT_TOKEN),
    ) as resp:
        data = await resp.json(content_type=None)
    if "access_token" not in data:
        raise SamsungAuthError(
            data.get("error_code") or data.get("error", "unknown"),
            data.get("error_description", ""),
        )
    return data


async def resolve_callback(
    session: ClientSession, auth: SamsungAuthSession, callback_url: str
) -> dict:
    """Decrypt the sasdk:// callback and exchange the code for a token.

    Returns the raw OSP token dict: access_token, refresh_token, expires_in,
    refresh_token_expires_in, token_type, userId. Raises SamsungAuthError on failure.
    """
    q = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
    if err := q.get("error", [None])[0]:
        raise SamsungAuthError(err, q.get("error_description", [""])[0])

    raw_code = q.get("code", [""])[0]
    cb_state = q.get("state", [""])[0]
    if not raw_code or not cb_state:
        raise SamsungAuthError(
            "invalid_callback", "The callback is missing its code or state"
        )

    # Decrypt the returned state with our original state; it must round-trip (integrity/CSRF).
    sess_key = sasdk_decrypt(cb_state, auth.state)
    if sess_key != auth.state:
        raise SamsungAuthError(
            "invalid_state", "The callback state does not match the login session"
        )
    code = sasdk_decrypt(raw_code, sess_key)

    osp_base = f"https://{auth.osp_host}"
    if auth_url := q.get("auth_server_url", [""])[0]:
        host = sasdk_decrypt(auth_url, sess_key)
        if "." in host and len(host) < 60:
            osp_base = f"https://{host}"

    return await _post_token(
        session,
        osp_base,
        {
            "code": code,
            "client_id": CLIENT_TOKEN,
            "code_verifier": auth.verifier,
            "grant_type": "authorization_code",
        },
    )


async def resolve_callback_code(
    session: ClientSession, auth: SamsungAuthSession, code_ciphertext: str
) -> dict:
    """Exchange an encrypted callback ``code`` for a token (HA-routed / extension path).

    Unlike ``resolve_callback``, this receives only the encrypted ``code`` — the value the
    extension forwards via ``my.home-assistant.io`` (which carries just ``code``+``state``,
    the latter being HA's routing JWT). The callback's own encrypted ``state`` /
    ``auth_server_url`` aren't available here, and aren't needed: we already hold the original
    ``state`` (the AES key) and ``osp_host`` from when we started the flow.
    """
    code = sasdk_decrypt(code_ciphertext, auth.state)
    return await _post_token(
        session,
        f"https://{auth.osp_host}",
        {
            "code": code,
            "client_id": CLIENT_TOKEN,
            "code_verifier": auth.verifier,
            "grant_type": "authorization_code",
        },
    )


async def refresh_token(
    session: ClientSession, refresh_token_value: str, osp_host: str = DEFAULT_OSP_HOST
) -> dict:
    """Non-interactive refresh -> fresh access_token (grant_type=refresh_token)."""
    return await _post_token(
        session,
        f"https://{osp_host}",
        {
            "refresh_token": refresh_token_value,
            "client_id": CLIENT_TOKEN,
            "grant_type": "refresh_token",
        },
    )


# --------------------------------------------------------------------------- #
# Stored token shape + refresh manager (for the config entry / setup)
# --------------------------------------------------------------------------- #

def normalize_token(osp: dict, previous: dict | None = None) -> dict:
    """Turn an OSP token response into the shape we persist in the config entry.

    Computes an absolute ``expires_at``. A refresh response may omit the refresh_token /
    userId; carry those over from ``previous``. ``osp_host`` is remembered for refreshes.
    """
    previous = previous or {}
    expires_in = int(osp.get("expires_in") or osp.get("access_token_expires_in") or 0)
    return {
        "access_token": osp["access_token"],
        "refresh_token": osp.get("refresh_token") or previous.get("refresh_token"),
        "expires_at": time.time() + expires_in if expires_in else 0.0,
        "refresh_token_expires_in": osp.get("refresh_token_expires_in")
        or previous.get("refresh_token_expires_in"),
        "user_id": osp.get("userId") or previous.get("user_id"),
        "osp_host": previous.get("osp_host", DEFAULT_OSP_HOST),
    }


class SamsungTokenManager:
    """Holds the stored token and refreshes it non-interactively when near expiry.

    ``update_token`` (optional) is awaited with the new stored-token dict whenever a refresh
    happens, so the caller can persist it back to the config entry.
    """

    def __init__(
        self,
        session: ClientSession,
        token: dict,
        update_token: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._token = token
        self._update_token = update_token
        self._refresh_lock = asyncio.Lock()

    @property
    def token(self) -> dict:
        return self._token

    @property
    def access_token(self) -> str:
        return self._token["access_token"]

    def valid(self, leeway: int = 60) -> bool:
        return float(self._token.get("expires_at", 0)) > time.time() + leeway

    async def async_ensure_valid_token(self) -> str:
        """Return a valid access token, refreshing first if needed."""
        if self.valid():
            return self.access_token
        async with self._refresh_lock:
            # Another request may have refreshed while this one waited for the lock.
            if self.valid():
                return self.access_token
            osp = await refresh_token(
                self._session,
                self._token["refresh_token"],
                self._token.get("osp_host", DEFAULT_OSP_HOST),
            )
            self._token = normalize_token(osp, self._token)
            if self._update_token is not None:
                await self._update_token(self._token)
            return self.access_token
