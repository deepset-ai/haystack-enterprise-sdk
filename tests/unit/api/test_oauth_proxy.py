import time
from typing import AsyncIterator, List

import httpx
import pytest

from haystack_enterprise_sdk._api.oauth import OAuthCredentials
from haystack_enterprise_sdk._api.oauth_proxy import oauth_proxy


class _Body(httpx.AsyncByteStream):
    """A body that is streamed like a network response, not read up front like ``httpx.Response(json=...)``."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'{"ok": '
        yield b"true}"


def _credentials() -> OAuthCredentials:
    return OAuthCredentials(
        api_url="https://platform.example.com",
        client_id="cli",
        token_endpoint="https://idp.example.com/token",
        access_token="access",
        refresh_token="refresh",
        expires_at=time.time() + 3600,
        organization_id="org",
    )


@pytest.mark.asyncio
async def test_proxy_swaps_in_the_session_token() -> None:
    seen: List[httpx.Request] = []

    def platform(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, stream=_Body(), headers={"Content-Type": "application/json", "X-Trace": "1"})

    async with oauth_proxy(_credentials(), transport=httpx.MockTransport(platform)) as (url, token):
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{url}/api/v1/workspaces?limit=1", json={"a": 1}, headers={"Authorization": f"Bearer {token}"}
            )

    assert response.status_code == 201
    assert response.json() == {"ok": True}
    assert response.headers["X-Trace"] == "1"
    assert str(seen[0].url) == "https://platform.example.com/api/v1/workspaces?limit=1"
    assert seen[0].headers["Authorization"] == "Bearer access"
    assert seen[0].headers["X-Organization-ID"] == "org"
    assert seen[0].content == b'{"a":1}'


@pytest.mark.asyncio
async def test_proxy_rejects_other_tokens() -> None:
    def platform(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the platform")

    async with oauth_proxy(_credentials(), transport=httpx.MockTransport(platform)) as (url, _):
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{url}/api/v1/workspaces", headers={"Authorization": "Bearer guess"})

    assert response.status_code == 401
