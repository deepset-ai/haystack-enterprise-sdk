"""Shared prototypes API for deepset AI Platform.

Thin async client over :class:`DeepsetCloudAPI` for the (workspace-scoped) ``shared_prototypes``
endpoint. A shared prototype is a shareable link that opens a chat UI for a deployed service.

This replicates the frontend's service-share call
(``POST /api/v1/workspaces/{workspace}/shared_prototypes`` with ``type: "service"``) and returns
the ``link`` field the platform generates.

Note: this endpoint is internal (not in the public OpenAPI schema).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from uuid import UUID

import structlog
from httpx import codes

from deepset_cloud_sdk._api.deepset_cloud_api import DeepsetCloudAPI

logger = structlog.get_logger(__name__)


@dataclass
class SharedPrototype:
    """A shared prototype: a shareable link that opens a chat UI for a deployed service."""

    shared_prototype_id: UUID
    link: str
    expiration_date: str
    is_revoked: bool
    service_names: List[str] = field(default_factory=list)

    @classmethod
    def from_response(cls, body: Dict[str, Any]) -> "SharedPrototype":
        """Build a :class:`SharedPrototype` from a shared-prototype response body.

        Tolerant of a missing ``service_names`` (falls back to the legacy ``shared_services``/
        ``service_name`` shapes) so minor server-side schema drift degrades gracefully.
        """
        service_names = body.get("service_names")
        if not service_names:
            shared_services = body.get("shared_services") or []
            service_names = [s["name"] for s in shared_services if isinstance(s, dict) and s.get("name")]
            if not service_names and body.get("service_name"):
                service_names = [body["service_name"]]
        return cls(
            shared_prototype_id=UUID(body["shared_prototype_id"]),
            link=body.get("link", ""),
            expiration_date=body.get("expiration_date", ""),
            is_revoked=bool(body.get("is_revoked", False)),
            service_names=list(service_names),
        )


class FailedToCreateSharedPrototypeError(Exception):
    """Raised when a shared prototype could not be created."""


class SharedPrototypesAPI:
    """Shared prototypes API for deepset AI Platform."""

    _ENDPOINT = "shared_prototypes"

    def __init__(self, deepset_cloud_api: DeepsetCloudAPI) -> None:
        """Create a SharedPrototypesAPI object.

        :param deepset_cloud_api: An initialized DeepsetCloudAPI instance.
        """
        self._deepset_cloud_api = deepset_cloud_api

    async def create(
        self,
        workspace_name: str,
        *,
        service_name: str,
        expiration_date: str,
        login_required: bool = True,
        description: Optional[str] = None,
        show_metadata_filters: bool = False,
        show_files: bool = False,
        file_upload_enabled: bool = False,
        runtime_params_enabled: bool = False,
    ) -> SharedPrototype:
        """Create a shared prototype (chat UI link) for a deployed service.

        :param workspace_name: Name of the workspace.
        :param service_name: Name of the deployed service to share.
        :param expiration_date: ISO 8601 timestamp when the link expires.
        :param login_required: Whether recipients must log in to open the link.
        :param description: Optional description shown in the chat UI.
        :param show_metadata_filters: Whether to expose metadata filter inputs.
        :param show_files: Whether to expose the documents/files output panel.
        :param file_upload_enabled: Whether visitors can attach files with the query.
        :param runtime_params_enabled: Whether to expose runtime query params inputs.
        :raises FailedToCreateSharedPrototypeError: If the shared prototype could not be created.
        :return: The created shared prototype, including its shareable ``link``.
        """
        payload: Dict[str, Any] = {
            "type": "service",
            "service_names": [service_name],
            "expiration_date": expiration_date,
            "show_metadata_filters": show_metadata_filters,
            "show_files": show_files,
            "file_upload_enabled": file_upload_enabled,
            "runtime_params_enabled": runtime_params_enabled,
            "login_required": login_required,
        }
        if description is not None:
            payload["description"] = description

        response = await self._deepset_cloud_api.post(
            workspace_name=workspace_name,
            endpoint=self._ENDPOINT,
            json=payload,
        )
        if response.status_code not in (codes.CREATED, codes.OK):
            logger.error("Failed to create shared prototype.", status_code=response.status_code, body=response.text)
            raise FailedToCreateSharedPrototypeError(
                f"Failed to create a shared prototype for service '{service_name}'. "
                f"Status code: {response.status_code}. {response.text}"
            )
        return SharedPrototype.from_response(response.json())
