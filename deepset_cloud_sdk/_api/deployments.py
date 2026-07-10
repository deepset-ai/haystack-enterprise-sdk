"""Service deployments API for deepset AI Platform.

Thin async client over :class:`DeepsetCloudAPI` for the (workspace-scoped) deployment endpoints:
list/create/get deployments, push and activate revisions, and read the activity log.

Note: these endpoints are internal (not in the public OpenAPI schema) and there is no
"get deployment by name" endpoint — :meth:`DeploymentsAPI.find_by_name` resolves a name by paging the
list endpoint and matching client-side.
"""

import enum
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import UUID

import structlog
from httpx import codes

from deepset_cloud_sdk._api.deepset_cloud_api import DeepsetCloudAPI

logger = structlog.get_logger(__name__)


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


@dataclass
class Deployment:
    """A service deployment."""

    deployment_id: UUID
    name: str
    status: DeploymentStatus
    service_level: DeploymentServiceLevel
    active_revision_id: Optional[UUID]
    pending_revision_id: Optional[UUID]

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


class DeploymentNotFoundError(Exception):
    """Raised when a deployment cannot be found by name."""


class FailedToCreateDeploymentError(Exception):
    """Raised when a deployment could not be created."""


class FailedToPushRevisionError(Exception):
    """Raised when a deployment revision could not be pushed."""


class FailedToActivateRevisionError(Exception):
    """Raised when a deployment revision could not be activated."""


class DeploymentsAPI:
    """Service deployments API for deepset AI Platform."""

    _ENDPOINT = "deployments"

    def __init__(self, deepset_cloud_api: DeepsetCloudAPI) -> None:
        """Create a DeploymentsAPI object.

        :param deepset_cloud_api: An initialized DeepsetCloudAPI instance.
        """
        self._deepset_cloud_api = deepset_cloud_api

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
        response = await self._deepset_cloud_api.get(
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

    async def create_deployment(
        self,
        workspace_name: str,
        name: str,
        service_level: Optional[DeploymentServiceLevel] = None,
        idle_timeout_in_seconds: Optional[int] = None,
        min_query_replica_count: Optional[int] = None,
        max_query_replica_count: Optional[int] = None,
        cpu_limit: Optional[str] = None,
        memory_limit: Optional[str] = None,
        gpu_limit_gigabyte: Optional[int] = None,
    ) -> Deployment:
        """Create a service deployment. Sizing defaults to the Development tier unless overridden.

        :param workspace_name: Name of the workspace.
        :param name: Deployment name.
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
        payload: Dict[str, Any] = {"name": name}
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

        response = await self._deepset_cloud_api.post(
            workspace_name=workspace_name,
            endpoint=self._ENDPOINT,
            json=payload,
        )
        if response.status_code != codes.CREATED:
            logger.error("Failed to create deployment.", status_code=response.status_code, body=response.text)
            raise FailedToCreateDeploymentError(
                f"Failed to create deployment '{name}'. Status code: {response.status_code}. {response.text}"
            )
        return Deployment.from_response(response.json())

    async def get_deployment(self, workspace_name: str, deployment_id: UUID) -> Deployment:
        """Get a deployment by id. The runtime status is reconciled from the operator on the server.

        :param workspace_name: Name of the workspace.
        :param deployment_id: Deployment id.
        :return: The deployment with its current runtime status.
        """
        response = await self._deepset_cloud_api.get(
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
        response = await self._deepset_cloud_api.post(
            workspace_name=workspace_name,
            endpoint=f"{self._ENDPOINT}/{deployment_id}/revisions",
            json={"config_yaml": config_yaml},
        )
        if response.status_code != codes.CREATED:
            logger.error("Failed to push revision.", status_code=response.status_code, body=response.text)
            raise FailedToPushRevisionError(
                f"Failed to push a revision to deployment '{deployment_id}'. "
                f"Status code: {response.status_code}. {response.text}"
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
        response = await self._deepset_cloud_api.post(
            workspace_name=workspace_name,
            endpoint=f"{self._ENDPOINT}/{deployment_id}/revisions/{revision_id}/activate",
        )
        if response.status_code != codes.OK:
            logger.error("Failed to activate revision.", status_code=response.status_code, body=response.text)
            raise FailedToActivateRevisionError(
                f"Failed to activate revision '{revision_id}'. "
                f"Status code: {response.status_code}. {response.text}"
            )
        return Deployment.from_response(response.json())

    async def list_activity(self, workspace_name: str, deployment_id: UUID) -> List[Dict[str, Any]]:
        """Return the deployment's lifecycle activity events (creation/activation/deactivation).

        These events do NOT carry failure reasons; the failure signal is the deployment/revision status.

        :param workspace_name: Name of the workspace.
        :param deployment_id: Deployment id.
        :return: The list of raw event bodies.
        """
        response = await self._deepset_cloud_api.get(
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


def _optional_uuid(value: Optional[str]) -> Optional[UUID]:
    return UUID(value) if value else None


def _enum_or_default(enum_cls: type, value: Any, default: Any) -> Any:
    """Return ``enum_cls(value)`` or ``default`` if ``value`` is missing/unknown (logs on unknown)."""
    if value is None:
        return default
    try:
        return enum_cls(value)
    except ValueError:
        logger.warning("Unknown %s value '%s'; defaulting to '%s'.", enum_cls.__name__, value, default.value)
        return default
