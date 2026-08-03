"""HaystackEnterpriseAPI class."""

from __future__ import annotations

import base64
import binascii
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable, Dict, Optional

import httpx
import structlog
from httpx import Response
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from haystack_enterprise_sdk._api.config import API_VERSION_PATH, CommonConfig

logger = structlog.get_logger(__name__)


DEFAULT_MAX_ATTEMPTS = 3
SAFE_MODE_MAX_ATTEMPTS = 10


class WorkspaceNotDefinedError(Exception):
    """The workspace_name is not defined. Set an environment variable or pass the `workspace_name` argument."""


class HaystackEnterpriseAPIError(Exception):
    """Raised for a proxy/infrastructure-level error, carrying a friendly message instead of a raw response body.

    Status codes like 401/403/502/503/504 are often raised by the reverse proxy sitting in front of the
    API rather than by the API itself, so the response body is typically an HTML error page. This is
    raised before that body would otherwise get embedded into a resource-specific exception message.
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


_FRIENDLY_STATUS_MESSAGES: Dict[int, str] = {
    403: "You don't have permission to perform this action in this workspace.",
    502: "The Haystack Enterprise Platform is temporarily unavailable (bad gateway). Please try again in a moment.",
    503: "The Haystack Enterprise Platform is temporarily unavailable. Please try again in a moment.",
    504: "The Haystack Enterprise Platform did not respond in time (gateway timeout). Please try again in a moment.",
}


def _looks_like_html(response: Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return "html" in content_type.lower() or response.text.lstrip().startswith("<")


def _decode_jwt_expiry(token: str) -> Optional[datetime]:
    """Best-effort extraction of the ``exp`` claim from a JWT, without verifying its signature.

    Only used to tell an expired API key apart from an invalid one in the 401 message below —
    the server is always the source of truth for whether a token is actually valid.

    :param token: The raw token (e.g. the API key sent as the bearer token). Haystack Enterprise API keys
        carry an ``api_`` prefix before the JWT itself (e.g. ``api_eyJhb....eyJzdWQ....pzxY``).
    :return: The expiry as a UTC datetime, or ``None`` if ``token`` isn't a decodable JWT with an ``exp`` claim.
    """
    try:
        _, payload_b64, _ = token.removeprefix("api_").split(".")
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, binascii.Error):
        return None


def _unauthorized_message(api_key: str) -> str:
    """Build a friendly 401 message, naming an expired API key explicitly when the token says so.

    :param api_key: The API key sent as the bearer token on the failing request.
    """
    expiry = _decode_jwt_expiry(api_key)
    if expiry is None:
        return "Authentication failed. Your API key may be missing, invalid, or expired."
    if expiry <= datetime.now(timezone.utc):
        return f"Authentication failed. Your API key expired on {expiry.isoformat(timespec='seconds')}. Generate a new one."
    return "Authentication failed. Your API key may be invalid, revoked, or issued for a different environment."


def _bearer_token(headers: Dict[str, str], default: str) -> str:
    """Extract the bearer token actually sent in ``headers``, falling back to ``default`` if none is set.

    :param headers: The request headers actually sent, after any caller override has been merged in.
    :param default: The API key to fall back to when ``headers`` carries no ``Authorization`` header.
    """
    auth = headers.get("Authorization", "")
    prefix = "Bearer "
    return auth[len(prefix) :] if auth.startswith(prefix) else default


def _raise_for_proxy_error(response: Response, api_key: str) -> None:
    """Raise a friendly ``HaystackEnterpriseAPIError`` for a proxy/infrastructure-level error response.

    Known status codes (401, 403, 502, 503, 504) always get a friendly message. Any other error
    status with an HTML body (an unrecognised proxy error page) falls back to a generic message,
    so an HTML page never ends up dumped verbatim into an exception raised further up the stack.

    :param response: The HTTP response to check.
    :param api_key: The API key sent as the bearer token on this request, used to distinguish an
        expired 401 from an invalid one.
    """
    if response.status_code < 400:
        return
    if response.status_code == httpx.codes.UNAUTHORIZED:
        message: Optional[str] = _unauthorized_message(api_key)
    else:
        message = _FRIENDLY_STATUS_MESSAGES.get(response.status_code)
    if message is None:
        if not _looks_like_html(response):
            return
        message = f"The Haystack Enterprise Platform returned an unexpected error (status code {response.status_code})."
    logger.error(
        "Haystack Enterprise Platform API returned a proxy/infrastructure error.", status_code=response.status_code
    )
    raise HaystackEnterpriseAPIError(response.status_code, message)


def raise_for_unexpected_status(
    response: Response,
    accepted: tuple,
    error_cls: type,
    message: str,
) -> None:
    """Log and raise ``error_cls`` when ``response`` has a status code outside ``accepted``.

    :param response: The HTTP response to check.
    :param accepted: Status codes that count as success.
    :param error_cls: Exception type to raise on an unexpected status.
    :param message: Context for the log entry and exception (e.g. ``"Failed to create deployment 'x'."``).
    """
    if response.status_code not in accepted:
        logger.error(message, status_code=response.status_code, body=response.text)
        raise error_cls(f"{message} Status code: {response.status_code}. {response.text}")


def deployment_base_url(api_url: str, workspace_name: str, deployment_id: Any) -> str:
    """Build the base URL of a deployment's OpenAI-compatible endpoint.

    The platform serves ``POST <this>/chat/completions`` for a deployment with an active revision. It is
    keyed on the deployment id, not the service name. Returned without the ``/chat/completions`` suffix
    because that is exactly the ``base_url`` an OpenAI client expects (it appends the path itself).

    :param api_url: Base API URL, already normalized (see :func:`~haystack_enterprise_sdk._api.config.normalize_base_url`).
    :param workspace_name: Name of the workspace the deployment lives in.
    :param deployment_id: The deployment's id.
    :return: e.g. ``https://api.cloud.deepset.ai/api/v1/workspaces/my-ws/deployments/<uuid>``.
    """
    return f"{api_url}/{API_VERSION_PATH}/workspaces/{workspace_name}/deployments/{deployment_id}"


class HaystackEnterpriseAPI:
    """Haystack Enterprise Platform API client.

    This class takes care of all API calls to Haystack Enterprise Platform and handles authentication and errors.
    """

    def __init__(self, config: CommonConfig, client: httpx.AsyncClient) -> None:
        """Create a Haystack Enterprise Platform API client.

        Add a config for authentication and a HTTPX client for
        sending requests.

        :param config: Config for authentication.
        :param client: HTTPX client for sending requests.
        """
        self.api_key = config.api_key
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {config.api_key}",
            "X-Client-Source": "haystack-enterprise-sdk",
        }
        self.base_url = lambda workspace_name: self._get_base_url(config.api_url)(workspace_name)
        self.client = client
        self.max_attempts = SAFE_MODE_MAX_ATTEMPTS if config.safe_mode else DEFAULT_MAX_ATTEMPTS

    @staticmethod
    def _get_base_url(api_url: str) -> Callable:
        def func(workspace_name: str) -> str:
            """Get the base URL for the API.

            :param workspace_name: Name of the workspace to use.
            :return: Base URL.
            """
            if not workspace_name or workspace_name == "":
                raise WorkspaceNotDefinedError(
                    f"Workspace name is not defined. Got '{workspace_name}'. Enter the name of the workspace in `workspace_name`."
                )

            return f"{api_url}/{API_VERSION_PATH}/workspaces/{workspace_name}"

        return func

    @classmethod
    @asynccontextmanager
    async def factory(cls, config: CommonConfig) -> AsyncGenerator[HaystackEnterpriseAPI, None]:
        """Create a new instance of the API client.

        :param config: CommonConfig object.
        """
        if config.safe_mode:
            safe_mode_limits = httpx.Limits(max_keepalive_connections=1, max_connections=1)
            safe_mode_timeout = httpx.Timeout(None)
            async with httpx.AsyncClient(limits=safe_mode_limits, timeout=safe_mode_timeout) as client:
                yield cls(config, client)
        else:
            async with httpx.AsyncClient() as client:
                yield cls(config, client)

    async def get(
        self, workspace_name: str, endpoint: str, params: Optional[Dict[str, Any]] = None, timeout_s: int = 20
    ) -> Response:
        """Make a GET request to the Haystack Enterprise Platform API.

        :param workspace_name: Name of the workspace to use.
        :param endpoint: Endpoint to call.
        :param params: Query parameters to pass.
        :param timeout_s: Timeout in seconds.
        :return: Response object.
        """

        @retry(
            retry=retry_if_exception_type(httpx.RequestError),
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_fixed(1),
            reraise=True,
        )
        async def retry_wrapper() -> Response:
            return await self._get(workspace_name, endpoint, params, timeout_s)

        return await retry_wrapper()

    async def _get(
        self, workspace_name: str, endpoint: str, params: Optional[Dict[str, Any]] = None, timeout_s: int = 20
    ) -> Response:
        response = await self.client.get(
            f"{self.base_url(workspace_name)}/{endpoint}",
            params=params or {},
            headers=self.headers,
            timeout=timeout_s,
        )
        logger.debug(
            "Called Haystack Enterprise Platform API.",
            method="GET",
            workspace=workspace_name,
            endpoint=endpoint,
            params=params,
            status=response.status_code,
        )
        _raise_for_proxy_error(response, self.api_key)
        return response

    async def post(
        self,
        workspace_name: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout_s: int = 20,
    ) -> Response:
        """Make a POST request to the Haystack Enterprise Platform API.

        :param workspace_name: Name of the workspace to use.
        :param endpoint: Endpoint to call.
        :param params: Query parameters to pass.
        :param json: JSON data to pass.
        :param data: Data to pass.
        :param files: Files to pass.
        :param headers: Extra headers to merge over the default auth headers for this request.
        :param timeout_s: Timeout in seconds.
        :return: Response object.
        """
        merged_headers = {**self.headers, **(headers or {})}
        response = await self.client.post(
            f"{self.base_url(workspace_name)}/{endpoint}",
            params=params or {},
            json=json,
            data=data,
            files=files,
            headers=merged_headers,
            timeout=timeout_s,
        )
        logger.debug(
            "Called Haystack Enterprise Platform API",
            method="POST",
            workspace=workspace_name,
            endpoint=endpoint,
            data=data or {},
            files=files,
            status=response.status_code,
        )
        _raise_for_proxy_error(response, _bearer_token(merged_headers, self.api_key))
        return response

    async def delete(
        self, workspace_name: str, endpoint: str, params: Optional[Dict[str, Any]] = None, timeout_s: int = 20
    ) -> Response:
        """
        Make a DELETE request to the Haystack Enterprise Platform API.

        :param workspace_name: Name of the workspace to use.
        :param endpoint: Endpoint to call.
        :param params: Query parameters to pass.
        :param timeout_s: Timeout in seconds.
        :return: Response object.
        """
        response = await self.client.delete(
            f"{self.base_url(workspace_name)}/{endpoint}",
            params=params or {},
            headers=self.headers,
            timeout=timeout_s,
        )
        logger.debug(
            "Called Haystack Enterprise Platform API",
            method="DELETE",
            workspace=workspace_name,
            endpoint=endpoint,
            params=params,
            status=response.status_code,
        )
        _raise_for_proxy_error(response, self.api_key)
        return response

    async def put(
        self,
        workspace_name: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        timeout_s: int = 20,
    ) -> Response:
        """Make a PUT request to the Haystack Enterprise Platform API.

        :param workspace_name: Name of the workspace to use.
        :param endpoint: Endpoint to call.
        :param params: Query parameters to pass.
        :param data: Data to pass.
        :param timeout_s: Timeout in seconds.
        :return: Response object.
        """

        @retry(
            retry=retry_if_exception_type(httpx.ConnectError),
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_fixed(1),
            reraise=True,
        )
        async def retry_wrapper() -> Response:
            return await self._put(workspace_name, endpoint, params, data, timeout_s)

        return await retry_wrapper()

    async def _put(
        self,
        workspace_name: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        timeout_s: int = 20,
    ) -> Response:
        response = await self.client.put(
            f"{self.base_url(workspace_name)}/{endpoint}",
            params=params or {},
            json=data or {},
            headers=self.headers,
            timeout=timeout_s,
        )
        logger.debug(
            "Called Haystack Enterprise Platform API",
            method="PUT",
            workspace=workspace_name,
            endpoint=endpoint,
            data=data or {},
            status=response.status_code,
        )
        _raise_for_proxy_error(response, self.api_key)
        return response

    async def patch(
        self,
        workspace_name: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        timeout_s: int = 20,
    ) -> Response:
        """Make a PATCH request to the Haystack Enterprise Platform API.

        :param workspace_name: Name of the workspace to use.
        :param endpoint: Endpoint to call.
        :param params: Query parameters to pass.
        :param json: JSON data to pass.
        :param data: Data to pass.
        :param timeout_s: Timeout in seconds.
        :return: Response object.
        """
        response = await self.client.patch(
            f"{self.base_url(workspace_name)}/{endpoint}",
            params=params or {},
            json=json,
            data=data,
            headers=self.headers,
            timeout=timeout_s,
        )
        logger.debug(
            "Called Haystack Enterprise Platform API",
            method="PATCH",
            workspace=workspace_name,
            endpoint=endpoint,
            json=json or {},
            data=data or {},
            status=response.status_code,
        )
        _raise_for_proxy_error(response, self.api_key)
        return response


def get_haystack_enterprise_api(config: CommonConfig, client: httpx.AsyncClient) -> HaystackEnterpriseAPI:  # noqa
    """Haystack Enterprise Platform API factory. Return an instance of HaystackEnterpriseAPI.

    :param config: CommonConfig object.
    :param client: httpx.AsyncClient object.
    :return: HaystackEnterpriseAPI object.
    """
    return HaystackEnterpriseAPI(config=config, client=client)
