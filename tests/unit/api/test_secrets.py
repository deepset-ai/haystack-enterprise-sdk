"""Tests for the service secrets API client."""

from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from httpx import Request, Response, codes

from haystack_enterprise_sdk._api.secrets import FailedToSetServiceSecretError, ServiceSecretsAPI

_REQUEST = Request("GET", "https://test.deepset.ai")


def _resp(status_code: int, **kwargs: Any) -> Response:
    return Response(status_code=status_code, request=_REQUEST, **kwargs)


@pytest.fixture
def secrets_api(mocked_haystack_enterprise_api: Mock) -> ServiceSecretsAPI:
    return ServiceSecretsAPI(mocked_haystack_enterprise_api)


@pytest.mark.asyncio
class TestServiceSecretsAPI:
    async def test_get_workspace_id_reads_v1_workspace(
        self, secrets_api: ServiceSecretsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        workspace_id = uuid4()
        mocked_haystack_enterprise_api.request.return_value = _resp(codes.OK, json={"workspace_id": str(workspace_id)})

        assert await secrets_api.get_workspace_id("ws") == workspace_id
        mocked_haystack_enterprise_api.request.assert_awaited_once_with("GET", "api/v1/workspaces/ws")

    async def test_list_names_pages_until_done(
        self, secrets_api: ServiceSecretsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        ws, dep, a, b = uuid4(), uuid4(), uuid4(), uuid4()
        mocked_haystack_enterprise_api.request.side_effect = [
            _resp(codes.OK, json={"data": [{"name": "A", "secret_id": str(a)}], "has_more": True}),
            _resp(codes.OK, json={"data": [{"name": "B", "secret_id": str(b)}], "has_more": False}),
        ]

        assert await secrets_api.list_names(ws, dep) == {"A": a, "B": b}
        method, path = mocked_haystack_enterprise_api.request.await_args.args
        assert (method, path) == ("GET", f"api/v2/workspaces/{ws}/deployments/{dep}/secrets")
        assert mocked_haystack_enterprise_api.request.await_args.kwargs["params"]["page_number"] == 2

    async def test_create_and_update(
        self, secrets_api: ServiceSecretsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        ws, dep, secret_id = uuid4(), uuid4(), uuid4()
        mocked_haystack_enterprise_api.request.side_effect = [_resp(codes.CREATED, json={}), _resp(codes.NO_CONTENT)]

        await secrets_api.create(ws, dep, "A", "1")
        await secrets_api.update(ws, dep, secret_id, "B", "2")

        create, update = mocked_haystack_enterprise_api.request.await_args_list
        assert create.args == ("POST", f"api/v2/workspaces/{ws}/deployments/{dep}/secrets")
        assert create.kwargs["json"] == {"name": "A", "secret": "1"}
        assert update.args == ("PUT", f"api/v2/workspaces/{ws}/deployments/{dep}/secrets/{secret_id}")
        assert update.kwargs["json"] == {"name": "B", "secret": "2"}

    async def test_rejected_name_surfaces_platform_message(
        self, secrets_api: ServiceSecretsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.request.return_value = _resp(codes.BAD_REQUEST, text="Invalid secret name")

        with pytest.raises(FailedToSetServiceSecretError, match="Invalid secret name"):
            await secrets_api.create(uuid4(), uuid4(), "1BAD", "x")
