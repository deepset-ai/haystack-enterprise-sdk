"""Async client for deploying local Haystack pipelines to deepset AI Platform service deployments."""

from pathlib import Path
from typing import Callable, Optional, Tuple

import structlog

from deepset_cloud_sdk._api.config import (
    API_KEY,
    API_URL,
    DEFAULT_WORKSPACE_NAME,
    CommonConfig,
)
from deepset_cloud_sdk._api.deepset_cloud_api import DeepsetCloudAPI
from deepset_cloud_sdk._api.deployments import Deployment, DeploymentStatus
from deepset_cloud_sdk._service.deployment_service import (
    DEFAULT_ACTIVATION_TIMEOUT_S,
    DEFAULT_POLL_INTERVAL_S,
    CreateOptions,
    DeploymentService,
    DeployResult,
)

logger = structlog.get_logger(__name__)


# pylint: disable=too-few-public-methods
class AsyncDeploymentClient:
    """Async client for deploying local Haystack pipelines as service deployment revisions.

    Example:
        ```python
        from deepset_cloud_sdk import AsyncDeploymentClient

        client = AsyncDeploymentClient()
        await client.deploy("pipeline.py", "my-service", activate=True)
        ```
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        workspace_name: Optional[str] = None,
        api_url: Optional[str] = None,
    ) -> None:
        """Initialize the async deployment client.

        :param api_key: deepset API key. Falls back to the ``API_KEY`` environment variable.
        :param workspace_name: Workspace to deploy into. Falls back to ``DEFAULT_WORKSPACE_NAME``.
        :param api_url: API URL. Falls back to the ``API_URL`` environment variable.
        :raises ValueError: If no workspace is configured.
        """
        self._api_config = CommonConfig(api_key=api_key or API_KEY, api_url=api_url or API_URL)
        self._workspace_name = workspace_name or DEFAULT_WORKSPACE_NAME
        if not self._workspace_name:
            raise ValueError(
                "Workspace not configured. Provide a workspace name or set the `DEFAULT_WORKSPACE_NAME` "
                "environment variable."
            )

    async def deploy(  # pylint: disable=too-many-arguments
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
        """Transform ``target`` and push it as a new revision of ``service_name``.

        See :meth:`deepset_cloud_sdk._service.deployment_service.DeploymentService.deploy` for details.
        """
        async with DeepsetCloudAPI.factory(self._api_config) as api:
            service = DeploymentService(api, self._workspace_name)
            return await service.deploy(
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

    async def get_service_status(self, service_name: str) -> Deployment:
        """Return the current deployment (with live runtime status) for ``service_name``."""
        async with DeepsetCloudAPI.factory(self._api_config) as api:
            service = DeploymentService(api, self._workspace_name)
            return await service.get_service_status(service_name)
