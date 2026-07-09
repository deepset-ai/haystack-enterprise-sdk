"""Tests for the sync deployment client."""

from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from deepset_cloud_sdk._api.deployments import (
    Deployment,
    DeploymentRevision,
    DeploymentRevisionStatus,
    DeploymentServiceLevel,
    DeploymentStatus,
)
from deepset_cloud_sdk._service.deployment_service import DeployResult
from deepset_cloud_sdk.workflows.sync_client.deployment_client import DeploymentClient


def _result() -> DeployResult:
    deployment = Deployment(
        deployment_id=uuid4(),
        name="svc",
        status=DeploymentStatus.DEPLOYED,
        service_level=DeploymentServiceLevel.DEVELOPMENT,
        active_revision_id=None,
        pending_revision_id=None,
    )
    revision = DeploymentRevision(
        revision_id=uuid4(),
        deployment_id=deployment.deployment_id,
        status=DeploymentRevisionStatus.PENDING,
        config_hash="hash",
    )
    return DeployResult(deployment=deployment, revision=revision, activated=False, timed_out=False)


@patch("deepset_cloud_sdk.workflows.sync_client.deployment_client.AsyncDeploymentClient")
def test_deploy_forwards_to_async_client(async_cls: Mock) -> None:
    async_instance = async_cls.return_value
    async_instance.deploy = AsyncMock(return_value=_result())

    client = DeploymentClient(api_key="k", workspace_name="ws", api_url="https://api")
    result = client.deploy("pipeline.py", "svc", activate=True)

    assert isinstance(result, DeployResult)
    async_instance.deploy.assert_awaited_once()
    _, kwargs = async_instance.deploy.call_args
    assert kwargs["activate"] is True


@patch("deepset_cloud_sdk.workflows.sync_client.deployment_client.AsyncDeploymentClient")
def test_get_service_status_forwards(async_cls: Mock) -> None:
    async_instance = async_cls.return_value
    deployment = _result().deployment
    async_instance.get_service_status = AsyncMock(return_value=deployment)

    client = DeploymentClient()
    result = client.get_service_status("svc")

    assert result is deployment
    async_instance.get_service_status.assert_awaited_once_with("svc")
