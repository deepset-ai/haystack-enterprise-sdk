"""Tests for the pipeline versions API client."""

from unittest.mock import Mock

import pytest
from httpx import Request, Response, codes

from haystack_enterprise_sdk._api.pipeline_versions import FailedToPublishPipelineVersionError, PipelineVersionsAPI

_REQUEST = Request("GET", "https://test.deepset.ai")


@pytest.mark.asyncio
class TestPipelineExists:
    async def test_existing(self, mocked_haystack_enterprise_api: Mock) -> None:
        mocked_haystack_enterprise_api.get.return_value = Response(codes.OK, request=_REQUEST, json={"name": "pipe"})

        assert await PipelineVersionsAPI(mocked_haystack_enterprise_api).pipeline_exists("ws", "pipe") is True

    async def test_missing(self, mocked_haystack_enterprise_api: Mock) -> None:
        mocked_haystack_enterprise_api.get.return_value = Response(codes.NOT_FOUND, request=_REQUEST)

        assert await PipelineVersionsAPI(mocked_haystack_enterprise_api).pipeline_exists("ws", "pipe") is False

    async def test_error_status_raises(self, mocked_haystack_enterprise_api: Mock) -> None:
        mocked_haystack_enterprise_api.get.return_value = Response(codes.FORBIDDEN, request=_REQUEST, text="no")

        with pytest.raises(FailedToPublishPipelineVersionError):
            await PipelineVersionsAPI(mocked_haystack_enterprise_api).pipeline_exists("ws", "pipe")


@pytest.mark.asyncio
async def test_publish_finalizes_the_draft(mocked_haystack_enterprise_api: Mock) -> None:
    mocked_haystack_enterprise_api.patch.return_value = Response(
        codes.OK, request=_REQUEST, json={"version_id": "d", "is_draft": False}
    )

    await PipelineVersionsAPI(mocked_haystack_enterprise_api).publish("ws", "pipe", "d")

    mocked_haystack_enterprise_api.patch.assert_awaited_once_with(
        workspace_name="ws",
        endpoint="pipelines/pipe/versions/d",
        json={"is_draft": False},
    )
