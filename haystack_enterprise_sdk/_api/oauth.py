"""OAuth 2.0 login for the CLI: authorization code with PKCE over a loopback redirect (RFC 8252), or the device flow.

The platform advertises its CLI OAuth client at ``/api/v1/sso/cli-config``. Tokens are stored in
``~/.haystack-enterprise/credentials.json`` (mode 600) and refreshed transparently by :class:`OAuthAuth`.
An API key, when configured, always wins over these credentials.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, AsyncGenerator, Callable, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from haystack_enterprise_sdk._api.config import API_VERSION_PATH, CREDENTIALS_PATH

# Refresh this many seconds before the access token expires, so a request never goes out with a dying token.
EXPIRY_SKEW_SECONDS = 60

LOGIN_SUCCESS_PAGE = (
    b"<!doctype html><title>Logged in</title>"
    b"<p>You're logged in to Haystack Enterprise Platform. You can close this tab and return to your terminal.</p>"
)
LOGIN_FAILURE_PAGE = b"<!doctype html><title>Login failed</title><p>Login failed. Check your terminal.</p>"

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class OAuthError(Exception):
    """The OAuth login or token refresh failed."""


@dataclass
class CliOAuthConfig:
    """The platform's CLI OAuth client, as advertised at ``/api/v1/sso/cli-config``."""

    issuer: str
    client_id: str
    scopes: list[str]


@dataclass
class OAuthCredentials:
    """Tokens from a CLI login, plus what's needed to refresh them without asking the platform again."""

    api_url: str
    client_id: str
    token_endpoint: str
    access_token: str
    refresh_token: str
    expires_at: float
    revocation_endpoint: Optional[str] = None
    organization_id: Optional[str] = None

    def expires_soon(self) -> bool:
        """:return: True if the access token expires within the refresh skew."""
        return time.time() >= self.expires_at - EXPIRY_SKEW_SECONDS

    def save(self) -> None:
        """Write the credentials atomically, readable by the current user only."""
        CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = CREDENTIALS_PATH.with_suffix(".tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(asdict(self), file)
        os.replace(tmp_path, CREDENTIALS_PATH)

    @classmethod
    def load(cls, api_url: Optional[str] = None) -> Optional[OAuthCredentials]:
        """Read stored credentials.

        :param api_url: Only return credentials issued for this API URL.
        :return: The credentials, or None if there are none (for this API URL).
        """
        try:
            credentials = cls(**json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None
        if api_url is not None and credentials.api_url != api_url:
            return None
        return credentials

    def with_token_response(self, token: Dict[str, Any]) -> OAuthCredentials:
        """:return: A copy updated from a token endpoint response. Keeps the old refresh token if none is rotated in."""
        return OAuthCredentials(
            **{
                **asdict(self),
                "access_token": token["access_token"],
                "refresh_token": token.get("refresh_token") or self.refresh_token,
                "expires_at": time.time() + float(token.get("expires_in", 300)),
            }
        )


def delete_credentials() -> bool:
    """:return: True if stored credentials were removed."""
    try:
        CREDENTIALS_PATH.unlink()
    except FileNotFoundError:
        return False
    return True


def fetch_cli_config(api_url: str) -> Optional[CliOAuthConfig]:
    """Ask the platform whether it offers OAuth login for the CLI.

    :param api_url: The normalized base API URL.
    :return: The CLI client config, or None if this platform doesn't offer it (yet).
    """
    try:
        response = httpx.get(f"{api_url}/{API_VERSION_PATH}/sso/cli-config", timeout=10)
    except httpx.HTTPError:
        return None
    if response.status_code != httpx.codes.OK:
        return None
    body = response.json()
    if not body.get("issuer") or not body.get("client_id"):
        return None
    return CliOAuthConfig(issuer=body["issuer"], client_id=body["client_id"], scopes=list(body.get("scopes", [])))


def discover(issuer: str) -> Dict[str, Any]:
    """:return: The issuer's OpenID Connect discovery document."""
    response = httpx.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration", timeout=10)
    response.raise_for_status()
    metadata: Dict[str, Any] = response.json()
    return metadata


def pkce_pair() -> tuple[str, str]:
    """:return: A PKCE code verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _request_token(token_endpoint: str, data: Dict[str, str]) -> httpx.Response:
    return httpx.post(token_endpoint, data=data, timeout=30)


def _token_or_raise(response: httpx.Response) -> Dict[str, Any]:
    body = response.json() if response.content else {}
    if response.status_code != httpx.codes.OK or "access_token" not in body:
        raise OAuthError(body.get("error_description") or body.get("error") or f"HTTP {response.status_code}")
    return body


def _credentials(
    api_url: str, config: CliOAuthConfig, metadata: Dict[str, Any], token: Dict[str, Any]
) -> OAuthCredentials:
    if not token.get("refresh_token"):
        raise OAuthError("The platform did not issue a refresh token. Is the offline_access scope enabled?")
    return OAuthCredentials(
        api_url=api_url,
        client_id=config.client_id,
        token_endpoint=metadata["token_endpoint"],
        revocation_endpoint=metadata.get("revocation_endpoint"),
        access_token="",
        refresh_token="",
        expires_at=0,
    ).with_token_response(token)


def authorization_code_login(
    api_url: str,
    config: CliOAuthConfig,
    open_url: Callable[[str], None],
    timeout: float,
) -> OAuthCredentials:
    """Log in through the browser: authorization code + PKCE, redirected to a one-shot loopback server.

    :param api_url: The normalized base API URL the credentials are for.
    :param config: The platform's CLI OAuth client.
    :param open_url: Shows the authorization URL to the user (opens the browser and prints it).
    :param timeout: Seconds to wait for the redirect.
    :return: The new credentials.
    :raises OAuthError: If the login was denied, timed out, or the code exchange failed.
    """
    metadata = discover(config.issuer)
    state = secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    received: Dict[str, str] = {}

    class _RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
            parts = urlsplit(self.path)
            params = {key: values[0] for key, values in parse_qs(parts.query).items()}
            if parts.path != "/callback" or not secrets.compare_digest(
                params.get("state", "").encode(), state.encode()
            ):
                self.send_error(400, "Invalid login redirect")
                return
            received.update(params)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(LOGIN_FAILURE_PAGE if "error" in params else LOGIN_SUCCESS_PAGE)

        def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
            """Keep the default request log off the terminal."""

    with HTTPServer(("127.0.0.1", 0), _RedirectHandler) as server:
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        query = urlencode(
            {
                "client_id": config.client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": " ".join(config.scopes),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        open_url(f"{metadata['authorization_endpoint']}?{query}")

        deadline = time.monotonic() + timeout
        while not received:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OAuthError("Timed out waiting for the browser login.")
            server.timeout = remaining
            server.handle_request()

    if "error" in received:
        raise OAuthError(received.get("error_description") or received["error"])

    token = _token_or_raise(
        _request_token(
            metadata["token_endpoint"],
            {
                "grant_type": "authorization_code",
                "code": received["code"],
                "redirect_uri": redirect_uri,
                "client_id": config.client_id,
                "code_verifier": verifier,
            },
        )
    )
    return _credentials(api_url, config, metadata, token)


def device_login(
    api_url: str,
    config: CliOAuthConfig,
    show_code: Callable[[str, str, Optional[str]], None],
    sleep: Callable[[float], None] = time.sleep,
) -> OAuthCredentials:
    """Log in without a local browser, using the device authorization grant (RFC 8628).

    :param api_url: The normalized base API URL the credentials are for.
    :param config: The platform's CLI OAuth client.
    :param show_code: Shows the user code and verification URLs to the user.
    :param sleep: Waits between polls. Replaced in tests.
    :return: The new credentials.
    :raises OAuthError: If the platform doesn't support the device flow, or the login was denied or expired.
    """
    metadata = discover(config.issuer)
    if "device_authorization_endpoint" not in metadata:
        raise OAuthError("This platform does not support device login. Run `haystack-enterprise login` instead.")

    response = httpx.post(
        metadata["device_authorization_endpoint"],
        data={"client_id": config.client_id, "scope": " ".join(config.scopes)},
        timeout=30,
    )
    if response.status_code != httpx.codes.OK:
        raise OAuthError(f"Device login could not start (HTTP {response.status_code}).")
    device = response.json()
    show_code(device["user_code"], device["verification_uri"], device.get("verification_uri_complete"))

    interval = float(device.get("interval", 5))
    deadline = time.monotonic() + float(device.get("expires_in", 600))
    while time.monotonic() < deadline:
        sleep(interval)
        poll = _request_token(
            metadata["token_endpoint"],
            {"grant_type": DEVICE_CODE_GRANT, "device_code": device["device_code"], "client_id": config.client_id},
        )
        error = (poll.json() if poll.content else {}).get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        return _credentials(api_url, config, metadata, _token_or_raise(poll))
    raise OAuthError("The device code expired before the login was approved.")


def revoke(credentials: OAuthCredentials) -> None:
    """Revoke the refresh token at the platform, best effort: logout must work offline too."""
    if not credentials.revocation_endpoint:
        return
    try:
        httpx.post(
            credentials.revocation_endpoint,
            data={
                "token": credentials.refresh_token,
                "token_type_hint": "refresh_token",
                "client_id": credentials.client_id,
            },
            timeout=10,
        )
    except httpx.HTTPError:
        pass


class OAuthAuth(httpx.Auth):
    """Sends the stored access token and refreshes it before it expires, or once after a 401."""

    def __init__(self, credentials: OAuthCredentials, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        """:param transport: Transport for token refreshes. Replaced in tests."""
        self.credentials = credentials
        self._transport = transport
        self._lock = asyncio.Lock()

    async def _refresh(self, stale_token: str) -> None:
        async with self._lock:
            if self.credentials.access_token != stale_token:
                return  # another request refreshed while we waited
            # Another process (a second CLI run) may have refreshed already; its refresh token supersedes ours.
            # ponytail: no cross-process lock, add a file lock if concurrent refreshes race on rotated tokens.
            on_disk = OAuthCredentials.load(self.credentials.api_url)
            if on_disk and on_disk.access_token != stale_token and not on_disk.expires_soon():
                self.credentials = on_disk
                return
            refresh_token = on_disk.refresh_token if on_disk else self.credentials.refresh_token
            async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
                response = await client.post(
                    self.credentials.token_endpoint,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self.credentials.client_id,
                    },
                )
            if response.status_code != httpx.codes.OK:
                return  # leave the request to fail with 401; the SDK's 401 message says to log in again
            self.credentials = self.credentials.with_token_response(response.json())
            self.credentials.save()

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Attach the access token, refreshing first if it is about to expire, and retry once on a 401."""
        if self.credentials.expires_soon():
            await self._refresh(self.credentials.access_token)
        sent_token = self.credentials.access_token
        request.headers["Authorization"] = f"Bearer {sent_token}"
        if self.credentials.organization_id:
            request.headers["X-Organization-ID"] = self.credentials.organization_id
        response = yield request

        if response.status_code == httpx.codes.UNAUTHORIZED:
            await self._refresh(sent_token)
            if self.credentials.access_token != sent_token:
                await response.aclose()
                request.headers["Authorization"] = f"Bearer {self.credentials.access_token}"
                yield request
