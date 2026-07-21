import pytest

from haystack_enterprise_sdk._api.config import CommonConfig
from haystack_enterprise_sdk._api.haystack_enterprise_api import HaystackEnterpriseAPI
from haystack_enterprise_sdk._api.upload_sessions import (
    UploadSession,
    UploadSessionDetailList,
    UploadSessionIngestionStatus,
    UploadSessionsAPI,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("integration_config", ["integration_config", "integration_config_safe_mode"], indirect=True)
class TestCreateUploadSessions:
    async def test_create_and_close_upload_session(self, integration_config: CommonConfig, workspace_name: str) -> None:
        async with HaystackEnterpriseAPI.factory(integration_config) as haystack_enterprise_api:
            upload_session_client = UploadSessionsAPI(haystack_enterprise_api)

            result: UploadSession = await upload_session_client.create(workspace_name=workspace_name)
            assert result.session_id is not None
            assert result.documentation_url is not None
            assert result.expires_at is not None

            assert "-user-files-upload.s3.amazonaws.com/" in result.aws_prefixed_request_config.url

            assert result.aws_prefixed_request_config.fields["key"] is not None

            await upload_session_client.close(workspace_name=workspace_name, session_id=result.session_id)

            session_status = await upload_session_client.status(
                workspace_name=workspace_name, session_id=result.session_id
            )
            assert session_status.session_id is not None
            assert session_status.documentation_url is not None
            assert session_status.expires_at is not None
            assert session_status.ingestion_status == UploadSessionIngestionStatus(failed_files=0, finished_files=0)

    async def test_list_upload_session(self, integration_config: CommonConfig, workspace_name: str) -> None:
        async with HaystackEnterpriseAPI.factory(integration_config) as haystack_enterprise_api:
            upload_session_client = UploadSessionsAPI(haystack_enterprise_api)

            await upload_session_client.create(workspace_name=workspace_name)

            result: UploadSessionDetailList = await upload_session_client.list(
                workspace_name=workspace_name, limit=1, page_number=1
            )

            assert result.total > 0
            assert result.data is not None
            assert len(result.data) == 1
