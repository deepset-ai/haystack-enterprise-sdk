"""Deployment service: orchestrates transforming a local pipeline and deploying it as a service revision."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple
from uuid import UUID

import structlog

from deepset_cloud_sdk._api.config import DEFAULT_WORKSPACE_NAME, CommonConfig
from deepset_cloud_sdk._api.deepset_cloud_api import DeepsetCloudAPI
from deepset_cloud_sdk._api.deployments import (
    Deployment,
    DeploymentRevision,
    DeploymentsAPI,
    DeploymentServiceLevel,
    DeploymentStatus,
)
from deepset_cloud_sdk._api.shared_prototypes import (
    SharedPrototype,
    SharedPrototypesAPI,
)
from deepset_cloud_sdk._service import pipeline_transform

logger = structlog.get_logger(__name__)

# Deployment runtime statuses that end a rollout poll.
_SUCCESS_STATUSES = frozenset({DeploymentStatus.DEPLOYED, DeploymentStatus.IDLE})
_FAILURE_STATUSES = frozenset({DeploymentStatus.DEPLOYMENT_FAILED})

DEFAULT_POLL_INTERVAL_S = 3.0
DEFAULT_ACTIVATION_TIMEOUT_S = 600.0


@dataclass
class CreateOptions:
    """Sizing options used when ``create=True`` creates the service."""

    service_level: Optional[DeploymentServiceLevel] = None
    idle_timeout_in_seconds: Optional[int] = None
    min_query_replica_count: Optional[int] = None
    max_query_replica_count: Optional[int] = None
    cpu_limit: Optional[str] = None
    memory_limit: Optional[str] = None
    gpu_limit_gigabyte: Optional[int] = None


@dataclass
class DeployResult:
    """Outcome of a deploy call."""

    deployment: Deployment
    revision: DeploymentRevision
    activated: bool
    timed_out: bool


@dataclass
class ShareOptions:
    """Options for creating a shared prototype (chat UI link) for a deployed service."""

    expiration_days: int = 30
    login_required: bool = True
    description: Optional[str] = None
    show_metadata_filters: bool = False
    show_files: bool = False
    file_upload_enabled: bool = False
    runtime_params_enabled: bool = False


class ServiceNotFoundError(Exception):
    """Raised when the target service does not exist and ``create`` was not requested."""


class DeploymentFailedError(Exception):
    """Raised when a rollout ends in DEPLOYMENT_FAILED. Carries the deployment and a UI hint."""

    def __init__(self, deployment: Deployment, ui_hint: str) -> None:
        super().__init__(
            f"Deployment '{deployment.name}' failed to roll out (status: {deployment.status.value}). {ui_hint}"
        )
        self.deployment = deployment
        self.ui_hint = ui_hint


class DeploymentService:
    """Transforms a local pipeline file and deploys it to a service deployment."""

    def __init__(self, api: DeepsetCloudAPI, workspace_name: Optional[str] = None) -> None:
        """Initialize the service.

        :param api: An initialized DeepsetCloudAPI instance.
        :param workspace_name: Workspace to deploy into. Falls back to the configured default.
        """
        self._api = api
        self._deployments = DeploymentsAPI(api)
        self._workspace_name = workspace_name or DEFAULT_WORKSPACE_NAME

    @classmethod
    async def factory(cls, config: CommonConfig, workspace_name: Optional[str] = None) -> "DeploymentService":
        """Create a DeploymentService with a managed API client."""
        async with DeepsetCloudAPI.factory(config) as api:
            return cls(api, workspace_name)

    async def deploy(  # pylint: disable=too-many-arguments
        self,
        target: Path,
        service_name: str,
        *,
        activate: bool = False,
        create: bool = False,
        create_options: Optional[CreateOptions] = None,
        entrypoint: Optional[str] = None,
        inputs: Optional[dict] = None,
        outputs: Optional[dict] = None,
        io_resolver: Optional[Callable[[dict], Tuple[dict, dict]]] = None,
        python_executable: Optional[str] = None,
        timeout_s: float = DEFAULT_ACTIVATION_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        on_status: Optional[Callable[[DeploymentStatus], None]] = None,
    ) -> DeployResult:
        """Transform ``target`` and push it as a new revision of ``service_name``.

        :param target: Path to the pipeline ``.py`` file.
        :param service_name: Name of the target service deployment.
        :param activate: If True, activate the new revision and poll until it is live or fails.
        :param create: If True, create the service when it does not exist.
        :param create_options: Sizing options used when creating the service.
        :param entrypoint: Name of the pipeline instance/factory when the file is ambiguous.
        :param inputs: Optional explicit pipeline inputs (overrides inference).
        :param outputs: Optional explicit pipeline outputs (overrides inference).
        :param io_resolver: Optional callback invoked with the extraction bundle when inputs or outputs
            could not be inferred; returns ``(inputs, outputs)`` dicts to use (empty means "leave unset").
        :param python_executable: Interpreter used to load the pipeline (defaults to an auto-detected venv).
        :param timeout_s: Max seconds to poll for activation before detaching.
        :param poll_interval_s: Seconds between activation polls.
        :param on_status: Optional callback invoked with the deployment status on each poll.
        :raises ServiceNotFoundError: If the service is missing and ``create`` is False.
        :raises DeploymentFailedError: If ``activate`` is set and the rollout fails.
        :return: A :class:`DeployResult`.
        """
        config_yaml = self.build_config_yaml(
            target,
            entrypoint=entrypoint,
            inputs=inputs,
            outputs=outputs,
            io_resolver=io_resolver,
            python_executable=python_executable,
        )

        deployment = await self._resolve_or_create(service_name, create, create_options)

        revision = await self._deployments.push_revision(
            self._workspace_name, deployment.deployment_id, config_yaml
        )
        logger.info("Pushed deployment revision.", service=service_name, revision_id=str(revision.revision_id))

        if not activate:
            return DeployResult(deployment=deployment, revision=revision, activated=False, timed_out=False)

        deployment = await self._deployments.activate_revision(
            self._workspace_name, deployment.deployment_id, revision.revision_id
        )
        deployment, timed_out = await self._poll_until_settled(
            deployment.deployment_id, timeout_s, poll_interval_s, on_status
        )

        if deployment.status in _FAILURE_STATUSES:
            raise DeploymentFailedError(deployment, self._ui_hint(service_name))

        return DeployResult(deployment=deployment, revision=revision, activated=True, timed_out=timed_out)

    async def get_service_status(self, service_name: str) -> Deployment:
        """Return the current deployment (with live runtime status) for ``service_name``.

        :raises ServiceNotFoundError: If no service with that name exists.
        """
        deployment = await self._deployments.find_by_name(self._workspace_name, service_name)
        if deployment is None:
            raise ServiceNotFoundError(f"No service deployment named '{service_name}' in workspace '{self._workspace_name}'.")
        return await self._deployments.get_deployment(self._workspace_name, deployment.deployment_id)

    async def create_shared_prototype(
        self, service_name: str, options: Optional[ShareOptions] = None
    ) -> SharedPrototype:
        """Create a shared prototype (a shareable chat UI link) for a deployed service.

        The link opens a chat UI backed by the deployed service. The service must exist; its chat
        only returns results once a revision has been activated and rolled out.

        :param service_name: Name of the deployed service to share.
        :param options: Share options (expiration, login requirement, exposed inputs/outputs).
            Defaults to a 30-day, login-required link.
        :raises FailedToCreateSharedPrototypeError: If the shared prototype could not be created.
        :return: The created shared prototype, including its shareable ``link``.
        """
        options = options or ShareOptions()
        expiration_date = (datetime.now(timezone.utc) + timedelta(days=options.expiration_days)).isoformat()
        prototypes = SharedPrototypesAPI(self._api)
        return await prototypes.create(
            self._workspace_name,
            service_name=service_name,
            expiration_date=expiration_date,
            login_required=options.login_required,
            description=options.description,
            show_metadata_filters=options.show_metadata_filters,
            show_files=options.show_files,
            file_upload_enabled=options.file_upload_enabled,
            runtime_params_enabled=options.runtime_params_enabled,
        )

    # ------------------------------------------------------------------ #
    def build_config_yaml(
        self,
        target: Path,
        *,
        entrypoint: Optional[str] = None,
        inputs: Optional[dict] = None,
        outputs: Optional[dict] = None,
        io_resolver: Optional[Callable[[dict], Tuple[dict, dict]]] = None,
        python_executable: Optional[str] = None,
    ) -> str:
        """Transform ``target`` into deployable YAML without any API calls (used for --dry-run too).

        The pipeline is loaded in a subprocess using ``python_executable`` (or an auto-detected venv),
        so this environment does not need the pipeline's dependencies installed.

        Explicit ``inputs``/``outputs`` win; otherwise inferred values are used. When ``io_resolver`` is
        provided and either side is still empty, it is called with the extraction bundle to obtain them
        (this is how the CLI prompts the user to set the inputs/outputs the shared prototype needs).
        """
        extraction = pipeline_transform.extract_via_subprocess(target, entrypoint, python_executable)

        resolved_inputs = inputs if inputs is not None else extraction.get("inferred_inputs") or {}
        resolved_outputs = outputs if outputs is not None else extraction.get("inferred_outputs") or {}
        if io_resolver is not None and (not resolved_inputs or not resolved_outputs):
            new_inputs, new_outputs = io_resolver(extraction)
            if new_inputs:
                resolved_inputs = new_inputs
            if new_outputs:
                resolved_outputs = new_outputs

        return pipeline_transform.render_config_yaml(
            extraction,
            inputs=resolved_inputs or None,
            outputs=resolved_outputs or None,
        )

    async def _resolve_or_create(
        self,
        service_name: str,
        create: bool,
        create_options: Optional[CreateOptions],
    ) -> Deployment:
        deployment = await self._deployments.find_by_name(self._workspace_name, service_name)
        if deployment is not None:
            return deployment
        if not create:
            raise ServiceNotFoundError(
                f"No service deployment named '{service_name}' in workspace '{self._workspace_name}'. "
                "Pass create=True (or --create) to create it."
            )
        options = create_options or CreateOptions()
        logger.info("Creating service deployment.", service=service_name)
        return await self._deployments.create_deployment(
            self._workspace_name,
            name=service_name,
            service_level=options.service_level,
            idle_timeout_in_seconds=options.idle_timeout_in_seconds,
            min_query_replica_count=options.min_query_replica_count,
            max_query_replica_count=options.max_query_replica_count,
            cpu_limit=options.cpu_limit,
            memory_limit=options.memory_limit,
            gpu_limit_gigabyte=options.gpu_limit_gigabyte,
        )

    async def _poll_until_settled(
        self,
        deployment_id: UUID,
        timeout_s: float,
        poll_interval_s: float,
        on_status: Optional[Callable[[DeploymentStatus], None]],
    ) -> Tuple[Deployment, bool]:
        """Poll the deployment until it succeeds, fails, or the timeout elapses.

        Returns ``(deployment, timed_out)``. Time is measured by accumulating the poll interval so the
        method stays deterministic under tests (no wall-clock reads).
        """
        elapsed = 0.0
        deployment = await self._deployments.get_deployment(self._workspace_name, deployment_id)
        while True:
            if on_status is not None:
                on_status(deployment.status)
            if deployment.status in _SUCCESS_STATUSES or deployment.status in _FAILURE_STATUSES:
                return deployment, False
            if elapsed >= timeout_s:
                return deployment, True
            await asyncio.sleep(poll_interval_s)
            elapsed += poll_interval_s
            deployment = await self._deployments.get_deployment(self._workspace_name, deployment_id)

    def _ui_hint(self, service_name: str) -> str:
        return (
            f"Open the service '{service_name}' in the deepset AI Platform to inspect the deployment logs "
            "(the API does not expose a failure reason)."
        )
