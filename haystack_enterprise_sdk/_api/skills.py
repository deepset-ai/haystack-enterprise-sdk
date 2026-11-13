"""Skill repos API for Haystack Enterprise Platform.

Thin async client over ``GET /api/v2/skills/repos``: the organization's own skill repos (connected and
managed). Each repo's ``id`` is what a pipeline's skill reference carries as ``source_id``.
"""

from typing import List, NamedTuple

from httpx import codes

from haystack_enterprise_sdk._api.haystack_enterprise_api import (
    HaystackEnterpriseAPI,
    raise_for_unexpected_status,
)


class FailedToListSkillReposError(Exception):
    """Raised when the organization's skill repos could not be listed."""


class SkillRepo(NamedTuple):
    """One of the organization's skill repos."""

    repository: str  # ``owner/name`` as the platform stores it
    provider: str  # host serving the repo: ``GITHUB`` or ``GITLAB``
    id: str  # connection id, a skill reference's ``source_id``


class SkillReposAPI:
    """Skill repos API for Haystack Enterprise Platform."""

    def __init__(self, haystack_enterprise_api: HaystackEnterpriseAPI) -> None:
        """Create a SkillReposAPI object.

        :param haystack_enterprise_api: An initialized HaystackEnterpriseAPI instance.
        """
        self._api = haystack_enterprise_api

    async def list_repos(self) -> List[SkillRepo]:
        """List the organization's skill repos.

        :return: Each repo with its host and connection id; one ``owner/name`` may appear once per host.
        """
        response = await self._api.request("GET", "api/v2/skills/repos")
        raise_for_unexpected_status(
            response, (codes.OK,), FailedToListSkillReposError, "Failed to list the organization's skill repos."
        )
        return [SkillRepo(repo["repository"], repo["provider"], repo["id"]) for repo in response.json()]
