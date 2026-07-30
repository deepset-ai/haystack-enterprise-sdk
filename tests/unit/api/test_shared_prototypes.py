"""Tests for the shared prototypes API client."""

from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from httpx import Request, Response, codes

from haystack_enterprise_sdk._api.shared_prototypes import (
    FailedToCreateSharedPrototypeError,
    SharedPrototype,
    SharedPrototypesAPI,
)

_REQUEST = Request("POST", "https://test.deepset.ai")


def _resp(status_code: int, **kwargs: Any) -> Response:
    return Response(status_code=status_code, request=_REQUEST, **kwargs)


@pytest.fixture
def prototypes_api(mocked_haystack_enterprise_api: Mock) -> SharedPrototypesAPI:
    return SharedPrototypesAPI(mocked_haystack_enterprise_api)


def _prototype_body(link: str = "https://app/shared_prototypes?share_token=tok") -> dict:
    return {
        "shared_prototype_id": str(uuid4()),
        "link": link,
        "expiration_date": "2026-08-12T00:00:00+00:00",
        "is_revoked": False,
        "service_names": ["svc"],
    }


@pytest.mark.asyncio
class TestCreate:
    async def test_create_sends_service_payload_and_parses_link(
        self, prototypes_api: SharedPrototypesAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.CREATED, json=_prototype_body())
        prototype = await prototypes_api.create(
            "ws",
            service_name="svc",
            expiration_date="2026-08-12T00:00:00+00:00",
            login_required=False,
            description="hi",
        )
        assert isinstance(prototype, SharedPrototype)
        assert prototype.link == "https://app/shared_prototypes?share_token=tok"

        _, kwargs = mocked_haystack_enterprise_api.post.call_args
        assert kwargs["workspace_name"] == "ws"
        assert kwargs["endpoint"] == "shared_prototypes"
        payload = kwargs["json"]
        assert payload["type"] == "service"
        assert payload["service_names"] == ["svc"]
        assert payload["expiration_date"] == "2026-08-12T00:00:00+00:00"
        assert payload["login_required"] is False
        assert payload["description"] == "hi"
        assert payload["show_metadata_filters"] is False
        assert payload["show_files"] is False
        assert payload["file_upload_enabled"] is False
        assert payload["runtime_params_enabled"] is False

    async def test_create_omits_description_when_none(
        self, prototypes_api: SharedPrototypesAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.OK, json=_prototype_body())
        await prototypes_api.create("ws", service_name="svc", expiration_date="2026-08-12T00:00:00+00:00")
        _, kwargs = mocked_haystack_enterprise_api.post.call_args
        assert "description" not in kwargs["json"]

    async def test_create_failure_raises(
        self, prototypes_api: SharedPrototypesAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.BAD_REQUEST, text="nope")
        with pytest.raises(FailedToCreateSharedPrototypeError):
            await prototypes_api.create("ws", service_name="svc", expiration_date="2026-08-12T00:00:00+00:00")


class TestFromResponse:
    def test_falls_back_to_shared_services(self) -> None:
        body = {
            "shared_prototype_id": str(uuid4()),
            "link": "l",
            "expiration_date": "d",
            "is_revoked": True,
            "shared_services": [{"name": "svc", "deployment_id": str(uuid4())}],
        }
        prototype = SharedPrototype.from_response(body)
        assert prototype.service_names == ["svc"]
        assert prototype.is_revoked is True
