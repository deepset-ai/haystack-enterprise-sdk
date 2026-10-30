"""Tests for the deployment service orchestration."""

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Tuple, cast
from unittest.mock import ANY, AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from haystack_enterprise_sdk._api.deployments import (
    CodeBundleRequestError,
    Deployment,
    DeploymentMode,
    DeploymentRevision,
    DeploymentRevisionStatus,
    DeploymentServiceLevel,
    DeploymentStatus,
    FailedToTagDeploymentError,
    PipelineValidationError,
    PipelineValidationIssue,
    PipelineValidationResult,
)
from haystack_enterprise_sdk._service import pipeline_transform
from haystack_enterprise_sdk._service.code_bundle import CodeBundle
from haystack_enterprise_sdk._service.deployment_service import (
    CreateOptions,
    DeploymentFailedError,
    DeploymentService,
    ServiceNotFoundError,
    _git,
    _repo_id,
    collect_source_metadata,
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
            "ws", deployment.deployment_id, "components: {}\n", "Bump embedder", source_metadata=ANY
        )
        service._deployments.activate_revision.assert_not_called()

    async def test_push_without_comment_generates_one(
        self, service: MockedDeploymentService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("haystack_enterprise_sdk._service.deployment_service._git", lambda _dir, *_args: None)
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)

        await service.deploy(FIXTURE, "svc")

        comment = service._deployments.push_revision.await_args.args[3]
        assert comment == "Deployed pipeline.py via haystack-enterprise CLI"

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

    async def test_create_with_tags_adds_each_after_creation(self, service: MockedDeploymentService) -> None:
        created = _deployment("svc")
        service._deployments.find_by_name.return_value = None
        service._deployments.create_deployment.return_value = created
        service._deployments.add_tag.side_effect = [["team-success"], ["team-success", "hackathon"]]
        service._deployments.push_revision.return_value = _revision(created.deployment_id)

        result = await service.deploy(
            FIXTURE, "svc", create=True, create_options=CreateOptions(tags=("team-success", "hackathon"))
        )

        # No tags endpoint on create-deployment itself: one add_tag call per tag, in order, against
        # the id the create call returned.
        assert service._deployments.add_tag.await_args_list == [
            (("ws", created.deployment_id, "team-success"),),
            (("ws", created.deployment_id, "hackathon"),),
        ]
        assert result.deployment.tags == ["team-success", "hackathon"]

    async def test_create_without_tags_never_calls_add_tag(self, service: MockedDeploymentService) -> None:
        created = _deployment("svc")
        service._deployments.find_by_name.return_value = None
        service._deployments.create_deployment.return_value = created
        service._deployments.push_revision.return_value = _revision(created.deployment_id)

        await service.deploy(FIXTURE, "svc", create=True)

        service._deployments.add_tag.assert_not_called()

    async def test_create_with_tag_failure_does_not_abort_deploy(self, service: MockedDeploymentService) -> None:
        # The service is already created by the time a tag add fails, so deploy must still push the
        # revision rather than raising -- the tag can be retried independently via `tag-add`.
        created = _deployment("svc")
        service._deployments.find_by_name.return_value = None
        service._deployments.create_deployment.return_value = created
        service._deployments.add_tag.side_effect = FailedToTagDeploymentError("nope")
        pushed = _revision(created.deployment_id)
        service._deployments.push_revision.return_value = pushed

        result = await service.deploy(FIXTURE, "svc", create=True, create_options=CreateOptions(tags=("team-success",)))

        assert result.revision is pushed
        assert result.deployment.tags == []


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

    def test_default_tags_is_empty(self) -> None:
        assert CreateOptions().tags == ()

    def test_serverless_accepts_tags(self) -> None:
        # Tags are not a sizing option: they apply to a serverless service too.
        options = CreateOptions(tags=("a", "b"))
        assert options.tags == ("a", "b")


@pytest.mark.asyncio
class TestValidation:
    async def test_error_blocks_deploy_before_push(self, service: MockedDeploymentService) -> None:
        service._deployments.find_by_name.return_value = _deployment()
        service._deployments.validate_pipeline.return_value = PipelineValidationResult(issues=[_issue("ERROR")])

        with pytest.raises(PipelineValidationError):
            await service.deploy(FIXTURE, "svc")

        service._deployments.validate_pipeline.assert_awaited_once_with(
            "ws", query_yaml="components: {}\n", haystack_version=None
        )
        service._deployments.find_by_name.assert_not_called()
        service._deployments.push_revision.assert_not_called()

    async def test_deploy_and_validate_pin_the_same_version(
        self, service: MockedDeploymentService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both commands must judge a pipeline against the version it pins, or they can disagree."""
        pinned = "components: {}\ndependencies:\n  - haystack-ai==2.30.2\n"
        monkeypatch.setattr(
            "haystack_enterprise_sdk._service.pipeline_transform.build_config_yaml", lambda *a, **k: pinned
        )
        service._deployments.find_by_name.return_value = _deployment()
        service._deployments.push_revision.return_value = _revision(_deployment().deployment_id)

        await service.validate(FIXTURE)
        await service.deploy(FIXTURE, "svc")

        sent = [call.kwargs["haystack_version"] for call in service._deployments.validate_pipeline.await_args_list]
        assert sent == ["2.30.2", "2.30.2"]

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
        service._deployments.validate_pipeline.assert_awaited_once_with(
            "ws", query_yaml="components: {}\n", haystack_version=None
        )


@pytest.mark.asyncio
class TestServiceSecrets:
    async def test_upserts_secrets_before_push(self, service: MockedDeploymentService) -> None:
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)
        workspace_id, existing_id = uuid4(), uuid4()
        secrets = AsyncMock()
        secrets.get_workspace_id.return_value = workspace_id
        secrets.list_names.return_value = {"OLD": existing_id, "UNTOUCHED": uuid4()}
        service._secrets = secrets  # type: ignore[assignment]

        await service.deploy(FIXTURE, "svc", secrets={"OLD": "new", "NEW": "1"})

        secrets.update.assert_awaited_once_with(workspace_id, deployment.deployment_id, existing_id, "OLD", "new")
        secrets.create.assert_awaited_once_with(workspace_id, deployment.deployment_id, "NEW", "1")

    async def test_failed_validation_sets_no_secrets(self, service: MockedDeploymentService) -> None:
        service._deployments.validate_pipeline.return_value = PipelineValidationResult(issues=[_issue("ERROR")])
        secrets = AsyncMock()
        service._secrets = secrets  # type: ignore[assignment]

        with pytest.raises(PipelineValidationError):
            await service.deploy(FIXTURE, "svc", secrets={"A": "1"})

        secrets.get_workspace_id.assert_not_called()


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
        assert "Haystack Enterprise Platform" in exc.value.ui_hint

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
class TestTagByName:
    async def test_add_tag_resolves_name_to_id(self, service: MockedDeploymentService) -> None:
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        service._deployments.add_tag.return_value = ["hackathon"]

        result = await service.add_tag("svc", "hackathon")

        assert result == ["hackathon"]
        service._deployments.add_tag.assert_awaited_once_with("ws", deployment.deployment_id, "hackathon")

    async def test_add_tag_missing_service_raises(self, service: MockedDeploymentService) -> None:
        service._deployments.find_by_name.return_value = None
        with pytest.raises(ServiceNotFoundError):
            await service.add_tag("svc", "hackathon")
        service._deployments.add_tag.assert_not_called()

    async def test_remove_tag_resolves_name_to_id(self, service: MockedDeploymentService) -> None:
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        service._deployments.remove_tag.return_value = []

        result = await service.remove_tag("svc", "hackathon")

        assert result == []
        service._deployments.remove_tag.assert_awaited_once_with("ws", deployment.deployment_id, "hackathon")

    async def test_remove_tag_missing_service_raises(self, service: MockedDeploymentService) -> None:
        service._deployments.find_by_name.return_value = None
        with pytest.raises(ServiceNotFoundError):
            await service.remove_tag("svc", "hackathon")
        service._deployments.remove_tag.assert_not_called()


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


class TestRepoId:
    @pytest.mark.parametrize(
        ("remote", "expected"),
        [
            ("https://github.com/org/repo.git", "github.com/org/repo"),
            ("git@github.com:org/repo.git", "github.com/org/repo"),
            ("https://GitHub.com/Org/Repo.git/", "github.com/Org/Repo"),
            ("https://user@bitbucket.org/org/repo.git", "bitbucket.org/org/repo"),
            ("ssh://git@gitlab.com/group/sub/repo.git", "gitlab.com/group/sub/repo"),
            ("ssh://git@git.corp.example:2222/team/repo.git", "git.corp.example/team/repo"),
            ("https://git.corp.example:8443/team/sub/repo", "git.corp.example/team/sub/repo"),
            ("git@git.internal.example.com:org/repo.git", "git.internal.example.com/org/repo"),
            ("git@host:/abs/repo.git", "host/abs/repo"),
            # scp syntax has no port: a numeric first path segment is part of the path.
            ("git@github.com:12345/repo.git", "github.com/12345/repo"),
            # Credentials never survive, however the URL is shaped.
            ("https://oauth2:glpat-XXXX@gitlab.com/org/repo.git", "gitlab.com/org/repo"),
            ("https://x-access-token:ghs_XXXX@github.com/org/repo", "github.com/org/repo"),
            ("https://gitlab-ci-token:glpat-ZZZZ@gitlab.com:8443/org/repo.git", "gitlab.com/org/repo"),
            ("https://user:p@ss@gitlab.com/org/repo.git", "gitlab.com/org/repo"),
            ("https://user:p%40ss@gitlab.com/org/repo.git", "gitlab.com/org/repo"),
            # Anything that could leak a credential or a local path is omitted rather than guessed at.
            ("https://tok@github.com", None),
            ("https://github.com", None),
            ("https://user:pa/ss@host/org/repo", None),
            ("https://gitlab.com/org/repo.git?private_token=glpat-QQQQ", None),
            ("file:///srv/git/repo.git", None),
            ("/srv/git/repo.git", None),
            ("../relative/repo.git", None),
            ("C:\\repos\\proj", None),
            ("ssh://git@[::1]:2222/org/repo", None),
            ("", None),
            (None, None),
        ],
    )
    def test_repo_id(self, remote: Optional[str], expected: Optional[str]) -> None:
        assert _repo_id(remote) == expected


class TestDefaultRevisionComment:
    def test_uses_the_head_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "haystack_enterprise_sdk._service.deployment_service._git",
            lambda _dir, *args: "a1b2c3d Add retry to retriever" if args[0] == "log" else None,
        )
        assert default_revision_comment(FIXTURE) == ("a1b2c3d Add retry to retriever", True)

    def test_marks_uncommitted_changes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "haystack_enterprise_sdk._service.deployment_service._git",
            lambda _dir, *args: "a1b2c3d Add retry to retriever" if args[0] == "log" else " M pipeline.py",
        )
        assert default_revision_comment(FIXTURE) == ("a1b2c3d Add retry to retriever (dirty)", False)

    def test_without_git_falls_back_to_plain_comment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("haystack_enterprise_sdk._service.deployment_service._git", lambda _dir, *_args: None)
        assert default_revision_comment(FIXTURE) == ("Deployed pipeline.py via haystack-enterprise CLI", False)


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


class TestCollectSourceMetadata:
    SHA = "a1b2c3d4" + "0" * 32
    INSIDE = ("rev-parse", "--is-inside-work-tree", "--show-prefix")
    STATUS = ("status", "--porcelain", "--branch")
    BRANCH = ("symbolic-ref", "--short", "-q", "HEAD")
    REPO_OUTPUTS = {
        INSIDE: "true\npipelines/",
        ("remote", "get-url", "origin"): "git@github.com:org/repo.git",
        BRANCH: "main",
        ("rev-parse", "HEAD"): SHA,
        STATUS: "## main",
    }

    @pytest.fixture(autouse=True)
    def _not_ci(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CI", raising=False)

    @staticmethod
    def _patch_git(monkeypatch: pytest.MonkeyPatch, outputs: Dict[Tuple[str, ...], str]) -> None:
        """Replace _git with a stand-in answering from ``outputs``, keyed by the git arguments."""
        monkeypatch.setattr(
            "haystack_enterprise_sdk._service.deployment_service._git", lambda _target_dir, *args: outputs.get(args)
        )

    def _without(self, *keys: Tuple[str, ...]) -> Dict[Tuple[str, ...], str]:
        return {k: v for k, v in self.REPO_OUTPUTS.items() if k not in keys}

    def test_normal_checkout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_git(monkeypatch, self.REPO_OUTPUTS)
        assert collect_source_metadata(FIXTURE) == {
            "ci": False,
            "custom": {"client": "Haystack Enterprise SDK"},
            "git": {"repo": "github.com/org/repo", "branch": "main", "sha": self.SHA, "dirty": False},
            "file": "pipelines/pipeline.py",
        }

    def test_dirty_tree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_git(monkeypatch, {**self.REPO_OUTPUTS, self.STATUS: "## main\n M x.py"})
        assert collect_source_metadata(FIXTURE)["git"]["dirty"] is True

    def test_failed_status_does_not_claim_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_git(monkeypatch, self._without(self.STATUS))
        assert "dirty" not in collect_source_metadata(FIXTURE)["git"]

    def test_file_at_repo_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_git(monkeypatch, {**self.REPO_OUTPUTS, self.INSIDE: "true"})
        assert collect_source_metadata(FIXTURE)["file"] == "pipeline.py"

    @pytest.mark.parametrize(
        "missing",
        [BRANCH, ("rev-parse", "HEAD"), ("remote", "get-url", "origin")],
        ids=["detached-head", "no-commits", "no-remote"],
    )
    def test_omits_what_it_cannot_determine(self, monkeypatch: pytest.MonkeyPatch, missing: Tuple[str, ...]) -> None:
        self._patch_git(monkeypatch, self._without(missing))
        info = collect_source_metadata(FIXTURE)
        assert len({"repo", "branch", "sha"} & set(info["git"])) == 2
        assert info["file"] == "pipelines/pipeline.py"

    def test_not_a_repo_or_no_git(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_git(monkeypatch, {})
        assert collect_source_metadata(FIXTURE) == {"ci": False, "custom": {"client": "Haystack Enterprise SDK"}}

    def test_self_hosted_host_with_custom_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        remote = "ssh://git@git.corp.example:2222/team/repo.git"
        self._patch_git(monkeypatch, {**self.REPO_OUTPUTS, ("remote", "get-url", "origin"): remote})
        assert collect_source_metadata(FIXTURE)["git"]["repo"] == "git.corp.example/team/repo"

    @pytest.mark.parametrize(("value", "expected"), [("true", True), ("1", True), ("false", False), ("", False)])
    def test_ci_flag(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
        monkeypatch.setenv("CI", value)
        self._patch_git(monkeypatch, {})
        assert collect_source_metadata(FIXTURE)["ci"] is expected

    def test_values_are_cut_to_the_platform_limits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_git(
            monkeypatch,
            {
                **self.REPO_OUTPUTS,
                self.BRANCH: "b" * 1000,
                ("remote", "get-url", "origin"): "git@github.com:" + "o" * 1000,
                ("rev-parse", "HEAD"): "c" * 100,
                self.INSIDE: "true\n" + "d" * 2000 + "/",
            },
        )
        info = collect_source_metadata(FIXTURE)
        assert len(info["git"]["branch"]) == 255
        assert len(info["git"]["repo"]) == 300
        assert len(info["git"]["sha"]) == 64
        assert len(info["file"]) == 1024

    def test_sends_only_known_keys_and_no_credentials(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        remote = "https://gitlab-ci-token:glpat-SECRET@gitlab.com/org/repo.git"
        self._patch_git(monkeypatch, {**self.REPO_OUTPUTS, ("remote", "get-url", "origin"): remote})
        info = collect_source_metadata(tmp_path / "pipeline.py")
        assert set(info) <= {"ci", "custom", "git", "file"}
        assert set(info["git"]) <= {"repo", "branch", "sha", "dirty"}
        sent = json.dumps(info)
        assert info["git"]["repo"] == "gitlab.com/org/repo"
        for forbidden in ("glpat-SECRET", "gitlab-ci-token", remote, str(tmp_path)):
            assert forbidden not in sent

    def test_real_repository(self, tmp_path: Path) -> None:
        def git(*args: str) -> None:
            subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)  # noqa: S603, S607

        git("init", "-b", "main")
        sub = tmp_path / "svc"
        sub.mkdir()
        target = sub / "pipeline.py"
        target.write_text("x = 1\n")

        # No commits yet: branch and file are known, sha is not.
        info = collect_source_metadata(target)
        assert info["file"] == "svc/pipeline.py"
        assert info["git"]["branch"] == "main"
        assert "sha" not in info["git"]

        git("add", ".")
        git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=author@secret.example",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "init",
        )
        assert collect_source_metadata(target)["git"]["dirty"] is False
        target.write_text("x = 2\n")
        info = collect_source_metadata(target)
        assert info["git"]["dirty"] is True
        assert len(info["git"]["sha"]) == 40
        assert "author@secret.example" not in json.dumps(info)


_PACKED = CodeBundle(
    archive=b"tgz",
    sha256="abc",
    files=["app.py"],
    config_yaml="inputs:\n  query:\n  - app.query\n",
    entrypoint="app.py:app",
    kind="super_component",
    input_schema={"type": "object"},
)


@pytest.mark.asyncio
class TestCodeBuild:
    @pytest.fixture(autouse=True)
    def _packed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("haystack_enterprise_sdk._service.code_bundle.build_code_bundle", lambda *a, **k: _PACKED)

    async def test_uploads_then_pushes_settings_with_code_bundle(self, service: MockedDeploymentService) -> None:
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        service._deployments.get_code_bundle.return_value = None  # typed 404
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)

        await service.deploy(FIXTURE, "svc", comment="c", build="code")

        service._deployments.upload_code_bundle.assert_awaited_once_with("ws", b"tgz", "abc")
        service._deployments.validate_pipeline.assert_not_called()
        service._deployments.push_revision.assert_awaited_once_with(
            "ws",
            deployment.deployment_id,
            _PACKED.config_yaml,
            "c",
            source_metadata=ANY,
            code_bundle={
                "sha256": "abc",
                "entrypoint": "app.py:app",
                "kind": "super_component",
                "input_schema": {"type": "object"},
            },
        )

    async def test_known_bundle_is_not_uploaded_again(self, service: MockedDeploymentService) -> None:
        deployment = _deployment()
        service._deployments.find_by_name.return_value = deployment
        service._deployments.get_code_bundle.return_value = {"sha256": "abc"}
        service._deployments.push_revision.return_value = _revision(deployment.deployment_id)

        await service.deploy(FIXTURE, "svc", build="code")

        service._deployments.upload_code_bundle.assert_not_called()
        service._deployments.push_revision.assert_awaited_once()

    async def test_failed_capability_check_creates_and_pushes_nothing(self, service: MockedDeploymentService) -> None:
        service._deployments.find_by_name.return_value = None
        service._deployments.get_code_bundle.side_effect = CodeBundleRequestError("use --build yaml")

        with pytest.raises(CodeBundleRequestError):
            await service.deploy(FIXTURE, "svc", create=True, build="code")

        service._deployments.create_deployment.assert_not_called()
        service._deployments.upload_code_bundle.assert_not_called()
        service._deployments.push_revision.assert_not_called()
