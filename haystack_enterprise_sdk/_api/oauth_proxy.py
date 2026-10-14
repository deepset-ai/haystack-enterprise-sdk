"""A localhost proxy that lets tools which only take a static API key use the CLI's OAuth login session."""

import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable, Optional, Tuple

import httpx
from aiohttp import web

from haystack_enterprise_sdk._api.oauth import OAuthAuth, OAuthCredentials

# Hop-by-hop headers, and the ones the proxy or httpx set themselves.
_SKIPPED_REQUEST_HEADERS = {"host", "authorization", "connection", "keep-alive", "content-length", "transfer-encoding"}
_SKIPPED_RESPONSE_HEADERS = {"connection", "keep-alive", "content-length", "transfer-encoding"}


def _forward(client: httpx.AsyncClient, token: str) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    async def handle(request: web.Request) -> web.StreamResponse:
        # Only the process we started knows the token, so other local processes can't borrow the session.
        if not secrets.compare_digest(request.headers.get("Authorization", ""), f"Bearer {token}"):
            return web.Response(status=401, text="Unknown proxy token.")
        upstream_request = client.build_request(
            request.method,
            request.path_qs,
            headers={k: v for k, v in request.headers.items() if k.lower() not in _SKIPPED_REQUEST_HEADERS},
            content=await request.read(),
        )
        upstream = await client.send(upstream_request, stream=True)
        try:
            response = web.StreamResponse(status=upstream.status_code)
            for name, value in upstream.headers.multi_items():
                if name.lower() not in _SKIPPED_RESPONSE_HEADERS:
                    response.headers.add(name, value)
            await response.prepare(request)
            # Raw bytes, so a compressed body still matches its Content-Encoding header.
            async for chunk in upstream.aiter_raw():
                await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            await upstream.aclose()

    return handle


@asynccontextmanager
async def oauth_proxy(
    credentials: OAuthCredentials, transport: Optional[httpx.AsyncBaseTransport] = None
) -> AsyncIterator[Tuple[str, str]]:
    """Serve a proxy to the platform on 127.0.0.1 that sends each request with a fresh OAuth access token.

    :param credentials: The stored login session. Refreshed tokens are saved back, as for any SDK call.
    :param transport: Transport for requests to the platform. Replaced in tests.
    :return: The proxy's base URL, in place of the platform's API URL, and the bearer token it accepts.
    """
    token = secrets.token_urlsafe(32)
    # No timeout here: the client behind the proxy keeps its own.
    async with httpx.AsyncClient(
        base_url=credentials.api_url, auth=OAuthAuth(credentials), timeout=None, transport=transport
    ) as client:
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", _forward(client, token))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            host, port = runner.addresses[0][:2]
            yield f"http://{host}:{port}", token
        finally:
            await runner.cleanup()
