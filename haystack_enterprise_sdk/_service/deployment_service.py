"""Deployment service: orchestrates transforming a local pipeline and deploying it as a service revision."""

from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import UUID

import structlog
from ruamel.yaml import YAML

from haystack_enterprise_sdk._api.config import DEFAULT_WORKSPACE_NAME, CommonConfig
from haystack_enterprise_sdk._api.deployments import (
    Deployment,
    DeploymentMode,
    DeploymentRevision,
    DeploymentsAPI,
    DeploymentServiceLevel,
    DeploymentStatus,
    PipelineValidationError,
    PipelineValidationResult,
)
from haystack_enterprise_sdk._api.haystack_enterprise_api import HaystackEnterpriseAPI
from haystack_enterprise_sdk._api.pipeline_run import (
    DEFAULT_RUN_RETRIES,
    HaystackRunAPI,
    OnRetry,
    build_run_inputs,
)
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

# Seconds allowed for the best-effort git lookups behind the default revision comment.
_GIT_TIMEOUT_S = 3.0

# Commit-page path template per known hosting provider. A self-hosted GitHub/GitLab is not
# recognizable from the remote URL alone, so anything unlisted gets no link rather than a wrong one.
_COMMIT_URL_TEMPLATES = {
    "github.com": "https://{host}/{path}/commit/{sha}",
    "gitlab.com": "https://{host}/{path}/-/commit/{sha}",
    "bitbucket.org": "https://{host}/{path}/commits/{sha}",
}


def _git(target_dir: Path, *args: str) -> Optional[str]:
    """Run a read-only git command in ``target_dir`` and return its stripped stdout.

    :param target_dir: Directory to run git in.
    :param args: Arguments passed to git.
    :return: The command output, or None if git is unavailable or the command failed.
    """
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "-C", str(target_dir), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        # No git binary, an unreadable directory, or a hung call: the comment just loses detail.
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _commit_url(remote: str, sha: str) -> Optional[str]:
    """Build a web URL for ``sha`` from a git remote URL.

    Handles ``git@host:org/repo.git``, ``ssh://git@host/org/repo.git`` and ``https://host/org/repo.git``.

    :param remote: The remote URL as reported by git.
    :param sha: Full commit sha.
    :return: A browsable commit URL, or None for an unrecognized host.
    """
    match = re.match(r"^(?:[\w.+-]+://)?(?:[^@/]+@)?([^/:]+)[:/](.+?)(?:\.git)?/?$", remote.strip())
    if match is None:
        return None
    host, path = match.group(1).lower(), match.group(2)
    template = _COMMIT_URL_TEMPLATES.get(host)
    if template is None:
        return None
    return template.format(host=host, path=path, sha=sha)


def default_revision_comment(target: Path) -> str:
    """Build the comment used when the caller does not supply one.

    Adds the current git branch, commit and a link to the commit page when the pipeline file sits in a
    git repository with a recognized remote. Every git lookup is best-effort: the comment degrades to
    branch@sha, and then to the bare CLI message, rather than failing the deploy.

    :param target: Path to the pipeline file being deployed.
    :return: A single-line comment.
    """
    base = f"Deployed {target.name} via haystack-enterprise CLI"

    target_dir = target.parent if target.parent != Path("") else Path(".")
    sha = _git(target_dir, "rev-parse", "HEAD")
    if sha is None:
        return base

    branch = _git(target_dir, "rev-parse", "--abbrev-ref", "HEAD")
    # A detached HEAD reports the branch as "HEAD", which says nothing: show the sha alone.
    ref = f"{branch}@{sha[:7]}" if branch and branch != "HEAD" else sha[:7]
    comment = f"{base} ({ref})"

    remote = _git(target_dir, "remote", "get-url", "origin")
    url = _commit_url(remote, sha) if remote else None
    # Appended as a bare URL, not markdown: the platform stores the comment as a plain string, and
    # unrendered markdown would show up as literal brackets.
    return f"{comment} {url}" if url else comment


@dataclass
class CreateOptions:
    """Options used when ``create=True`` creates the service.

    The service is created serverless unless ``deployment_mode`` says otherwise. A serverless service
    provisions no workload, so the sizing fields are meaningless for it and passing them is rejected
    rather than silently dropped.
    """

    deployment_mode: DeploymentMode = DeploymentMode.SERVERLESS
    service_level: Optional[DeploymentServiceLevel] = None
    idle_timeout_in_seconds: Optional[int] = None
    min_query_replica_count: Optional[int] = None
    max_query_replica_count: Optional[int] = None
    cpu_limit: Optional[str] = None
    memory_limit: Optional[str] = None
    gpu_limit_gigabyte: Optional[int] = None

    _SIZING_FIELDS = (
        "service_level",
        "idle_timeout_in_seconds",
        "min_query_replica_count",
        "max_query_replica_count",
        "cpu_limit",
        "memory_limit",
        "gpu_limit_gigabyte",
    )

    def __post_init__(self) -> None:
        """Reject sizing options that the requested deployment mode cannot honour.

        :raises ValueError: If sizing options are set on a serverless deployment.
        """
        if self.deployment_mode is not DeploymentMode.SERVERLESS:
            return
        set_fields = [field for field in self._SIZING_FIELDS if getattr(self, field) is not None]
        if set_fields:
            raise ValueError(
                f"Sizing options {', '.join(set_fields)} only apply to managed deployments. "
                f"Pass deployment_mode=DeploymentMode.MANAGED (or --managed) to size the service."
            )


@dataclass
class DeployResult:
    """Outcome of a deploy call."""

    deployment: Deployment
    revision: DeploymentRevision
    activated: bool
    timed_out: bool

    @property
    def is_deployed(self) -> bool:
        """Whether the service is serving the revision.

        A serverless deployment has no rollout to finish and its status never settles into a deployed
        state, so activating the revision is what makes it serve; a managed deployment has to reach a
        deployed (running or idle) status.
        """
        if self.timed_out:
            return False
        if self.deployment.deployment_mode is DeploymentMode.SERVERLESS:
            return self.activated
        return self.deployment.status in _SUCCESS_STATUSES


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
        comment: Optional[str] = None,
        entrypoint: Optional[str] = None,
        inputs: Optional[dict] = None,
        outputs: Optional[dict] = None,
        settings: Optional[pipeline_transform.PipelineSettings] = None,
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
        :param create_options: Options used when creating the service. Defaults to a serverless service.
        :param comment: Comment stored on the new revision. The platform requires one; when omitted it
            is generated by :func:`default_revision_comment`.
        :param entrypoint: Name of the pipeline instance/factory when the file is ambiguous.
        :param inputs: Optional explicit pipeline inputs (overrides inference).
        :param outputs: Optional explicit pipeline outputs (overrides inference).
        :param settings: Top-level ``config_yaml`` keys to declare — output type, session storage,
            dependency pins, plus any key this SDK has no field for. See
            :class:`pipeline_transform.PipelineSettings`.
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
            settings=settings,
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

        revision = await self._deployments.push_revision(
            self._workspace_name,
            deployment.deployment_id,
            config_yaml,
            comment or default_revision_comment(target),
        )
        logger.info("Pushed deployment revision.", service=service_name, revision_id=str(revision.revision_id))

        if not activate:
            return DeployResult(deployment=deployment, revision=revision, activated=False, timed_out=False)

        deployment = await self._deployments.activate_revision(
            self._workspace_name, deployment.deployment_id, revision.revision_id
        )

        if deployment.deployment_mode is DeploymentMode.SERVERLESS:
            # Serverless provisions no workload, so there is no rollout to observe and no terminal
            # status to wait for: the activated revision is what gets run per request. Polling here
            # would only burn the timeout on a status that never settles.
            logger.info("Activated serverless revision; no rollout to wait for.", service=service_name)
            return DeployResult(deployment=deployment, revision=revision, activated=True, timed_out=False)

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
        settings: Optional[pipeline_transform.PipelineSettings] = None,
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
        :param settings: Top-level ``config_yaml`` keys to declare — output type, session storage,
            dependency pins, plus any key this SDK has no field for. See
            :class:`pipeline_transform.PipelineSettings`.
        :param io_resolver: Optional callback that gets the final say on the resolved inputs/outputs.
        :param python_executable: Interpreter used to load the pipeline (defaults to an auto-detected venv).
        :return: The validation result (issues split into errors/warnings).
        """
        config_yaml = pipeline_transform.build_config_yaml(
            target,
            entrypoint=entrypoint,
            inputs=inputs,
            outputs=outputs,
            settings=settings,
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
        named_inputs: Optional[Dict[str, Any]] = None,
        extra_inputs: Optional[Dict[str, Dict[str, Any]]] = None,
        include_outputs_from: Optional[List[str]] = None,
        retries: int = DEFAULT_RUN_RETRIES,
        on_retry: Optional[OnRetry] = None,
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
        :param named_inputs: Values for input keys beyond ``query``/``filters``/``files``, each routed
            through the same ``inputs:`` mapping (see :func:`build_run_inputs`).
        :param extra_inputs: Explicit ``{component: {socket: value}}`` run inputs, merged last (wins).
        :param include_outputs_from: Component names whose outputs to include (defaults to all).
        :param retries: Number of retry attempts after a transient failure. ``0`` disables retrying.
        :param on_retry: Optional callback invoked as ``(next_attempt, total_attempts, reason)``
            before each backoff sleep.
        :raises PipelineRunError: If the run fails or nothing could be mapped to any input.
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
        # The sandbox run endpoint executes the config in place: it installs nothing, has no search
        # session, and renders no Playground result, so every key that describes a deployed revision is
        # meaningless here and is dropped rather than left to interfere with sandbox execution.
        for key in pipeline_transform.DEPLOY_ONLY_KEYS:
            pipeline_config.pop(key, None)
        run_inputs = build_run_inputs(
            pipeline_config,
            query=query,
            filters=filters,
            named_inputs=named_inputs,
            extra_inputs=extra_inputs,
        )
        return await HaystackRunAPI(self._api).run_pipeline(
            self._workspace_name,
            pipeline_config=pipeline_config,
            inputs=run_inputs,
            include_outputs_from=include_outputs_from,
            retries=retries,
            on_retry=on_retry,
        )

    async def find_service(self, service_name: str) -> Optional[Deployment]:
        """Return the service deployment named ``service_name``, or ``None`` if the workspace has none.

        :param service_name: Name of the service deployment to look up.
        :return: The matching deployment, or ``None``.
        """
        return await self._deployments.find_by_name(self._workspace_name, service_name)

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
                "Pass create=True (or drop --no-create) to create it."
            )
        options = create_options or CreateOptions()
        logger.info("Creating service deployment.", service=service_name, mode=options.deployment_mode.value)
        return await self._deployments.create_deployment(
            self._workspace_name,
            name=service_name,
            deployment_mode=options.deployment_mode,
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
