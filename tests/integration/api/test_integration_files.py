from datetime import timedelta

import pytest
import tenacity

from haystack_enterprise_sdk._api.config import CommonConfig
from haystack_enterprise_sdk._api.files import FilesAPI
from haystack_enterprise_sdk._api.haystack_enterprise_api import HaystackEnterpriseAPI


@pytest.mark.asyncio
class TestListFiles:
    async def test_list_paginated(
        self,
        integration_config: CommonConfig,
        workspace_name: str,
    ) -> None:
        async with HaystackEnterpriseAPI.factory(integration_config) as haystack_enterprise_api:
            files_api = FilesAPI(haystack_enterprise_api)

            # We need to retry fetching this, because the file itself is available
            # immediately, but the search index might not be updated yet.
            # We are searching by context here which is otherwise not available.
            for attempt in tenacity.Retrying(
                stop=tenacity.stop_after_delay(300),
                wait=tenacity.wait_fixed(wait=timedelta(seconds=0.5)),
                reraise=True,
            ):
                with attempt:
                    result = await files_api.list_paginated(
                        workspace_name=workspace_name,
                        limit=10,
                        name="example0.txt",
                        odata_filter="find eq 'me'",
                    )
                    assert result.total == 1
                    assert result.has_more is False
                    assert len(result.data) == 1
                    found_file = result.data[0]
                    assert found_file.name == "example0.txt"
                    assert found_file.size > 0
                    assert found_file.meta == {"find": "me"}
