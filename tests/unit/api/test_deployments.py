"""Tests for the deployments API client."""

from typing import Any, Optional
from unittest.mock import Mock
from uuid import uuid4

import pytest
from httpx import ReadTimeout, Request, Response, codes

from haystack_enterprise_sdk._api.deployments import (
    Deployment,
    DeploymentMode,
    DeploymentRevision,
    DeploymentRevisionStatus,
    DeploymentsAPI,
    DeploymentServiceLevel,
    DeploymentStatus,
    FailedToActivateRevisionError,
    FailedToCreateDeploymentError,
    FailedToPushRevisionError,
    FailedToValidatePipelineError,
    PipelineValidationResult,
)
from haystack_enterprise_sdk.models import PipelineOutputType

_REQUEST = Request("GET", "https://test.deepset.ai")


def _resp(status_code: int, **kwargs: Any) -> Response:
    """Build a Response with a request attached so `raise_for_status` works in tests."""
    return Response(status_code=status_code, request=_REQUEST, **kwargs)


@pytest.fixture
def deployments_api(mocked_haystack_enterprise_api: Mock) -> DeploymentsAPI:
    return DeploymentsAPI(mocked_haystack_enterprise_api)


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
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.get.return_value = _resp(
            codes.OK,
            json={"data": [_deployment_body("a"), _deployment_body("b")], "has_more": True, "total": 5},
        )
        page = await deployments_api.list_deployments("ws", page_number=2)
        assert [d.name for d in page.data] == ["a", "b"]
        assert page.has_more is True
        assert page.total == 5
        mocked_haystack_enterprise_api.get.assert_called_once_with(
            workspace_name="ws", endpoint="deployments", params={"limit": 100, "page_number": 2}
        )

    async def test_find_by_name_matches_on_first_page(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        target = _deployment_body("wanted")
        mocked_haystack_enterprise_api.get.return_value = _resp(
            codes.OK,
            json={"data": [_deployment_body("other"), target], "has_more": False, "total": 2},
        )
        found = await deployments_api.find_by_name("ws", "wanted")
        assert found is not None
        assert found.name == "wanted"
        assert found.deployment_id == Deployment.from_response(target).deployment_id

    async def test_find_by_name_pages_until_match(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        page1 = _resp(codes.OK, json={"data": [_deployment_body("p1")], "has_more": True, "total": 2})
        page2 = _resp(codes.OK, json={"data": [_deployment_body("wanted")], "has_more": False, "total": 2})
        mocked_haystack_enterprise_api.get.side_effect = [page1, page2]
        found = await deployments_api.find_by_name("ws", "wanted")
        assert found is not None and found.name == "wanted"
        assert mocked_haystack_enterprise_api.get.call_count == 2

    async def test_find_by_name_returns_none_when_absent(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.get.return_value = _resp(
            codes.OK,
            json={"data": [_deployment_body("other")], "has_more": False, "total": 1},
        )
        assert await deployments_api.find_by_name("ws", "missing") is None


@pytest.mark.asyncio
class TestCreate:
    async def test_create_deployment_sends_overrides(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.CREATED, json=_deployment_body("svc"))
        result = await deployments_api.create_deployment(
            "ws",
            name="svc",
            deployment_mode=DeploymentMode.MANAGED,
            service_level=DeploymentServiceLevel.PRODUCTION,
            cpu_limit="2",
            max_query_replica_count=3,
        )
        assert isinstance(result, Deployment)
        mocked_haystack_enterprise_api.post.assert_called_once_with(
            workspace_name="ws",
            endpoint="deployments",
            json={
                "name": "svc",
                "source_type": "EXTERNAL_PIPELINE",
                "deployment_mode": "MANAGED",
                "service_level": "PRODUCTION",
                "max_query_replica_count": 3,
                "cpu_limit": "2",
            },
        )

    async def test_create_deployment_minimal_payload_is_serverless(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.CREATED, json=_deployment_body("svc"))
        await deployments_api.create_deployment("ws", name="svc")
        _, kwargs = mocked_haystack_enterprise_api.post.call_args
        # The platform itself defaults to MANAGED, so the serverless default has to be sent explicitly.
        assert kwargs["json"] == {
            "name": "svc",
            "source_type": "EXTERNAL_PIPELINE",
            "deployment_mode": "SERVERLESS",
        }

    async def test_create_deployment_failure_raises(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.CONFLICT, text="exists")
        with pytest.raises(FailedToCreateDeploymentError):
            await deployments_api.create_deployment("ws", name="svc")


@pytest.mark.asyncio
class TestRevisions:
    async def test_push_revision(self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock) -> None:
        deployment_id = uuid4()
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.CREATED, json=_revision_body(str(deployment_id)))
        revision = await deployments_api.push_revision(
            "ws", deployment_id, config_yaml="components: {}", comment="Bump embedder"
        )
        assert isinstance(revision, DeploymentRevision)
        assert revision.status == DeploymentRevisionStatus.PENDING
        mocked_haystack_enterprise_api.post.assert_called_once_with(
            workspace_name="ws",
            endpoint=f"deployments/{deployment_id}/revisions",
            json={
                "comment": "Bump embedder",
                "config_yaml": "components: {}",
                "source_type": "EXTERNAL_PIPELINE",
            },
        )

    async def test_push_revision_tolerates_missing_status(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        deployment_id = uuid4()
        body = {"revision_id": str(uuid4()), "deployment_id": str(deployment_id)}  # no "status"/"config_hash"
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.CREATED, json=body)
        revision = await deployments_api.push_revision("ws", deployment_id, config_yaml="components: {}", comment="c")
        assert revision.status == DeploymentRevisionStatus.PENDING
        assert revision.config_hash == ""

    async def test_push_revision_failure_raises(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.UNPROCESSABLE_ENTITY, text="empty")
        with pytest.raises(FailedToPushRevisionError):
            await deployments_api.push_revision("ws", uuid4(), config_yaml="", comment="c")

    async def test_activate_revision(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        deployment_id, revision_id = uuid4(), uuid4()
        mocked_haystack_enterprise_api.post.return_value = _resp(
            codes.OK, json=_deployment_body("svc", status="DEPLOYMENT_IN_PROGRESS")
        )
        result = await deployments_api.activate_revision("ws", deployment_id, revision_id)
        assert result.status == DeploymentStatus.DEPLOYMENT_IN_PROGRESS
        mocked_haystack_enterprise_api.post.assert_called_once_with(
            workspace_name="ws",
            endpoint=f"deployments/{deployment_id}/revisions/{revision_id}/activate",
        )

    async def test_activate_revision_failure_raises(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.CONFLICT, text="nope")
        with pytest.raises(FailedToActivateRevisionError):
            await deployments_api.activate_revision("ws", uuid4(), uuid4())


@pytest.fixture
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the cold-start backoff so the retry tests don't actually wait."""
    monkeypatch.setattr("haystack_enterprise_sdk._api.deployments._PINNED_RETRY_DELAY_S", 0.0)


@pytest.mark.asyncio
class TestValidatePipeline:
    async def test_valid_204(self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.NO_CONTENT)
        result = await deployments_api.validate_pipeline("ws", query_yaml="components: {}")
        assert isinstance(result, PipelineValidationResult)
        assert result.is_valid is True
        assert result.issues == []
        mocked_haystack_enterprise_api.post.assert_called_once_with(
            workspace_name="ws",
            endpoint="pipeline_validations",
            json={"deepset_cloud_version": "v2", "query_yaml": "components: {}"},
        )

    async def test_only_provided_yaml_fields_are_sent(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.NO_CONTENT)
        await deployments_api.validate_pipeline("ws", indexing_yaml="components: {}", pipeline_id="pid")
        _, kwargs = mocked_haystack_enterprise_api.post.call_args
        assert kwargs["json"] == {
            "deepset_cloud_version": "v2",
            "indexing_yaml": "components: {}",
            "pipeline_id": "pid",
        }

    async def test_errors_parsed_from_400(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(
            codes.BAD_REQUEST,
            json={
                "error_details": [
                    {"category": "ERROR", "code": "X", "json_pointer": "/components/0", "message": "bad"},
                    {"category": "WARNING", "message": "meh"},
                ]
            },
        )
        result = await deployments_api.validate_pipeline("ws", query_yaml="y")
        assert result.has_errors is True
        assert len(result.errors) == 1
        assert result.errors[0].json_pointer == "/components/0"
        assert len(result.warnings) == 1

    async def test_warning_only_400_is_valid(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(
            codes.BAD_REQUEST,
            json={"error_details": [{"category": "WARNING", "message": "deprecated"}]},
        )
        result = await deployments_api.validate_pipeline("ws", query_yaml="y")
        assert result.is_valid is True
        assert result.has_errors is False
        assert len(result.warnings) == 1

    async def test_tolerates_alternate_detail_keys(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(
            codes.BAD_REQUEST,
            json={"detail": [{"msg": "boom", "json_path": "/x"}]},  # no category -> defaults to ERROR
        )
        result = await deployments_api.validate_pipeline("ws", query_yaml="y")
        assert result.has_errors is True
        assert result.errors[0].message == "boom"
        assert result.errors[0].json_pointer == "/x"

    async def test_falls_back_to_top_level_message(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.BAD_REQUEST, json={"message": "invalid config"})
        result = await deployments_api.validate_pipeline("ws", query_yaml="y")
        assert result.has_errors is True
        assert result.errors[0].message == "invalid config"

    async def test_request_failure_raises(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.INTERNAL_SERVER_ERROR, text="boom")
        with pytest.raises(FailedToValidatePipelineError):
            await deployments_api.validate_pipeline("ws", query_yaml="y")

    async def test_haystack_version_is_sent_as_env_switch_header(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        """A pinned version selects the platform's worker environment, and buys a longer timeout."""
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.NO_CONTENT)
        await deployments_api.validate_pipeline("ws", query_yaml="y", haystack_version="2.30.2")
        _, kwargs = mocked_haystack_enterprise_api.post.call_args
        assert kwargs["headers"] == {"x-haystack-version": "2.30.2"}
        assert kwargs["timeout_s"] > 20

    async def test_no_pin_sends_no_header(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        """Without a pin the request is byte-for-byte the unpinned one (host validation, default timeout)."""
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.NO_CONTENT)
        await deployments_api.validate_pipeline("ws", query_yaml="y")
        _, kwargs = mocked_haystack_enterprise_api.post.call_args
        assert "headers" not in kwargs
        assert "timeout_s" not in kwargs

    async def test_unsupported_pin_falls_back_to_unpinned_with_a_warning(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        """The platform serves any pin but validates only its targets, so a rejected pin must not fail
        a pipeline that would deploy -- it re-asks unpinned and says the pin went unhonored."""
        rejection = _resp(
            codes.BAD_REQUEST,
            json={"detail": "Unsupported haystack version '2.20.0'. Supported versions: 2.30.2"},
        )
        mocked_haystack_enterprise_api.post.side_effect = [rejection, _resp(codes.NO_CONTENT)]

        result = await deployments_api.validate_pipeline("ws", query_yaml="y", haystack_version="2.20.0")

        assert result.is_valid is True, "a pin the platform won't validate is not an invalid pipeline"
        assert len(result.warnings) == 1
        assert "2.20.0" in (result.warnings[0].message or "")
        # Second attempt dropped the pin.
        assert mocked_haystack_enterprise_api.post.call_count == 2
        assert "headers" not in mocked_haystack_enterprise_api.post.call_args_list[1].kwargs

    async def test_unsupported_pin_keeps_real_errors_from_the_fallback(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        """Falling back must not swallow the config errors the unpinned check does find."""
        mocked_haystack_enterprise_api.post.side_effect = [
            _resp(codes.BAD_REQUEST, json={"detail": "Unsupported haystack version '2.20.0'."}),
            _resp(codes.BAD_REQUEST, json={"error_details": [{"category": "ERROR", "message": "bad component"}]}),
        ]

        result = await deployments_api.validate_pipeline("ws", query_yaml="y", haystack_version="2.20.0")

        assert result.has_errors is True
        assert result.errors[0].message == "bad component"
        assert len(result.warnings) == 1

    async def test_cold_start_retries_the_pin_and_succeeds(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock, no_retry_delay: None
    ) -> None:
        """A cold environment build outlasts the platform's validation timeout, so the first pinned
        attempt comes back transient -- the retry finds the build's downloads cached and lands."""
        mocked_haystack_enterprise_api.post.side_effect = [
            _resp(codes.GATEWAY_TIMEOUT, text="upstream timed out"),
            _resp(codes.NO_CONTENT),
        ]

        result = await deployments_api.validate_pipeline("ws", query_yaml="y", haystack_version="2.30.2")

        assert result.is_valid is True
        assert not result.warnings, "a retry that landed pinned needs no caveat"
        # Both attempts kept the pin -- the retry is not a silent downgrade.
        assert mocked_haystack_enterprise_api.post.call_count == 2
        for call in mocked_haystack_enterprise_api.post.call_args_list:
            assert call.kwargs["headers"] == {"x-haystack-version": "2.30.2"}

    async def test_client_timeout_on_the_pin_is_retried(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock, no_retry_delay: None
    ) -> None:
        """Our own read timeout is the same cold-start signal as a transient status."""
        mocked_haystack_enterprise_api.post.side_effect = [ReadTimeout("too slow"), _resp(codes.NO_CONTENT)]

        result = await deployments_api.validate_pipeline("ws", query_yaml="y", haystack_version="2.30.2")

        assert result.is_valid is True
        assert mocked_haystack_enterprise_api.post.call_count == 2

    async def test_exhausted_retries_fall_back_to_unpinned_with_a_warning(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock, no_retry_delay: None
    ) -> None:
        """If the build never finishes in time, say so and validate unpinned rather than hard-fail."""
        mocked_haystack_enterprise_api.post.side_effect = [
            _resp(codes.GATEWAY_TIMEOUT),
            _resp(codes.GATEWAY_TIMEOUT),
            _resp(codes.NO_CONTENT),
        ]

        result = await deployments_api.validate_pipeline("ws", query_yaml="y", haystack_version="2.30.2")

        assert result.is_valid is True
        assert len(result.warnings) == 1
        assert "did not return a verdict" in (result.warnings[0].message or "")
        # Two pinned attempts, then one unpinned.
        assert mocked_haystack_enterprise_api.post.call_count == 3
        assert "headers" not in mocked_haystack_enterprise_api.post.call_args_list[2].kwargs

    async def test_non_transient_failure_still_raises(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock, no_retry_delay: None
    ) -> None:
        """A permanent failure (e.g. auth) must not be retried or hidden behind an unpinned fallback."""
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.FORBIDDEN, text="nope")

        with pytest.raises(FailedToValidatePipelineError):
            await deployments_api.validate_pipeline("ws", query_yaml="y", haystack_version="2.30.2")

        assert mocked_haystack_enterprise_api.post.call_count == 1

    async def test_request_rejection_without_a_pin_is_blocking(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        """With no pin to blame there is nothing to retry, so the rejection must surface as blocking."""
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.BAD_REQUEST, json={"detail": "nope"})
        result = await deployments_api.validate_pipeline("ws", query_yaml="y")
        assert result.has_errors is True
        assert result.errors[0].message == "nope"
        assert mocked_haystack_enterprise_api.post.call_count == 1

    async def test_unparseable_400_is_blocking_not_valid(
        self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        """A 400 whose body yields no issues still means refused -- never ``is_valid``."""
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.BAD_REQUEST, json={"unexpected": "shape"})
        result = await deployments_api.validate_pipeline("ws", query_yaml="y")
        assert result.is_valid is False


@pytest.mark.asyncio
class TestGetAndActivity:
    async def test_get_deployment(self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock) -> None:
        did = uuid4()
        mocked_haystack_enterprise_api.get.return_value = _resp(
            codes.OK, json=_deployment_body("svc", deployment_id=str(did), status="DEPLOYED")
        )
        result = await deployments_api.get_deployment("ws", did)
        assert result.deployment_id == did
        assert result.status == DeploymentStatus.DEPLOYED

    async def test_list_activity(self, deployments_api: DeploymentsAPI, mocked_haystack_enterprise_api: Mock) -> None:
        mocked_haystack_enterprise_api.get.return_value = _resp(
            codes.OK,
            json={"data": [{"event_type": "REVISION_CREATED"}], "has_more": False, "total": 1},
        )
        events = await deployments_api.list_activity("ws", uuid4())
        assert events == [{"event_type": "REVISION_CREATED"}]


class TestDeploymentModeParsing:
    def test_reads_deployment_mode(self) -> None:
        body = {**_deployment_body("svc"), "deployment_mode": "SERVERLESS"}
        assert Deployment.from_response(body).deployment_mode == DeploymentMode.SERVERLESS

    def test_missing_deployment_mode_defaults_to_managed(self) -> None:
        assert Deployment.from_response(_deployment_body("svc")).deployment_mode == DeploymentMode.MANAGED

    def test_unknown_deployment_mode_defaults_to_managed(self) -> None:
        body = {**_deployment_body("svc"), "deployment_mode": "WAT"}
        assert Deployment.from_response(body).deployment_mode == DeploymentMode.MANAGED


class TestOutputTypeParsing:
    """The platform's own answer to "is this a chat pipeline?", derived from the active revision."""

    def test_reads_output_type(self) -> None:
        body = {**_deployment_body("svc"), "output_type": "chat"}
        assert Deployment.from_response(body).output_type is PipelineOutputType.CHAT

    def test_missing_output_type_is_none(self) -> None:
        # Absent until a revision is active, which is the normal case for a freshly pushed revision.
        assert Deployment.from_response(_deployment_body("svc")).output_type is None

    def test_null_output_type_is_none(self) -> None:
        body = {**_deployment_body("svc"), "output_type": None}
        assert Deployment.from_response(body).output_type is None

    def test_unknown_output_type_is_none(self) -> None:
        # The platform has values this SDK does not model (e.g. "unknown"); they must not crash a deploy.
        body = {**_deployment_body("svc"), "output_type": "unknown"}
        assert Deployment.from_response(body).output_type is None
