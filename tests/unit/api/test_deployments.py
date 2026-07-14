"""Tests for the deployments API client."""

from typing import Optional
from unittest.mock import Mock
from uuid import uuid4

import pytest
from httpx import Request, Response, codes

from deepset_cloud_sdk._api.deployments import (
    Deployment,
    DeploymentRevision,
    DeploymentRevisionStatus,
    DeploymentsAPI,
    DeploymentServiceLevel,
    DeploymentStatus,
    FailedToActivateRevisionError,
    FailedToCreateDeploymentError,
    FailedToPushRevisionError,
)

_REQUEST = Request("GET", "https://test.deepset.ai")


def _resp(status_code: int, **kwargs: object) -> Response:
    """Build a Response with a request attached so `raise_for_status` works in tests."""
    return Response(status_code=status_code, request=_REQUEST, **kwargs)


@pytest.fixture
def deployments_api(mocked_deepset_cloud_api: Mock) -> DeploymentsAPI:
    return DeploymentsAPI(mocked_deepset_cloud_api)


def _deployment_body(name: str = "svc", deployment_id: Optional[str] = None, status: str = "DEPLOYED") -> dict:
    return {
        "deployment_id": deployment_id or str(uuid4()),
        "name": name,
        "status": status,
        "service_level": "DEVELOPMENT",
        "active_revision_id": None,
        "pending_revision_id": None,
    }


def _revision_body(deployment_id: str, status: str = "PENDING") -> dict:
    return {
        "revision_id": str(uuid4()),
        "deployment_id": deployment_id,
        "status": status,
        "config_hash": "abc123",
    }


@pytest.mark.asyncio
class TestListAndFind:
    async def test_list_deployments_parses_page(
        self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock
    ) -> None:
        mocked_deepset_cloud_api.get.return_value = _resp(
            codes.OK,
            json={"data": [_deployment_body("a"), _deployment_body("b")], "has_more": True, "total": 5},
        )
        page = await deployments_api.list_deployments("ws", page_number=2)
        assert [d.name for d in page.data] == ["a", "b"]
        assert page.has_more is True
        assert page.total == 5
        mocked_deepset_cloud_api.get.assert_called_once_with(
            workspace_name="ws", endpoint="deployments", params={"limit": 100, "page_number": 2}
        )

    async def test_find_by_name_matches_on_first_page(
        self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock
    ) -> None:
        target = _deployment_body("wanted")
        mocked_deepset_cloud_api.get.return_value = _resp(
            codes.OK,
            json={"data": [_deployment_body("other"), target], "has_more": False, "total": 2},
        )
        found = await deployments_api.find_by_name("ws", "wanted")
        assert found is not None
        assert found.name == "wanted"
        assert found.deployment_id == Deployment.from_response(target).deployment_id

    async def test_find_by_name_pages_until_match(
        self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock
    ) -> None:
        page1 = _resp(codes.OK, json={"data": [_deployment_body("p1")], "has_more": True, "total": 2})
        page2 = _resp(codes.OK, json={"data": [_deployment_body("wanted")], "has_more": False, "total": 2})
        mocked_deepset_cloud_api.get.side_effect = [page1, page2]
        found = await deployments_api.find_by_name("ws", "wanted")
        assert found is not None and found.name == "wanted"
        assert mocked_deepset_cloud_api.get.call_count == 2

    async def test_find_by_name_returns_none_when_absent(
        self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock
    ) -> None:
        mocked_deepset_cloud_api.get.return_value = _resp(
            codes.OK,
            json={"data": [_deployment_body("other")], "has_more": False, "total": 1},
        )
        assert await deployments_api.find_by_name("ws", "missing") is None


@pytest.mark.asyncio
class TestCreate:
    async def test_create_deployment_sends_overrides(
        self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock
    ) -> None:
        mocked_deepset_cloud_api.post.return_value = _resp(codes.CREATED, json=_deployment_body("svc"))
        result = await deployments_api.create_deployment(
            "ws",
            name="svc",
            service_level=DeploymentServiceLevel.PRODUCTION,
            cpu_limit="2",
            max_query_replica_count=3,
        )
        assert isinstance(result, Deployment)
        mocked_deepset_cloud_api.post.assert_called_once_with(
            workspace_name="ws",
            endpoint="deployments",
            json={
                "name": "svc",
                "source_type": "EXTERNAL_PIPELINE",
                "service_level": "PRODUCTION",
                "max_query_replica_count": 3,
                "cpu_limit": "2",
            },
        )

    async def test_create_deployment_minimal_payload(
        self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock
    ) -> None:
        mocked_deepset_cloud_api.post.return_value = _resp(codes.CREATED, json=_deployment_body("svc"))
        await deployments_api.create_deployment("ws", name="svc")
        _, kwargs = mocked_deepset_cloud_api.post.call_args
        assert kwargs["json"] == {"name": "svc", "source_type": "EXTERNAL_PIPELINE"}

    async def test_create_deployment_failure_raises(
        self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock
    ) -> None:
        mocked_deepset_cloud_api.post.return_value = _resp(codes.CONFLICT, text="exists")
        with pytest.raises(FailedToCreateDeploymentError):
            await deployments_api.create_deployment("ws", name="svc")


@pytest.mark.asyncio
class TestRevisions:
    async def test_push_revision(self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock) -> None:
        deployment_id = uuid4()
        mocked_deepset_cloud_api.post.return_value = _resp(codes.CREATED, json=_revision_body(str(deployment_id)))
        revision = await deployments_api.push_revision("ws", deployment_id, config_yaml="components: {}")
        assert isinstance(revision, DeploymentRevision)
        assert revision.status == DeploymentRevisionStatus.PENDING
        mocked_deepset_cloud_api.post.assert_called_once_with(
            workspace_name="ws",
            endpoint=f"deployments/{deployment_id}/revisions",
            json={"config_yaml": "components: {}", "source_type": "EXTERNAL_PIPELINE"},
        )

    async def test_push_revision_tolerates_missing_status(
        self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock
    ) -> None:
        deployment_id = uuid4()
        body = {"revision_id": str(uuid4()), "deployment_id": str(deployment_id)}  # no "status"/"config_hash"
        mocked_deepset_cloud_api.post.return_value = _resp(codes.CREATED, json=body)
        revision = await deployments_api.push_revision("ws", deployment_id, config_yaml="components: {}")
        assert revision.status == DeploymentRevisionStatus.PENDING
        assert revision.config_hash == ""

    async def test_push_revision_failure_raises(
        self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock
    ) -> None:
        mocked_deepset_cloud_api.post.return_value = _resp(codes.UNPROCESSABLE_ENTITY, text="empty")
        with pytest.raises(FailedToPushRevisionError):
            await deployments_api.push_revision("ws", uuid4(), config_yaml="")

    async def test_activate_revision(self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock) -> None:
        deployment_id, revision_id = uuid4(), uuid4()
        mocked_deepset_cloud_api.post.return_value = _resp(
            codes.OK, json=_deployment_body("svc", status="DEPLOYMENT_IN_PROGRESS")
        )
        result = await deployments_api.activate_revision("ws", deployment_id, revision_id)
        assert result.status == DeploymentStatus.DEPLOYMENT_IN_PROGRESS
        mocked_deepset_cloud_api.post.assert_called_once_with(
            workspace_name="ws",
            endpoint=f"deployments/{deployment_id}/revisions/{revision_id}/activate",
        )

    async def test_activate_revision_failure_raises(
        self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock
    ) -> None:
        mocked_deepset_cloud_api.post.return_value = _resp(codes.CONFLICT, text="nope")
        with pytest.raises(FailedToActivateRevisionError):
            await deployments_api.activate_revision("ws", uuid4(), uuid4())


@pytest.mark.asyncio
class TestGetAndActivity:
    async def test_get_deployment(self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock) -> None:
        did = uuid4()
        mocked_deepset_cloud_api.get.return_value = _resp(
            codes.OK, json=_deployment_body("svc", deployment_id=str(did), status="DEPLOYED")
        )
        result = await deployments_api.get_deployment("ws", did)
        assert result.deployment_id == did
        assert result.status == DeploymentStatus.DEPLOYED

    async def test_list_activity(self, deployments_api: DeploymentsAPI, mocked_deepset_cloud_api: Mock) -> None:
        mocked_deepset_cloud_api.get.return_value = _resp(
            codes.OK,
            json={"data": [{"event_type": "REVISION_CREATED"}], "has_more": False, "total": 1},
        )
        events = await deployments_api.list_activity("ws", uuid4())
        assert events == [{"event_type": "REVISION_CREATED"}]
