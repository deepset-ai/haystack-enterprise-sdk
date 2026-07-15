"""Render a deployable platform YAML from a pipeline extraction bundle.

The heavy lifting (importing the user's pipeline, inlining local components, pinning dependencies)
lives in :mod:`deepset_cloud_sdk._service.pipeline_extract`, which runs in the *pipeline's* Python
environment — in-process for the programmatic API, or in a subprocess (:func:`extract_via_subprocess`)
so the CLI/SDK environment never needs the pipeline's dependencies installed.

This module turns the resulting JSON bundle into the final ``config_yaml`` using ``ruamel.yaml``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from typing import Any, Optional

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
    "PipelineTransformError",
    "classify_module",
    "load_pipeline_from_file",
    "extract_from_file",
    "extract_from_pipeline",
    "extract_via_subprocess",
    "render_config_yaml",
    "transform_to_config_yaml",
    "detect_project_python",
    "unmapped_mandatory_inputs",
]

# Interpreter names to look for inside a discovered virtual environment.
_VENV_PYTHONS = ("bin/python", "bin/python3", "Scripts/python.exe")


def render_config_yaml(
    extraction: dict,
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
) -> str:
    """Render deployable YAML from an extraction bundle.

    :param extraction: A bundle produced by the extractor (see :func:`extract_from_pipeline`).
    :param inputs: Optional explicit inputs dict; overrides the inferred inputs.
    :param outputs: Optional explicit outputs dict; overrides the inferred outputs.
    :return: The platform-ready ``config_yaml`` string.
    """
    pipeline_dict = extraction["pipeline"]

    resolved_inputs = inputs if inputs is not None else extraction.get("inferred_inputs") or {}
    resolved_outputs = outputs if outputs is not None else extraction.get("inferred_outputs") or {}
    if resolved_inputs:
        pipeline_dict["inputs"] = resolved_inputs
    if resolved_outputs:
        pipeline_dict["outputs"] = resolved_outputs
    if not resolved_inputs and not resolved_outputs:
        logger.warning(
            "Could not infer pipeline inputs/outputs from open sockets. Deploying without them; "
            "the Playground query UI will be unavailable. Pass inputs/outputs explicitly to enable it."
        )

    unmapped = unmapped_mandatory_inputs(extraction.get("mandatory_inputs") or {}, resolved_inputs)
    if unmapped:
        logger.warning(
            "Deploying with mandatory pipeline inputs not mapped to any platform input: %s. "
            'The pipeline will fail at query time with "Missing mandatory input". Pass an explicit '
            "`inputs` mapping that routes a platform input (e.g. `query`) to each of these sockets.",
            ", ".join(unmapped),
        )

    if extraction.get("async_enabled"):
        pipeline_dict["async_enabled"] = True

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=2)
    buffer = StringIO()
    yaml.dump(pipeline_dict, buffer)
    config_yaml = buffer.getvalue()

    dependency_block = _build_dependency_block(extraction.get("dependencies") or [])
    if dependency_block:
        config_yaml = f"{config_yaml}\n{dependency_block}"
    return config_yaml


def unmapped_mandatory_inputs(mandatory: dict, inputs: dict) -> list[str]:
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


def transform_to_config_yaml(
    pipeline: Any,
    project_root: Path,
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
) -> str:
    """Transform an in-memory Haystack pipeline into deployable YAML (programmatic, in-process).

    Convenience wrapper that extracts from the live pipeline object and renders. Requires Haystack and
    the pipeline's dependencies to be importable in the current process; the CLI uses
    :func:`extract_via_subprocess` instead to avoid that requirement.
    """
    extraction = extract_from_pipeline(pipeline, Path(project_root))
    return render_config_yaml(extraction, inputs=inputs, outputs=outputs)


def extract_via_subprocess(
    target: Path,
    entrypoint: Optional[str] = None,
    python_executable: Optional[str] = None,
) -> dict:
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
        bundle: dict = json.load(out_file)
        return bundle


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
    """Build the commented-out ``dependencies`` YAML block that pins the Haystack version, e.g.::

        # dependencies:
        #   - haystack-ai==2.30.2

    The block is emitted commented out so it does not affect deployment by default; users can
    uncomment it to pin the listed dependencies. Returns an empty string when there is nothing to pin.
    """
    if not dependencies:
        return ""
    body = "\n".join(f"#   - {line}" for line in dependencies)
    return f"# dependencies:\n{body}\n"
