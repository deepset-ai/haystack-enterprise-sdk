"""Service secrets API for Haystack Enterprise Platform.

Thin async client over :class:`HaystackEnterpriseAPI` for a service's own secrets
(``/api/v2/workspaces/{workspace_id}/deployments/{deployment_id}/secrets``). Service secrets override
workspace and organization secrets of the same name and reach the service as environment variables on
every request, so a change applies without a redeploy.

The route is keyed by workspace id, not name; :meth:`ServiceSecretsAPI.get_workspace_id` resolves it.
"""

from typing import Dict
from uuid import UUID

import structlog
from httpx import codes

from haystack_enterprise_sdk._api.config import API_VERSION_PATH
from haystack_enterprise_sdk._api.haystack_enterprise_api import (
    HaystackEnterpriseAPI,
    raise_for_unexpected_status,
)

logger = structlog.get_logger(__name__)


class FailedToSetServiceSecretError(Exception):
    """Raised when a service secret could not be read, created, or updated."""


class ServiceSecretsAPI:
    """Service secrets API for Haystack Enterprise Platform."""

    def __init__(self, haystack_enterprise_api: HaystackEnterpriseAPI) -> None:
        """Create a ServiceSecretsAPI object.

        :param haystack_enterprise_api: An initialized HaystackEnterpriseAPI instance.
        """
        self._api = haystack_enterprise_api

    async def get_workspace_id(self, workspace_name: str) -> UUID:
        """Resolve a workspace name to its id.

        :param workspace_name: Name of the workspace.
        :return: The workspace id.
        """
        response = await self._api.request("GET", f"{API_VERSION_PATH}/workspaces/{workspace_name}")
        raise_for_unexpected_status(
            response, (codes.OK,), FailedToSetServiceSecretError, f"Failed to look up workspace '{workspace_name}'."
        )
        return UUID(response.json()["workspace_id"])

    async def list_names(self, workspace_id: UUID, deployment_id: UUID) -> Dict[str, UUID]:
        """List the service's secrets.

        :param workspace_id: Workspace id.
        :param deployment_id: Service (deployment) id.
        :return: Secret name to secret id. Values are never returned by the platform.
        """
        names: Dict[str, UUID] = {}
        page_number = 1
        while True:
            response = await self._api.request(
                "GET", self._path(workspace_id, deployment_id), params={"limit": 100, "page_number": page_number}
            )
            raise_for_unexpected_status(
                response, (codes.OK,), FailedToSetServiceSecretError, "Failed to list service secrets."
            )
            body = response.json()
            names.update({item["name"]: UUID(item["secret_id"]) for item in body.get("data", [])})
            if not body.get("has_more"):
                return names
            page_number += 1

    async def create(self, workspace_id: UUID, deployment_id: UUID, name: str, value: str) -> None:
        """Create a service secret.

        :param workspace_id: Workspace id.
        :param deployment_id: Service (deployment) id.
        :param name: Secret name, i.e. the environment variable name.
        :param value: Secret value.
        """
        response = await self._api.request(
            "POST", self._path(workspace_id, deployment_id), json={"name": name, "secret": value}
        )
        raise_for_unexpected_status(
            response, (codes.CREATED,), FailedToSetServiceSecretError, f"Failed to create service secret '{name}'."
        )

    async def update(self, workspace_id: UUID, deployment_id: UUID, secret_id: UUID, name: str, value: str) -> None:
        """Update a service secret's value.

        :param workspace_id: Workspace id.
        :param deployment_id: Service (deployment) id.
        :param secret_id: Id of the existing secret.
        :param name: Secret name, i.e. the environment variable name.
        :param value: New secret value.
        """
        response = await self._api.request(
            "PUT", f"{self._path(workspace_id, deployment_id)}/{secret_id}", json={"name": name, "secret": value}
        )
        raise_for_unexpected_status(
            response, (codes.NO_CONTENT,), FailedToSetServiceSecretError, f"Failed to update service secret '{name}'."
        )

    @staticmethod
    def _path(workspace_id: UUID, deployment_id: UUID) -> str:
        return f"api/v2/workspaces/{workspace_id}/deployments/{deployment_id}/secrets"
