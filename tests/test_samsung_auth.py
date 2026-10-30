"""Tests for Samsung authentication and token management."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def _encrypt_callback(value: str, key: str) -> str:
    """Produce the AES/ECB callback representation expected by Samsung."""
    raw = value.encode()
    padding = 16 - len(raw) % 16
    encryptor = Cipher(algorithms.AES(key.encode()[:16]), modes.ECB()).encryptor()
    encrypted = encryptor.update(raw + bytes([padding]) * padding)
    return (encrypted + encryptor.finalize()).hex()


class FakeResponse:
    """Minimal aiohttp response context manager."""

    def __init__(self, *, json_data=None, text_data="", status=200):
        self._json_data = json_data
        self._text_data = text_data
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self, content_type=None):
        return self._json_data

    async def text(self):
        return self._text_data


def test_new_pkce_is_url_safe_and_matches_challenge(samsung_auth):
    verifier, challenge = samsung_auth.new_pkce()

    assert len(verifier) == 86
    assert "=" not in verifier
    assert "=" not in challenge
    assert challenge == samsung_auth._b64u_np(
        samsung_auth.hashlib.sha256(verifier.encode("ascii")).digest()
    )


def test_build_svc_param_contains_required_mobile_client_fields(samsung_auth):
    result = samsung_auth.build_svc_param_vo(
        "client", "challenge", "state", "physical", "device", "GB"
    )

    assert result["clientId"] == "client"
    assert result["redirect_uri"] == samsung_auth.REDIRECT_URI
    assert result["code_challenge"] == "challenge"
    assert result["code_challenge_method"] == "S256"
    assert result["state"] == "state"
    assert result["countryCode"] == "GB"
    assert result["responseEncryptionYNFlag"] == "Y"


def test_encrypt_svc_param_has_decryptable_payload(samsung_auth):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo
        )
    ).decode()
    vo = {"clientId": "client", "state": "state"}

    encoded = samsung_auth.encrypt_svc_param(vo, public_key)
    outer = json.loads(base64.b64decode(unquote(encoded)))

    assert set(outer) == {"svcEncIV", "chkDoNum", "svcEncKY", "svcEncParam"}
    assert outer["chkDoNum"] == "1"
    assert len(outer["svcEncIV"]) == 32


def test_sasdk_decrypt_round_trip(samsung_auth):
    key = "0123456789abcdef-extra"
    ciphertext = _encrypt_callback("secret value", key)

    assert samsung_auth.sasdk_decrypt(ciphertext, key) == "secret value"


@pytest.mark.asyncio
async def test_resolve_callback_validates_state_and_exchanges_code(samsung_auth):
    auth = samsung_auth.SamsungAuthSession(
        verifier="verifier",
        state="0123456789abcdef0123456789abcdef",
        osp_host="regional.example",
    )
    state = _encrypt_callback(auth.state, auth.state)
    code = _encrypt_callback("authorization-code", auth.state)
    auth_host = _encrypt_callback("token.example", auth.state)
    callback = (
        f"{samsung_auth.REDIRECT_URI}?code={code}&state={state}"
        f"&auth_server_url={auth_host}"
    )
    session = SimpleNamespace()
    seen = {}

    async def fake_post_token(received_session, base, fields):
        seen.update(session=received_session, base=base, fields=fields)
        return {"access_token": "redacted"}

    samsung_auth._post_token = fake_post_token
    result = await samsung_auth.resolve_callback(session, auth, callback)

    assert result == {"access_token": "redacted"}
    assert seen["session"] is session
    assert seen["base"] == "https://token.example"
    assert seen["fields"] == {
        "code": "authorization-code",
        "client_id": samsung_auth.CLIENT_TOKEN,
        "code_verifier": "verifier",
        "grant_type": "authorization_code",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "",
        "code=abcd",
        "state=abcd",
    ],
)
async def test_resolve_callback_rejects_missing_fields(samsung_auth, query):
    auth = samsung_auth.SamsungAuthSession(
        verifier="verifier", state="0123456789abcdef0123456789abcdef"
    )

    with pytest.raises(samsung_auth.SamsungAuthError) as error:
        await samsung_auth.resolve_callback(object(), auth, f"sasdk://callback?{query}")

    assert error.value.error == "invalid_callback"


@pytest.mark.asyncio
async def test_resolve_callback_rejects_mismatched_state(samsung_auth):
    auth = samsung_auth.SamsungAuthSession(
        verifier="verifier", state="0123456789abcdef0123456789abcdef"
    )
    state = _encrypt_callback("wrong-state-value", auth.state)
    code = _encrypt_callback("authorization-code", auth.state)

    with pytest.raises(samsung_auth.SamsungAuthError) as error:
        await samsung_auth.resolve_callback(
            object(), auth, f"sasdk://callback?code={code}&state={state}"
        )

    assert error.value.error == "invalid_state"


@pytest.mark.asyncio
async def test_resolve_callback_rejects_state_encrypted_with_other_key(samsung_auth):
    auth = samsung_auth.SamsungAuthSession(
        verifier="verifier", state="0123456789abcdef0123456789abcdef"
    )
    # Decrypting with the wrong key yields non-ASCII garbage, not a clean mismatch.
    state = _encrypt_callback(auth.state, "fedcba9876543210")
    code = _encrypt_callback("authorization-code", auth.state)

    with pytest.raises(samsung_auth.SamsungAuthError) as error:
        await samsung_auth.resolve_callback(
            object(), auth, f"sasdk://callback?code={code}&state={state}"
        )

    assert error.value.error == "invalid_state"


@pytest.mark.asyncio
async def test_resolve_callback_surfaces_samsung_error(samsung_auth):
    auth = samsung_auth.SamsungAuthSession.create()

    with pytest.raises(samsung_auth.SamsungAuthError) as error:
        await samsung_auth.resolve_callback(
            object(),
            auth,
            "sasdk://callback?error=access_denied&error_description=Cancelled",
        )

    assert error.value.error == "access_denied"
    assert error.value.description == "Cancelled"


@pytest.mark.asyncio
async def test_post_token_rejects_response_without_access_token(samsung_auth):
    session = SimpleNamespace(
        post=lambda *args, **kwargs: FakeResponse(
            json_data={"error": "invalid_grant", "error_description": "Expired"}
        )
    )

    with pytest.raises(samsung_auth.SamsungAuthError) as error:
        await samsung_auth._post_token(session, "https://token.example", {})

    assert error.value.error == "invalid_grant"
    assert error.value.description == "Expired"


def test_normalize_token_preserves_refresh_metadata(samsung_auth, monkeypatch):
    monkeypatch.setattr(samsung_auth.time, "time", lambda: 1000)
    previous = {
        "refresh_token": "old-refresh",
        "refresh_token_expires_in": 999,
        "user_id": "user",
        "osp_host": "regional.example",
    }

    result = samsung_auth.normalize_token(
        {"access_token": "new-access", "expires_in": "3600"}, previous
    )

    assert result == {
        "access_token": "new-access",
        "refresh_token": "old-refresh",
        "expires_at": 4600,
        "refresh_token_expires_in": 999,
        "user_id": "user",
        "osp_host": "regional.example",
    }


@pytest.mark.asyncio
async def test_token_manager_reuses_valid_token(samsung_auth, monkeypatch):
    manager = samsung_auth.SamsungTokenManager(
        object(), {"access_token": "valid", "expires_at": 9999}
    )
    monkeypatch.setattr(samsung_auth.time, "time", lambda: 1000)

    async def unexpected_refresh(*args):
        pytest.fail("valid token should not be refreshed")

    monkeypatch.setattr(samsung_auth, "refresh_token", unexpected_refresh)

    assert await manager.async_ensure_valid_token() == "valid"


@pytest.mark.asyncio
async def test_token_manager_refreshes_once_for_concurrent_requests(
    samsung_auth, monkeypatch
):
    calls = 0
    persisted = []

    async def fake_refresh(*args):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }

    async def persist(token):
        persisted.append(dict(token))

    monkeypatch.setattr(samsung_auth, "refresh_token", fake_refresh)
    manager = samsung_auth.SamsungTokenManager(
        object(),
        {
            "access_token": "expired",
            "refresh_token": "old-refresh",
            "expires_at": 0,
        },
        persist,
    )

    results = await asyncio.gather(
        manager.async_ensure_valid_token(),
        manager.async_ensure_valid_token(),
        manager.async_ensure_valid_token(),
    )

    assert results == ["new-access"] * 3
    assert calls == 1
    assert len(persisted) == 1
    assert persisted[0]["refresh_token"] == "new-refresh"
