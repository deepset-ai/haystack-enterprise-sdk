"""Tests for the pipeline versions API client."""

from unittest.mock import Mock

import pytest
from httpx import Request, Response, codes

from haystack_enterprise_sdk._api.pipeline_versions import FailedToPublishPipelineVersionError, PipelineVersionsAPI

_REQUEST = Request("GET", "https://test.deepset.ai")


@pytest.mark.asyncio
class TestGetHead:
    async def test_splits_draft_and_latest_final(self, mocked_haystack_enterprise_api: Mock) -> None:
        draft = {"version_id": "d", "is_draft": True}
        final = {"version_id": "v2", "is_draft": False}
        mocked_haystack_enterprise_api.get.return_value = Response(
            codes.OK, request=_REQUEST, json={"data": [draft, final]}
        )

        assert await PipelineVersionsAPI(mocked_haystack_enterprise_api).get_head("ws", "pipe") == (draft, final)

    async def test_missing_pipeline_returns_none(self, mocked_haystack_enterprise_api: Mock) -> None:
        mocked_haystack_enterprise_api.get.return_value = Response(codes.NOT_FOUND, request=_REQUEST)

        assert await PipelineVersionsAPI(mocked_haystack_enterprise_api).get_head("ws", "pipe") is None

    async def test_error_status_raises(self, mocked_haystack_enterprise_api: Mock) -> None:
        mocked_haystack_enterprise_api.get.return_value = Response(codes.FORBIDDEN, request=_REQUEST, text="no")

        with pytest.raises(FailedToPublishPipelineVersionError):
            await PipelineVersionsAPI(mocked_haystack_enterprise_api).get_head("ws", "pipe")


@pytest.mark.asyncio
async def test_publish_finalizes_the_draft(mocked_haystack_enterprise_api: Mock) -> None:
    mocked_haystack_enterprise_api.patch.return_value = Response(
        codes.OK, request=_REQUEST, json={"version_id": "d", "is_draft": False}
    )

    await PipelineVersionsAPI(mocked_haystack_enterprise_api).publish("ws", "pipe", "d", {"config_yaml": "a: 1"})

    mocked_haystack_enterprise_api.patch.assert_awaited_once_with(
        workspace_name="ws",
        endpoint="pipelines/pipe/versions/d",
        json={"config_yaml": "a: 1", "is_draft": False},
    )
