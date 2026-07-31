"""Async client for deploying local Haystack pipelines to deepset AI Platform service deployments."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import structlog

from haystack_enterprise_sdk._api.config import (
    API_KEY,
    API_URL,
    DEFAULT_WORKSPACE_NAME,
    CommonConfig,
)
from haystack_enterprise_sdk._api.deployments import (
    Deployment,
    DeploymentStatus,
    PipelineValidationResult,
)
from haystack_enterprise_sdk._api.haystack_enterprise_api import (
    HaystackEnterpriseAPI,
    deployment_base_url,
)
from haystack_enterprise_sdk._api.pipeline_run import DEFAULT_RUN_RETRIES, OnRetry
from haystack_enterprise_sdk._api.shared_prototypes import SharedPrototype
from haystack_enterprise_sdk._service.deployment_service import (
    DEFAULT_ACTIVATION_TIMEOUT_S,
    DEFAULT_POLL_INTERVAL_S,
    CreateOptions,
    DeploymentService,
    DeployResult,
    ShareOptions,
)
from haystack_enterprise_sdk._service.pipeline_transform import IoResolver

logger = structlog.get_logger(__name__)


# pylint: disable=too-few-public-methods
class AsyncDeploymentClient:
    """Async client for deploying local Haystack pipelines as service deployment revisions.

    Example:
        ```python
        from haystack_enterprise_sdk import AsyncDeploymentClient

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

    @property
    def workspace_name(self) -> str:
        """The workspace this client deploys into."""
        return self._workspace_name

    def deployment_base_url(self, deployment_id: Any) -> str:
        """The OpenAI-compatible base URL of a deployment in this client's workspace.

        Append ``/chat/completions`` to call it directly, or hand it to an OpenAI client as ``base_url``.
        Only usable once the deployment has an active revision.

        :param deployment_id: Id of the deployment (e.g. ``DeployResult.deployment.deployment_id``).
        :return: The deployment's base URL.
        """
        return deployment_base_url(self._api_config.api_url, self._workspace_name, deployment_id)

    @asynccontextmanager
    async def _service(self) -> AsyncIterator[DeploymentService]:
        """Yield a :class:`DeploymentService` backed by a managed API client."""
        async with HaystackEnterpriseAPI.factory(self._api_config) as api:
            yield DeploymentService(api, self._workspace_name)

    async def deploy(  # pylint: disable=too-many-arguments
        self,
        target: Path,
        service_name: str,
        *,
        activate: bool = False,
        create: bool = False,
        create_options: Optional[CreateOptions] = None,
        comment: Optional[str] = None,
        entrypoint: Optional[str] = None,
        inputs: Optional[dict] = None,
        outputs: Optional[dict] = None,
        pipeline_output_type: Optional[str] = None,
        io_resolver: Optional[IoResolver] = None,
        python_executable: Optional[str] = None,
        validate: bool = True,
        timeout_s: float = DEFAULT_ACTIVATION_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        on_status: Optional[Callable[[DeploymentStatus], None]] = None,
    ) -> DeployResult:
        """Transform ``target`` and push it as a new revision of ``service_name``.

        See :meth:`haystack_enterprise_sdk._service.deployment_service.DeploymentService.deploy` for details.
        """
        async with self._service() as service:
            return await service.deploy(
                target,
                service_name,
                activate=activate,
                create=create,
                create_options=create_options,
                comment=comment,
                entrypoint=entrypoint,
                inputs=inputs,
                outputs=outputs,
                pipeline_output_type=pipeline_output_type,
                io_resolver=io_resolver,
                python_executable=python_executable,
                validate=validate,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
                on_status=on_status,
            )

    async def validate(
        self,
        target: Path,
        *,
        entrypoint: Optional[str] = None,
        inputs: Optional[dict] = None,
        outputs: Optional[dict] = None,
        pipeline_output_type: Optional[str] = None,
        io_resolver: Optional[IoResolver] = None,
        python_executable: Optional[str] = None,
    ) -> PipelineValidationResult:
        """Transform ``target`` and validate the generated YAML against the platform without deploying.

        See :meth:`haystack_enterprise_sdk._service.deployment_service.DeploymentService.validate` for details.
        """
        async with self._service() as service:
            return await service.validate(
                target,
                entrypoint=entrypoint,
                inputs=inputs,
                outputs=outputs,
                pipeline_output_type=pipeline_output_type,
                io_resolver=io_resolver,
                python_executable=python_executable,
            )

    async def run(  # pylint: disable=too-many-arguments
        self,
        target: Path,
        *,
        entrypoint: Optional[str] = None,
        inputs: Optional[dict] = None,
        outputs: Optional[dict] = None,
        io_resolver: Optional[IoResolver] = None,
        python_executable: Optional[str] = None,
        query: Optional[str] = None,
        filters: Optional[Any] = None,
        extra_inputs: Optional[Dict[str, Dict[str, Any]]] = None,
        include_outputs_from: Optional[List[str]] = None,
        retries: int = DEFAULT_RUN_RETRIES,
        on_retry: Optional[OnRetry] = None,
    ) -> Dict[str, Any]:
        """Transform ``target`` and run the generated YAML in the platform sandbox, without deploying.

        See :meth:`haystack_enterprise_sdk._service.deployment_service.DeploymentService.run` for details.
        """
        async with self._service() as service:
            return await service.run(
                target,
                entrypoint=entrypoint,
                inputs=inputs,
                outputs=outputs,
                io_resolver=io_resolver,
                python_executable=python_executable,
                query=query,
                filters=filters,
                extra_inputs=extra_inputs,
                include_outputs_from=include_outputs_from,
                retries=retries,
                on_retry=on_retry,
            )

    async def find_service(self, service_name: str) -> Optional[Deployment]:
        """Return the service deployment named ``service_name``, or ``None`` if the workspace has none."""
        async with self._service() as service:
            return await service.find_service(service_name)

    async def get_service_status(self, service_name: str) -> Deployment:
        """Return the current deployment (with live runtime status) for ``service_name``."""
        async with self._service() as service:
            return await service.get_service_status(service_name)

    async def create_shared_prototype(
        self, service_name: str, options: Optional[ShareOptions] = None
    ) -> SharedPrototype:
        """Create a shared prototype (a shareable chat UI link) for a deployed service.

        See :meth:`haystack_enterprise_sdk._service.deployment_service.DeploymentService.create_shared_prototype`.
        """
        async with self._service() as service:
            return await service.create_shared_prototype(service_name, options)
