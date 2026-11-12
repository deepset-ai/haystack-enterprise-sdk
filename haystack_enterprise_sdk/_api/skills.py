"""Skill repos API for Haystack Enterprise Platform.

Thin async client over ``GET /api/v2/skills/repos``: the organization's own skill repos (connected and
managed). Each repo's ``id`` is what a pipeline's skill reference carries as ``source_id``.
"""

from typing import Dict

from httpx import codes

from haystack_enterprise_sdk._api.haystack_enterprise_api import (
    HaystackEnterpriseAPI,
    raise_for_unexpected_status,
)


class FailedToListSkillReposError(Exception):
    """Raised when the organization's skill repos could not be listed."""


class SkillReposAPI:
    """Skill repos API for Haystack Enterprise Platform."""

    def __init__(self, haystack_enterprise_api: HaystackEnterpriseAPI) -> None:
        """Create a SkillReposAPI object.

        :param haystack_enterprise_api: An initialized HaystackEnterpriseAPI instance.
        """
        self._api = haystack_enterprise_api

    async def list_repo_ids(self) -> Dict[str, str]:
        """List the organization's skill repos.

        :return: Repository (``owner/name`` as the platform stores it) to its connection id.
        """
        response = await self._api.request("GET", "api/v2/skills/repos")
        raise_for_unexpected_status(
            response, (codes.OK,), FailedToListSkillReposError, "Failed to list the organization's skill repos."
        )
        return {repo["repository"]: repo["id"] for repo in response.json()}
