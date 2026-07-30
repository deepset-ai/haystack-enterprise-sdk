"""CLI tests for the `deploy` and `service-status` commands."""

from pathlib import Path
from typing import Literal
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from haystack_enterprise_sdk._api.deployments import (
    Deployment,
    DeploymentRevision,
    DeploymentRevisionStatus,
    DeploymentServiceLevel,
    DeploymentStatus,
    PipelineValidationError,
    PipelineValidationIssue,
    PipelineValidationResult,
)
from haystack_enterprise_sdk._api.pipeline_run import PipelineRunError
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

runner = CliRunner()

FIXTURE = "tests/test_data/deploy/pipeline.py"


def _bundle(**raw: object) -> ExtractionBundle:
    """An ExtractionBundle for extract_via_subprocess mocks, from the raw dict keys."""
    return ExtractionBundle.from_dict({"pipeline": {"components": {}}, **raw})  # type: ignore[arg-type]


def _deployment(status: DeploymentStatus = DeploymentStatus.DEPLOYED) -> Deployment:
    return Deployment(
        deployment_id=uuid4(),
        name="svc",
        status=status,
        service_level=DeploymentServiceLevel.DEVELOPMENT,
        active_revision_id=uuid4(),
        pending_revision_id=None,
    )


def _result(
    activated: bool, timed_out: bool = False, status: DeploymentStatus = DeploymentStatus.DEPLOYED
) -> DeployResult:
    deployment = _deployment(status)
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
    def test_deploy_create_passes_options(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.return_value = _result(activated=True)
        result = runner.invoke(
            cli_app,
            [
                "deploy",
                FIXTURE,
                "svc",
                "--create",
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
        assert options.service_level == DeploymentServiceLevel.PRODUCTION
        assert options.cpu_limit == "2"
        assert options.max_query_replica_count == 3

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_forwards_review_io_resolver(self, client_cls: Mock) -> None:
        # Every deploy gets the interactive resolver in review mode (no longer coupled to --share).
        from haystack_enterprise_sdk.cli import _resolve_io_interactive

        client_cls.return_value.deploy.return_value = _result(activated=False)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc"])
        assert result.exit_code == 0
        _, kwargs = client_cls.return_value.deploy.call_args
        # The resolver is wrapped so the deploy spinner is paused while it prompts; unwrap to inspect it.
        resolver = kwargs["io_resolver"].__wrapped__
        assert resolver.func is _resolve_io_interactive
        assert resolver.keywords["mode"] == "review"
        assert resolver.keywords["skip_validation"] is False
        assert resolver.keywords["save_path"] == Path(FIXTURE).with_suffix(".io.yaml")
        client_cls.return_value.create_shared_prototype.assert_not_called()

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_deploy_share_creates_prototype(self, client_cls: Mock) -> None:
        client_cls.return_value.deploy.return_value = _result(activated=True)
        client_cls.return_value.create_shared_prototype.return_value = _prototype()
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--share"])
        assert result.exit_code == 0
        assert "https://app.example/shared_prototypes?share_token=tok" in result.stdout
        service_name, options = client_cls.return_value.create_shared_prototype.call_args.args
        assert service_name == "svc"
        assert options.expiration_days == 30
        assert options.login_required is True

    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_skip_io_validation_forwards_flag_true(self, client_cls: Mock) -> None:
        from haystack_enterprise_sdk.cli import _resolve_io_interactive

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
    def test_dry_run_prompt_sets_io(self, extract_mock: Mock, _isatty: Mock) -> None:
        extract_mock.return_value = _bundle(
            available_inputs={"retriever": ["query"]},
            available_outputs={"reader": ["answers", "documents"]},
        )
        # inputs: query=1, filters/files/messages=skip(0); outputs: answers=1, documents/messages=skip(0)
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run"], input="1\n0\n0\n0\n1\n0\n0\n")
        assert result.exit_code == 0
        assert "- retriever.query" in result.stdout  # rendered YAML, not just the menu text
        assert "answers: reader.answers" in result.stdout

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
    def test_unmapped_mandatory_socket_prompts_mapping(self, extract_mock: Mock, _isatty: Mock) -> None:
        # Inference produced a `query` input but left a differently-named mandatory socket dangling;
        # the CLI must prompt to map it rather than shipping a prototype that crashes at query time.
        extract_mock.return_value = _bundle(
            inferred_inputs={"query": ["answer_builder.query"]},
            inferred_outputs={"answers": "answer_builder.answers"},
            available_inputs={"answer_builder": ["query"], "prompt_builder": ["passage"]},
            available_outputs={"answer_builder": ["answers"]},
            mandatory_inputs={"prompt_builder": ["passage"]},
        )
        # skip filters/files/messages inputs and documents/messages outputs (0); then map the
        # mandatory 'prompt_builder.passage' to 'query' (choice 1).
        result = runner.invoke(cli_app, ["deploy", FIXTURE, "svc", "--dry-run"], input="0\n0\n0\n0\n0\n1\n")
        assert result.exit_code == 0
        assert "- prompt_builder.passage" in result.stdout  # rendered into the inputs YAML
        assert "Mapped mandatory input 'prompt_builder.passage' to 'query'." in result.stdout

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
        assert "fail at query time" in result.stdout

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


class TestDeployReviewFlow:
    """The always-on I/O mapping review on deploy (review mode of _resolve_io_interactive)."""

    def _invoke_with_resolver(self, client_cls: Mock, args: list, input_: str) -> tuple:
        """Invoke deploy with a client mock that exercises the io_resolver like the real service."""
        captured = {}

        def fake_deploy(target, service_name, **kwargs):  # type: ignore[no-untyped-def]
            resolver = kwargs["io_resolver"]
            if resolver is not None:
                captured["io"] = resolver(
                    _typed_bundle(), {"query": ["retriever.query"]}, {"answers": "reader.answers"}
                )
            else:
                captured["io"] = (kwargs["inputs"], kwargs["outputs"])
            return _result(activated=True)

        client_cls.return_value.deploy.side_effect = fake_deploy
        result = runner.invoke(cli_app, args, input=input_)
        return result, captured.get("io")

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=True)
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_review_shows_summary_and_enter_accepts(self, client_cls: Mock, _tty: Mock, tmp_path: Path) -> None:
        target = tmp_path / "pipeline.py"
        target.write_text("# stub\n", encoding="utf-8")
        # Enter accepts the mapping; 'n' declines saving it.
        result, io = self._invoke_with_resolver(client_cls, ["deploy", str(target), "svc", "--no-share"], "\nn\n")
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
        result, io = self._invoke_with_resolver(client_cls, ["deploy", str(target), "svc", "--no-share"], edit_input)
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
        result, _ = self._invoke_with_resolver(client_cls, ["deploy", str(target), "svc", "--no-share"], "\ny\n")
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
        result, io = self._invoke_with_resolver(client_cls, ["deploy", str(target), "svc", "--no-share"], "")
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
            client_cls, ["deploy", str(target), "svc", "--no-share", "--io-config", str(other)], ""
        )
        assert result.exit_code == 0
        assert "Using I/O mapping from" not in result.stdout
        assert io[0] == {"query": ["right.socket"]}

    @patch("haystack_enterprise_sdk.cli._stdin_is_tty", return_value=False)
    @patch("haystack_enterprise_sdk.cli.DeploymentClient")
    def test_non_tty_deploy_never_prompts(self, client_cls: Mock, _tty: Mock, tmp_path: Path) -> None:
        target = tmp_path / "pipeline.py"
        target.write_text("# stub\n", encoding="utf-8")
        result, io = self._invoke_with_resolver(client_cls, ["deploy", str(target), "svc", "--no-share"], "")
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
        inputs, outputs, output_type = _load_io_config(cfg)
        assert inputs == {"query": ["retriever.query"], "filters": ["retriever.filters"]}
        assert outputs == {"documents": "retriever.documents"}
        assert output_type is None

    def test_loads_json(self, tmp_path: Path) -> None:
        from haystack_enterprise_sdk.cli import _load_io_config

        cfg = tmp_path / "io.json"
        cfg.write_text('{"inputs": {"query": ["r.query"]}, "outputs": {"answers": "r.answers"}}', encoding="utf-8")
        inputs, outputs, _ = _load_io_config(cfg)
        assert inputs == {"query": ["r.query"]}
        assert outputs == {"answers": "r.answers"}

    def test_absent_sections_return_none(self, tmp_path: Path) -> None:
        from haystack_enterprise_sdk.cli import _load_io_config

        cfg = tmp_path / "io.yaml"
        cfg.write_text("outputs:\n  answers: r.answers\n", encoding="utf-8")
        inputs, outputs, _ = _load_io_config(cfg)
        assert inputs is None
        assert outputs == {"answers": "r.answers"}

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
        _, _, output_type = _load_io_config(cfg)
        assert output_type == "generative"

        cfg.write_text("outputs:\n  answers: r.answers\npipeline_output_type: bogus\n", encoding="utf-8")
        with pytest.raises(typer.Exit):
            _load_io_config(cfg)

    def test_messages_only_outputs_load(self, tmp_path: Path) -> None:
        # A chat pipeline mapping only `messages` must load (no answers/documents requirement).
        from haystack_enterprise_sdk.cli import _load_io_config

        cfg = tmp_path / "io.yaml"
        cfg.write_text("outputs:\n  messages: llm.replies\n", encoding="utf-8")
        _, outputs, _ = _load_io_config(cfg)
        assert outputs == {"messages": "llm.replies"}

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
