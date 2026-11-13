"""Tests for the skill repos API client."""

from unittest.mock import Mock

import pytest
from httpx import Request, Response, codes

from haystack_enterprise_sdk._api.skills import FailedToListSkillReposError, SkillRepo, SkillReposAPI

_REQUEST = Request("GET", "https://test.deepset.ai")


@pytest.mark.asyncio
class TestSkillReposAPI:
    async def test_list_repos(self, mocked_haystack_enterprise_api: Mock) -> None:
        mocked_haystack_enterprise_api.request.return_value = Response(
            codes.OK,
            request=_REQUEST,
            json=[{"id": "uuid-1", "repository": "acme/skills", "source": "ORG", "provider": "GITHUB"}],
        )

        assert await SkillReposAPI(mocked_haystack_enterprise_api).list_repos() == [
            SkillRepo("acme/skills", "GITHUB", "uuid-1")
        ]
        mocked_haystack_enterprise_api.request.assert_awaited_once_with("GET", "api/v2/skills/repos")

    async def test_error_status_raises(self, mocked_haystack_enterprise_api: Mock) -> None:
        mocked_haystack_enterprise_api.request.return_value = Response(codes.FORBIDDEN, request=_REQUEST, text="no")

        with pytest.raises(FailedToListSkillReposError):
            await SkillReposAPI(mocked_haystack_enterprise_api).list_repos()
