"""Tests for the deployment service orchestration."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from deepset_cloud_sdk._api.deployments import (
    Deployment,
    DeploymentRevision,
    DeploymentRevisionStatus,
    DeploymentServiceLevel,
    DeploymentStatus,
)
from deepset_cloud_sdk._service.deployment_service import (
    CreateOptions,
    DeploymentFailedError,
    DeploymentService,
    ServiceNotFoundError,
)

FIXTURE = Path(__file__).parent.parent.parent / "test_data" / "deploy" / "pipeline.py"


def _deployment(name: str = "svc", status: DeploymentStatus = DeploymentStatus.UNDEPLOYED) -> Deployment:
    return Deployment(
        deployment_id=uuid4(),
        name=name,
        status=status,
        service_level=DeploymentServiceLevel.DEVELOPMENT,
        active_revision_id=None,
        pending_revision_id=None,
    )


def _revision(deployment_id: object) -> DeploymentRevision:
    return DeploymentRevision(
        revision_id=uuid4(),
        deployment_id=deployment_id,
        status=DeploymentRevisionStatus.PENDING,
        config_hash="hash",
    )


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> DeploymentService:
    """A DeploymentService whose DeploymentsAPI is a fully mocked AsyncMock."""
    svc = DeploymentService(api=Mock(), workspace_name="ws")
    svc._deployments = AsyncMock()  # type: ignore[assignment]
    # short-circuit the transform so tests don't need Haystack/import machinery
    monkeypatch.setattr(
        "deepset_cloud_sdk._service.pipeline_transform.build_config_yaml", lambda *a, **k: "components: {}\n"
    )
    return svc


@pytest.mark.asyncio
class TestResolveAndPush:
    async def test_push_without_activate_stops_at_pending(self, service: DeploymentService) -> None:
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)

        result = await service.deploy(FIXTURE, "svc")

        assert result.activated is False
        assert result.timed_out is False
        service._deployments.push_revision.assert_awaited_once_with("ws", deployment.deployment_id, "components: {}\n")
        service._deployments.activate_revision.assert_not_called()

    async def test_missing_service_without_create_raises(self, service: DeploymentService) -> None:
        service._deployments.find_by_name.return_value = None
        with pytest.raises(ServiceNotFoundError):
            await service.deploy(FIXTURE, "svc")

    async def test_create_when_missing(self, service: DeploymentService) -> None:
        created = _deployment("svc")
        service._deployments.find_by_name.return_value = None
        service._deployments.create_deployment.return_value = created
        service._deployments.push_revision.return_value = _revision(created.deployment_id)

        result = await service.deploy(
            FIXTURE,
            "svc",
            create=True,
            create_options=CreateOptions(service_level=DeploymentServiceLevel.PRODUCTION, cpu_limit="2"),
        )

        assert result.deployment is created
        service._deployments.create_deployment.assert_awaited_once()
        _, kwargs = service._deployments.create_deployment.call_args
        assert kwargs["service_level"] == DeploymentServiceLevel.PRODUCTION
        assert kwargs["cpu_limit"] == "2"


@pytest.mark.asyncio
class TestActivateAndPoll:
    async def test_activate_polls_until_deployed(self, service: DeploymentService) -> None:
        deployment = _deployment(status=DeploymentStatus.UNDEPLOYED)
        service._deployments.find_by_name.return_value = deployment
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)
        service._deployments.activate_revision.return_value = _deployment(
            status=DeploymentStatus.DEPLOYMENT_IN_PROGRESS
        )
        # poll: in-progress -> deployed
        service._deployments.get_deployment.side_effect = [
            _deployment(status=DeploymentStatus.DEPLOYMENT_IN_PROGRESS),
            _deployment(status=DeploymentStatus.DEPLOYED),
        ]
        seen: list = []

        result = await service.deploy(
            FIXTURE, "svc", activate=True, poll_interval_s=0, timeout_s=100, on_status=seen.append
        )

        assert result.activated is True
        assert result.timed_out is False
        assert result.deployment.status == DeploymentStatus.DEPLOYED
        assert DeploymentStatus.DEPLOYED in seen

    async def test_activate_failure_raises_with_ui_hint(self, service: DeploymentService) -> None:
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)
        service._deployments.activate_revision.return_value = _deployment(
            status=DeploymentStatus.DEPLOYMENT_IN_PROGRESS
        )
        service._deployments.get_deployment.return_value = _deployment(status=DeploymentStatus.DEPLOYMENT_FAILED)

        with pytest.raises(DeploymentFailedError) as exc:
            await service.deploy(FIXTURE, "svc", activate=True, poll_interval_s=0)
        assert "deepset AI Platform" in exc.value.ui_hint

    async def test_activate_times_out_and_detaches(self, service: DeploymentService) -> None:
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)
        service._deployments.activate_revision.return_value = _deployment(
            status=DeploymentStatus.DEPLOYMENT_IN_PROGRESS
        )
        service._deployments.get_deployment.return_value = _deployment(status=DeploymentStatus.DEPLOYMENT_IN_PROGRESS)

        result = await service.deploy(FIXTURE, "svc", activate=True, poll_interval_s=0, timeout_s=0)

        assert result.timed_out is True
        assert result.activated is True


@pytest.mark.asyncio
class TestGetServiceStatus:
    async def test_get_service_status(self, service: DeploymentService) -> None:
        deployment = _deployment(status=DeploymentStatus.DEPLOYED)
        service._deployments.find_by_name.return_value = deployment
        service._deployments.get_deployment.return_value = deployment
        result = await service.get_service_status("svc")
        assert result.status == DeploymentStatus.DEPLOYED

    async def test_get_service_status_missing_raises(self, service: DeploymentService) -> None:
        service._deployments.find_by_name.return_value = None
        with pytest.raises(ServiceNotFoundError):
            await service.get_service_status("svc")


@pytest.mark.asyncio
class TestCreateSharedPrototype:
    async def test_computes_expiration_and_forwards_options(
        self, service: DeploymentService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime, timezone

        from deepset_cloud_sdk._api.shared_prototypes import SharedPrototype
        from deepset_cloud_sdk._service.deployment_service import ShareOptions

        prototype = SharedPrototype(
            shared_prototype_id=uuid4(),
            link="https://app/shared_prototypes?share_token=tok",
            expiration_date="d",
            is_revoked=False,
            service_names=["svc"],
        )
        api_mock = AsyncMock()
        api_mock.create.return_value = prototype
        monkeypatch.setattr(
            "deepset_cloud_sdk._service.deployment_service.SharedPrototypesAPI",
            lambda _api: api_mock,
        )

        result = await service.create_shared_prototype(
            "svc", ShareOptions(expiration_days=7, login_required=False, show_files=True)
        )

        assert result is prototype
        _, kwargs = api_mock.create.call_args
        assert kwargs["service_name"] == "svc"
        assert kwargs["login_required"] is False
        assert kwargs["show_files"] is True
        # expiration_date is ~7 days out, ISO 8601 with timezone
        expiry = datetime.fromisoformat(kwargs["expiration_date"])
        delta_days = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
        assert 6.9 < delta_days < 7.1
