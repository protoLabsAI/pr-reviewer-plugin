"""GitHub App identity — installation-token minting for a single-identity reviewer.

The reviewer's `gh`/git calls authenticate via GH_TOKEN. A GitHub App is the clean
identity (reviews post as `<app>[bot]`, per-repo install, revocable) — but App
installation tokens expire HOURLY, and `gh` has no App-auth mode. This module keeps
a fresh token in the PROCESS ENV: every `gh`/git subprocess copies `os.environ` at
spawn, so refreshing `GH_TOKEN`/`GITHUB_TOKEN` in-process re-auths all future calls
(the github-plugin's tools included) with no credential files on disk.

Flow (standard App auth):
  RS256 JWT (iss = App ID, signed with the App private key, ≤10 min)
    → GET  /app/installations                 (installation id, auto-discovered)
    → POST /app/installations/{id}/access_tokens  → 1h token, refreshed at T-10min.

Config (all with env fallbacks for headless config-as-code):
  app_id            / PROTOREVIEW_APP_ID
  app_private_key   / PROTOREVIEW_APP_PRIVATE_KEY   (PEM; the secrets overlay or env)
  installation_id   / PROTOREVIEW_APP_INSTALLATION_ID (optional — auto-discovered)

Failure posture: minting failures log + retry with backoff; the surface never
crashes the host. Until the first successful mint, `gh` calls fail auth loudly —
better than a silent wrong identity.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time

log = logging.getLogger("protoagent.plugins.pr_reviewer")

GITHUB_API = "https://api.github.com"
REFRESH_MARGIN_S = 10 * 60  # refresh when less than this remains on the token
RETRY_BASE_S = 30  # backoff base after a failed mint (doubles, capped)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint_jwt(app_id: str, private_key_pem: str, *, now: int | None = None) -> str:
    """A short-lived RS256 App JWT (the only thing the private key ever signs)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time()) if now is None else now
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    # iat backdated 60s for clock drift; exp 9 min (GitHub caps at 10).
    payload = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    signing_input = f"{header}.{payload}".encode()
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


class AppAuthConfig:
    """Resolved App credentials; `configured` is False when any required part is absent."""

    def __init__(self, cfg: dict):
        cfg = cfg or {}
        self.app_id = str(cfg.get("app_id") or os.environ.get("PROTOREVIEW_APP_ID") or "").strip()
        self.private_key = str(
            cfg.get("app_private_key") or os.environ.get("PROTOREVIEW_APP_PRIVATE_KEY") or ""
        ).strip()
        self.installation_id = str(
            cfg.get("installation_id") or os.environ.get("PROTOREVIEW_APP_INSTALLATION_ID") or ""
        ).strip()

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.private_key)


async def fetch_installation_token(config: AppAuthConfig, *, http_post=None, http_get=None) -> tuple[str, float]:
    """(token, expires_at_epoch). Raises on failure — the caller owns retry policy.
    `http_get`/`http_post` are seams for tests; None = httpx."""
    jwt = mint_jwt(config.app_id, config.private_key)
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if http_get is None or http_post is None:
        import httpx

        async def _get(url, hdrs):
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url, headers=hdrs)
                return r.status_code, r.json()

        async def _post(url, hdrs):
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, headers=hdrs)
                return r.status_code, r.json()

        http_get, http_post = http_get or _get, http_post or _post

    installation_id = config.installation_id
    if not installation_id:
        status, data = await http_get(f"{GITHUB_API}/app/installations", headers)
        if status != 200 or not isinstance(data, list) or not data:
            raise RuntimeError(f"could not list App installations (HTTP {status})")
        if len(data) > 1:
            log.warning("[pr-reviewer] App has %d installations; using the first — pin installation_id", len(data))
        installation_id = str(data[0]["id"])

    status, data = await http_post(f"{GITHUB_API}/app/installations/{installation_id}/access_tokens", headers)
    if status != 201 or "token" not in data:
        raise RuntimeError(f"installation token mint failed (HTTP {status}): {str(data)[:200]}")
    # expires_at: ISO like 2026-07-06T18:00:00Z — parse cheaply, fall back to +55min.
    expires_at = time.time() + 55 * 60
    try:
        from datetime import datetime, timezone

        expires_at = (
            datetime.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        )
    except (KeyError, ValueError):
        pass
    return str(data["token"]), expires_at


def apply_token(token: str) -> None:
    """Publish the fresh token where every consumer looks: subprocesses copy
    os.environ at spawn, so gh (GH_TOKEN), git-over-gh, and the checkout cache
    (GITHUB_TOKEN fallback chain) all pick it up on their next call."""
    os.environ["GH_TOKEN"] = token
    os.environ["GITHUB_TOKEN"] = token


async def token_refresh_loop(config: AppAuthConfig, stop_event: asyncio.Event, *, fetch=None) -> None:
    """The background surface body: mint, publish, sleep until T-margin; on
    failure, backoff-retry. Never raises (a failing App auth must not kill boot)."""
    fetch = fetch or fetch_installation_token
    failures = 0
    while not stop_event.is_set():
        try:
            token, expires_at = await fetch(config)
            apply_token(token)
            failures = 0
            delay = max(expires_at - time.time() - REFRESH_MARGIN_S, 60)
            log.info("[pr-reviewer] App installation token refreshed (next in %ds)", int(delay))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            delay = min(RETRY_BASE_S * (2 ** min(failures, 5)), 15 * 60)
            log.warning(
                "[pr-reviewer] App token mint failed (attempt %d): %s — retry in %ds", failures, exc, int(delay)
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
