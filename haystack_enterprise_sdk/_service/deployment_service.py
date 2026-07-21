"""Deployment service: orchestrates transforming a local pipeline and deploying it as a service revision."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import UUID

import structlog
from ruamel.yaml import YAML

from haystack_enterprise_sdk._api.config import DEFAULT_WORKSPACE_NAME, CommonConfig
from haystack_enterprise_sdk._api.haystack_enterprise_api import HaystackEnterpriseAPI
from haystack_enterprise_sdk._api.deployments import (
    Deployment,
    DeploymentRevision,
    DeploymentsAPI,
    DeploymentServiceLevel,
    DeploymentStatus,
    PipelineValidationError,
    PipelineValidationResult,
)
from haystack_enterprise_sdk._api.pipeline_run import HaystackRunAPI, build_run_inputs
from haystack_enterprise_sdk._api.shared_prototypes import (
    SharedPrototype,
    SharedPrototypesAPI,
)
from haystack_enterprise_sdk._service import pipeline_transform

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

    @property
    def is_deployed(self) -> bool:
        """Whether the rollout finished in a deployed (running or idle) state."""
        return not self.timed_out and self.deployment.status in _SUCCESS_STATUSES


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

    def __init__(self, api: HaystackEnterpriseAPI, workspace_name: Optional[str] = None) -> None:
        """Initialize the service.

        :param api: An initialized HaystackEnterpriseAPI instance.
        :param workspace_name: Workspace to deploy into. Falls back to the configured default.
        """
        self._api = api
        self._deployments = DeploymentsAPI(api)
        self._workspace_name = workspace_name or DEFAULT_WORKSPACE_NAME

    @classmethod
    async def factory(cls, config: CommonConfig, workspace_name: Optional[str] = None) -> "DeploymentService":
        """Create a DeploymentService with a managed API client."""
        async with HaystackEnterpriseAPI.factory(config) as api:
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
        pipeline_output_type: Optional[str] = None,
        io_resolver: Optional[pipeline_transform.IoResolver] = None,
        python_executable: Optional[str] = None,
        validate: bool = True,
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
        :param pipeline_output_type: Optional platform ``pipeline_output_type`` hint for the YAML.
        :param io_resolver: Optional callback that gets the final say on the resolved inputs/outputs
            (see :func:`pipeline_transform.resolve_io`); returns ``(inputs, outputs)`` dicts to use.
        :param python_executable: Interpreter used to load the pipeline (defaults to an auto-detected venv).
        :param validate: If True (default), validate the generated YAML against the platform and abort
            on blocking (ERROR) issues before creating/pushing anything. Set False to skip the check.
        :param timeout_s: Max seconds to poll for activation before detaching.
        :param poll_interval_s: Seconds between activation polls.
        :param on_status: Optional callback invoked with the deployment status on each poll.
        :raises ServiceNotFoundError: If the service is missing and ``create`` is False.
        :raises PipelineValidationError: If ``validate`` is set and the YAML has blocking issues.
        :raises DeploymentFailedError: If ``activate`` is set and the rollout fails.
        :return: A :class:`DeployResult`.
        """
        config_yaml = pipeline_transform.build_config_yaml(
            target,
            entrypoint=entrypoint,
            inputs=inputs,
            outputs=outputs,
            pipeline_output_type=pipeline_output_type,
            io_resolver=io_resolver,
            python_executable=python_executable,
        )

        if validate:
            # Validate before resolving/creating the service so an invalid config never provisions one.
            result = await self._deployments.validate_pipeline(self._workspace_name, query_yaml=config_yaml)
            for issue in result.warnings:
                logger.warning("Pipeline validation warning.", message=issue.message, pointer=issue.json_pointer)
            if result.has_errors:
                raise PipelineValidationError(result.errors)

        deployment = await self._resolve_or_create(service_name, create, create_options)

        revision = await self._deployments.push_revision(self._workspace_name, deployment.deployment_id, config_yaml)
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

    async def validate(
        self,
        target: Path,
        *,
        entrypoint: Optional[str] = None,
        inputs: Optional[dict] = None,
        outputs: Optional[dict] = None,
        pipeline_output_type: Optional[str] = None,
        io_resolver: Optional[pipeline_transform.IoResolver] = None,
        python_executable: Optional[str] = None,
    ) -> PipelineValidationResult:
        """Transform ``target`` and validate the generated YAML against the platform without deploying.

        Runs the exact same transform path as :meth:`deploy`, so the validated YAML matches what a
        deploy would push. The result is returned as-is (no gating); the caller decides how to react.

        :param target: Path to the pipeline ``.py`` file.
        :param entrypoint: Name of the pipeline instance/factory when the file is ambiguous.
        :param inputs: Optional explicit pipeline inputs (overrides inference).
        :param outputs: Optional explicit pipeline outputs (overrides inference).
        :param pipeline_output_type: Optional platform ``pipeline_output_type`` hint for the YAML.
        :param io_resolver: Optional callback that gets the final say on the resolved inputs/outputs.
        :param python_executable: Interpreter used to load the pipeline (defaults to an auto-detected venv).
        :return: The validation result (issues split into errors/warnings).
        """
        config_yaml = pipeline_transform.build_config_yaml(
            target,
            entrypoint=entrypoint,
            inputs=inputs,
            outputs=outputs,
            pipeline_output_type=pipeline_output_type,
            io_resolver=io_resolver,
            python_executable=python_executable,
        )
        return await self._deployments.validate_pipeline(self._workspace_name, query_yaml=config_yaml)

    async def run(  # pylint: disable=too-many-arguments
        self,
        target: Path,
        *,
        entrypoint: Optional[str] = None,
        inputs: Optional[dict] = None,
        outputs: Optional[dict] = None,
        io_resolver: Optional[pipeline_transform.IoResolver] = None,
        python_executable: Optional[str] = None,
        query: Optional[str] = None,
        filters: Optional[Any] = None,
        extra_inputs: Optional[Dict[str, Dict[str, Any]]] = None,
        include_outputs_from: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Transform ``target`` and run the generated YAML in the platform sandbox, without deploying.

        Runs the exact same transform path as :meth:`deploy`, so the YAML executed matches what a
        deploy would push. The generated ``config_yaml`` is parsed back into a dict and sent, together
        with the run inputs, to the sandbox run endpoint (the same call the builder/playground makes).

        :param target: Path to the pipeline ``.py`` file.
        :param entrypoint: Name of the pipeline instance/factory when the file is ambiguous.
        :param inputs: Optional explicit pipeline inputs (overrides inference) embedded in the YAML.
        :param outputs: Optional explicit pipeline outputs (overrides inference) embedded in the YAML.
        :param io_resolver: Optional callback consulted when input/output resolution is incomplete.
        :param python_executable: Interpreter used to load the pipeline (defaults to an auto-detected venv).
        :param query: Query text routed to the sockets mapped under the ``query`` input key.
        :param filters: Optional filters routed to the ``filters`` input key.
        :param extra_inputs: Explicit ``{component: {socket: value}}`` run inputs, merged last (wins).
        :param include_outputs_from: Component names whose outputs to include (defaults to all).
        :raises PipelineRunError: If the run fails or the query cannot be mapped to any input.
        :return: The pipeline output, a dict keyed by component name.
        """
        config_yaml = pipeline_transform.build_config_yaml(
            target,
            entrypoint=entrypoint,
            inputs=inputs,
            outputs=outputs,
            io_resolver=io_resolver,
            python_executable=python_executable,
        )
        pipeline_config = dict(YAML().load(config_yaml))
        # The sandbox run endpoint executes the config in place and does not install dependencies, so a
        # ``dependencies`` block is meaningless here (it belongs to the deployed revision). Drop it for
        # the run so the pinned version can't interfere with sandbox execution.
        pipeline_config.pop("dependencies", None)
        run_inputs = build_run_inputs(
            pipeline_config,
            query=query,
            filters=filters,
            extra_inputs=extra_inputs,
        )
        return await HaystackRunAPI(self._api).run_pipeline(
            self._workspace_name,
            pipeline_config=pipeline_config,
            inputs=run_inputs,
            include_outputs_from=include_outputs_from,
        )

    async def get_service_status(self, service_name: str) -> Deployment:
        """Return the current deployment (with live runtime status) for ``service_name``.

        :raises ServiceNotFoundError: If no service with that name exists.
        """
        deployment = await self._deployments.find_by_name(self._workspace_name, service_name)
        if deployment is None:
            raise ServiceNotFoundError(
                f"No service deployment named '{service_name}' in workspace '{self._workspace_name}'."
            )
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
