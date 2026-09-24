import base64
import hashlib
import threading
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from haystack_enterprise_sdk._api import oauth
from haystack_enterprise_sdk._api.config import CommonConfig
from haystack_enterprise_sdk._api.oauth import (
    CliOAuthConfig,
    OAuthAuth,
    OAuthCredentials,
    OAuthError,
    authorization_code_login,
    device_login,
    fetch_cli_config,
    pkce_pair,
)

API_URL = "https://api.example.com"
CONFIG = CliOAuthConfig(issuer="https://auth.example.com/application/o/cli/", client_id="cli", scopes=["openid"])
METADATA = {
    "authorization_endpoint": "https://auth.example.com/authorize",
    "token_endpoint": "https://auth.example.com/token",
    "revocation_endpoint": "https://auth.example.com/revoke",
    "device_authorization_endpoint": "https://auth.example.com/device",
}
TOKEN = {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 600}


@pytest.fixture(autouse=True)
def credentials_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "credentials.json"
    monkeypatch.setattr(oauth, "CREDENTIALS_PATH", path)
    return path


def _credentials(**overrides: Any) -> OAuthCredentials:
    values: Dict[str, Any] = {
        "api_url": API_URL,
        "client_id": "cli",
        "token_endpoint": METADATA["token_endpoint"],
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_at": time.time() + 600,
        **overrides,
    }
    return OAuthCredentials(**values)


def _response(status_code: int, json: Dict[str, Any]) -> httpx.Response:
    return httpx.Response(status_code, json=json, request=httpx.Request("POST", "https://example.com"))


def test_pkce_challenge_is_s256_of_verifier() -> None:
    verifier, challenge = pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert 43 <= len(verifier) <= 128


def test_credentials_round_trip_and_permissions(credentials_path: Path) -> None:
    _credentials(organization_id="org-1").save()

    assert credentials_path.stat().st_mode & 0o777 == 0o600
    assert OAuthCredentials.load(API_URL) == _credentials(
        organization_id="org-1",
        expires_at=OAuthCredentials.load().expires_at,  # type: ignore[union-attr]
    )
    assert OAuthCredentials.load("https://api.other.com") is None


@pytest.mark.parametrize(
    "status_code, body, expected",
    [
        (200, {"issuer": CONFIG.issuer, "client_id": "cli", "scopes": ["openid"]}, CONFIG),
        (200, {}, None),
        (404, {"detail": "Not Found"}, None),
    ],
)
def test_fetch_cli_config(status_code: int, body: Dict[str, Any], expected: Any) -> None:
    with patch("haystack_enterprise_sdk._api.oauth.httpx.get", return_value=_response(status_code, body)):
        assert fetch_cli_config(API_URL) == expected


def test_authorization_code_login() -> None:
    redirect_statuses: List[int] = []
    token_requests: List[Dict[str, str]] = []
    threads: List[threading.Thread] = []

    def open_url(url: str) -> None:
        # Stand in for the browser: a forged redirect first, then the one the authorization server sends.
        query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        assert query["code_challenge_method"] == "S256"
        token_requests.append({"challenge": query["code_challenge"]})

        def redirect() -> None:
            callback = query["redirect_uri"]
            redirect_statuses.append(httpx.get(callback, params={"code": "c", "state": "forged"}).status_code)
            redirect_statuses.append(httpx.get(callback, params={"code": "c", "state": query["state"]}).status_code)

        threads.append(threading.Thread(target=redirect))
        threads[0].start()

    def request_token(endpoint: str, data: Dict[str, str]) -> httpx.Response:
        token_requests.append(data)
        return _response(200, TOKEN)

    with (
        patch("haystack_enterprise_sdk._api.oauth.discover", return_value=METADATA),
        patch("haystack_enterprise_sdk._api.oauth._request_token", side_effect=request_token),
    ):
        credentials = authorization_code_login(API_URL, CONFIG, open_url, timeout=5)

    threads[0].join(timeout=5)
    assert redirect_statuses == [400, 200]
    challenge, exchange = token_requests[0]["challenge"], token_requests[1]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "c"
    expected_challenge = base64.urlsafe_b64encode(hashlib.sha256(exchange["code_verifier"].encode()).digest())
    assert expected_challenge.rstrip(b"=").decode() == challenge
    assert (credentials.access_token, credentials.refresh_token) == ("access-1", "refresh-1")
    assert credentials.revocation_endpoint == METADATA["revocation_endpoint"]


def test_authorization_code_login_times_out() -> None:
    with (
        patch("haystack_enterprise_sdk._api.oauth.discover", return_value=METADATA),
        pytest.raises(OAuthError, match="Timed out"),
    ):
        authorization_code_login(API_URL, CONFIG, lambda url: None, timeout=0.2)


def test_device_login_polls_until_approved() -> None:
    device = {"device_code": "d", "user_code": "ABCD-EFGH", "verification_uri": "https://auth.example.com/device"}
    polls = [
        _response(400, {"error": "authorization_pending"}),
        _response(400, {"error": "slow_down"}),
        _response(200, TOKEN),
    ]
    show_code = Mock()
    sleep = Mock()

    with (
        patch("haystack_enterprise_sdk._api.oauth.discover", return_value=METADATA),
        patch("haystack_enterprise_sdk._api.oauth.httpx.post", return_value=_response(200, device)),
        patch("haystack_enterprise_sdk._api.oauth._request_token", side_effect=polls) as request_token,
    ):
        credentials = device_login(API_URL, CONFIG, show_code, sleep=sleep)

    show_code.assert_called_once_with("ABCD-EFGH", device["verification_uri"], None)
    assert [call.args[0] for call in sleep.call_args_list] == [5.0, 5.0, 10.0]
    assert request_token.call_args.args[1]["grant_type"] == oauth.DEVICE_CODE_GRANT
    assert credentials.access_token == "access-1"


def test_device_login_denied() -> None:
    device = {"device_code": "d", "user_code": "X", "verification_uri": "https://auth.example.com/device"}
    with (
        patch("haystack_enterprise_sdk._api.oauth.discover", return_value=METADATA),
        patch("haystack_enterprise_sdk._api.oauth.httpx.post", return_value=_response(200, device)),
        patch(
            "haystack_enterprise_sdk._api.oauth._request_token",
            return_value=_response(400, {"error": "access_denied"}),
        ),
        pytest.raises(OAuthError, match="access_denied"),
    ):
        device_login(API_URL, CONFIG, Mock(), sleep=Mock())


def _platform(seen_tokens: List[str], refreshes: List[Dict[str, str]]) -> httpx.MockTransport:
    """A fake API that only accepts `access-2`, and a token endpoint that hands it out."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == METADATA["token_endpoint"]:
            refreshes.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return httpx.Response(200, json={"access_token": "access-2", "refresh_token": "refresh-2"})
        token = request.headers["Authorization"].removeprefix("Bearer ")
        seen_tokens.append(token)
        return httpx.Response(200 if token == "access-2" else 401)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_auth_refreshes_expiring_token(credentials_path: Path) -> None:
    seen: List[str] = []
    refreshes: List[Dict[str, str]] = []
    transport = _platform(seen, refreshes)
    auth = OAuthAuth(_credentials(expires_at=time.time() + 10, organization_id="org-1"), transport=transport)

    async with httpx.AsyncClient(transport=transport, auth=auth) as client:
        response = await client.get(f"{API_URL}/api/v1/workspaces")

    assert response.status_code == 200
    assert response.request.headers["X-Organization-ID"] == "org-1"
    assert seen == ["access-2"]
    assert refreshes == [{"grant_type": "refresh_token", "refresh_token": "refresh-1", "client_id": "cli"}]
    stored = OAuthCredentials.load(API_URL)
    assert stored is not None and (stored.access_token, stored.refresh_token) == ("access-2", "refresh-2")


@pytest.mark.asyncio
async def test_auth_refreshes_once_after_401() -> None:
    seen: List[str] = []
    refreshes: List[Dict[str, str]] = []
    transport = _platform(seen, refreshes)
    auth = OAuthAuth(_credentials(), transport=transport)  # not expiring, but revoked server-side

    async with httpx.AsyncClient(transport=transport, auth=auth) as client:
        response = await client.get(f"{API_URL}/api/v1/workspaces")

    assert response.status_code == 200
    assert seen == ["access-1", "access-2"]
    assert len(refreshes) == 1


def test_config_uses_oauth_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setattr("haystack_enterprise_sdk._api.config.load_environment", lambda show_warnings: True)
    _credentials().save()

    config = CommonConfig(api_url=API_URL)
    assert config.oauth is not None and config.oauth.credentials.access_token == "access-1"

    assert CommonConfig(api_key="api_key", api_url=API_URL).oauth is None
