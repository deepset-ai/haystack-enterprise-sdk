"""Service deployments API for deepset AI Platform.

Thin async client over :class:`HaystackEnterpriseAPI` for the (workspace-scoped) deployment endpoints:
list/create/get deployments, push and activate revisions, and read the activity log.

Note: these endpoints are internal (not in the public OpenAPI schema) and there is no
"get deployment by name" endpoint — :meth:`DeploymentsAPI.find_by_name` resolves a name by paging the
list endpoint and matching client-side.
"""

import enum
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Type, TypeVar
from uuid import UUID

import structlog
from httpx import codes

from haystack_enterprise_sdk._api.haystack_enterprise_api import (
    HaystackEnterpriseAPI,
    raise_for_unexpected_status,
)
from haystack_enterprise_sdk.models import PipelineOutputType

logger = structlog.get_logger(__name__)

_E = TypeVar("_E", bound=enum.Enum)


class DeploymentStatus(str, enum.Enum):
    """Observed runtime status of a deployment, reconciled from the operator."""

    UNDEPLOYED = "UNDEPLOYED"
    DEPLOYED = "DEPLOYED"
    DEPLOYMENT_IN_PROGRESS = "DEPLOYMENT_IN_PROGRESS"
    DEPLOYMENT_FAILED = "DEPLOYMENT_FAILED"
    IDLE = "IDLE"


class DeploymentRevisionStatus(str, enum.Enum):
    """Lifecycle status of a single deployment revision."""

    PENDING = "PENDING"
    DEPLOYING = "DEPLOYING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    INACTIVE = "INACTIVE"


class DeploymentServiceLevel(str, enum.Enum):
    """Service sizing tier for a deployment."""

    PRODUCTION = "PRODUCTION"
    DEVELOPMENT = "DEVELOPMENT"
    CUSTOM = "CUSTOM"


class DeploymentMode(str, enum.Enum):
    """How a deployment is hosted.

    ``SERVERLESS`` deployments provision no workload: the active revision's config is run ad-hoc per
    request, so the sizing options (service level, replica counts, CPU/memory/GPU, idle timeout) do not
    apply to them. ``MANAGED`` deployments run as a provisioned service and are sized by those options.
    """

    MANAGED = "MANAGED"
    SERVERLESS = "SERVERLESS"


class DeploymentSourceType(str, enum.Enum):
    """Type of artifact a deployment revision was created from.

    ``PLATFORM_PIPELINE`` revisions are built from a stored query-pipeline version and carry a
    ``source_version_id``. Revisions pushed by this SDK send inline ``config_yaml`` with no source
    version, so they are ``EXTERNAL_PIPELINE``.
    """

    PLATFORM_PIPELINE = "PLATFORM_PIPELINE"
    EXTERNAL_PIPELINE = "EXTERNAL_PIPELINE"


@dataclass
class Deployment:
    """A service deployment."""

    deployment_id: UUID
    name: str
    status: DeploymentStatus
    service_level: DeploymentServiceLevel
    active_revision_id: Optional[UUID]
    pending_revision_id: Optional[UUID]
    deployment_mode: DeploymentMode = DeploymentMode.MANAGED
    # How the platform classifies what this deployment returns. Derived server-side from the *active*
    # revision, so it stays None until a revision is activated. This is the platform's own answer to
    # "is this a chat pipeline?" -- the CLI reads it rather than guessing from components or sockets.
    output_type: Optional[PipelineOutputType] = None

    @classmethod
    def from_response(cls, body: Dict[str, Any]) -> "Deployment":
        """Build a :class:`Deployment` from a ``DeploymentResponse`` body.

        Tolerant of missing/unknown enum values so a minor server-side schema drift degrades
        gracefully (e.g. an unknown status just keeps polling) instead of crashing.
        """
        return cls(
            deployment_id=UUID(body["deployment_id"]),
            name=body["name"],
            status=_enum_or_default(DeploymentStatus, body.get("status"), DeploymentStatus.UNDEPLOYED),
            service_level=_enum_or_default(
                DeploymentServiceLevel, body.get("service_level"), DeploymentServiceLevel.DEVELOPMENT
            ),
            active_revision_id=_optional_uuid(body.get("active_revision_id")),
            pending_revision_id=_optional_uuid(body.get("pending_revision_id")),
            # A server that does not report the mode is read as managed: that is the platform default,
            # and treating an unknown deployment as managed keeps the rollout polling in place.
            deployment_mode=_enum_or_default(DeploymentMode, body.get("deployment_mode"), DeploymentMode.MANAGED),
            # The platform enum has values this SDK does not model (e.g. "unknown"), and the field is
            # absent until a revision is active, so anything unrecognized degrades to None.
            output_type=_enum_or_none(PipelineOutputType, body.get("output_type")),
        )


@dataclass
class DeploymentRevision:
    """A single revision of a deployment."""

    revision_id: UUID
    deployment_id: UUID
    status: DeploymentRevisionStatus
    config_hash: str

    @classmethod
    def from_response(cls, body: Dict[str, Any]) -> "DeploymentRevision":
        """Build a :class:`DeploymentRevision` from a ``DeploymentRevisionResponse`` body.

        ``status`` is informational here (the deploy flow tracks the deployment's status, not the
        revision's), so a missing/unknown value defaults to ``PENDING`` rather than raising.
        """
        return cls(
            revision_id=UUID(body["revision_id"]),
            deployment_id=UUID(body["deployment_id"]),
            status=_enum_or_default(DeploymentRevisionStatus, body.get("status"), DeploymentRevisionStatus.PENDING),
            config_hash=body.get("config_hash", ""),
        )


_ERROR_CATEGORY = "ERROR"
_WARNING_CATEGORY = "WARNING"


@dataclass
class PipelineValidationIssue:
    """A single issue reported by the pipeline validation endpoint."""

    category: str
    code: Optional[str]
    json_pointer: Optional[str]
    message: Optional[str]

    @classmethod
    def from_response(cls, body: Dict[str, Any]) -> "PipelineValidationIssue":
        """Build a :class:`PipelineValidationIssue` from a raw ``error_details`` item.

        Tolerant of key drift: the location may arrive as ``json_pointer``/``json_path``/``pointer``
        and the text as ``message``/``msg`` (mirrors the UI's tolerant parser). Category defaults to
        ``ERROR`` when absent, matching the platform default.
        """
        return cls(
            category=str(body.get("category") or _ERROR_CATEGORY).upper(),
            code=body.get("code"),
            json_pointer=body.get("json_pointer") or body.get("json_path") or body.get("pointer"),
            message=body.get("message") or body.get("msg"),
        )

    def __str__(self) -> str:
        """Render as ``[CATEGORY] json_pointer: message`` for readable CLI output."""
        location = f"{self.json_pointer}: " if self.json_pointer else ""
        return f"[{self.category}] {location}{self.message or ''}".rstrip()


@dataclass
class PipelineValidationResult:
    """The outcome of validating a pipeline YAML against the platform."""

    issues: List[PipelineValidationIssue]

    @property
    def errors(self) -> List[PipelineValidationIssue]:
        """Blocking issues (``category == ERROR``)."""
        return [issue for issue in self.issues if issue.category == _ERROR_CATEGORY]

    @property
    def warnings(self) -> List[PipelineValidationIssue]:
        """Non-blocking issues (``category == WARNING``)."""
        return [issue for issue in self.issues if issue.category == _WARNING_CATEGORY]

    @property
    def has_errors(self) -> bool:
        """Whether there is at least one blocking (ERROR) issue."""
        return len(self.errors) > 0

    @property
    def is_valid(self) -> bool:
        """Whether the pipeline has no blocking (ERROR) issues."""
        return not self.has_errors


class DeploymentNotFoundError(Exception):
    """Raised when a deployment cannot be found by name."""


class FailedToCreateDeploymentError(Exception):
    """Raised when a deployment could not be created."""


class FailedToPushRevisionError(Exception):
    """Raised when a deployment revision could not be pushed."""


class FailedToActivateRevisionError(Exception):
    """Raised when a deployment revision could not be activated."""


class FailedToValidatePipelineError(Exception):
    """Raised when the validation request itself failed (e.g. auth/5xx), not the pipeline config."""


class PipelineValidationError(Exception):
    """Raised when a pipeline YAML has blocking (ERROR) validation issues.

    Carries the list of :class:`PipelineValidationIssue` errors so callers can inspect them; its
    string form is a readable multi-line summary suitable for direct CLI output.
    """

    def __init__(self, errors: List[PipelineValidationIssue]) -> None:
        """Create the error from the list of blocking validation issues."""
        self.errors = errors
        count = len(errors)
        header = f"Pipeline validation failed with {count} error{'s' if count != 1 else ''}:"
        super().__init__("\n".join([header, *(f"  - {issue}" for issue in errors)]))


class DeploymentsAPI:
    """Service deployments API for deepset AI Platform."""

    _ENDPOINT = "deployments"
    # Workspace-scoped validation endpoint (the base URL already includes /workspaces/{workspace}).
    _VALIDATION_ENDPOINT = "pipeline_validations"
    # Revisions pushed by this SDK send self-contained inline YAML with no platform pipeline version
    # behind them, so their source is EXTERNAL_PIPELINE rather than the default PLATFORM_PIPELINE.
    _SOURCE_TYPE = DeploymentSourceType.EXTERNAL_PIPELINE.value

    def __init__(self, haystack_enterprise_api: HaystackEnterpriseAPI) -> None:
        """Create a DeploymentsAPI object.

        :param haystack_enterprise_api: An initialized HaystackEnterpriseAPI instance.
        """
        self._haystack_enterprise_api = haystack_enterprise_api

    async def list_deployments(
        self,
        workspace_name: str,
        limit: int = 100,
        page_number: int = 1,
    ) -> "PaginatedDeployments":
        """List a page of deployments in the workspace.

        :param workspace_name: Name of the workspace.
        :param limit: Page size.
        :param page_number: 1-based page number.
        :return: A page of deployments plus the ``has_more`` flag.
        """
        response = await self._haystack_enterprise_api.get(
            workspace_name=workspace_name,
            endpoint=self._ENDPOINT,
            params={"limit": limit, "page_number": page_number},
        )
        response.raise_for_status()
        body = response.json()
        return PaginatedDeployments(
            data=[Deployment.from_response(item) for item in body.get("data", [])],
            has_more=bool(body.get("has_more", False)),
            total=int(body.get("total", 0)),
        )

    async def find_by_name(self, workspace_name: str, name: str) -> Optional[Deployment]:
        """Resolve a deployment by name by paging the list endpoint and matching client-side.

        There is no server-side name lookup, so this pages until a match is found or the pages run out.

        :param workspace_name: Name of the workspace.
        :param name: Exact deployment name to match.
        :return: The matching deployment, or ``None`` if no deployment has that name.
        """
        page_number = 1
        while True:
            page = await self.list_deployments(workspace_name, page_number=page_number)
            for deployment in page.data:
                if deployment.name == name:
                    return deployment
            if not page.has_more or not page.data:
                return None
            page_number += 1

    async def create_deployment(  # pylint: disable=too-many-arguments
        self,
        workspace_name: str,
        name: str,
        deployment_mode: DeploymentMode = DeploymentMode.SERVERLESS,
        service_level: Optional[DeploymentServiceLevel] = None,
        idle_timeout_in_seconds: Optional[int] = None,
        min_query_replica_count: Optional[int] = None,
        max_query_replica_count: Optional[int] = None,
        cpu_limit: Optional[str] = None,
        memory_limit: Optional[str] = None,
        gpu_limit_gigabyte: Optional[int] = None,
    ) -> Deployment:
        """Create a service deployment. Serverless unless a managed mode is requested.

        The platform itself defaults to ``MANAGED``, so the mode is always sent explicitly: a service
        created through this SDK provisions no workload unless it was asked for. The sizing parameters
        only apply to ``MANAGED`` deployments.

        :param workspace_name: Name of the workspace.
        :param name: Deployment name.
        :param deployment_mode: How the deployment is hosted. Defaults to serverless.
        :param service_level: Service sizing tier. Defaults to Development server-side.
        :param idle_timeout_in_seconds: Idle timeout before scale-down.
        :param min_query_replica_count: Minimum query replicas.
        :param max_query_replica_count: Maximum query replicas.
        :param cpu_limit: CPU limit (e.g. ``"1"``).
        :param memory_limit: Memory limit (e.g. ``"2Gi"``).
        :param gpu_limit_gigabyte: GPU memory limit in gigabytes.
        :raises FailedToCreateDeploymentError: If the deployment could not be created.
        :return: The created deployment.
        """
        payload: Dict[str, Any] = {
            "name": name,
            "source_type": self._SOURCE_TYPE,
            "deployment_mode": deployment_mode.value,
        }
        if service_level is not None:
            payload["service_level"] = service_level.value
        for key, value in {
            "idle_timeout_in_seconds": idle_timeout_in_seconds,
            "min_query_replica_count": min_query_replica_count,
            "max_query_replica_count": max_query_replica_count,
            "cpu_limit": cpu_limit,
            "memory_limit": memory_limit,
            "gpu_limit_gigabyte": gpu_limit_gigabyte,
        }.items():
            if value is not None:
                payload[key] = value

        response = await self._haystack_enterprise_api.post(
            workspace_name=workspace_name,
            endpoint=self._ENDPOINT,
            json=payload,
        )
        raise_for_unexpected_status(
            response, (codes.CREATED,), FailedToCreateDeploymentError, f"Failed to create deployment '{name}'."
        )
        return Deployment.from_response(response.json())

    async def get_deployment(self, workspace_name: str, deployment_id: UUID) -> Deployment:
        """Get a deployment by id. The runtime status is reconciled from the operator on the server.

        :param workspace_name: Name of the workspace.
        :param deployment_id: Deployment id.
        :return: The deployment with its current runtime status.
        """
        response = await self._haystack_enterprise_api.get(
            workspace_name=workspace_name,
            endpoint=f"{self._ENDPOINT}/{deployment_id}",
        )
        response.raise_for_status()
        return Deployment.from_response(response.json())

    async def push_revision(
        self,
        workspace_name: str,
        deployment_id: UUID,
        config_yaml: str,
    ) -> DeploymentRevision:
        """Push a new revision from raw ``config_yaml``. The revision starts as ``PENDING``.

        :param workspace_name: Name of the workspace.
        :param deployment_id: Deployment id.
        :param config_yaml: The platform-ready pipeline YAML.
        :raises FailedToPushRevisionError: If the revision could not be created.
        :return: The created revision.
        """
        response = await self._haystack_enterprise_api.post(
            workspace_name=workspace_name,
            endpoint=f"{self._ENDPOINT}/{deployment_id}/revisions",
            json={"config_yaml": config_yaml, "source_type": self._SOURCE_TYPE},
        )
        raise_for_unexpected_status(
            response,
            (codes.CREATED,),
            FailedToPushRevisionError,
            f"Failed to push a revision to deployment '{deployment_id}'.",
        )
        return DeploymentRevision.from_response(response.json())

    async def activate_revision(
        self,
        workspace_name: str,
        deployment_id: UUID,
        revision_id: UUID,
    ) -> Deployment:
        """Activate a revision, marking it as the deployment's desired served revision.

        The server kicks off a background rollout; poll :meth:`get_deployment` to track it.

        :param workspace_name: Name of the workspace.
        :param deployment_id: Deployment id.
        :param revision_id: Revision id to activate.
        :raises FailedToActivateRevisionError: If activation was rejected.
        :return: The deployment after activation was requested.
        """
        response = await self._haystack_enterprise_api.post(
            workspace_name=workspace_name,
            endpoint=f"{self._ENDPOINT}/{deployment_id}/revisions/{revision_id}/activate",
        )
        raise_for_unexpected_status(
            response, (codes.OK,), FailedToActivateRevisionError, f"Failed to activate revision '{revision_id}'."
        )
        return Deployment.from_response(response.json())

    async def validate_pipeline(
        self,
        workspace_name: str,
        *,
        query_yaml: Optional[str] = None,
        indexing_yaml: Optional[str] = None,
        pipeline_id: Optional[str] = None,
        deepset_cloud_version: str = "v2",
    ) -> PipelineValidationResult:
        """Validate a pipeline YAML against the platform without deploying it.

        Mirrors the check the UI runs before deploying: a ``204`` means the config is valid, a ``400``
        carries the validation issues (which the caller inspects), and any other status is treated as
        a request failure. Send only the YAML fields that apply (this SDK deploys query pipelines, so
        it sends ``query_yaml``).

        :param workspace_name: Name of the workspace.
        :param query_yaml: Query-pipeline YAML to validate, if any.
        :param indexing_yaml: Indexing-pipeline YAML to validate, if any.
        :param pipeline_id: Optional pipeline id (used server-side for event tracking).
        :param deepset_cloud_version: Platform config version (defaults to ``v2``).
        :raises FailedToValidatePipelineError: If the validation request could not be completed.
        :return: The validation result (issues split into errors/warnings).
        """
        payload: Dict[str, Any] = {"deepset_cloud_version": deepset_cloud_version}
        for key, value in {
            "query_yaml": query_yaml,
            "indexing_yaml": indexing_yaml,
            "pipeline_id": pipeline_id,
        }.items():
            if value is not None:
                payload[key] = value

        response = await self._haystack_enterprise_api.post(
            workspace_name=workspace_name,
            endpoint=self._VALIDATION_ENDPOINT,
            json=payload,
        )
        if response.status_code == codes.NO_CONTENT:
            return PipelineValidationResult(issues=[])
        raise_for_unexpected_status(
            response,
            (codes.NO_CONTENT, codes.BAD_REQUEST),
            FailedToValidatePipelineError,
            "Failed to validate the pipeline.",
        )
        return PipelineValidationResult(issues=_parse_validation_issues(response.json()))

    async def list_activity(self, workspace_name: str, deployment_id: UUID) -> List[Dict[str, Any]]:
        """Return the deployment's lifecycle activity events (creation/activation/deactivation).

        These events do NOT carry failure reasons; the failure signal is the deployment/revision status.

        :param workspace_name: Name of the workspace.
        :param deployment_id: Deployment id.
        :return: The list of raw event bodies.
        """
        response = await self._haystack_enterprise_api.get(
            workspace_name=workspace_name,
            endpoint=f"{self._ENDPOINT}/{deployment_id}/activity",
        )
        response.raise_for_status()
        return list(response.json().get("data", []))


@dataclass
class PaginatedDeployments:
    """A page of deployments."""

    data: List[Deployment]
    has_more: bool
    total: int


def _parse_validation_issues(body: Any) -> List[PipelineValidationIssue]:
    """Extract validation issues from a validation error body, tolerant of key drift.

    Scans the known detail keys in the same order the UI does (``error_details`` first), and falls
    back to a single issue built from a top-level ``message`` so a schema change degrades to surfacing
    the message rather than losing it.
    """
    if not isinstance(body, dict):
        return []
    for key in ("error_details", "details", "detail", "errors"):
        items = body.get(key)
        if isinstance(items, list):
            return [PipelineValidationIssue.from_response(item) for item in items if isinstance(item, dict)]
    message = body.get("message")
    if message:
        return [PipelineValidationIssue(category=_ERROR_CATEGORY, code=None, json_pointer=None, message=str(message))]
    return []


def _optional_uuid(value: Optional[str]) -> Optional[UUID]:
    return UUID(value) if value else None


def _enum_or_none(enum_cls: Type[_E], value: Any) -> Optional[_E]:
    """Return ``enum_cls(value)`` or None if ``value`` is missing/unknown (logs on unknown)."""
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        logger.warning("Unknown enum value; ignoring.", enum=enum_cls.__name__, value=value)
        return None


def _enum_or_default(enum_cls: Type[_E], value: Any, default: _E) -> _E:
    """Return ``enum_cls(value)`` or ``default`` if ``value`` is missing/unknown (logs on unknown)."""
    if value is None:
        return default
    try:
        return enum_cls(value)
    except ValueError:
        logger.warning("Unknown enum value; using default.", enum=enum_cls.__name__, value=value, default=default.value)
        return default
