"""Build a deployable platform YAML from a local pipeline file.

The heavy lifting (importing the user's pipeline, inlining local components, recording the executing
``haystack-ai`` version) lives in :mod:`haystack_enterprise_sdk._service.pipeline_extract`, which runs in
the *pipeline's* Python environment — in-process for the programmatic API, or in a subprocess
(:func:`extract_via_subprocess`) so the CLI/SDK environment never needs the pipeline's dependencies.

This module owns the SDK side of that boundary: it parses the extractor's JSON into a typed
:class:`ExtractionBundle`, resolves the platform inputs/outputs (:func:`resolve_io`), and renders the
final ``config_yaml`` (:func:`render_config_yaml`). :func:`build_config_yaml` is the single
extract → resolve → render path used by both the deploy flow and the CLI's ``--dry-run``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, fields
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import structlog
from ruamel.yaml import YAML

# Re-export the extractor's public surface so existing imports keep working.
from haystack_enterprise_sdk._service.pipeline_extract import (
    CODE_COMPONENT_TYPE,
    STANDARD_INPUT_KEYS,
    STANDARD_OUTPUT_KEYS,
    PipelineTransformError,
    classify_module,
    extract_from_file,
    extract_from_pipeline,
    load_pipeline_from_file,
)

logger = structlog.get_logger(__name__)

# How the renderer spells the Haystack pin inside the ``dependencies`` block.
_HAYSTACK_PIN_PREFIX = "haystack-ai=="

__all__ = [
    "CODE_COMPONENT_TYPE",
    "DEPLOY_ONLY_KEYS",
    "ExtractionBundle",
    "IoResolver",
    "KNOWN_SETTING_KEYS",
    "PipelineSettings",
    "PipelineTransformError",
    "RESERVED_ROOT_KEYS",
    "build_config_yaml",
    "classify_module",
    "detect_project_python",
    "extract_from_file",
    "extract_from_pipeline",
    "extract_via_subprocess",
    "flatten_sockets",
    "haystack_pin",
    "load_pipeline_from_file",
    "render_config_yaml",
    "SocketOption",
    "socket_options",
    "STANDARD_INPUT_KEYS",
    "STANDARD_OUTPUT_KEYS",
    "resolve_io",
    "unmapped_mandatory_inputs",
    "unmapped_mandatory_warning",
    "validate_extra_root_keys",
]

# Interpreter names to look for inside a discovered virtual environment.
_VENV_PYTHONS = ("bin/python", "bin/python3", "Scripts/python.exe")

#: Emitter width for every YAML we render, chosen so ruamel never wraps a scalar.
#:
#: ruamel wraps at ``best_width`` (80 by default) and a wrapped line reads back FOLDED — the
#: break becomes a space. For a multi-line scalar, which ruamel emits double-quoted, that
#: break can land mid-token, and landing next to a backslash rewrites the escape: a folded
#: ``re.compile('(?:,(\\d+))')`` loads back as ``re.compile('(?:,(\\ d+))')``, a regex that
#: compiles fine and matches nothing.
#:
#: Generated ``Code`` blocks are exactly that shape — one long multi-line scalar, nested deep
#: enough that content crosses column 80 quickly. Nothing downstream can detect it: the block
#: is valid Python, only its behaviour changed. Whether a given block is hit depends on where
#: column 80 falls, so it corrupts some components and not others in the same pipeline.
NO_WRAP_WIDTH = 2**31


@dataclass(frozen=True)
class ExtractionBundle:
    """Typed view of the JSON "extraction bundle" produced by :mod:`pipeline_extract`.

    The extractor cannot import the SDK (it runs in the user's interpreter), so it emits a plain JSON
    dict; this class is the single place that dict's keys are interpreted on the SDK side.

    Input mappings are ``{input_key: ["component.socket", ...]}``; output mappings are
    ``{output_key: "component.socket"}``. ``available_*`` are
    ``{component_name: {socket_name: {"type": str | None, "is_mandatory": bool}}}`` (see
    :func:`socket_options`); ``mandatory_inputs`` is ``{component_name: [socket_name, ...]}``.
    """

    pipeline: dict = field(default_factory=dict)
    async_enabled: bool = False
    inferred_inputs: dict = field(default_factory=dict)
    inferred_outputs: dict = field(default_factory=dict)
    available_inputs: dict = field(default_factory=dict)
    available_outputs: dict = field(default_factory=dict)
    mandatory_inputs: dict = field(default_factory=dict)
    dependencies: list = field(default_factory=list)
    # Platform ``pipeline_output_type`` the extractor inferred from the pipeline shape (e.g. ``chat``
    # for a compiled agent); used as the default when the caller doesn't pass one explicitly.
    suggested_pipeline_output_type: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: dict) -> "ExtractionBundle":
        """Parse the extractor's JSON dict, tolerating absent keys."""
        return cls(
            pipeline=raw.get("pipeline") or {},
            async_enabled=bool(raw.get("async_enabled")),
            inferred_inputs=raw.get("inferred_inputs") or {},
            inferred_outputs=raw.get("inferred_outputs") or {},
            available_inputs=_normalize_available(raw.get("available_inputs") or {}),
            available_outputs=_normalize_available(raw.get("available_outputs") or {}),
            mandatory_inputs=raw.get("mandatory_inputs") or {},
            dependencies=raw.get("dependencies") or [],
            suggested_pipeline_output_type=raw.get("suggested_pipeline_output_type"),
        )


def _normalize_available(available: dict) -> dict:
    """Normalize ``available_*`` to the typed shape, tolerating a stale extractor's list shape.

    Old extractors emitted ``{component: [socket_name, ...]}``; the current shape is
    ``{component: {socket_name: {"type": ..., "is_mandatory": ...}}}``.
    """
    normalized: dict = {}
    for comp_name, sockets in available.items():
        if isinstance(sockets, dict):
            normalized[comp_name] = {
                name: (info if isinstance(info, dict) else {"type": None, "is_mandatory": False})
                for name, info in sockets.items()
            }
        else:
            normalized[comp_name] = {name: {"type": None, "is_mandatory": False} for name in sockets}
    return normalized


@dataclass(frozen=True)
class PipelineSettings:
    """The top-level ``config_yaml`` keys an author declares, as opposed to the socket mapping.

    The only way to set a top-level key. One container rather than one kwarg per key: every setting
    here has to cross :func:`build_config_yaml`, :func:`render_config_yaml`,
    ``DeploymentService.deploy``/``.validate`` and both deployment clients, so a kwarg per key costs six
    signatures every time the platform grows one. ``extra`` carries root keys this SDK has no field for,
    so a key the platform adds needs no SDK change at all — it only has to survive
    :func:`validate_extra_root_keys`.

    ``None`` means "not declared" and defers to what the extractor inferred. That is why
    ``dependencies=[]`` and ``dependencies=None`` differ: the empty list is an explicit "pin nothing"
    that suppresses the auto-detected ``haystack-ai`` pin.
    """

    pipeline_output_type: Optional[str] = None
    session_storage: Optional[bool] = None
    dependencies: Optional[List[str]] = None
    extra: Mapping[str, Any] = field(default_factory=dict)


#: The root keys :class:`PipelineSettings` names, and therefore validates, rather than passing through
#: in ``extra``. Derived from the fields so a new setting cannot fall out of sync with the io-config
#: loader or with the stubs a generated io-config documents (``io_spec.PIPELINE_SETTINGS``).
KNOWN_SETTING_KEYS = frozenset(f.name for f in fields(PipelineSettings)) - {"extra"}

#: Root keys the renderer derives rather than accepts, so nothing may pass them through: the first
#: five come straight out of ``Pipeline.dumps()``, ``inputs``/``outputs`` are the socket mapping (with
#: their own io-config sections and their own resolution path), and ``async_enabled`` follows the
#: pipeline class.
RESERVED_ROOT_KEYS = frozenset(
    {
        "components",
        "connections",
        "connection_type_validation",
        "max_runs_per_component",
        "metadata",
        "inputs",
        "outputs",
        "async_enabled",
    }
)

#: Root keys that describe a *deployed revision* and are therefore stripped before a sandbox run: that
#: endpoint installs nothing (``dependencies``), has no search session to scope a workspace to
#: (``session_storage``), and renders no Playground result (``pipeline_output_type``).
DEPLOY_ONLY_KEYS = frozenset({"dependencies", "session_storage", "pipeline_output_type"})


def validate_extra_root_keys(extra: Mapping[str, Any]) -> None:
    """Reject passthrough root keys that collide with something the SDK already owns.

    :param extra: The passthrough keys to check (``PipelineSettings.extra``).
    :raises PipelineTransformError: If a key is reserved (:data:`RESERVED_ROOT_KEYS`) or has its own
        :class:`PipelineSettings` field — either way the renderer would overwrite the passthrough
        value, so accepting it would mean honouring the config silently and partially.
    """
    reserved = sorted(set(extra) & RESERVED_ROOT_KEYS)
    if reserved:
        raise PipelineTransformError(f"cannot set {_quoted(reserved)} — derived from the pipeline itself.")
    named = sorted(set(extra) & KNOWN_SETTING_KEYS)
    if named:
        raise PipelineTransformError(f"cannot pass {_quoted(named)} through — covered by a dedicated setting.")


def _quoted(keys: List[str]) -> str:
    """Render key names for an error message, e.g. ``'inputs' and 'outputs'``."""
    return " and ".join(", ".join(f"'{key}'" for key in keys).rsplit(", ", 1))


# Callback consulted to finalize inputs/outputs. Receives the bundle plus the already-resolved
# ``(inputs, outputs)`` and returns the ``(inputs, outputs)`` dicts to use (empty means "leave
# unset"). Whether/how it interacts (review the full mapping, fill only gaps, or return unchanged)
# is the resolver's own policy — see the CLI's ``_resolve_io_interactive``.
IoResolver = Callable[[ExtractionBundle, dict, dict], Tuple[dict, dict]]


def build_config_yaml(
    target: Path,
    *,
    entrypoint: Optional[str] = None,
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
    settings: Optional[PipelineSettings] = None,
    io_resolver: Optional[IoResolver] = None,
    python_executable: Optional[str] = None,
) -> str:
    """Transform ``target`` into deployable YAML — the single extract → resolve → render path.

    The pipeline is loaded in a subprocess using ``python_executable`` (or an auto-detected venv),
    so this environment does not need the pipeline's dependencies installed. No API calls are made;
    the deploy flow and the CLI's ``--dry-run`` both go through here.

    Explicit ``inputs``/``outputs`` win; otherwise inferred values are used, and ``io_resolver`` (when
    given) gets the final say — see :func:`resolve_io`.

    :param settings: The top-level ``config_yaml`` keys to declare — see :class:`PipelineSettings`.
    """
    bundle = extract_via_subprocess(target, entrypoint, python_executable)
    resolved_inputs, resolved_outputs = resolve_io(bundle, inputs, outputs, io_resolver)
    return render_config_yaml(bundle, inputs=resolved_inputs, outputs=resolved_outputs, settings=settings)


def resolve_io(
    bundle: ExtractionBundle,
    inputs: Optional[dict],
    outputs: Optional[dict],
    io_resolver: Optional[IoResolver],
) -> Tuple[dict, dict]:
    """Resolve the platform inputs/outputs to deploy with.

    Explicit ``inputs``/``outputs`` args win (each replaces its side wholesale); otherwise name-based
    inference provides the starting point. When an ``io_resolver`` is given it is always called with
    the resolved mappings and gets the final say — the resolver itself decides whether to interact
    (review the mapping, prompt for gaps) or pass the mappings through unchanged.
    """
    resolved_inputs = inputs if inputs is not None else dict(bundle.inferred_inputs)
    resolved_outputs = outputs if outputs is not None else dict(bundle.inferred_outputs)

    if io_resolver is not None:
        new_inputs, new_outputs = io_resolver(bundle, resolved_inputs, resolved_outputs)
        if new_inputs:
            resolved_inputs = new_inputs
        if new_outputs:
            resolved_outputs = new_outputs
    return resolved_inputs, resolved_outputs


def render_config_yaml(
    bundle: ExtractionBundle,
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
    settings: Optional[PipelineSettings] = None,
) -> str:
    """Render deployable YAML from an extraction bundle and the final inputs/outputs.

    Every top-level key the platform reads is written here, from one of two sources: the author's
    :class:`PipelineSettings`, or what the extractor inferred for whatever they left undeclared.

    :param bundle: The extraction bundle.
    :param inputs: The resolved inputs mapping to embed; ``None``/empty omits the ``inputs`` section.
    :param outputs: The resolved outputs mapping to embed; ``None``/empty omits the ``outputs`` section.
    :param settings: The author-declared top-level keys (see :class:`PipelineSettings`). A declared
        value replaces the inferred one wholesale — the same override the ``inputs``/``outputs``
        mappings get; ``extra`` keys are written as they came in.
    :raises PipelineTransformError: If ``settings.extra`` names a key the SDK owns.
    :return: The platform-ready ``config_yaml`` string.
    """
    settings = settings or PipelineSettings()
    validate_extra_root_keys(settings.extra)

    # Copied, not aliased: every assignment below would otherwise land on the caller's bundle, so
    # rendering the same bundle twice leaked the first call's keys into the second. A shallow copy is
    # enough — this function only ever writes top-level keys.
    pipeline_dict = dict(bundle.pipeline)
    # Passthrough keys first, so a validated key of the same name overwrites one of these rather than
    # the reverse. validate_extra_root_keys has already ruled that collision out; ordering it this way
    # means the guarantee does not rest on the check alone.
    pipeline_dict.update(settings.extra)

    if inputs:
        pipeline_dict["inputs"] = inputs
    if outputs:
        pipeline_dict["outputs"] = outputs
    if not inputs and not outputs:
        logger.warning(
            "Could not infer pipeline inputs/outputs from open sockets. Deploying without them; "
            "the Playground query UI will be unavailable. Pass inputs/outputs explicitly to enable it."
        )

    unmapped = unmapped_mandatory_inputs(bundle.mandatory_inputs, inputs or {})
    if unmapped:
        logger.warning(unmapped_mandatory_warning(unmapped))

    # An explicit type always wins; otherwise fall back to what the extractor inferred from the
    # pipeline shape (e.g. ``chat`` for a compiled agent).
    output_type = settings.pipeline_output_type or bundle.suggested_pipeline_output_type
    if output_type:
        pipeline_dict["pipeline_output_type"] = output_type

    # A per-search-session workspace, mounted and snapshotted by the worker gateway, so files a tool
    # writes are still there on the next run in the same session. The platform reads the key as a plain
    # truthy flag (``pipeline_config.get("session_storage")``), so a false value means exactly what an
    # absent one does and is left out rather than written as ``false``.
    if settings.session_storage:
        pipeline_dict["session_storage"] = True

    if bundle.async_enabled:
        pipeline_dict["async_enabled"] = True

    # Written last so the pins keep their place at the bottom of the file. Declared pins replace the
    # extractor's auto-detected ``haystack-ai`` pin outright, which is what makes ``dependencies: []``
    # a way to ship no pin at all — worth having, since that pin is read off whichever interpreter
    # loaded the pipeline and is not always the one the deployed revision should install.
    dependencies = bundle.dependencies if settings.dependencies is None else settings.dependencies
    if dependencies:
        pipeline_dict["dependencies"] = list(dependencies)

    yaml = YAML()
    yaml.indent(mapping=2, sequence=2)
    yaml.width = NO_WRAP_WIDTH
    buffer = StringIO()
    yaml.dump(pipeline_dict, buffer)
    return buffer.getvalue()


def unmapped_mandatory_inputs(mandatory: dict, inputs: dict) -> List[str]:
    """Return the mandatory ``"component.socket"`` paths not covered by any platform ``inputs`` mapping.

    :param mandatory: ``{component_name: [socket_name, ...]}`` mandatory open sockets (from the bundle).
    :param inputs: The resolved platform inputs mapping (``{input_key: ["component.socket", ...]}``).
    :return: Sorted list of ``"component.socket"`` paths that no input routes to.
    """
    mapped = {socket for sockets in (inputs or {}).values() for socket in sockets}
    return sorted(
        f"{comp}.{socket}"
        for comp, sockets in (mandatory or {}).items()
        for socket in sockets
        if f"{comp}.{socket}" not in mapped
    )


def unmapped_mandatory_warning(unmapped: List[str]) -> str:
    """The canonical warning for mandatory pipeline inputs left unmapped — used by log and CLI output."""
    return (
        f"These mandatory pipeline inputs are not mapped to any platform input: {', '.join(unmapped)}. "
        'A query sent through the platform will fail with "Missing mandatory input". Pass an inputs '
        "mapping that routes a platform input (e.g. `query`) to each of these sockets -- via an "
        "io-config file, or interactively with `deploy --share`. Harmless if you invoke the pipeline "
        "directly with explicit inputs."
    )


@dataclass(frozen=True)
class SocketOption:
    """One selectable socket for interactive mapping: its path plus display metadata."""

    path: str  # "retriever.query"
    type_str: Optional[str] = None  # e.g. "str", "List[Document]"; None when unknown
    is_mandatory: bool = False


def socket_options(sockets_by_component: Dict[str, dict]) -> List[SocketOption]:
    """Flatten a bundle's typed ``available_*`` mapping into sorted :class:`SocketOption` entries."""
    options = []
    for comp, sockets in sockets_by_component.items():
        for socket, info in sockets.items():
            info = info if isinstance(info, dict) else {}
            options.append(
                SocketOption(
                    path=f"{comp}.{socket}",
                    type_str=info.get("type"),
                    is_mandatory=bool(info.get("is_mandatory")),
                )
            )
    return sorted(options, key=lambda option: option.path)


def flatten_sockets(sockets_by_component: Dict[str, dict]) -> List[str]:
    """Flatten a bundle's ``available_*`` mapping into sorted ``"component.socket"`` paths."""
    return [option.path for option in socket_options(sockets_by_component)]


def extract_via_subprocess(
    target: Path,
    entrypoint: Optional[str] = None,
    python_executable: Optional[str] = None,
) -> ExtractionBundle:
    """Extract the pipeline bundle by running the extractor in the pipeline's own interpreter.

    This is the decoupling that lets the CLI/SDK environment stay free of the pipeline's dependencies:
    the extractor is executed by ``python_executable`` (which has Haystack and the pipeline deps), and
    only its JSON result crosses back. Anything the extractor itself logged (e.g. a silent-but-notable
    adjustment for platform compatibility) is forwarded through the SDK's own logger -- see
    :func:`_forward_extractor_warnings`.

    :param target: Path to the pipeline ``.py`` file.
    :param entrypoint: Pipeline instance/factory name (when the file is ambiguous).
    :param python_executable: Interpreter to run the extractor with. Defaults to a virtualenv detected
        near ``target``, falling back to the current interpreter.
    :raises PipelineTransformError: If extraction fails (bubbling up the extractor's error message).
    :return: The extraction bundle.
    """
    target = Path(target).resolve()
    python = python_executable or detect_project_python(target)
    extractor = str(Path(__file__).with_name("pipeline_extract.py"))

    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=True) as out_file:
        cmd = [python, extractor, str(target), "--out", out_file.name]
        if entrypoint:
            cmd += ["--entrypoint", entrypoint]
        logger.debug("Extracting pipeline via subprocess.", python=python, target=str(target))
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise PipelineTransformError(f"Failed to load the pipeline using interpreter '{python}':\n{message}")
        _forward_extractor_warnings(completed.stderr)
        out_file.seek(0)
        return ExtractionBundle.from_dict(json.load(out_file))


def _forward_extractor_warnings(stderr: str) -> None:
    """Surface the extractor subprocess's own log lines after a run that otherwise SUCCEEDED.

    ``pipeline_extract.py`` deliberately depends on nothing but the standard library plus Haystack (see
    its module docstring), so it logs through stdlib ``logging`` rather than the SDK's ``structlog`` --
    unconfigured, which means WARNING+ records reach ``stderr`` via Python's own default
    (``logging.lastResort``). That is exactly how it reports something it silently adjusted for
    platform compatibility, e.g. stripping an Agent's ``hooks`` because the platform Agent does not
    accept them (see ``_sanitize_agent_init_params``). Only a FAILING run read ``stderr`` before this,
    so on success that warning shipped with the deployed pipeline and nothing said so.

    :param stderr: The subprocess's captured stderr from a run whose exit code was already 0.
    """
    text = stderr.strip()
    if not text:
        return
    for line in text.splitlines():
        logger.warning("pipeline_extract: %s", line)


def detect_project_python(target: Path) -> str:
    """Find the interpreter to load the pipeline with.

    Walks up from ``target`` looking for a virtual environment (``.venv``/``venv``); falls back to the
    current interpreter. Users can always override this with ``--python``.
    """
    target = Path(target).resolve()
    for directory in [target.parent, *target.parents]:
        for venv_name in (".venv", "venv"):
            for rel in _VENV_PYTHONS:
                candidate = directory / venv_name / rel
                if candidate.is_file():
                    logger.debug("Detected project virtualenv interpreter.", python=str(candidate))
                    return str(candidate)
    return sys.executable


def haystack_pin(config_yaml: str) -> Optional[str]:
    """Return the ``haystack-ai`` version pinned in ``config_yaml``'s ``dependencies`` block.

    The inverse of the ``dependencies`` key :func:`render_config_yaml` writes. The platform picks the
    validation worker's environment from a request header, but rejects a header that disagrees with
    the YAML's own pin, so the header has to be read back off the block rather than sniffed
    separately -- and a declared ``dependencies`` list overrides the auto-detected pin, so the block
    is the only place the shipped version is known.

    :param config_yaml: Rendered platform YAML, with or without a ``dependencies`` block.
    :return: The pinned version, or ``None`` when the block is absent or pins no ``haystack-ai``.
    """
    parsed = YAML(typ="safe").load(config_yaml)
    if not isinstance(parsed, dict):
        return None
    for requirement in parsed.get("dependencies") or []:
        if isinstance(requirement, str) and requirement.startswith(_HAYSTACK_PIN_PREFIX):
            return requirement[len(_HAYSTACK_PIN_PREFIX) :].strip() or None
    return None
