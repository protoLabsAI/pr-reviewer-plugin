"""GitHub App identity — JWT minting, installation-token fetch, the refresh loop."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time

import pytest
from pr_reviewer.app_auth import (
    AppAuthConfig,
    apply_token,
    fetch_installation_token,
    mint_jwt,
    token_refresh_loop,
)


@pytest.fixture(scope="module")
def keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return pem, key.public_key()


def _b64pad(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_jwt_shape_and_signature(keypair):
    pem, public = keypair
    token = mint_jwt("12345", pem, now=1_700_000_000)
    header_b64, payload_b64, sig_b64 = token.split(".")
    assert json.loads(_b64pad(header_b64)) == {"alg": "RS256", "typ": "JWT"}
    payload = json.loads(_b64pad(payload_b64))
    assert payload == {"iat": 1_700_000_000 - 60, "exp": 1_700_000_000 + 540, "iss": "12345"}
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    public.verify(_b64pad(sig_b64), f"{header_b64}.{payload_b64}".encode(), padding.PKCS1v15(), hashes.SHA256())


def make_http(installations, token_status=201, token_body=None):
    calls = []

    async def get(url, headers):
        calls.append(("GET", url, headers))
        return 200, installations

    async def post(url, headers):
        calls.append(("POST", url, headers))
        return token_status, token_body if token_body is not None else {
            "token": "ghs_fresh",
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)),
        }

    return get, post, calls


async def test_fetch_auto_discovers_installation(keypair):
    pem, _ = keypair
    cfg = AppAuthConfig({"app_id": "12345", "app_private_key": pem})
    get, post, calls = make_http([{"id": 777}])
    token, expires_at = await fetch_installation_token(cfg, http_get=get, http_post=post)
    assert token == "ghs_fresh"
    assert expires_at > time.time() + 3000
    assert calls[0][0] == "GET" and calls[1][1].endswith("/app/installations/777/access_tokens")
    assert calls[1][2]["Authorization"].startswith("Bearer ey")


async def test_fetch_uses_pinned_installation_and_raises_on_failure(keypair):
    pem, _ = keypair
    cfg = AppAuthConfig({"app_id": "12345", "app_private_key": pem, "installation_id": "42"})
    get, post, calls = make_http([], token_status=401, token_body={"message": "bad"})
    with pytest.raises(RuntimeError):
        await fetch_installation_token(cfg, http_get=get, http_post=post)
    assert all(c[0] != "GET" for c in calls)  # pinned id: no discovery call


def test_config_env_fallbacks(monkeypatch):
    monkeypatch.setenv("PROTOREVIEW_APP_ID", "999")
    monkeypatch.setenv("PROTOREVIEW_APP_PRIVATE_KEY", "PEM")
    cfg = AppAuthConfig({})
    assert cfg.configured and cfg.app_id == "999"
    monkeypatch.delenv("PROTOREVIEW_APP_ID")
    monkeypatch.delenv("PROTOREVIEW_APP_PRIVATE_KEY")
    assert not AppAuthConfig({}).configured  # nothing set ⇒ BYO GH_TOKEN mode


async def test_refresh_loop_publishes_token_and_backs_off_on_failure(monkeypatch, keypair):
    pem, _ = keypair
    cfg = AppAuthConfig({"app_id": "1", "app_private_key": pem})
    monkeypatch.delenv("GH_TOKEN", raising=False)
    stop = asyncio.Event()
    outcomes = [RuntimeError("mint failed"), ("ghs_ok", time.time() + 3600)]

    async def fake_fetch(_cfg):
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        if not outcomes:
            stop.set()  # success delivered — end the loop
        return out

    monkeypatch.setattr("pr_reviewer.app_auth.RETRY_BASE_S", 0.01)
    await asyncio.wait_for(token_refresh_loop(cfg, stop, fetch=fake_fetch), timeout=5)
    assert os.environ["GH_TOKEN"] == "ghs_ok" and os.environ["GITHUB_TOKEN"] == "ghs_ok"


def test_apply_token_sets_both_env_names(monkeypatch):
    apply_token("ghs_x")
    assert os.environ["GH_TOKEN"] == "ghs_x" and os.environ["GITHUB_TOKEN"] == "ghs_x"
