"""Tests for the deployment service orchestration."""

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Tuple, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

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
from haystack_enterprise_sdk._service import pipeline_transform
from haystack_enterprise_sdk._service.deployment_service import (
    CreateOptions,
    DeploymentFailedError,
    DeploymentService,
    ServiceNotFoundError,
    _commit_url,
    _git,
    default_revision_comment,
)

FIXTURE = Path(__file__).parent.parent.parent / "test_data" / "deploy" / "pipeline.py"


def _deployment(
    name: str = "svc",
    status: DeploymentStatus = DeploymentStatus.UNDEPLOYED,
    mode: DeploymentMode = DeploymentMode.MANAGED,
) -> Deployment:
    return Deployment(
        deployment_id=uuid4(),
        name=name,
        status=status,
        service_level=DeploymentServiceLevel.DEVELOPMENT,
        active_revision_id=None,
        pending_revision_id=None,
        deployment_mode=mode,
    )


def _revision(deployment_id: UUID) -> DeploymentRevision:
    return DeploymentRevision(
        revision_id=uuid4(),
        deployment_id=deployment_id,
        status=DeploymentRevisionStatus.PENDING,
        config_hash="hash",
    )


if TYPE_CHECKING:

    class MockedDeploymentService(DeploymentService):
        """Typing-only view of the fixture below: ``_deployments`` is really an AsyncMock.

        Without this, every ``service._deployments.<mock attr>`` access below fails mypy against
        the real ``DeploymentsAPI`` type.
        """

        _deployments: AsyncMock  # type: ignore[assignment]

else:
    MockedDeploymentService = DeploymentService


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> MockedDeploymentService:
    """A DeploymentService whose DeploymentsAPI is a fully mocked AsyncMock."""
    svc = DeploymentService(api=Mock(), workspace_name="ws")
    svc._deployments = AsyncMock()  # type: ignore[assignment]
    # validation passes by default so deploy tests can focus on the rollout path
    svc._deployments.validate_pipeline.return_value = PipelineValidationResult(issues=[])
    # short-circuit the transform so tests don't need Haystack/import machinery
    monkeypatch.setattr(
        "haystack_enterprise_sdk._service.pipeline_transform.build_config_yaml", lambda *a, **k: "components: {}\n"
    )
    return cast("MockedDeploymentService", svc)


def _issue(category: str, message: str = "msg") -> PipelineValidationIssue:
    return PipelineValidationIssue(category=category, code=None, json_pointer=None, message=message)


@pytest.mark.asyncio
class TestResolveAndPush:
    async def test_push_without_activate_stops_at_pending(self, service: MockedDeploymentService) -> None:
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)

        result = await service.deploy(FIXTURE, "svc", comment="Bump embedder")

        assert result.activated is False
        assert result.timed_out is False
        service._deployments.push_revision.assert_awaited_once_with(
            "ws", deployment.deployment_id, "components: {}\n", "Bump embedder"
        )
        service._deployments.activate_revision.assert_not_called()

    async def test_push_without_comment_generates_one(self, service: MockedDeploymentService) -> None:
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)

        await service.deploy(FIXTURE, "svc")

        comment = service._deployments.push_revision.await_args.args[3]
        assert comment.startswith("Deployed pipeline.py via haystack-enterprise CLI")

    async def test_missing_service_without_create_raises(self, service: MockedDeploymentService) -> None:
        service._deployments.find_by_name.return_value = None
        with pytest.raises(ServiceNotFoundError):
            await service.deploy(FIXTURE, "svc")

    async def test_create_when_missing(self, service: MockedDeploymentService) -> None:
        created = _deployment("svc")
        service._deployments.find_by_name.return_value = None
        service._deployments.create_deployment.return_value = created
        service._deployments.push_revision.return_value = _revision(created.deployment_id)

        result = await service.deploy(
            FIXTURE,
            "svc",
            create=True,
            create_options=CreateOptions(
                deployment_mode=DeploymentMode.MANAGED,
                service_level=DeploymentServiceLevel.PRODUCTION,
                cpu_limit="2",
            ),
        )

        assert result.deployment is created
        service._deployments.create_deployment.assert_awaited_once()
        _, kwargs = service._deployments.create_deployment.call_args
        assert kwargs["deployment_mode"] == DeploymentMode.MANAGED
        assert kwargs["service_level"] == DeploymentServiceLevel.PRODUCTION
        assert kwargs["cpu_limit"] == "2"

    async def test_create_defaults_to_serverless(self, service: MockedDeploymentService) -> None:
        created = _deployment("svc", mode=DeploymentMode.SERVERLESS)
        service._deployments.find_by_name.return_value = None
        service._deployments.create_deployment.return_value = created
        service._deployments.push_revision.return_value = _revision(created.deployment_id)

        await service.deploy(FIXTURE, "svc", create=True)

        _, kwargs = service._deployments.create_deployment.call_args
        assert kwargs["deployment_mode"] == DeploymentMode.SERVERLESS
        assert kwargs["service_level"] is None
        assert kwargs["cpu_limit"] is None


class TestCreateOptions:
    def test_serverless_rejects_sizing_options(self) -> None:
        with pytest.raises(ValueError, match="cpu_limit"):
            CreateOptions(cpu_limit="2")

    def test_serverless_names_every_offending_option(self) -> None:
        with pytest.raises(ValueError) as exc:
            CreateOptions(service_level=DeploymentServiceLevel.PRODUCTION, max_query_replica_count=3)
        assert "service_level" in str(exc.value)
        assert "max_query_replica_count" in str(exc.value)

    def test_managed_accepts_sizing_options(self) -> None:
        options = CreateOptions(deployment_mode=DeploymentMode.MANAGED, cpu_limit="2")
        assert options.cpu_limit == "2"

    def test_default_mode_is_serverless(self) -> None:
        assert CreateOptions().deployment_mode is DeploymentMode.SERVERLESS


@pytest.mark.asyncio
class TestValidation:
    async def test_error_blocks_deploy_before_push(self, service: MockedDeploymentService) -> None:
        service._deployments.find_by_name.return_value = _deployment()
        service._deployments.validate_pipeline.return_value = PipelineValidationResult(issues=[_issue("ERROR")])

        with pytest.raises(PipelineValidationError):
            await service.deploy(FIXTURE, "svc")

        service._deployments.validate_pipeline.assert_awaited_once_with("ws", query_yaml="components: {}\n")
        service._deployments.find_by_name.assert_not_called()
        service._deployments.push_revision.assert_not_called()

    async def test_warning_only_proceeds(self, service: MockedDeploymentService) -> None:
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)
        service._deployments.validate_pipeline.return_value = PipelineValidationResult(issues=[_issue("WARNING")])

        result = await service.deploy(FIXTURE, "svc")

        assert result.activated is False
        service._deployments.push_revision.assert_awaited_once()

    async def test_skip_validation_does_not_call_endpoint(self, service: MockedDeploymentService) -> None:
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)

        await service.deploy(FIXTURE, "svc", validate=False)

        service._deployments.validate_pipeline.assert_not_called()
        service._deployments.push_revision.assert_awaited_once()

    async def test_standalone_validate_returns_result(self, service: MockedDeploymentService) -> None:
        expected = PipelineValidationResult(issues=[_issue("ERROR")])
        service._deployments.validate_pipeline.return_value = expected

        result = await service.validate(FIXTURE)

        assert result is expected
        service._deployments.validate_pipeline.assert_awaited_once_with("ws", query_yaml="components: {}\n")


@pytest.mark.asyncio
class TestRun:
    def _mock_run_response(self, service: MockedDeploymentService, body: dict) -> AsyncMock:
        """Wire ``service._api.post`` to return an OK response carrying ``body``."""
        from httpx import Request, Response, codes

        post = AsyncMock(return_value=Response(codes.OK, request=Request("POST", "https://x"), json=body))
        service._api.post = post  # type: ignore[attr-defined,method-assign]
        return post

    async def test_run_sends_parsed_config_and_mapped_inputs(
        self, service: MockedDeploymentService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "haystack_enterprise_sdk._service.pipeline_transform.build_config_yaml",
            lambda *a, **k: "components: {}\ninputs:\n  query:\n  - retriever.query\n",
        )
        post = self._mock_run_response(service, {"llm": {"replies": ["hello"]}})

        result = await service.run(FIXTURE, query="who?")

        assert result == {"llm": {"replies": ["hello"]}}
        _, kwargs = post.call_args
        assert kwargs["endpoint"] == "haystack/pipelines/run"
        assert kwargs["json"]["pipeline_config"]["inputs"] == {"query": ["retriever.query"]}
        assert kwargs["json"]["inputs"] == {"retriever": {"query": "who?"}}

    async def test_run_strips_every_deploy_only_key_from_config(
        self, service: MockedDeploymentService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sandbox run endpoint executes the config in place: it installs nothing, carries no search
        session, and renders no Playground result. Every key describing a deployed revision has to be
        dropped here, and by the shared list rather than one remembered key at a time -- the run path
        does not choose which of them to send, so it cannot be the thing that keeps the list honest."""
        # A stand-in value per deploy-only key; only their absence downstream is asserted.
        rendered = (
            "components: {}\ninputs:\n  query:\n  - retriever.query\n"
            "dependencies:\n- haystack-ai==3.0.0\nsession_storage: true\npipeline_output_type: chat\n"
        )
        monkeypatch.setattr(
            "haystack_enterprise_sdk._service.pipeline_transform.build_config_yaml", lambda *a, **k: rendered
        )
        post = self._mock_run_response(service, {})

        await service.run(FIXTURE, query="who?")

        _, kwargs = post.call_args
        config = kwargs["json"]["pipeline_config"]
        assert not pipeline_transform.DEPLOY_ONLY_KEYS & set(config)
        # The mapping itself still has to survive the strip.
        assert config["inputs"] == {"query": ["retriever.query"]}

    async def test_run_forwards_include_outputs_from(
        self, service: MockedDeploymentService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "haystack_enterprise_sdk._service.pipeline_transform.build_config_yaml",
            lambda *a, **k: "components: {}\ninputs:\n  query:\n  - retriever.query\n",
        )
        post = self._mock_run_response(service, {})

        await service.run(FIXTURE, query="q", include_outputs_from=["retriever"])

        _, kwargs = post.call_args
        assert kwargs["json"]["include_outputs_from"] == ["retriever"]

    async def test_run_maps_named_inputs_through_the_config(
        self, service: MockedDeploymentService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # named_inputs routes through the SAME 'inputs:' mapping as query -- it isn't limited to the
        # standard query/filters/files keys, so a pipeline's own custom platform inputs work too.
        monkeypatch.setattr(
            "haystack_enterprise_sdk._service.pipeline_transform.build_config_yaml",
            lambda *a, **k: (
                "components: {}\ninputs:\n  query:\n  - retriever.query\n  github_token:\n  - payload.github_token\n"
            ),
        )
        post = self._mock_run_response(service, {})

        await service.run(FIXTURE, query="q", named_inputs={"github_token": "ghs_abc"})

        _, kwargs = post.call_args
        assert kwargs["json"]["inputs"] == {
            "retriever": {"query": "q"},
            "payload": {"github_token": "ghs_abc"},
        }


@pytest.mark.asyncio
class TestActivateAndPoll:
    async def test_activate_polls_until_deployed(self, service: MockedDeploymentService) -> None:
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

    async def test_activate_failure_raises_with_ui_hint(self, service: MockedDeploymentService) -> None:
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

    async def test_activate_times_out_and_detaches(self, service: MockedDeploymentService) -> None:
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

    async def test_serverless_activates_without_polling(self, service: MockedDeploymentService) -> None:
        # Serverless provisions no workload, so there is no rollout status to poll for: the activated
        # revision is what runs, and waiting would only burn the timeout.
        deployment = _deployment(mode=DeploymentMode.SERVERLESS)
        service._deployments.find_by_name.return_value = deployment
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)
        service._deployments.activate_revision.return_value = _deployment(
            status=DeploymentStatus.DEPLOYMENT_IN_PROGRESS, mode=DeploymentMode.SERVERLESS
        )

        result = await service.deploy(FIXTURE, "svc", activate=True, poll_interval_s=0)

        service._deployments.activate_revision.assert_awaited_once()
        service._deployments.get_deployment.assert_not_called()
        assert result.activated is True
        assert result.timed_out is False
        # The status never settles for serverless, so activation alone counts as serving.
        assert result.is_deployed is True

    async def test_serverless_push_without_activate_is_not_serving(self, service: MockedDeploymentService) -> None:
        deployment = _deployment(mode=DeploymentMode.SERVERLESS)
        service._deployments.find_by_name.return_value = deployment
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)

        result = await service.deploy(FIXTURE, "svc")

        assert result.activated is False
        assert result.is_deployed is False


@pytest.mark.asyncio
class TestFindService:
    async def test_find_service_returns_match(self, service: MockedDeploymentService) -> None:
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        assert await service.find_service("svc") is deployment
        service._deployments.find_by_name.assert_awaited_once_with("ws", "svc")

    async def test_find_service_returns_none_when_missing(self, service: MockedDeploymentService) -> None:
        service._deployments.find_by_name.return_value = None
        assert await service.find_service("svc") is None


@pytest.mark.asyncio
class TestGetServiceStatus:
    async def test_get_service_status(self, service: MockedDeploymentService) -> None:
        deployment = _deployment(status=DeploymentStatus.DEPLOYED)
        service._deployments.find_by_name.return_value = deployment
        service._deployments.get_deployment.return_value = deployment
        result = await service.get_service_status("svc")
        assert result.status == DeploymentStatus.DEPLOYED

    async def test_get_service_status_missing_raises(self, service: MockedDeploymentService) -> None:
        service._deployments.find_by_name.return_value = None
        with pytest.raises(ServiceNotFoundError):
            await service.get_service_status("svc")


@pytest.mark.asyncio
class TestCreateSharedPrototype:
    async def test_computes_expiration_and_forwards_options(
        self, service: MockedDeploymentService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime, timezone

        from haystack_enterprise_sdk._api.shared_prototypes import SharedPrototype
        from haystack_enterprise_sdk._service.deployment_service import ShareOptions

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
            "haystack_enterprise_sdk._service.deployment_service.SharedPrototypesAPI",
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


class TestCommitUrl:
    @pytest.mark.parametrize(
        ("remote", "expected"),
        [
            ("git@github.com:org/repo.git", "https://github.com/org/repo/commit/" + "a" * 40),
            ("https://github.com/org/repo.git", "https://github.com/org/repo/commit/" + "a" * 40),
            ("https://user@github.com/org/repo", "https://github.com/org/repo/commit/" + "a" * 40),
            ("ssh://git@gitlab.com/group/sub/repo.git", "https://gitlab.com/group/sub/repo/-/commit/" + "a" * 40),
            ("https://bitbucket.org/org/repo", "https://bitbucket.org/org/repo/commits/" + "a" * 40),
            ("git@git.internal.example.com:org/repo.git", None),
            ("/srv/git/repo.git", None),
        ],
    )
    def test_commit_url(self, remote: str, expected: Optional[str]) -> None:
        assert _commit_url(remote, "a" * 40) == expected


class TestDefaultRevisionComment:
    @staticmethod
    def _patch_git(monkeypatch: pytest.MonkeyPatch, outputs: Dict[Tuple[str, ...], str]) -> None:
        """Replace _git with a stand-in answering from ``outputs``, keyed by the git arguments."""

        def _fake_git(_target_dir: Path, *args: str) -> Optional[str]:
            return outputs.get(args)

        monkeypatch.setattr("haystack_enterprise_sdk._service.deployment_service._git", _fake_git)

    def test_includes_branch_and_commit_link(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_git(
            monkeypatch,
            {
                ("rev-parse", "HEAD"): "a1b2c3d4" + "0" * 32,
                ("rev-parse", "--abbrev-ref", "HEAD"): "main",
                ("remote", "get-url", "origin"): "git@github.com:org/repo.git",
            },
        )
        comment = default_revision_comment(FIXTURE)
        assert comment == (
            "Deployed pipeline.py via haystack-enterprise CLI (main@a1b2c3d) "
            f"https://github.com/org/repo/commit/a1b2c3d4{'0' * 32}"
        )

    def test_unknown_remote_host_omits_link(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_git(
            monkeypatch,
            {
                ("rev-parse", "HEAD"): "a1b2c3d4" + "0" * 32,
                ("rev-parse", "--abbrev-ref", "HEAD"): "main",
                ("remote", "get-url", "origin"): "git@git.internal.example.com:org/repo.git",
            },
        )
        assert default_revision_comment(FIXTURE) == "Deployed pipeline.py via haystack-enterprise CLI (main@a1b2c3d)"

    def test_no_origin_remote_omits_link(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_git(
            monkeypatch,
            {
                ("rev-parse", "HEAD"): "a1b2c3d4" + "0" * 32,
                ("rev-parse", "--abbrev-ref", "HEAD"): "main",
            },
        )
        assert default_revision_comment(FIXTURE) == "Deployed pipeline.py via haystack-enterprise CLI (main@a1b2c3d)"

    def test_detached_head_shows_sha_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_git(
            monkeypatch,
            {
                ("rev-parse", "HEAD"): "a1b2c3d4" + "0" * 32,
                ("rev-parse", "--abbrev-ref", "HEAD"): "HEAD",
            },
        )
        assert default_revision_comment(FIXTURE) == "Deployed pipeline.py via haystack-enterprise CLI (a1b2c3d)"

    def test_without_git_falls_back_to_plain_comment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_git(monkeypatch, {})
        assert default_revision_comment(FIXTURE) == "Deployed pipeline.py via haystack-enterprise CLI"


class TestGitHelper:
    def test_missing_git_binary_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise FileNotFoundError("git")

        monkeypatch.setattr("subprocess.run", _raise)
        assert _git(Path("."), "rev-parse", "HEAD") is None

    def test_timeout_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr("subprocess.run", _raise)
        assert _git(Path("."), "rev-parse", "HEAD") is None

    def test_non_zero_exit_returns_none(self) -> None:
        # A real call outside any repository: git exits non-zero.
        assert _git(Path("/"), "rev-parse", "HEAD") is None

    def test_returns_stripped_stdout(self) -> None:
        assert _git(Path(__file__).parent, "rev-parse", "--abbrev-ref", "HEAD")
