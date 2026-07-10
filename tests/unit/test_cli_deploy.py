"""CLI tests for the `deploy` and `service-status` commands."""

from pathlib import Path
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
from deepset_cloud_sdk._service.pipeline_transform import PipelineTransformError
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


class TestDryRun:
    @patch("deepset_cloud_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_dry_run_prints_yaml_and_skips_api(self, extract_mock: Mock) -> None:
        extract_mock.return_value = {
            "pipeline": {"components": {"c": {"type": "haystack.X", "init_parameters": {}}}},
            "async_enabled": False,
            "inferred_inputs": {},
            "inferred_outputs": {},
            "dependencies": ["requests==2.32.5"],
        }
        with patch("deepset_cloud_sdk.cli.DeploymentClient") as client_cls:
            result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run"])
        assert result.exit_code == 0
        assert "haystack.X" in result.stdout
        assert "# dependencies:" in result.stdout
        client_cls.assert_not_called()  # dry-run never touches the API

    @patch("deepset_cloud_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_dry_run_writes_output_file(self, extract_mock: Mock, tmp_path: Path) -> None:
        extract_mock.return_value = {
            "pipeline": {"components": {}},
            "async_enabled": False,
            "inferred_inputs": {},
            "inferred_outputs": {},
            "dependencies": [],
        }
        out = tmp_path / "out.yaml"
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run", "--output", str(out)])
        assert result.exit_code == 0
        assert out.is_file()
        assert "components" in out.read_text()

    @patch("deepset_cloud_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_dry_run_transform_error_exits_1(self, extract_mock: Mock) -> None:
        extract_mock.side_effect = PipelineTransformError("missing dependency 'tiktoken'")
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run"])
        assert result.exit_code == 1
        assert "tiktoken" in result.stdout


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
