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
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

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

__all__ = [
    "CODE_COMPONENT_TYPE",
    "ExtractionBundle",
    "IoResolver",
    "PipelineTransformError",
    "build_config_yaml",
    "classify_module",
    "detect_project_python",
    "extract_from_file",
    "extract_from_pipeline",
    "extract_via_subprocess",
    "flatten_sockets",
    "load_pipeline_from_file",
    "render_config_yaml",
    "SocketOption",
    "socket_options",
    "STANDARD_INPUT_KEYS",
    "STANDARD_OUTPUT_KEYS",
    "resolve_io",
    "unmapped_mandatory_inputs",
    "unmapped_mandatory_warning",
]

# Interpreter names to look for inside a discovered virtual environment.
_VENV_PYTHONS = ("bin/python", "bin/python3", "Scripts/python.exe")


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
    pipeline_output_type: Optional[str] = None,
    io_resolver: Optional[IoResolver] = None,
    python_executable: Optional[str] = None,
) -> str:
    """Transform ``target`` into deployable YAML — the single extract → resolve → render path.

    The pipeline is loaded in a subprocess using ``python_executable`` (or an auto-detected venv),
    so this environment does not need the pipeline's dependencies installed. No API calls are made;
    the deploy flow and the CLI's ``--dry-run`` both go through here.

    Explicit ``inputs``/``outputs`` win; otherwise inferred values are used, and ``io_resolver`` (when
    given) gets the final say — see :func:`resolve_io`.
    """
    bundle = extract_via_subprocess(target, entrypoint, python_executable)
    resolved_inputs, resolved_outputs = resolve_io(bundle, inputs, outputs, io_resolver)
    return render_config_yaml(
        bundle, inputs=resolved_inputs, outputs=resolved_outputs, pipeline_output_type=pipeline_output_type
    )


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
    pipeline_output_type: Optional[str] = None,
) -> str:
    """Render deployable YAML from an extraction bundle and the final inputs/outputs.

    :param bundle: The extraction bundle.
    :param inputs: The resolved inputs mapping to embed; ``None``/empty omits the ``inputs`` section.
    :param outputs: The resolved outputs mapping to embed; ``None``/empty omits the ``outputs`` section.
    :param pipeline_output_type: Optional platform ``pipeline_output_type`` hint (``generative``,
        ``chat``, ``extractive``, ``document``); omitted from the YAML when ``None``.
    :return: The platform-ready ``config_yaml`` string.
    """
    pipeline_dict = bundle.pipeline

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

    if pipeline_output_type:
        pipeline_dict["pipeline_output_type"] = pipeline_output_type

    if bundle.async_enabled:
        pipeline_dict["async_enabled"] = True

    yaml = YAML()
    yaml.indent(mapping=2, sequence=2)
    buffer = StringIO()
    yaml.dump(pipeline_dict, buffer)
    config_yaml = buffer.getvalue()

    dependency_block = _build_dependency_block(bundle.dependencies)
    if dependency_block:
        config_yaml = f"{config_yaml}\n{dependency_block}"
    return config_yaml


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
        'The pipeline will fail at query time with "Missing mandatory input". Map them interactively '
        "on a terminal, or pass an explicit inputs mapping that routes a platform input (e.g. `query`) "
        "to each of these sockets."
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
    only its JSON result crosses back.

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
        out_file.seek(0)
        return ExtractionBundle.from_dict(json.load(out_file))


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


def _build_dependency_block(dependencies: list) -> str:
    """Build the ``dependencies`` YAML block pinning the Haystack version, e.g.::

        dependencies:
          - haystack-ai==2.30.2

    Returns an empty string when there is nothing to pin.
    """
    if not dependencies:
        return ""
    body = "\n".join(f"  - {line}" for line in dependencies)
    return f"dependencies:\n{body}\n"
