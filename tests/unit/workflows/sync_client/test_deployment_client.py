"""Tests for the sync deployment client."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from haystack_enterprise_sdk._api.deployments import (
    Deployment,
    DeploymentRevision,
    DeploymentRevisionStatus,
    DeploymentServiceLevel,
    DeploymentStatus,
)
from haystack_enterprise_sdk._service.deployment_service import DeployResult
from haystack_enterprise_sdk.workflows.sync_client.deployment_client import DeploymentClient


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


@patch("haystack_enterprise_sdk.workflows.sync_client.deployment_client.AsyncDeploymentClient")
def test_deploy_forwards_to_async_client(async_cls: Mock) -> None:
    async_instance = async_cls.return_value
    async_instance.deploy = AsyncMock(return_value=_result())

    client = DeploymentClient(api_key="k", workspace_name="ws", api_url="https://api")
    result = client.deploy(Path("pipeline.py"), "svc", activate=True, comment="Bump embedder")

    assert isinstance(result, DeployResult)
    async_instance.deploy.assert_awaited_once()
    _, kwargs = async_instance.deploy.call_args
    assert kwargs["activate"] is True
    assert kwargs["comment"] == "Bump embedder"


@patch("haystack_enterprise_sdk.workflows.sync_client.deployment_client.AsyncDeploymentClient")
def test_run_forwards_to_async_client(async_cls: Mock) -> None:
    async_instance = async_cls.return_value
    async_instance.run = AsyncMock(return_value={"llm": {"replies": ["hi"]}})

    client = DeploymentClient(api_key="k", workspace_name="ws", api_url="https://api")
    result = client.run(Path("pipeline.py"), query="who?", extra_inputs={"retriever": {"top_k": 3}})

    assert result == {"llm": {"replies": ["hi"]}}
    async_instance.run.assert_awaited_once()
    _, kwargs = async_instance.run.call_args
    assert kwargs["query"] == "who?"
    assert kwargs["extra_inputs"] == {"retriever": {"top_k": 3}}


@patch("haystack_enterprise_sdk.workflows.sync_client.deployment_client.AsyncDeploymentClient")
def test_find_service_forwards(async_cls: Mock) -> None:
    async_instance = async_cls.return_value
    deployment = _result().deployment
    async_instance.find_service = AsyncMock(return_value=deployment)

    client = DeploymentClient()
    result = client.find_service("svc")

    assert result is deployment
    async_instance.find_service.assert_awaited_once_with("svc")


@patch("haystack_enterprise_sdk.workflows.sync_client.deployment_client.AsyncDeploymentClient")
def test_get_service_status_forwards(async_cls: Mock) -> None:
    async_instance = async_cls.return_value
    deployment = _result().deployment
    async_instance.get_service_status = AsyncMock(return_value=deployment)

    client = DeploymentClient()
    result = client.get_service_status("svc")

    assert result is deployment
    async_instance.get_service_status.assert_awaited_once_with("svc")


@patch("haystack_enterprise_sdk.workflows.sync_client.deployment_client.AsyncDeploymentClient")
def test_create_shared_prototype_forwards(async_cls: Mock) -> None:
    from haystack_enterprise_sdk._api.shared_prototypes import SharedPrototype
    from haystack_enterprise_sdk._service.deployment_service import ShareOptions

    prototype = SharedPrototype(
        shared_prototype_id=uuid4(),
        link="https://app/shared_prototypes?share_token=tok",
        expiration_date="2026-08-12T00:00:00+00:00",
        is_revoked=False,
        service_names=["svc"],
    )
    async_instance = async_cls.return_value
    async_instance.create_shared_prototype = AsyncMock(return_value=prototype)

    client = DeploymentClient()
    options = ShareOptions(expiration_days=7, login_required=False)
    result = client.create_shared_prototype("svc", options)

    assert result is prototype
    async_instance.create_shared_prototype.assert_awaited_once_with("svc", options)
