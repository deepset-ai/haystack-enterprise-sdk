"""Tests for the sandbox pipeline run API client."""

from typing import Any
from unittest.mock import Mock

import pytest
from httpx import Request, Response, codes

from haystack_enterprise_sdk._api.pipeline_run import (
    HaystackRunAPI,
    PipelineRunError,
    build_run_inputs,
)

_REQUEST = Request("POST", "https://test.deepset.ai")


def _resp(status_code: int, **kwargs: Any) -> Response:
    return Response(status_code=status_code, request=_REQUEST, **kwargs)


@pytest.fixture
def run_api(mocked_haystack_enterprise_api: Mock) -> HaystackRunAPI:
    return HaystackRunAPI(mocked_haystack_enterprise_api)


class TestBuildRunInputs:
    def test_maps_query_across_multiple_sockets(self) -> None:
        config = {"inputs": {"query": ["prompt_builder.query", "retriever.query"]}}
        inputs = build_run_inputs(config, query="who?")
        assert inputs == {
            "prompt_builder": {"query": "who?"},
            "retriever": {"query": "who?"},
        }

    def test_maps_filters_and_query(self) -> None:
        config = {"inputs": {"query": ["retriever.query"], "filters": ["retriever.filters"]}}
        inputs = build_run_inputs(config, query="q", filters={"field": "x"})
        assert inputs == {"retriever": {"query": "q", "filters": {"field": "x"}}}

    def test_extra_inputs_merge_and_win(self) -> None:
        config = {"inputs": {"query": ["retriever.query"]}}
        inputs = build_run_inputs(
            config,
            query="from-query",
            extra_inputs={"retriever": {"query": "override", "top_k": 5}},
        )
        assert inputs == {"retriever": {"query": "override", "top_k": 5}}

    def test_extra_inputs_only_without_query(self) -> None:
        inputs = build_run_inputs({}, extra_inputs={"llm": {"prompt": "hi"}})
        assert inputs == {"llm": {"prompt": "hi"}}

    def test_raises_when_nothing_to_send(self) -> None:
        # A query but no inputs mapping and no explicit inputs -> cannot route anything.
        with pytest.raises(PipelineRunError):
            build_run_inputs({}, query="who?")

    def test_query_wrapped_into_messages_for_chat_pipeline(self) -> None:
        # An agent/chat pipeline's input is 'messages' (List[ChatMessage]); a bare --query is wrapped
        # into a single user message and routed there.
        config = {"inputs": {"messages": ["agent.messages"]}}
        inputs = build_run_inputs(config, query="What is deepset?")
        assert inputs == {
            "agent": {
                "messages": [{"role": "user", "meta": {}, "name": None, "content": [{"text": "What is deepset?"}]}]
            }
        }

    def test_query_key_wins_over_messages_when_both_present(self) -> None:
        config = {"inputs": {"query": ["retriever.query"], "messages": ["agent.messages"]}}
        inputs = build_run_inputs(config, query="hi")
        assert inputs == {"retriever": {"query": "hi"}}

    def test_named_inputs_route_through_the_same_mapping_as_query(self) -> None:
        # A pipeline's own custom platform inputs (e.g. a per-agent system prompt) are just more
        # entries in the same 'inputs:' mapping query/filters/files already route through.
        config = {
            "inputs": {
                "query": ["retriever.query"],
                "security_prompt": ["review_security.system_prompt", "ctx_security.system_prompt"],
            }
        }
        inputs = build_run_inputs(config, query="hi", named_inputs={"security_prompt": "be thorough"})
        assert inputs == {
            "retriever": {"query": "hi"},
            "review_security": {"system_prompt": "be thorough"},
            "ctx_security": {"system_prompt": "be thorough"},
        }

    def test_named_inputs_only_without_query(self) -> None:
        config = {"inputs": {"github_token": ["payload.github_token"]}}
        inputs = build_run_inputs(config, named_inputs={"github_token": "ghs_abc"})
        assert inputs == {"payload": {"github_token": "ghs_abc"}}

    def test_named_inputs_are_sent_as_given_not_wrapped(self) -> None:
        # Unlike --query, a named input is never wrapped into a ChatMessage: the caller already
        # supplied the raw value for a key it explicitly named.
        config = {"inputs": {"messages": ["agent.messages"]}}
        inputs = build_run_inputs(config, named_inputs={"messages": ["raw-value"]})
        assert inputs == {"agent": {"messages": ["raw-value"]}}

    def test_named_inputs_win_over_query_for_the_same_key(self) -> None:
        config = {"inputs": {"query": ["retriever.query"]}}
        inputs = build_run_inputs(config, query="from-query", named_inputs={"query": "from-set"})
        assert inputs == {"retriever": {"query": "from-set"}}

    def test_named_inputs_merge_with_extra_inputs_and_lose_to_them(self) -> None:
        config = {"inputs": {"github_token": ["payload.github_token"]}}
        inputs = build_run_inputs(
            config,
            named_inputs={"github_token": "from-set"},
            extra_inputs={"payload": {"github_token": "from-inputs"}},
        )
        assert inputs == {"payload": {"github_token": "from-inputs"}}


@pytest.mark.asyncio
class TestRunPipeline:
    async def test_posts_expected_payload(self, run_api: HaystackRunAPI, mocked_haystack_enterprise_api: Mock) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.OK, json={"llm": {"replies": ["hi"]}})
        result = await run_api.run_pipeline(
            "ws",
            pipeline_config={"components": {}},
            inputs={"retriever": {"query": "q"}},
            include_outputs_from=["llm"],
        )
        assert result == {"llm": {"replies": ["hi"]}}
        _, kwargs = mocked_haystack_enterprise_api.post.call_args
        assert kwargs["workspace_name"] == "ws"
        assert kwargs["endpoint"] == "haystack/pipelines/run"
        assert kwargs["json"] == {
            "pipeline_config": {"components": {}},
            "inputs": {"retriever": {"query": "q"}},
            "include_outputs_from": ["llm"],
        }

    async def test_omits_include_outputs_from_when_none(
        self, run_api: HaystackRunAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.OK, json={})
        await run_api.run_pipeline("ws", pipeline_config={}, inputs={})
        _, kwargs = mocked_haystack_enterprise_api.post.call_args
        assert "include_outputs_from" not in kwargs["json"]

    async def test_raises_with_errors_body(self, run_api: HaystackRunAPI, mocked_haystack_enterprise_api: Mock) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(
            codes.BAD_REQUEST, json={"errors": ["missing secret OPENAI_API_KEY"]}
        )
        with pytest.raises(PipelineRunError, match="missing secret OPENAI_API_KEY"):
            await run_api.run_pipeline("ws", pipeline_config={}, inputs={})

    async def test_raises_with_raw_body_when_unparseable(
        self, run_api: HaystackRunAPI, mocked_haystack_enterprise_api: Mock
    ) -> None:
        mocked_haystack_enterprise_api.post.return_value = _resp(codes.INTERNAL_SERVER_ERROR, text="boom")
        with pytest.raises(PipelineRunError, match="boom"):
            await run_api.run_pipeline("ws", pipeline_config={}, inputs={})
