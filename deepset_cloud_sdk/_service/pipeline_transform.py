"""Build a deployable platform YAML from a local pipeline file.

The heavy lifting (importing the user's pipeline, inlining local components, recording the executing
``haystack-ai`` version) lives in :mod:`deepset_cloud_sdk._service.pipeline_extract`, which runs in
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
from deepset_cloud_sdk._service.pipeline_extract import (
    CODE_COMPONENT_TYPE,
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
    "resolve_io",
    "unmapped_mandatory_inputs",
    "unmapped_mandatory_warning",
]

# Interpreter names to look for inside a discovered virtual environment.
_VENV_PYTHONS = ("bin/python", "bin/python3", "Scripts/python.exe")

# Haystack version pinning is temporarily disabled: the ``dependencies`` block is rendered commented
# out so it has no effect on deployments. Flip to True to emit an active block again.
_EMIT_ACTIVE_DEPENDENCY_BLOCK = False


@dataclass(frozen=True)
class ExtractionBundle:
    """Typed view of the JSON "extraction bundle" produced by :mod:`pipeline_extract`.

    The extractor cannot import the SDK (it runs in the user's interpreter), so it emits a plain JSON
    dict; this class is the single place that dict's keys are interpreted on the SDK side.

    Input mappings are ``{input_key: ["component.socket", ...]}``; output mappings are
    ``{output_key: "component.socket"}``. ``available_*`` and ``mandatory_inputs`` are
    ``{component_name: [socket_name, ...]}``.
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
            available_inputs=raw.get("available_inputs") or {},
            available_outputs=raw.get("available_outputs") or {},
            mandatory_inputs=raw.get("mandatory_inputs") or {},
            dependencies=raw.get("dependencies") or [],
        )


# Callback that receives the bundle when inputs/outputs need resolving and returns the
# ``(inputs, outputs)`` dicts to use (empty means "leave unset").
IoResolver = Callable[[ExtractionBundle], Tuple[dict, dict]]


def build_config_yaml(
    target: Path,
    *,
    entrypoint: Optional[str] = None,
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
    io_resolver: Optional[IoResolver] = None,
    python_executable: Optional[str] = None,
) -> str:
    """Transform ``target`` into deployable YAML — the single extract → resolve → render path.

    The pipeline is loaded in a subprocess using ``python_executable`` (or an auto-detected venv),
    so this environment does not need the pipeline's dependencies installed. No API calls are made;
    the deploy flow and the CLI's ``--dry-run`` both go through here.

    Explicit ``inputs``/``outputs`` win; otherwise inferred values are used. See :func:`resolve_io`
    for when ``io_resolver`` is consulted.
    """
    bundle = extract_via_subprocess(target, entrypoint, python_executable)
    resolved_inputs, resolved_outputs = resolve_io(bundle, inputs, outputs, io_resolver)
    return render_config_yaml(bundle, inputs=resolved_inputs, outputs=resolved_outputs)


def resolve_io(
    bundle: ExtractionBundle,
    inputs: Optional[dict],
    outputs: Optional[dict],
    io_resolver: Optional[IoResolver],
) -> Tuple[dict, dict]:
    """Resolve the platform inputs/outputs to deploy with.

    Explicit ``inputs``/``outputs`` win; otherwise the bundle's inferred values are used. When
    ``io_resolver`` is provided it is consulted only if resolution is incomplete: a side is still
    empty, or a mandatory input socket is not mapped (which would fail at query time).
    """
    resolved_inputs = inputs if inputs is not None else bundle.inferred_inputs
    resolved_outputs = outputs if outputs is not None else bundle.inferred_outputs
    incomplete = (
        not resolved_inputs
        or not resolved_outputs
        or unmapped_mandatory_inputs(bundle.mandatory_inputs, resolved_inputs)
    )
    if io_resolver is not None and incomplete:
        new_inputs, new_outputs = io_resolver(bundle)
        if new_inputs:
            resolved_inputs = new_inputs
        if new_outputs:
            resolved_outputs = new_outputs
    return resolved_inputs, resolved_outputs


def render_config_yaml(
    bundle: ExtractionBundle,
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
) -> str:
    """Render deployable YAML from an extraction bundle and the final inputs/outputs.

    :param bundle: The extraction bundle.
    :param inputs: The resolved inputs mapping to embed; ``None``/empty omits the ``inputs`` section.
    :param outputs: The resolved outputs mapping to embed; ``None``/empty omits the ``outputs`` section.
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


def flatten_sockets(sockets_by_component: Dict[str, List[str]]) -> List[str]:
    """Flatten ``{component: [socket, ...]}`` into sorted ``"component.socket"`` paths."""
    return sorted(f"{comp}.{socket}" for comp, sockets in sockets_by_component.items() for socket in sockets)


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
    """Build the ``dependencies`` YAML block pinning the Haystack version.

    While :data:`_EMIT_ACTIVE_DEPENDENCY_BLOCK` is False the block is emitted commented out, e.g.::

        # dependencies:
        #   - haystack-ai==2.30.2

    so it does not affect deployment; users can uncomment it to pin the listed dependencies.
    Returns an empty string when there is nothing to pin.
    """
    if not dependencies:
        return ""
    if _EMIT_ACTIVE_DEPENDENCY_BLOCK:
        body = "\n".join(f"  - {line}" for line in dependencies)
        return f"dependencies:\n{body}\n"
    body = "\n".join(f"#   - {line}" for line in dependencies)
    return f"# dependencies:\n{body}\n"
