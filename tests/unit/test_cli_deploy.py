"""CLI tests for the `deploy` and `service-status` commands."""

import json
from pathlib import Path
from typing import Literal, Optional
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from haystack_enterprise_sdk._api.deployments import (
    Deployment,
    DeploymentMode,
    DeploymentRevision,
    DeploymentRevisionStatus,
    DeploymentServiceLevel,
    DeploymentStatus,
    PipelineValidationError,
    PipelineValidationIssue,
    PipelineValidationResult,
)
from haystack_enterprise_sdk._api.pipeline_run import DEFAULT_RUN_RETRIES, PipelineRunError
from haystack_enterprise_sdk._api.shared_prototypes import (
    FailedToCreateSharedPrototypeError,
    SharedPrototype,
)
from haystack_enterprise_sdk._service.deployment_service import (
    DeploymentFailedError,
    DeployResult,
    ServiceNotFoundError,
)
from haystack_enterprise_sdk._service.pipeline_transform import (
    ExtractionBundle,
    PipelineTransformError,
)
from haystack_enterprise_sdk.cli import cli_app
from haystack_enterprise_sdk.models import PipelineOutputType

runner = CliRunner()

FIXTURE = "tests/test_data/deploy/pipeline.py"


def _bundle(**raw: object) -> ExtractionBundle:
    """An ExtractionBundle for extract_via_subprocess mocks, from the raw dict keys."""
    return ExtractionBundle.from_dict({"pipeline": {"components": {}}, **raw})  # type: ignore[arg-type]


def _deployment(
    status: DeploymentStatus = DeploymentStatus.DEPLOYED,
    mode: DeploymentMode = DeploymentMode.MANAGED,
    output_type: Optional[PipelineOutputType] = None,
) -> Deployment:
    return Deployment(
        deployment_id=uuid4(),
        name="svc",
        status=status,
        service_level=DeploymentServiceLevel.DEVELOPMENT,
        active_revision_id=uuid4(),
        pending_revision_id=None,
        deployment_mode=mode,
        output_type=output_type,
    )


BASE_URL = "https://api.example/api/v1/workspaces/ws/deployments/dep-1"


def _stub_endpoint(client_cls: Mock) -> None:
    """Give the mocked client the pieces `_echo_endpoint` prints (a bare Mock renders as junk)."""
    client_cls.return_value.workspace_name = "ws"
    client_cls.return_value.deployment_base_url.return_value = BASE_URL


def _result(
    activated: bool,
    timed_out: bool = False,
    status: DeploymentStatus = DeploymentStatus.DEPLOYED,
    mode: DeploymentMode = DeploymentMode.MANAGED,
    output_type: Optional[PipelineOutputType] = None,
) -> DeployResult:
    deployment = _deployment(status, mode, output_type)
    revision = DeploymentRevision(
        revision_id=uuid4(),
        deployment_id=deployment.deployment_id,
        status=DeploymentRevisionStatus.PENDING,
        config_hash="hash",
    )
    return DeployResult(deployment=deployment, revision=revision, activated=activated, timed_out=timed_out)


def _prototype() -> SharedPrototype:
    return SharedPrototype(
        shared_prototype_id=uuid4(),
        link="https://app.example/shared_prototypes?share_token=tok",
        expiration_date="2026-08-12T00:00:00+00:00",
        is_revoked=False,
        service_names=["svc"],
    )


class TestDeployCommand:
    # ``find_service`` on the mocked client returns a truthy Mock by default, i.e. the service already
    # exists — the common case. Tests about creating a service set it to ``None`` explicitly.

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_skip_activation(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.return_value = _result(activated=False)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--skip-activation"])
        assert result.exit_code == 0
        assert "PENDING" in result.stdout
        _, kwargs = client_cls.return_value.deploy.call_args
        assert kwargs["create_options"] is None
        assert "activate" not in kwargs or kwargs["activate"] is False

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_forwards_comment(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.return_value = _result(activated=False)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--skip-activation", "-m", "Bump embedder"])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.deploy.call_args
        assert kwargs["comment"] == "Bump embedder"

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_without_comment_leaves_it_to_the_service(self, client_cls: Mock) -> None:
        # The default comment is generated in the service layer, so the CLI passes None through.
        client_cls.return_value.deploy.return_value = _result(activated=False)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--skip-activation"])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.deploy.call_args
        assert kwargs["comment"] is None

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_activates_by_default(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.return_value = _result(activated=True, status=DeploymentStatus.DEPLOYED)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 0
        assert "DEPLOYED" in result.stdout
        _, kwargs = client_cls.return_value.deploy.call_args
        assert kwargs["activate"] is True
        assert kwargs["on_status"] is not None

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_activate_timed_out(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.return_value = _result(activated=True, timed_out=True)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 0
        assert "still in progress" in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_failure_exits_1(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.side_effect = DeploymentFailedError(_deployment(), "check the UI")
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 1
        assert "check the UI" in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_service_not_found_exits_1(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.side_effect = ServiceNotFoundError("no such service")
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 1
        assert "no such service" in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_validates_by_default(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.return_value = _result(activated=True)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.deploy.call_args
        assert kwargs["validate"] is True

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_skip_validation(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.return_value = _result(activated=True)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--skip-validation"])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.deploy.call_args
        assert kwargs["validate"] is False

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_validation_error_exits_1(self, client_cls: Mock) -> None:
        errors = [PipelineValidationIssue(category="ERROR", code=None, json_pointer="/x", message="bad thing")]
        client_cls.return_value.deploy.side_effect = PipelineValidationError(errors)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 1
        assert "bad thing" in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_managed_passes_options(self, client_cls: Mock) -> None:
        client_cls.return_value.find_service.return_value = None
        client_cls.return_value.deploy.return_value = _result(activated=True)
        result = runner.invoke(
            cli_app,
            [
                "deploy",
                FIXTURE,
                "svc",
                "--managed",
                "--service-level",
                "PRODUCTION",
                "--cpu",
                "2",
                "--max-replicas",
                "3",
            ],
        )
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.deploy.call_args
        options = kwargs["create_options"]
        assert options.deployment_mode == DeploymentMode.MANAGED
        assert options.service_level == DeploymentServiceLevel.PRODUCTION
        assert options.cpu_limit == "2"
        assert options.max_query_replica_count == 3

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_missing_service_is_created_serverless(self, client_cls: Mock) -> None:
        # No --create needed: a service that doesn't exist yet is created (serverless) and reported.
        client_cls.return_value.find_service.return_value = None
        client_cls.return_value.deploy.return_value = _result(activated=True, mode=DeploymentMode.SERVERLESS)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 0
        assert "Created serverless service 'svc'" in result.stdout
        _, kwargs = client_cls.return_value.deploy.call_args
        assert kwargs["create"] is True
        options = kwargs["create_options"]
        assert options.deployment_mode == DeploymentMode.SERVERLESS
        assert options.service_level is None
        assert options.cpu_limit is None

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_existing_service_is_reused(self, client_cls: Mock) -> None:
        client_cls.return_value.find_service.return_value = _deployment()
        client_cls.return_value.deploy.return_value = _result(activated=True)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 0
        assert "Created" not in result.stdout
        _, kwargs = client_cls.return_value.deploy.call_args
        assert kwargs["create_options"] is None

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_create_on_existing_service_fails(self, client_cls: Mock) -> None:
        client_cls.return_value.find_service.return_value = _deployment()
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--create"])
        assert result.exit_code == 1
        assert "already exists" in result.stdout
        assert "--create" in result.stdout
        client_cls.return_value.deploy.assert_not_called()

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_no_create_on_missing_service_fails(self, client_cls: Mock) -> None:
        client_cls.return_value.find_service.return_value = None
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--no-create"])
        assert result.exit_code == 1
        assert "No service named 'svc'" in result.stdout
        assert "--no-create" in result.stdout
        client_cls.return_value.deploy.assert_not_called()

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_managed_on_existing_service_fails(self, client_cls: Mock) -> None:
        client_cls.return_value.find_service.return_value = _deployment()
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--managed", "--cpu", "2"])
        assert result.exit_code == 1
        assert "already exists" in result.stdout
        assert "--managed" in result.stdout
        assert "--cpu" in result.stdout
        client_cls.return_value.deploy.assert_not_called()

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_serverless_reports_served_revision(self, client_cls: Mock) -> None:
        # A serverless service has no rollout status worth reporting; its persisted status stays
        # DEPLOYMENT_IN_PROGRESS, which would read as a stuck deploy.
        deploy_result = _result(
            activated=True, status=DeploymentStatus.DEPLOYMENT_IN_PROGRESS, mode=DeploymentMode.SERVERLESS
        )
        client_cls.return_value.find_service.return_value = None
        client_cls.return_value.deploy.return_value = deploy_result
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--no-share"])
        assert result.exit_code == 0
        assert f"serving revision {deploy_result.revision.revision_id}" in result.stdout
        assert "DEPLOYMENT_IN_PROGRESS" not in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_sizing_flags_without_managed_fail(self, client_cls: Mock) -> None:
        # This guard is pure flag validation: it fires before the service is even looked up.
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--cpu", "2", "--max-replicas", "3"])
        assert result.exit_code == 1
        assert "--cpu" in result.stdout
        assert "--max-replicas" in result.stdout
        assert "--managed" in result.stdout
        client_cls.return_value.find_service.assert_not_called()
        client_cls.return_value.deploy.assert_not_called()

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_forwards_review_io_resolver_with_share(self, client_cls: Mock) -> None:
        # The mapping is what the shared prototype's chat UI routes through, so --share is what asks
        # for a review of it.
        from haystack_enterprise_sdk.cli import _resolve_io_interactive

        _stub_endpoint(client_cls)
        client_cls.return_value.deploy.return_value = _result(activated=True)
        client_cls.return_value.create_shared_prototype.return_value = _prototype()
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--share"])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.deploy.call_args
        # The resolver is wrapped so the deploy spinner is paused while it prompts; unwrap to inspect it.
        resolver = kwargs["io_resolver"].__wrapped__
        assert resolver.func is _resolve_io_interactive
        assert resolver.keywords["mode"] == "review"
        assert resolver.keywords["skip_validation"] is False
        assert resolver.keywords["save_path"] == Path(FIXTURE).with_suffix(".io.yaml")

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_without_share_asks_only_what_serving_needs(self, client_cls: Mock) -> None:
        # A plain deploy skips the full review but must still end up with a servable mapping, so it gets
        # the narrower "serve" resolver rather than a silent one.
        from haystack_enterprise_sdk.cli import _resolve_io_interactive

        _stub_endpoint(client_cls)
        client_cls.return_value.deploy.return_value = _result(activated=False)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.deploy.call_args
        resolver = kwargs["io_resolver"]
        resolver = getattr(resolver, "__wrapped__", resolver)
        assert resolver.func is _resolve_io_interactive
        assert resolver.keywords["mode"] == "serve"
        assert "save_path" not in resolver.keywords
        client_cls.return_value.create_shared_prototype.assert_not_called()

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_pinned_io_config_asks_nothing(self, client_cls: Mock, tmp_path: Path) -> None:
        # An io-config already answers both questions, so even a plain deploy has nothing to ask.
        from haystack_enterprise_sdk.cli import _resolve_io_interactive

        _stub_endpoint(client_cls)
        io_config = tmp_path / "io.yaml"
        io_config.write_text("inputs:\n  query:\n    - r.query\noutputs:\n  answers: g.replies\n", encoding="utf-8")
        client_cls.return_value.deploy.return_value = _result(activated=False)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--io-config", str(io_config)])
        assert result.exit_code == 0
        resolver = client_cls.return_value.deploy.call_args.kwargs["io_resolver"]
        resolver = getattr(resolver, "__wrapped__", resolver)
        assert resolver.func is _resolve_io_interactive
        assert resolver.keywords["mode"] == "warn"

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_share_creates_prototype(self, client_cls: Mock) -> None:
        _stub_endpoint(client_cls)
        client_cls.return_value.deploy.return_value = _result(activated=True)
        client_cls.return_value.create_shared_prototype.return_value = _prototype()
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--share"])
        assert result.exit_code == 0
        assert "https://app.example/shared_prototypes?share_token=tok" in result.stdout
        service_name, options = client_cls.return_value.create_shared_prototype.call_args.args
        assert service_name == "svc"
        assert options.expiration_days == 30
        assert options.login_required is True

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_no_share_question_on_a_plain_deploy(self, client_cls: Mock, _isatty: Mock) -> None:
        # A shared prototype is never created unless asked for, so nothing is asked either. The
        # endpoint is the answer a plain deploy gives instead.
        _stub_endpoint(client_cls)
        client_cls.return_value.deploy.return_value = _result(activated=True)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"], input="y\ny\n")
        assert result.exit_code == 0
        assert "shareable prototype link" not in result.stdout
        assert "Require login" not in result.stdout
        client_cls.return_value.create_shared_prototype.assert_not_called()
        assert "chat/completions" in result.stdout

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_share_asks_only_the_login_question(self, client_cls: Mock, _isatty: Mock) -> None:
        # --share already answered "do you want a link?", so the only thing left to ask is the login
        # requirement — and it comes after the rollout, where it cannot straddle the deploy.
        _stub_endpoint(client_cls)
        client_cls.return_value.deploy.return_value = _result(activated=True)
        client_cls.return_value.create_shared_prototype.return_value = _prototype()
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--share"], input="y\n")
        assert result.exit_code == 0
        assert result.stdout.index("is now DEPLOYED") < result.stdout.index("Require login to open the shared link")
        _, options = client_cls.return_value.create_shared_prototype.call_args.args
        assert options.login_required is True

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_skip_io_validation_forwards_flag_true(self, client_cls: Mock) -> None:
        from haystack_enterprise_sdk.cli import _resolve_io_interactive

        _stub_endpoint(client_cls)
        client_cls.return_value.deploy.return_value = _result(activated=True)
        client_cls.return_value.create_shared_prototype.return_value = _prototype()
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--share", "--skip-io-validation"])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.deploy.call_args
        resolver = kwargs["io_resolver"].__wrapped__
        assert resolver.func is _resolve_io_interactive
        assert resolver.keywords["skip_validation"] is True

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_share_flags_forward_to_options(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.return_value = _result(activated=True)
        client_cls.return_value.create_shared_prototype.return_value = _prototype()
        result = runner.invoke(
            cli_app,
            [
                "deploy",
                FIXTURE,
                "svc",
                "--share",
                "--share-expiration-days",
                "7",
                "--no-share-login-required",
            ],
        )
        assert result.exit_code == 0
        _, options = client_cls.return_value.create_shared_prototype.call_args.args
        assert options.expiration_days == 7
        assert options.login_required is False

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_share_with_skip_activation_errors(self, client_cls: Mock) -> None:
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--share", "--skip-activation"])
        assert result.exit_code == 1
        assert "--share requires activation" in result.stdout
        client_cls.return_value.deploy.assert_not_called()
        client_cls.return_value.create_shared_prototype.assert_not_called()

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_share_skipped_when_not_deployed(self, client_cls: Mock) -> None:
        # Rollout timed out: the service never reached DEPLOYED, so the prototype must not be attempted.
        client_cls.return_value.deploy.return_value = _result(activated=True, timed_out=True)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--share"])
        assert result.exit_code == 0
        assert "Skipped shared prototype" in result.stdout
        client_cls.return_value.create_shared_prototype.assert_not_called()

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_share_failure_warns_but_exits_0(self, client_cls: Mock) -> None:
        # The deploy itself succeeded; a failed share link is a warning, not a failure exit.
        client_cls.return_value.deploy.return_value = _result(activated=True)
        client_cls.return_value.create_shared_prototype.side_effect = FailedToCreateSharedPrototypeError("boom")
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--share"])
        assert result.exit_code == 0
        assert "could not create the shared prototype" in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_prints_chat_completions_endpoint(self, client_cls: Mock) -> None:
        # The endpoint is what a plain deploy hands back instead of asking about the I/O mapping.
        _stub_endpoint(client_cls)
        result = _result(activated=True)
        client_cls.return_value.deploy.return_value = result
        invocation = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert invocation.exit_code == 0
        assert f"POST {BASE_URL}/chat/completions" in invocation.stdout
        assert '"model": "ws/svc"' in invocation.stdout
        # Keyed on the deployment, and the snippet must never bake in a real key.
        client_cls.return_value.deployment_base_url.assert_called_once_with(result.deployment.deployment_id)
        assert "$API_KEY" in invocation.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_no_endpoint_without_activation(self, client_cls: Mock) -> None:
        # The platform needs an active revision to serve chat completions, so printing it would mislead.
        _stub_endpoint(client_cls)
        client_cls.return_value.deploy.return_value = _result(activated=False)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--skip-activation"])
        assert result.exit_code == 0
        assert "chat/completions" not in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_no_endpoint_when_rollout_timed_out(self, client_cls: Mock) -> None:
        _stub_endpoint(client_cls)
        client_cls.return_value.deploy.return_value = _result(activated=True, timed_out=True)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 0
        assert "chat/completions" not in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_chat_pipeline_is_pointed_at_share(self, client_cls: Mock) -> None:
        # The platform classified it as chat, so a chat UI would actually render — worth mentioning.
        _stub_endpoint(client_cls)
        client_cls.return_value.deploy.return_value = _result(activated=True, output_type=PipelineOutputType.CHAT)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 0
        assert "re-run with --share" in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_no_share_hint_for_a_non_chat_pipeline(self, client_cls: Mock) -> None:
        _stub_endpoint(client_cls)
        client_cls.return_value.deploy.return_value = _result(activated=True, output_type=PipelineOutputType.GENERATIVE)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 0
        assert "--share" not in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_share_hint_not_repeated_when_already_shared(self, client_cls: Mock) -> None:
        _stub_endpoint(client_cls)
        client_cls.return_value.deploy.return_value = _result(activated=True, output_type=PipelineOutputType.CHAT)
        client_cls.return_value.create_shared_prototype.return_value = _prototype()
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--share"])
        assert result.exit_code == 0
        assert "re-run with --share" not in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_share_on_non_chat_pipeline_warns_but_still_creates_link(self, client_cls: Mock) -> None:
        # The platform accepts the link regardless, and --share was explicit — so warn, don't refuse.
        _stub_endpoint(client_cls)
        client_cls.return_value.deploy.return_value = _result(activated=True, output_type=PipelineOutputType.DOCUMENT)
        client_cls.return_value.create_shared_prototype.return_value = _prototype()
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--share"])
        assert result.exit_code == 0
        assert "not a chat pipeline" in result.stdout
        assert "https://app.example/shared_prototypes?share_token=tok" in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_share_on_chat_pipeline_does_not_warn(self, client_cls: Mock) -> None:
        _stub_endpoint(client_cls)
        client_cls.return_value.deploy.return_value = _result(activated=True, output_type=PipelineOutputType.CHAT)
        client_cls.return_value.create_shared_prototype.return_value = _prototype()
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--share"])
        assert result.exit_code == 0
        assert "not a chat pipeline" not in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_interrupt_detaches(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.side_effect = KeyboardInterrupt()
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 0
        assert "Detached" in result.stdout


class TestDryRun:
    @patch("haystack_enterprise_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_dry_run_prints_yaml_and_skips_api(self, extract_mock: Mock) -> None:
        extract_mock.return_value = _bundle(
            pipeline={"components": {"c": {"type": "haystack.X", "init_parameters": {}}}},
            dependencies=["haystack-ai==2.30.2"],
        )
        with patch("haystack_enterprise_sdk.cli.DeploymentClient") as client_cls:
            result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run"])
        assert result.exit_code == 0
        assert "haystack.X" in result.stdout
        assert "dependencies:" in result.stdout
        assert "- haystack-ai==2.30.2" in result.stdout
        client_cls.assert_not_called()  # dry-run never touches the API

    @patch("haystack_enterprise_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_dry_run_writes_output_file(self, extract_mock: Mock, tmp_path: Path) -> None:
        extract_mock.return_value = _bundle()
        out = tmp_path / "out.yaml"
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run", "--output", str(out)])
        assert result.exit_code == 0
        assert out.is_file()
        assert "components" in out.read_text()

    @patch("haystack_enterprise_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_dry_run_transform_error_exits_1(self, extract_mock: Mock) -> None:
        extract_mock.side_effect = PipelineTransformError("missing dependency 'tiktoken'")
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run"])
        assert result.exit_code == 1
        assert "tiktoken" in result.stdout

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=False)
    @patch("haystack_enterprise_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_dry_run_non_interactive_skips_prompt(self, extract_mock: Mock, _isatty: Mock) -> None:
        extract_mock.return_value = _bundle(
            available_inputs={"retriever": ["query"]},
            available_outputs={"reader": ["answers"]},
        )
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run"])
        assert result.exit_code == 0
        assert "inputs:" not in result.stdout

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_dry_run_never_prompts_even_on_a_tty(self, extract_mock: Mock, _isatty: Mock) -> None:
        # A dry run renders YAML; it neither serves a chat UI nor sends a query, so the mapping it does
        # not have is nothing to ask about. No stdin is provided on purpose.
        extract_mock.return_value = _bundle(
            available_inputs={"retriever": ["query"]},
            available_outputs={"reader": ["answers", "documents"]},
        )
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run"])
        assert result.exit_code == 0
        assert "Which socket" not in result.stdout
        assert "components" in result.stdout

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_fully_inferred_io_skips_prompt(self, extract_mock: Mock, _isatty: Mock) -> None:
        # Inference covered the (mandatory) question socket, so no interactive prompt is needed.
        extract_mock.return_value = _bundle(
            inferred_inputs={"query": ["prompt_builder.question"]},
            inferred_outputs={"answers": "answer_builder.answers"},
            available_inputs={"prompt_builder": ["question"]},
            available_outputs={"answer_builder": ["answers"]},
            mandatory_inputs={"prompt_builder": ["question"]},
        )
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run"])
        assert result.exit_code == 0
        assert "prompt_builder.question" in result.stdout
        assert "Which socket" not in result.stdout

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_unmapped_mandatory_socket_warns_without_prompting(self, extract_mock: Mock, _isatty: Mock) -> None:
        # A dangling mandatory socket is worth saying out loud, but a dry run has no mapping to fix it
        # for — so it warns and moves on rather than forcing a question.
        extract_mock.return_value = _bundle(
            inferred_inputs={"query": ["answer_builder.query"]},
            inferred_outputs={"answers": "answer_builder.answers"},
            available_inputs={"answer_builder": ["query"], "prompt_builder": ["passage"]},
            available_outputs={"answer_builder": ["answers"]},
            mandatory_inputs={"prompt_builder": ["passage"]},
        )
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run"])
        assert result.exit_code == 0
        assert "Missing mandatory input" in result.stdout
        assert "Which input feeds" not in result.stdout

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=False)
    @patch("haystack_enterprise_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_unmapped_mandatory_socket_warns_non_interactive(self, extract_mock: Mock, _isatty: Mock) -> None:
        extract_mock.return_value = _bundle(
            inferred_inputs={"query": ["answer_builder.query"]},
            inferred_outputs={"answers": "answer_builder.answers"},
            available_inputs={"answer_builder": ["query"], "prompt_builder": ["passage"]},
            available_outputs={"answer_builder": ["answers"]},
            mandatory_inputs={"prompt_builder": ["passage"]},
        )
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run"])
        assert result.exit_code == 0
        assert "prompt_builder.passage" in result.stdout
        assert "Missing mandatory input" in result.stdout

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_skip_io_validation_bypasses_prompt(self, extract_mock: Mock, _isatty: Mock) -> None:
        # On a TTY a dangling mandatory socket would normally force a prompt; --skip-io-validation
        # deploys with whatever was inferred and asks nothing (no stdin provided).
        extract_mock.return_value = _bundle(
            inferred_inputs={"query": ["answer_builder.query"]},
            inferred_outputs={"answers": "answer_builder.answers"},
            available_inputs={"answer_builder": ["query"], "prompt_builder": ["passage"]},
            available_outputs={"answer_builder": ["answers"]},
            mandatory_inputs={"prompt_builder": ["passage"]},
        )
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run", "--skip-io-validation"])
        assert result.exit_code == 0
        assert "Which socket" not in result.stdout
        assert "Which input feeds" not in result.stdout
        assert "answer_builder.query" in result.stdout

    def test_resolve_io_skip_validation_returns_inferred(self) -> None:
        from haystack_enterprise_sdk.cli import _resolve_io_interactive

        extraction = _bundle(
            inferred_inputs={"query": ["answer_builder.query"]},
            inferred_outputs={"answers": "answer_builder.answers"},
            mandatory_inputs={"prompt_builder": ["passage"]},  # unmapped, but validation skipped
        )
        # The resolver now receives the already-resolved mappings and, with skip_validation, echoes them.
        inputs, outputs = _resolve_io_interactive(
            extraction,
            {"query": ["answer_builder.query"]},
            {"answers": "answer_builder.answers"},
            skip_validation=True,
        )
        assert inputs == {"query": ["answer_builder.query"]}
        assert outputs == {"answers": "answer_builder.answers"}


class TestValidateCommand:
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_validate_valid_exits_0(self, client_cls: Mock) -> None:
        client_cls.return_value.validate.return_value = PipelineValidationResult(issues=[])
        result = runner.invoke(cli_app, ["validate", FIXTURE])
        assert result.exit_code == 0
        assert "valid" in result.stdout.lower()

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_validate_error_exits_1(self, client_cls: Mock) -> None:
        issues = [PipelineValidationIssue(category="ERROR", code=None, json_pointer="/c", message="broken")]
        client_cls.return_value.validate.return_value = PipelineValidationResult(issues=issues)
        result = runner.invoke(cli_app, ["validate", FIXTURE])
        assert result.exit_code == 1
        assert "broken" in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_validate_warning_only_exits_0(self, client_cls: Mock) -> None:
        issues = [PipelineValidationIssue(category="WARNING", code=None, json_pointer=None, message="deprecated")]
        client_cls.return_value.validate.return_value = PipelineValidationResult(issues=issues)
        result = runner.invoke(cli_app, ["validate", FIXTURE])
        assert result.exit_code == 0
        assert "deprecated" in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_validate_transform_error_exits_1(self, client_cls: Mock) -> None:
        client_cls.return_value.validate.side_effect = PipelineTransformError("cannot transform")
        result = runner.invoke(cli_app, ["validate", FIXTURE])
        assert result.exit_code == 1
        assert "cannot transform" in result.stdout


class TestServiceStatusCommand:
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_service_status(self, client_cls: Mock) -> None:
        client_cls.return_value.get_service_status.return_value = _deployment(DeploymentStatus.DEPLOYED)
        result = runner.invoke(cli_app, ["service-status", "svc"])
        assert result.exit_code == 0
        assert "DEPLOYED" in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_service_status_reports_deployment_mode(self, client_cls: Mock) -> None:
        client_cls.return_value.get_service_status.return_value = _deployment(
            DeploymentStatus.DEPLOYED, mode=DeploymentMode.SERVERLESS
        )
        result = runner.invoke(cli_app, ["service-status", "svc"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["deployment_mode"] == "SERVERLESS"

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_service_status_not_found(self, client_cls: Mock) -> None:
        client_cls.return_value.get_service_status.side_effect = ServiceNotFoundError("missing")
        result = runner.invoke(cli_app, ["service-status", "svc"])
        assert result.exit_code == 1
        assert "missing" in result.stdout


class TestRunCommand:
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_prints_output_json(self, client_cls: Mock) -> None:
        client_cls.return_value.run.return_value = {"llm": {"replies": ["hi"]}}
        result = runner.invoke(cli_app, ["run", FIXTURE, "--query", "who?"])
        assert result.exit_code == 0
        assert '"replies"' in result.stdout
        _, kwargs = client_cls.return_value.run.call_args
        assert kwargs["query"] == "who?"
        assert kwargs["extra_inputs"] is None

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_parses_inline_inputs_json(self, client_cls: Mock) -> None:
        client_cls.return_value.run.return_value = {}
        result = runner.invoke(cli_app, ["run", FIXTURE, "--inputs", '{"retriever": {"top_k": 3}}'])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.run.call_args
        assert kwargs["extra_inputs"] == {"retriever": {"top_k": 3}}

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_reads_inputs_from_file(self, client_cls: Mock, tmp_path: Path) -> None:
        client_cls.return_value.run.return_value = {}
        inputs_file = tmp_path / "inputs.json"
        inputs_file.write_text('{"llm": {"prompt": "hi"}}', encoding="utf-8")
        result = runner.invoke(cli_app, ["run", FIXTURE, "--inputs", f"@{inputs_file}"])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.run.call_args
        assert kwargs["extra_inputs"] == {"llm": {"prompt": "hi"}}

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_invalid_inputs_json_exits(self, client_cls: Mock) -> None:
        result = runner.invoke(cli_app, ["run", FIXTURE, "--inputs", "{not json"])
        assert result.exit_code == 1
        assert "not valid JSON" in result.stdout
        client_cls.return_value.run.assert_not_called()

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_prompts_for_query_on_tty(self, client_cls: Mock, _tty: Mock) -> None:
        client_cls.return_value.run.return_value = {}
        result = runner.invoke(cli_app, ["run", FIXTURE], input="my question\n")
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.run.call_args
        assert kwargs["query"] == "my question"

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_with_a_query_resolves_the_query_socket(self, client_cls: Mock) -> None:
        from haystack_enterprise_sdk.cli import _resolve_io_interactive

        client_cls.return_value.run.return_value = {}
        result = runner.invoke(cli_app, ["run", FIXTURE, "--query", "q"])
        assert result.exit_code == 0
        resolver = client_cls.return_value.run.call_args.kwargs["io_resolver"]
        assert resolver.func is _resolve_io_interactive
        assert resolver.keywords["mode"] == "query"

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_with_inputs_only_asks_nothing(self, client_cls: Mock) -> None:
        # Explicit --inputs already name their sockets, so there is no query to route and nothing to ask.
        from haystack_enterprise_sdk.cli import _resolve_io_interactive

        client_cls.return_value.run.return_value = {}
        result = runner.invoke(cli_app, ["run", FIXTURE, "--inputs", '{"retriever": {"query": "q"}}'])
        assert result.exit_code == 0
        resolver = client_cls.return_value.run.call_args.kwargs["io_resolver"]
        assert resolver.func is _resolve_io_interactive
        assert resolver.keywords["mode"] == "warn"


class TestEnsureQueryInput:
    """`run`'s single question: which socket a --query is routed to (mode="query")."""

    def _resolve(self, bundle: ExtractionBundle, inputs: dict, input_: str) -> dict:
        """Drive the resolver in query mode over ``input_``, returning the resulting inputs mapping."""
        from haystack_enterprise_sdk.cli import _resolve_io_interactive

        # typer.prompt reads click's stdin, so borrow the runner's isolation to feed the menu answers.
        with patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True), runner.isolation(input=input_):
            resolved, _ = _resolve_io_interactive(bundle, inputs, {}, mode="query")
        return resolved

    def test_query_mode_never_asks_about_outputs(self) -> None:
        # A run prints raw pipeline output, so the outputs mapping is irrelevant there.
        bundle = _bundle(
            inferred_inputs={"query": ["retriever.query"]},
            available_outputs={"reader": {"answers": {"type": "list"}}},
        )
        assert self._resolve(bundle, {"query": ["retriever.query"]}, "") == {"query": ["retriever.query"]}

    def test_asks_nothing_when_query_is_already_inferred(self) -> None:
        bundle = _bundle(available_inputs={"retriever": {"query": {"type": "str"}}})
        assert self._resolve(bundle, {"query": ["retriever.query"]}, "") == {"query": ["retriever.query"]}

    def test_asks_nothing_when_messages_is_already_inferred(self) -> None:
        # build_run_inputs wraps a --query into a ChatMessage for the 'messages' key, so this is enough.
        bundle = _bundle(available_inputs={"agent": {"messages": {"type": "List[ChatMessage]"}}})
        assert self._resolve(bundle, {"messages": ["agent.messages"]}, "") == {"messages": ["agent.messages"]}

    def test_maps_a_plain_socket_to_query(self) -> None:
        bundle = _bundle(available_inputs={"prompt_builder": {"passage": {"type": "str"}}})
        assert self._resolve(bundle, {}, "1\n") == {"query": ["prompt_builder.passage"]}

    def test_maps_a_chat_message_socket_to_messages(self) -> None:
        # A List[ChatMessage] socket needs the query wrapped as a chat message — that is the 'messages' key.
        bundle = _bundle(available_inputs={"agent": {"history": {"type": "List[ChatMessage]"}}})
        assert self._resolve(bundle, {}, "1\n") == {"messages": ["agent.history"]}

    def test_skipping_leaves_the_mapping_alone(self) -> None:
        # build_run_inputs then raises its own "no inputs to send" error, which names --inputs.
        bundle = _bundle(available_inputs={"prompt_builder": {"passage": {"type": "str"}}})
        assert self._resolve(bundle, {}, "0\n") == {}

    def test_never_asks_about_outputs(self) -> None:
        from haystack_enterprise_sdk.cli import _resolve_io_interactive

        bundle = _bundle(
            available_inputs={"retriever": {"query": {"type": "str"}}},
            available_outputs={"reader": {"answers": {"type": "list"}}},
        )
        with patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True):
            with runner.isolation(input="1\n") as outstreams:
                _resolve_io_interactive(bundle, {}, {}, mode="query")
            printed = outstreams[0].getvalue().decode()
        assert "answers" not in printed

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_writes_output_file(self, client_cls: Mock, tmp_path: Path) -> None:
        client_cls.return_value.run.return_value = {"a": 1}
        out = tmp_path / "out.json"
        result = runner.invoke(cli_app, ["run", FIXTURE, "--query", "q", "--output", str(out)])
        assert result.exit_code == 0
        assert '"a": 1' in out.read_text(encoding="utf-8")

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_surfaces_run_error(self, client_cls: Mock) -> None:
        client_cls.return_value.run.side_effect = PipelineRunError("missing secret")
        result = runner.invoke(cli_app, ["run", FIXTURE, "--query", "q"])
        assert result.exit_code == 1
        assert "missing secret" in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_passes_retries_through(self, client_cls: Mock) -> None:
        client_cls.return_value.run.return_value = {}
        result = runner.invoke(cli_app, ["run", FIXTURE, "--query", "q", "--retries", "5"])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.run.call_args
        assert kwargs["retries"] == 5

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_defaults_to_three_attempts(self, client_cls: Mock) -> None:
        client_cls.return_value.run.return_value = {}
        runner.invoke(cli_app, ["run", FIXTURE, "--query", "q"])
        _, kwargs = client_cls.return_value.run.call_args
        assert kwargs["retries"] == DEFAULT_RUN_RETRIES

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_output_stays_parseable_json_off_tty(self, client_cls: Mock) -> None:
        # Piped output (jq, redirect) must not contain spinner escape codes.
        client_cls.return_value.run.return_value = {"llm": {"replies": ["hi"]}}
        result = runner.invoke(cli_app, ["run", FIXTURE, "--query", "q"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"llm": {"replies": ["hi"]}}
        _, kwargs = client_cls.return_value.run.call_args
        assert kwargs["on_retry"] is None

    @patch("haystack_enterprise_sdk.cli._stdout_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_shows_spinner_on_tty(self, client_cls: Mock, _tty: Mock) -> None:
        client_cls.return_value.run.return_value = {}
        result = runner.invoke(cli_app, ["run", FIXTURE, "--query", "q"])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.run.call_args
        # On a terminal the retry hook is wired up and the I/O resolver is spinner-aware.
        assert kwargs["on_retry"] is not None
        assert hasattr(kwargs["io_resolver"], "__wrapped__")

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_cancelled_exits_cleanly(self, client_cls: Mock) -> None:
        client_cls.return_value.run.side_effect = KeyboardInterrupt()
        result = runner.invoke(cli_app, ["run", FIXTURE, "--query", "q"])
        assert result.exit_code == 130
        assert "Cancelled" in result.stdout

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_parses_repeated_set_options(self, client_cls: Mock) -> None:
        client_cls.return_value.run.return_value = {}
        result = runner.invoke(
            cli_app,
            ["run", FIXTURE, "--query", "q", "--set", "github_token=ghs_abc", "--set", "top_k=5"],
        )
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.run.call_args
        assert kwargs["named_inputs"] == {"github_token": "ghs_abc", "top_k": "5"}

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_reads_set_value_from_file(self, client_cls: Mock, tmp_path: Path) -> None:
        client_cls.return_value.run.return_value = {}
        prompt_file = tmp_path / "security.md"
        prompt_file.write_text("Be thorough.", encoding="utf-8")
        result = runner.invoke(cli_app, ["run", FIXTURE, "--query", "q", "--set", f"security_prompt=@{prompt_file}"])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.run.call_args
        assert kwargs["named_inputs"] == {"security_prompt": "Be thorough."}

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_set_missing_file_exits(self, client_cls: Mock, tmp_path: Path) -> None:
        missing = tmp_path / "missing.md"
        result = runner.invoke(cli_app, ["run", FIXTURE, "--set", f"security_prompt=@{missing}"])
        assert result.exit_code == 1
        assert "Could not read --set" in result.stdout
        client_cls.return_value.run.assert_not_called()

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_set_without_equals_exits(self, client_cls: Mock) -> None:
        result = runner.invoke(cli_app, ["run", FIXTURE, "--set", "github_token"])
        assert result.exit_code == 1
        assert "not in KEY=VALUE form" in result.stdout
        client_cls.return_value.run.assert_not_called()

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_run_set_suppresses_the_interactive_query_prompt(self, client_cls: Mock, _tty: Mock) -> None:
        # --set alone is a valid, complete input source (e.g. a chat pipeline whose only mandatory
        # input is a custom one) -- it must not still prompt for a --query nobody asked for.
        client_cls.return_value.run.return_value = {}
        result = runner.invoke(cli_app, ["run", FIXTURE, "--set", "github_token=ghs_abc"])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.run.call_args
        assert kwargs["query"] is None


def _typed_bundle() -> ExtractionBundle:
    """A bundle with typed available sockets and a fully inferred mapping, for review-flow tests."""
    return _bundle(
        inferred_inputs={"query": ["retriever.query"]},
        inferred_outputs={"answers": "reader.answers"},
        available_inputs={
            "retriever": {"query": {"type": "str", "is_mandatory": True}},
            "prompt_builder": {"question": {"type": "str", "is_mandatory": False}},
        },
        available_outputs={"reader": {"answers": {"type": "List[GeneratedAnswer]", "is_mandatory": False}}},
        mandatory_inputs={"retriever": ["query"]},
    )


class TestServeMode:
    """A plain `deploy` asks only for what the platform requires of a servable pipeline (mode="serve")."""

    def _resolve(self, bundle: ExtractionBundle, inputs: dict, outputs: dict, input_: str) -> tuple:
        from haystack_enterprise_sdk.cli import _resolve_io_interactive

        with patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True), runner.isolation(input=input_):
            return _resolve_io_interactive(bundle, inputs, outputs, mode="serve")

    def _servable_bundle(self) -> ExtractionBundle:
        return _bundle(
            available_inputs={"greeter": {"name": {"type": "str", "is_mandatory": True}}},
            available_outputs={"prompt_builder": {"prompt": {"type": "str"}}},
        )

    def test_a_fully_inferred_mapping_asks_nothing(self) -> None:
        # The common case: sockets named `query`/`answers` are already mapped by inference.
        bundle = _bundle(
            available_inputs={"retriever": {"query": {"type": "str"}}},
            available_outputs={"reader": {"answers": {"type": "list"}}},
        )
        inputs, outputs = self._resolve(bundle, {"query": ["retriever.query"]}, {"answers": "reader.answers"}, "")
        assert inputs == {"query": ["retriever.query"]}
        assert outputs == {"answers": "reader.answers"}

    def test_asks_for_both_when_nothing_was_inferred(self) -> None:
        inputs, outputs = self._resolve(self._servable_bundle(), {}, {}, "1\n1\n")
        assert inputs == {"query": ["greeter.name"]}
        assert outputs == {"answers": "prompt_builder.prompt"}

    def test_asks_only_for_the_missing_half(self) -> None:
        # The query input was inferred, so only the output question is left — one answer is enough.
        inputs, outputs = self._resolve(self._servable_bundle(), {"query": ["greeter.name"]}, {}, "1\n")
        assert inputs == {"query": ["greeter.name"]}
        assert outputs == {"answers": "prompt_builder.prompt"}

    def test_an_existing_output_of_any_kind_is_enough(self) -> None:
        bundle = _bundle(
            available_inputs={"greeter": {"name": {"type": "str"}}},
            available_outputs={"retriever": {"documents": {"type": "List[Document]"}}},
        )
        _, outputs = self._resolve(bundle, {"query": ["greeter.name"]}, {"documents": "retriever.documents"}, "")
        assert outputs == {"documents": "retriever.documents"}

    def test_output_key_follows_the_socket_type(self) -> None:
        bundle = _bundle(
            available_inputs={"greeter": {"name": {"type": "str"}}},
            available_outputs={"retriever": {"docs": {"type": "List[Document]"}}},
        )
        _, outputs = self._resolve(bundle, {"query": ["greeter.name"]}, {}, "1\n")
        assert outputs == {"documents": "retriever.docs"}

    def test_chat_message_output_maps_to_messages(self) -> None:
        bundle = _bundle(
            available_inputs={"agent": {"text": {"type": "str"}}},
            available_outputs={"agent": {"replies": {"type": "List[ChatMessage]"}}},
        )
        _, outputs = self._resolve(bundle, {"query": ["agent.text"]}, {}, "1\n")
        assert outputs == {"messages": "agent.replies"}

    def test_skipping_leaves_the_mapping_empty(self) -> None:
        # Deliberate skip: the platform will reject it at validation, which says exactly what is missing.
        inputs, outputs = self._resolve(self._servable_bundle(), {}, {}, "0\n0\n")
        assert inputs == {}
        assert outputs == {}

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=False)
    def test_off_a_tty_it_only_warns(self, _tty: Mock) -> None:
        from haystack_enterprise_sdk.cli import _resolve_io_interactive

        bundle = _bundle(
            available_inputs={"greeter": {"name": {"type": "str"}}},
            mandatory_inputs={"greeter": ["name"]},
        )
        with runner.isolation() as outstreams:
            inputs, outputs = _resolve_io_interactive(bundle, {}, {}, mode="serve")
            printed = outstreams[0].getvalue().decode()
        assert (inputs, outputs) == ({}, {})
        assert "Missing mandatory input" in printed
        assert "Which socket" not in printed


class TestDeployReviewFlow:
    """The I/O mapping review, which `deploy --share` turns on (review mode of _resolve_io_interactive)."""

    # --share is what asks for the review; --share-login-required answers the one question that would
    # otherwise follow the rollout, so each test's input script covers only the review itself.
    SHARE_ARGS = ["--share", "--share-login-required"]

    def _invoke_with_resolver(self, client_cls: Mock, args: list, input_: str) -> tuple:
        """Invoke deploy with a client mock that exercises the io_resolver like the real service."""
        captured = {}

        def fake_deploy(target, service_name, **kwargs):  # type: ignore[no-untyped-def]
            # Mirrors resolve_io: explicit inputs/outputs replace inference, then the resolver (which is
            # always set now — only its mode varies) gets the final say.
            inputs = kwargs["inputs"] if kwargs["inputs"] is not None else {"query": ["retriever.query"]}
            outputs = kwargs["outputs"] if kwargs["outputs"] is not None else {"answers": "reader.answers"}
            captured["io"] = kwargs["io_resolver"](_typed_bundle(), inputs, outputs)
            return _result(activated=True)

        _stub_endpoint(client_cls)
        client_cls.return_value.create_shared_prototype.return_value = _prototype()
        client_cls.return_value.deploy.side_effect = fake_deploy
        result = runner.invoke(cli_app, args, input=input_)
        return result, captured.get("io")

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_review_shows_summary_and_enter_accepts(self, client_cls: Mock, _tty: Mock, tmp_path: Path) -> None:
        target = tmp_path / "pipeline.py"
        target.write_text("# stub\n", encoding="utf-8")
        # Enter accepts the mapping; 'n' declines saving it.
        result, io = self._invoke_with_resolver(client_cls, ["deploy", str(target), "svc", *self.SHARE_ARGS], "\nn\n")
        assert result.exit_code == 0
        assert "I/O mapping (how the platform talks to your pipeline)" in result.stdout
        assert "The user's question/text sent by the Playground and chat UI (str)" in result.stdout
        assert "(not mapped)" in result.stdout  # unmapped keys are listed too
        assert io == ({"query": ["retriever.query"]}, {"answers": "reader.answers"})
        assert not (tmp_path / "pipeline.io.yaml").exists()

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_review_edit_remaps_with_typed_menu(self, client_cls: Mock, _tty: Mock, tmp_path: Path) -> None:
        target = tmp_path / "pipeline.py"
        target.write_text("# stub\n", encoding="utf-8")
        # 'e' edits; remap query to option 1 (prompt_builder.question, sorted first), keep/skip the
        # rest (Enter), accept the re-rendered summary, decline save. The mandatory retriever.query
        # socket is then unmapped, so the mandatory gate prompts (1 = feed it from 'query').
        edit_input = "e\n" + "1\n" + "\n" * 3 + "\n" * 3 + "\n" + "1\n" + "n\n"
        result, io = self._invoke_with_resolver(
            client_cls, ["deploy", str(target), "svc", *self.SHARE_ARGS], edit_input
        )
        assert result.exit_code == 0
        assert "retriever.query (str, mandatory)" in result.stdout  # typed menu labels
        assert "[current]" in result.stdout
        inputs, outputs = io
        assert "prompt_builder.question" in inputs["query"]
        assert "retriever.query" in inputs["query"]  # re-added by the mandatory gate
        assert outputs == {"answers": "reader.answers"}

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_review_save_writes_io_config(self, client_cls: Mock, _tty: Mock, tmp_path: Path) -> None:
        target = tmp_path / "pipeline.py"
        target.write_text("# stub\n", encoding="utf-8")
        result, _ = self._invoke_with_resolver(client_cls, ["deploy", str(target), "svc", *self.SHARE_ARGS], "\ny\n")
        assert result.exit_code == 0
        saved = tmp_path / "pipeline.io.yaml"
        assert saved.is_file()
        content = saved.read_text(encoding="utf-8")
        assert "- retriever.query" in content
        assert "answers: reader.answers" in content
        assert "# The user's question/text sent by the Playground and chat UI (str)" in content

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_saved_io_config_auto_detected_and_skips_review(self, client_cls: Mock, _tty: Mock, tmp_path: Path) -> None:
        target = tmp_path / "pipeline.py"
        target.write_text("# stub\n", encoding="utf-8")
        (tmp_path / "pipeline.io.yaml").write_text(
            "inputs:\n  query:\n    - retriever.query\noutputs:\n  answers: reader.answers\n", encoding="utf-8"
        )
        result, io = self._invoke_with_resolver(client_cls, ["deploy", str(target), "svc", *self.SHARE_ARGS], "")
        assert result.exit_code == 0
        assert "Using I/O mapping from" in result.stdout
        assert "I/O mapping (how the platform talks to your pipeline)" not in result.stdout  # no review
        assert io == ({"query": ["retriever.query"]}, {"answers": "reader.answers"})

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_explicit_io_config_beats_auto_detected(self, client_cls: Mock, _tty: Mock, tmp_path: Path) -> None:
        target = tmp_path / "pipeline.py"
        target.write_text("# stub\n", encoding="utf-8")
        (tmp_path / "pipeline.io.yaml").write_text("inputs:\n  query:\n    - wrong.socket\n", encoding="utf-8")
        other = tmp_path / "other.yaml"
        other.write_text("inputs:\n  query:\n    - right.socket\n", encoding="utf-8")
        result, io = self._invoke_with_resolver(
            client_cls, ["deploy", str(target), "svc", *self.SHARE_ARGS, "--io-config", str(other)], ""
        )
        assert result.exit_code == 0
        assert "Using I/O mapping from" not in result.stdout
        assert io[0] == {"query": ["right.socket"]}

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=False)
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_non_tty_deploy_never_prompts(self, client_cls: Mock, _tty: Mock, tmp_path: Path) -> None:
        target = tmp_path / "pipeline.py"
        target.write_text("# stub\n", encoding="utf-8")
        result, io = self._invoke_with_resolver(client_cls, ["deploy", str(target), "svc", *self.SHARE_ARGS], "")
        assert result.exit_code == 0
        assert "I/O mapping (how the platform talks to your pipeline)" not in result.stdout
        assert io == ({"query": ["retriever.query"]}, {"answers": "reader.answers"})


class TestLoadIoConfig:
    def test_loads_yaml_inputs_and_outputs(self, tmp_path: Path) -> None:
        from haystack_enterprise_sdk.cli import _load_io_config

        cfg = tmp_path / "io.yaml"
        cfg.write_text(
            "inputs:\n"
            "  query:\n"
            "    - retriever.query\n"
            "  filters: retriever.filters\n"  # scalar coerced to a list
            "outputs:\n"
            "  documents: retriever.documents\n",
            encoding="utf-8",
        )
        cfg_io = _load_io_config(cfg)
        assert cfg_io.inputs == {"query": ["retriever.query"], "filters": ["retriever.filters"]}
        assert cfg_io.outputs == {"documents": "retriever.documents"}
        assert cfg_io.settings.pipeline_output_type is None

    def test_loads_json(self, tmp_path: Path) -> None:
        from haystack_enterprise_sdk.cli import _load_io_config

        cfg = tmp_path / "io.json"
        cfg.write_text('{"inputs": {"query": ["r.query"]}, "outputs": {"answers": "r.answers"}}', encoding="utf-8")
        cfg_io = _load_io_config(cfg)
        assert cfg_io.inputs == {"query": ["r.query"]}
        assert cfg_io.outputs == {"answers": "r.answers"}

    def test_absent_sections_return_none(self, tmp_path: Path) -> None:
        from haystack_enterprise_sdk.cli import _load_io_config

        cfg = tmp_path / "io.yaml"
        cfg.write_text("outputs:\n  answers: r.answers\n", encoding="utf-8")
        cfg_io = _load_io_config(cfg)
        assert cfg_io.inputs is None
        assert cfg_io.outputs == {"answers": "r.answers"}

    def test_invalid_shape_exits(self, tmp_path: Path) -> None:
        import typer

        from haystack_enterprise_sdk.cli import _load_io_config

        cfg = tmp_path / "io.yaml"
        cfg.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(typer.Exit):
            _load_io_config(cfg)

    def test_pipeline_output_type_validated(self, tmp_path: Path) -> None:
        import typer

        from haystack_enterprise_sdk.cli import _load_io_config

        cfg = tmp_path / "io.yaml"
        cfg.write_text("outputs:\n  answers: r.answers\npipeline_output_type: generative\n", encoding="utf-8")
        cfg_io = _load_io_config(cfg)
        assert cfg_io.settings.pipeline_output_type == "generative"

        cfg.write_text("outputs:\n  answers: r.answers\npipeline_output_type: bogus\n", encoding="utf-8")
        with pytest.raises(typer.Exit):
            _load_io_config(cfg)

    def test_session_storage_validated(self, tmp_path: Path) -> None:
        import typer

        from haystack_enterprise_sdk.cli import _load_io_config

        cfg = tmp_path / "io.yaml"
        cfg.write_text("outputs:\n  answers: r.answers\nsession_storage: true\n", encoding="utf-8")
        assert _load_io_config(cfg).settings.session_storage is True

        # Absent means off, so it stays None rather than defaulting to False.
        cfg.write_text("outputs:\n  answers: r.answers\n", encoding="utf-8")
        assert _load_io_config(cfg).settings.session_storage is None

        # Rejected rather than coerced: a non-bool here is a typo, not an intent.
        cfg.write_text("outputs:\n  answers: r.answers\nsession_storage: sure\n", encoding="utf-8")
        with pytest.raises(typer.Exit):
            _load_io_config(cfg)

    def test_dependencies_validated(self, tmp_path: Path) -> None:
        import typer

        from haystack_enterprise_sdk.cli import _load_io_config

        cfg = tmp_path / "io.yaml"
        cfg.write_text("dependencies:\n  - haystack-ai==2.30.2\n  - foo==1.0\n", encoding="utf-8")
        assert _load_io_config(cfg).settings.dependencies == ["haystack-ai==2.30.2", "foo==1.0"]

        # An explicit empty list is a declaration ("pin nothing"); a commented-out section is not.
        cfg.write_text("dependencies: []\n", encoding="utf-8")
        assert _load_io_config(cfg).settings.dependencies == []
        cfg.write_text("outputs:\n  answers: r.answers\n", encoding="utf-8")
        assert _load_io_config(cfg).settings.dependencies is None

        for bad in ("dependencies: haystack-ai==2.30.2\n", "dependencies:\n  - 3\n"):
            cfg.write_text(bad, encoding="utf-8")
            with pytest.raises(typer.Exit):
                _load_io_config(cfg)

    def test_unknown_root_key_is_passed_through_with_a_note(self, tmp_path: Path) -> None:
        """The gap this closes: an unknown top-level key used to vanish without a word, so a setting the
        platform understands and this SDK does not could be written in good faith and silently ignored."""
        from haystack_enterprise_sdk.cli import _load_io_config

        cfg = tmp_path / "io.yaml"
        cfg.write_text("outputs:\n  answers: r.answers\nsome_future_key: 42\n", encoding="utf-8")
        assert _load_io_config(cfg).settings.extra == {"some_future_key": 42}

    def test_root_key_the_sdk_owns_is_rejected(self, tmp_path: Path) -> None:
        # Passing these through would have the renderer overwrite them, honouring the file only in part.
        import typer

        from haystack_enterprise_sdk.cli import _load_io_config

        cfg = tmp_path / "io.yaml"
        for key in ("components", "async_enabled"):
            cfg.write_text(f"outputs:\n  answers: r.answers\n{key}: something\n", encoding="utf-8")
            with pytest.raises(typer.Exit):
                _load_io_config(cfg)

    def test_messages_only_outputs_load(self, tmp_path: Path) -> None:
        # A chat pipeline mapping only `messages` must load (no answers/documents requirement).
        from haystack_enterprise_sdk.cli import _load_io_config

        cfg = tmp_path / "io.yaml"
        cfg.write_text("outputs:\n  messages: llm.replies\n", encoding="utf-8")
        cfg_io = _load_io_config(cfg)
        assert cfg_io.outputs == {"messages": "llm.replies"}

    @patch("haystack_enterprise_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_dry_run_io_config_overrides_inference(self, extract_mock: Mock, tmp_path: Path) -> None:
        extract_mock.return_value = _bundle(
            inferred_inputs={"query": ["retriever.query"]},
            inferred_outputs={"answers": "reader.answers"},
        )
        cfg = tmp_path / "io.yaml"
        cfg.write_text(
            "inputs:\n  query:\n    - prompt_builder.query\noutputs:\n  answers: prompt_builder.prompt\n"
            "pipeline_output_type: generative\n",
            encoding="utf-8",
        )
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run", "--io-config", str(cfg)])
        assert result.exit_code == 0
        assert "prompt_builder.query" in result.stdout
        # The io-config outputs replace the inferred ones wholesale, and the output type is rendered.
        assert "answers: prompt_builder.prompt" in result.stdout
        assert "reader.answers" not in result.stdout
        assert "pipeline_output_type: generative" in result.stdout

    @patch("haystack_enterprise_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_dry_run_renders_passthrough_root_keys_from_io_config(self, extract_mock: Mock, tmp_path: Path) -> None:
        """End-to-end for the passthrough channel: a root key the SDK has no field for still has to reach
        the rendered YAML, and the note has to say so rather than leaving the author guessing."""
        extract_mock.return_value = _bundle(inferred_outputs={"answers": "reader.answers"})
        cfg = tmp_path / "io.yaml"
        cfg.write_text("outputs:\n  answers: reader.answers\nsome_future_key: 42\n", encoding="utf-8")
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run", "--io-config", str(cfg)])
        assert result.exit_code == 0
        assert "some_future_key: 42" in result.stdout
        assert "not a setting this SDK knows" in result.stdout

    @patch("haystack_enterprise_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_dry_run_renders_declared_dependencies_from_io_config(self, extract_mock: Mock, tmp_path: Path) -> None:
        # The declared pins replace the inferred one rather than joining it.
        extract_mock.return_value = _bundle(
            inferred_outputs={"answers": "reader.answers"}, dependencies=["haystack-ai==2.30.2"]
        )
        cfg = tmp_path / "io.yaml"
        cfg.write_text("outputs:\n  answers: reader.answers\ndependencies:\n  - haystack-ai==1.2.3\n", encoding="utf-8")
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run", "--io-config", str(cfg)])
        assert result.exit_code == 0
        assert "- haystack-ai==1.2.3" in result.stdout
        assert "2.30.2" not in result.stdout

    @patch("haystack_enterprise_sdk._service.pipeline_transform.extract_via_subprocess")
    def test_dry_run_renders_session_storage_from_io_config(self, extract_mock: Mock, tmp_path: Path) -> None:
        """The whole point of the key: an io-config asking for a per-session workspace has to reach the
        deployed YAML, since that is the only place the platform looks for it."""
        extract_mock.return_value = _bundle(
            inferred_inputs={"query": ["retriever.query"]},
            inferred_outputs={"answers": "reader.answers"},
        )
        cfg = tmp_path / "io.yaml"
        cfg.write_text("outputs:\n  answers: reader.answers\nsession_storage: true\n", encoding="utf-8")
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run", "--io-config", str(cfg)])
        assert result.exit_code == 0
        assert "session_storage: true" in result.stdout


class TestSpinnerPausedResolver:
    """The deploy spinner must be paused while the interactive I/O resolver prompts."""

    def test_hides_spinner_around_resolver_and_returns_result(self) -> None:
        from haystack_enterprise_sdk.cli import _spinner_paused_resolver

        events: list[str] = []

        class _FakeSpinnerCtx:
            def __enter__(self) -> "None":
                events.append("hidden-enter")

            def __exit__(self, *exc: object) -> Literal[False]:
                events.append("hidden-exit")
                return False

        spinner = Mock()
        spinner.hidden.return_value = _FakeSpinnerCtx()

        def inner(bundle: object, inputs: dict, outputs: dict) -> tuple:
            events.append("resolve")
            return {"query": ["a.q"]}, {"answers": "b.a"}

        wrapped = _spinner_paused_resolver(spinner, inner)
        result = wrapped(_bundle(), {}, {})

        # The resolver runs strictly inside the hidden() context, and its result is passed through.
        assert events == ["hidden-enter", "resolve", "hidden-exit"]
        assert result == ({"query": ["a.q"]}, {"answers": "b.a"})
        # The underlying resolver stays introspectable.
        assert getattr(wrapped, "__wrapped__") is inner
