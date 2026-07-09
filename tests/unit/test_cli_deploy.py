"""CLI tests for the `deploy` and `service-status` commands."""

from unittest.mock import Mock, patch
from uuid import uuid4

from typer.testing import CliRunner

from deepset_cloud_sdk._api.deployments import (
    Deployment,
    DeploymentRevision,
    DeploymentRevisionStatus,
    DeploymentServiceLevel,
    DeploymentStatus,
)
from deepset_cloud_sdk._service.deployment_service import (
    DeploymentFailedError,
    DeployResult,
    ServiceNotFoundError,
)
from deepset_cloud_sdk.cli import cli_app

runner = CliRunner()

FIXTURE = "tests/test_data/deploy/pipeline.py"


def _deployment(status: DeploymentStatus = DeploymentStatus.DEPLOYED) -> Deployment:
    return Deployment(
        deployment_id=uuid4(),
        name="svc",
        status=status,
        service_level=DeploymentServiceLevel.DEVELOPMENT,
        active_revision_id=uuid4(),
        pending_revision_id=None,
    )


def _result(activated: bool, timed_out: bool = False, status: DeploymentStatus = DeploymentStatus.DEPLOYED) -> DeployResult:
    deployment = _deployment(status)
    revision = DeploymentRevision(
        revision_id=uuid4(),
        deployment_id=deployment.deployment_id,
        status=DeploymentRevisionStatus.PENDING,
        config_hash="hash",
    )
    return DeployResult(deployment=deployment, revision=revision, activated=activated, timed_out=timed_out)


class TestDeployCommand:
    @patch("deepset_cloud_sdk.cli.DeploymentClient")
    def test_deploy_without_activate(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.return_value = _result(activated=False)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 0
        assert "PENDING" in result.stdout
        _, kwargs = client_cls.return_value.deploy.call_args
        assert kwargs["create_options"] is None
        assert "activate" not in kwargs or kwargs["activate"] is False

    @patch("deepset_cloud_sdk.cli.DeploymentClient")
    def test_deploy_activate_success(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.return_value = _result(activated=True, status=DeploymentStatus.DEPLOYED)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--activate"])
        assert result.exit_code == 0
        assert "DEPLOYED" in result.stdout
        _, kwargs = client_cls.return_value.deploy.call_args
        assert kwargs["activate"] is True
        assert kwargs["on_status"] is not None

    @patch("deepset_cloud_sdk.cli.DeploymentClient")
    def test_deploy_activate_timed_out(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.return_value = _result(activated=True, timed_out=True)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--activate"])
        assert result.exit_code == 0
        assert "still in progress" in result.stdout

    @patch("deepset_cloud_sdk.cli.DeploymentClient")
    def test_deploy_failure_exits_1(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.side_effect = DeploymentFailedError(_deployment(), "check the UI")
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--activate"])
        assert result.exit_code == 1
        assert "check the UI" in result.stdout

    @patch("deepset_cloud_sdk.cli.DeploymentClient")
    def test_deploy_service_not_found_exits_1(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.side_effect = ServiceNotFoundError("no such service")
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 1
        assert "no such service" in result.stdout

    @patch("deepset_cloud_sdk.cli.DeploymentClient")
    def test_deploy_create_passes_options(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.return_value = _result(activated=False)
        result = runner.invoke(
            cli_app,
            ["deploy", FIXTURE, "svc", "--create", "--service-level", "PRODUCTION", "--cpu", "2", "--max-replicas", "3"],
        )
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.deploy.call_args
        options = kwargs["create_options"]
        assert options.service_level == DeploymentServiceLevel.PRODUCTION
        assert options.cpu_limit == "2"
        assert options.max_query_replica_count == 3

    @patch("deepset_cloud_sdk.cli.DeploymentClient")
    def test_deploy_interrupt_detaches(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.side_effect = KeyboardInterrupt()
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--activate"])
        assert result.exit_code == 0
        assert "Detached" in result.stdout


class TestServiceStatusCommand:
    @patch("deepset_cloud_sdk.cli.DeploymentClient")
    def test_service_status(self, client_cls: Mock) -> None:
        client_cls.return_value.get_service_status.return_value = _deployment(DeploymentStatus.DEPLOYED)
        result = runner.invoke(cli_app, ["service-status", "svc"])
        assert result.exit_code == 0
        assert "DEPLOYED" in result.stdout

    @patch("deepset_cloud_sdk.cli.DeploymentClient")
    def test_service_status_not_found(self, client_cls: Mock) -> None:
        client_cls.return_value.get_service_status.side_effect = ServiceNotFoundError("missing")
        result = runner.invoke(cli_app, ["service-status", "svc"])
        assert result.exit_code == 1
        assert "missing" in result.stdout
