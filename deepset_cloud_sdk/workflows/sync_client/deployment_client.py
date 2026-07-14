"""Sync client for deploying local Haystack pipelines to deepset AI Platform service deployments."""

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple, TypeVar

import structlog

from deepset_cloud_sdk._api.deployments import Deployment, DeploymentStatus
from deepset_cloud_sdk._api.shared_prototypes import SharedPrototype
from deepset_cloud_sdk._service.deployment_service import (
    DEFAULT_ACTIVATION_TIMEOUT_S,
    DEFAULT_POLL_INTERVAL_S,
    CreateOptions,
    DeployResult,
    ShareOptions,
)
from deepset_cloud_sdk.workflows.async_client.deployment_client import (
    AsyncDeploymentClient,
)

logger = structlog.get_logger(__name__)

T = TypeVar("T")


def _run(coro: Awaitable[T]) -> T:
    """Run ``coro`` to completion, reusing an existing event loop (e.g. in Jupyter) when present."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("Event loop is closed")
        should_close = False
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        should_close = True
    try:
        return loop.run_until_complete(coro)
    finally:
        if should_close:
            loop.close()


class DeploymentClient:  # pylint: disable=too-few-public-methods
    """Sync client for deploying local Haystack pipelines as service deployment revisions.

    Example:
        ```python
        from deepset_cloud_sdk import DeploymentClient

        client = DeploymentClient()
        client.deploy("pipeline.py", "my-service", activate=True)
        ```
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        workspace_name: Optional[str] = None,
        api_url: Optional[str] = None,
    ) -> None:
        """Initialize the sync deployment client. See :class:`AsyncDeploymentClient` for parameters."""
        self._async_client = AsyncDeploymentClient(
            api_key=api_key,
            workspace_name=workspace_name,
            api_url=api_url,
        )

    def deploy(  # pylint: disable=too-many-arguments
        self,
        target: Path,
        service_name: str,
        *,
        activate: bool = False,
        create: bool = False,
        create_options: Optional[CreateOptions] = None,
        entrypoint: Optional[str] = None,
        requirements: Optional[Path] = None,
        inputs: Optional[dict] = None,
        outputs: Optional[dict] = None,
        io_resolver: Optional[Callable[[dict], Tuple[dict, dict]]] = None,
        python_executable: Optional[str] = None,
        timeout_s: float = DEFAULT_ACTIVATION_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        on_status: Optional[Callable[[DeploymentStatus], None]] = None,
    ) -> DeployResult:
        """Transform ``target`` and push it as a new revision of ``service_name`` synchronously."""
        return _run(
            self._async_client.deploy(
                target,
                service_name,
                activate=activate,
                create=create,
                create_options=create_options,
                entrypoint=entrypoint,
                requirements=requirements,
                inputs=inputs,
                outputs=outputs,
                io_resolver=io_resolver,
                python_executable=python_executable,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
                on_status=on_status,
            )
        )

    def get_service_status(self, service_name: str) -> Deployment:
        """Return the current deployment (with live runtime status) for ``service_name``."""
        return _run(self._async_client.get_service_status(service_name))

    def create_shared_prototype(
        self, service_name: str, options: Optional[ShareOptions] = None
    ) -> SharedPrototype:
        """Create a shared prototype (a shareable chat UI link) for a deployed service synchronously."""
        return _run(self._async_client.create_shared_prototype(service_name, options))
