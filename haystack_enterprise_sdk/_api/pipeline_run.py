"""Haystack pipeline run API for deepset AI Platform.

Thin async client over :class:`HaystackEnterpriseAPI` for the (workspace-scoped) sandbox run endpoint:
``POST /workspaces/{workspace}/haystack/pipelines/run``. This runs a pipeline configuration with
the provided inputs *without* deploying it -- the same call the builder/playground makes.

Note: this endpoint is beta. The pipeline is sent as ``pipeline_config`` (the YAML parsed into a
dict), not as a YAML string, and the run inputs are keyed by ``{component_name: {socket: value}}``.
"""

import asyncio
from typing import Any, Callable, Dict, List, Optional

import httpx
import structlog
from httpx import codes

from haystack_enterprise_sdk._api.config import ASYNC_CLIENT_TIMEOUT
from haystack_enterprise_sdk._api.haystack_enterprise_api import (
    TRANSIENT_STATUS_CODES,
    HaystackEnterpriseAPI,
)

logger = structlog.get_logger(__name__)

# Retries *after* the first attempt, so the default is 3 attempts in total.
DEFAULT_RUN_RETRIES = 2
_RETRY_BASE_DELAY_S = 2.0

# Called as ``(next_attempt, total_attempts, reason)`` before each backoff sleep.
OnRetry = Callable[[int, int, str], None]


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

    async def run_pipeline(  # pylint: disable=too-many-arguments
        self,
        workspace_name: str,
        *,
        pipeline_config: Dict[str, Any],
        inputs: Dict[str, Dict[str, Any]],
        include_outputs_from: Optional[List[str]] = None,
        retries: int = DEFAULT_RUN_RETRIES,
        on_retry: Optional[OnRetry] = None,
    ) -> Dict[str, Any]:
        """Run a pipeline configuration with the given inputs, without deploying it.

        Mirrors the builder/playground "Run" call: send the parsed pipeline config plus the run
        inputs and get back the pipeline output keyed by component name.

        Transient failures (network errors, timeouts, 429 and 5xx) are retried with exponential
        backoff. A permanent failure -- a bad config, bad inputs, auth -- is raised on the first
        attempt, since repeating it would only burn time and LLM credits.

        :param workspace_name: Name of the workspace.
        :param pipeline_config: The pipeline definition (components, connections, inputs/outputs)
            as a dict -- i.e. the platform YAML parsed into an object, not a YAML string.
        :param inputs: Run inputs, shape ``{component_name: {socket: value}}``.
        :param include_outputs_from: Component names whose outputs to include. When ``None`` the
            platform defaults to all components.
        :param retries: Number of retry attempts after a transient failure. ``0`` disables retrying.
        :param on_retry: Optional callback invoked as ``(next_attempt, total_attempts, reason)``
            before each backoff sleep, so callers can report the retry to the user.
        :raises PipelineRunError: If the run fails (bad config/inputs, a server error, or every
            attempt failed transiently).
        :return: The pipeline output, a dict keyed by component name.
        """
        payload: Dict[str, Any] = {"pipeline_config": pipeline_config, "inputs": inputs}
        if include_outputs_from is not None:
            payload["include_outputs_from"] = include_outputs_from

        attempts = max(1, retries + 1)
        reason = ""
        for attempt in range(1, attempts + 1):
            try:
                response = await self._haystack_enterprise_api.post(
                    workspace_name=workspace_name,
                    endpoint=self._ENDPOINT,
                    json=payload,
                    timeout_s=ASYNC_CLIENT_TIMEOUT,  # runs can be slow (LLM calls); 20s is too short.
                )
            except httpx.RequestError as err:  # covers timeouts, connection and read errors
                reason = f"Pipeline run failed: {type(err).__name__}: {err}"
            else:
                if response.status_code == codes.OK:
                    return dict(response.json())
                reason = _format_run_error(response.status_code, response.text)
                if response.status_code not in TRANSIENT_STATUS_CODES:
                    raise PipelineRunError(reason)

            if attempt == attempts:
                break
            logger.debug("Retrying pipeline run", attempt=attempt, attempts=attempts, reason=reason)
            if on_retry is not None:
                on_retry(attempt + 1, attempts, reason)
            await asyncio.sleep(_RETRY_BASE_DELAY_S * 2 ** (attempt - 1))

        raise PipelineRunError(reason if attempts == 1 else f"{reason} (after {attempts} attempts)")


def build_run_inputs(
    pipeline_config: Dict[str, Any],
    *,
    query: Optional[str] = None,
    filters: Optional[Any] = None,
    files: Optional[List[Any]] = None,
    named_inputs: Optional[Dict[str, Any]] = None,
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
    :param named_inputs: Values for input keys beyond ``query``/``filters``/``files`` (e.g. a pipeline's
        own custom platform inputs), each routed through the same ``inputs:`` mapping. Unlike ``query``,
        a named input is sent exactly as given -- no chat-message wrapping -- since the caller already
        supplied the raw value for a key it explicitly named. A key here wins over ``query``/``filters``/
        ``files`` for the same input key, e.g. an explicit ``named_inputs={"query": ...}`` overrides
        ``query=...``.
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
    if named_inputs:
        values.update(named_inputs)

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
            "No pipeline inputs to send. Nothing could be mapped to any component (the config has no "
            "matching 'inputs' entry). Pass explicit inputs with --set KEY=VALUE or --inputs."
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
