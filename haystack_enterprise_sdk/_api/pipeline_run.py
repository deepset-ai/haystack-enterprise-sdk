"""Haystack pipeline run API for deepset AI Platform.

Thin async client over :class:`HaystackEnterpriseAPI` for the (workspace-scoped) sandbox run endpoint:
``POST /workspaces/{workspace}/haystack/pipelines/run``. This runs a pipeline configuration with
the provided inputs *without* deploying it -- the same call the builder/playground makes.

Note: this endpoint is beta. The pipeline is sent as ``pipeline_config`` (the YAML parsed into a
dict), not as a YAML string, and the run inputs are keyed by ``{component_name: {socket: value}}``.
"""

from typing import Any, Dict, List, Optional

import structlog
from httpx import codes

from haystack_enterprise_sdk._api.config import ASYNC_CLIENT_TIMEOUT
from haystack_enterprise_sdk._api.haystack_enterprise_api import HaystackEnterpriseAPI

logger = structlog.get_logger(__name__)


class PipelineRunError(Exception):
    """Raised when running a pipeline in the sandbox fails (bad config/inputs or a server error).

    Carries the messages the platform returned (its ``{"errors": [...]}`` body) so the string form is
    suitable for direct CLI output.
    """


class HaystackRunAPI:
    """Sandbox pipeline run API for deepset AI Platform."""

    _ENDPOINT = "haystack/pipelines/run"

    def __init__(self, haystack_enterprise_api: HaystackEnterpriseAPI) -> None:
        """Create a HaystackRunAPI object.

        :param haystack_enterprise_api: An initialized HaystackEnterpriseAPI instance.
        """
        self._haystack_enterprise_api = haystack_enterprise_api

    async def run_pipeline(
        self,
        workspace_name: str,
        *,
        pipeline_config: Dict[str, Any],
        inputs: Dict[str, Dict[str, Any]],
        include_outputs_from: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Run a pipeline configuration with the given inputs, without deploying it.

        Mirrors the builder/playground "Run" call: send the parsed pipeline config plus the run
        inputs and get back the pipeline output keyed by component name.

        :param workspace_name: Name of the workspace.
        :param pipeline_config: The pipeline definition (components, connections, inputs/outputs)
            as a dict -- i.e. the platform YAML parsed into an object, not a YAML string.
        :param inputs: Run inputs, shape ``{component_name: {socket: value}}``.
        :param include_outputs_from: Component names whose outputs to include. When ``None`` the
            platform defaults to all components.
        :raises PipelineRunError: If the run fails (bad config/inputs or a server error).
        :return: The pipeline output, a dict keyed by component name.
        """
        payload: Dict[str, Any] = {"pipeline_config": pipeline_config, "inputs": inputs}
        if include_outputs_from is not None:
            payload["include_outputs_from"] = include_outputs_from

        response = await self._haystack_enterprise_api.post(
            workspace_name=workspace_name,
            endpoint=self._ENDPOINT,
            json=payload,
            timeout_s=ASYNC_CLIENT_TIMEOUT,  # pipeline runs can be slow (LLM calls); 20s is too short.
        )
        if response.status_code != codes.OK:
            raise PipelineRunError(_format_run_error(response.status_code, response.text))
        return dict(response.json())


def build_run_inputs(
    pipeline_config: Dict[str, Any],
    *,
    query: Optional[str] = None,
    filters: Optional[Any] = None,
    files: Optional[List[Any]] = None,
    extra_inputs: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build the ``inputs`` dict for a run from the config's platform ``inputs:`` mapping.

    Replicates the UI's ``generatePipelineRunInputs``: the platform ``inputs:`` section maps an input
    key to the component sockets it feeds (``{"query": ["prompt_builder.query", "retriever.query"]}``).
    Each provided value is expanded onto every ``"component.socket"`` path it maps to, producing the
    nested ``{component: {socket: value}}`` shape the run endpoint expects. ``extra_inputs`` (e.g. from
    ``--inputs``) is deep-merged on top so explicit inputs win.

    :param pipeline_config: The parsed pipeline config (its ``inputs:`` section drives the mapping).
    :param query: The query text to route to every socket mapped under the ``query`` input key. For a
        chat/agent pipeline whose input key is ``messages`` (and has no ``query`` key), the query is
        wrapped into a single user ``ChatMessage`` and routed there instead.
    :param filters: Optional filters to route to the ``filters`` input key.
    :param files: Optional files to route to the ``files`` input key.
    :param extra_inputs: Explicit ``{component: {socket: value}}`` inputs, merged last (wins).
    :raises PipelineRunError: If there is nothing to route (no mapping for the given values and no
        ``extra_inputs``) -- typically a query with no ``inputs:`` section in the config.
    :return: The run inputs, shape ``{component_name: {socket: value}}``.
    """
    yaml_inputs = pipeline_config.get("inputs") or {}
    values: Dict[str, Any] = {}
    if query is not None:
        # A plain --query fills the 'query' input when present. For a chat/agent pipeline whose input
        # is 'messages' (List[ChatMessage]) and has no 'query' key, wrap it into one user message so a
        # bare --query still works.
        if not yaml_inputs.get("query") and yaml_inputs.get("messages"):
            values["messages"] = [_user_chat_message(query)]
        else:
            values["query"] = query
    if filters is not None:
        values["filters"] = filters
    if files is not None:
        values["files"] = files

    inputs: Dict[str, Dict[str, Any]] = {}
    for input_key, value in values.items():
        paths = yaml_inputs.get(input_key)
        if not paths:
            continue
        for path in paths:
            component, _, socket = str(path).partition(".")
            if not socket:
                continue
            inputs.setdefault(component, {})[socket] = value

    if extra_inputs:
        for component, sockets in extra_inputs.items():
            inputs.setdefault(component, {}).update(sockets)

    if not inputs:
        raise PipelineRunError(
            "No pipeline inputs to send. The query could not be mapped to any component "
            "(the config has no 'inputs' section). Pass explicit inputs with --inputs."
        )
    return inputs


def _user_chat_message(text: str) -> Dict[str, Any]:
    """A user ``ChatMessage`` in Haystack's serialized form.

    Built as a plain dict (not via ``haystack.dataclasses.ChatMessage``) so the SDK doesn't need
    Haystack installed in its own environment to run a chat/agent pipeline; the platform deserializes
    it back into a ``ChatMessage`` on the run endpoint.
    """
    return {"role": "user", "meta": {}, "name": None, "content": [{"text": text}]}


def _format_run_error(status_code: int, body: str) -> str:
    """Turn a failed run response into a readable message, unwrapping the ``{"errors": [...]}`` body."""
    import json  # pylint: disable=import-outside-toplevel

    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        errors = parsed.get("errors")
        if isinstance(errors, list) and errors:
            joined = "\n".join(f"  - {err}" for err in errors)
            return f"Pipeline run failed (status {status_code}):\n{joined}"
        message = parsed.get("message") or parsed.get("detail")
        if message:
            return f"Pipeline run failed (status {status_code}): {message}"
    return f"Pipeline run failed (status {status_code}): {body}"
