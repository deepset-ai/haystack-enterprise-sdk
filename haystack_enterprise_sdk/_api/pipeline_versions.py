"""Pipeline versions API for Haystack Enterprise Platform.

Thin async client over ``/workspaces/{workspace}/pipelines`` and its ``/versions`` routes, used by a deploy
that records its YAML as a version of a platform pipeline. A pipeline has at most one draft (the version the
builder edits) plus numbered, finalized versions.
"""

from typing import Any, Dict, Optional

from httpx import codes

from haystack_enterprise_sdk._api.haystack_enterprise_api import (
    HaystackEnterpriseAPI,
    raise_for_unexpected_status,
)


class FailedToPublishPipelineVersionError(Exception):
    """Raised when a pipeline or one of its versions could not be read, created, or published."""


class PipelineVersionsAPI:
    """Pipeline versions API for Haystack Enterprise Platform."""

    def __init__(self, haystack_enterprise_api: HaystackEnterpriseAPI) -> None:
        """Create a PipelineVersionsAPI object.

        :param haystack_enterprise_api: An initialized HaystackEnterpriseAPI instance.
        """
        self._api = haystack_enterprise_api

    async def pipeline_exists(self, workspace_name: str, pipeline_name: str) -> bool:
        """Check whether the pipeline exists.

        :param workspace_name: Name of the workspace.
        :param pipeline_name: Name of the pipeline.
        :return: True if it exists.
        """
        response = await self._api.get(workspace_name=workspace_name, endpoint=f"pipelines/{pipeline_name}")
        if response.status_code == codes.NOT_FOUND:
            return False
        raise_for_unexpected_status(
            response,
            (codes.OK,),
            FailedToPublishPipelineVersionError,
            f"Failed to look up pipeline '{pipeline_name}'.",
        )
        return True

    async def create_pipeline(self, workspace_name: str, pipeline_name: str) -> None:
        """Create an empty pipeline: no YAML, so it starts without any version.

        :param workspace_name: Name of the workspace.
        :param pipeline_name: Name of the pipeline.
        """
        response = await self._api.post(
            workspace_name=workspace_name, endpoint="pipelines", json={"name": pipeline_name}
        )
        raise_for_unexpected_status(
            response,
            (codes.OK, codes.CREATED),
            FailedToPublishPipelineVersionError,
            f"Failed to create pipeline '{pipeline_name}'.",
        )

    async def create_draft(
        self, workspace_name: str, pipeline_name: str, config_yaml: str, description: Optional[str]
    ) -> Dict[str, Any]:
        """Create a new draft. The platform first finalizes the pipeline's current draft, if any, as a version.

        :param workspace_name: Name of the workspace.
        :param pipeline_name: Name of the pipeline.
        :param config_yaml: YAML of the new draft.
        :param description: Description of the new version.
        :return: The created draft.
        """
        response = await self._api.post(
            workspace_name=workspace_name,
            endpoint=f"pipelines/{pipeline_name}/versions",
            json={"config_yaml": config_yaml, "is_draft": True, "description": description},
        )
        raise_for_unexpected_status(
            response,
            (codes.CREATED,),
            FailedToPublishPipelineVersionError,
            f"Failed to create a draft of pipeline '{pipeline_name}'.",
        )
        body: Dict[str, Any] = response.json()
        return body

    async def publish(self, workspace_name: str, pipeline_name: str, version_id: str) -> Dict[str, Any]:
        """Finalize a draft. The platform then opens a new draft with the same content.

        :param workspace_name: Name of the workspace.
        :param pipeline_name: Name of the pipeline.
        :param version_id: Id of the draft.
        :return: The finalized version.
        """
        response = await self._api.patch(
            workspace_name=workspace_name,
            endpoint=f"pipelines/{pipeline_name}/versions/{version_id}",
            json={"is_draft": False},
        )
        raise_for_unexpected_status(
            response,
            (codes.OK,),
            FailedToPublishPipelineVersionError,
            f"Failed to publish version '{version_id}' of pipeline '{pipeline_name}'.",
        )
        body: Dict[str, Any] = response.json()
        return body
