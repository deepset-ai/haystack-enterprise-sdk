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
]

# Interpreter names to look for inside a discovered virtual environment.
_VENV_PYTHONS = ("bin/python", "bin/python3", "Scripts/python.exe")


def render_config_yaml(
    extraction: dict,
    requirements: Optional[Path] = None,
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
) -> str:
    """Render deployable YAML from an extraction bundle.

    :param extraction: A bundle produced by the extractor (see :func:`extract_from_pipeline`).
    :param requirements: Optional requirements file (or ``pyproject.toml``) whose dependencies
        override the extracted ones.
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

    if extraction.get("async_enabled"):
        pipeline_dict["async_enabled"] = True

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=2)
    buffer = StringIO()
    yaml.dump(pipeline_dict, buffer)
    config_yaml = buffer.getvalue()

    dependency_block = _build_dependency_block(extraction.get("dependencies") or [], requirements)
    if dependency_block:
        config_yaml = f"{config_yaml}\n{dependency_block}"
    return config_yaml


def transform_to_config_yaml(
    pipeline: Any,
    project_root: Path,
    requirements: Optional[Path] = None,
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
) -> str:
    """Transform an in-memory Haystack pipeline into deployable YAML (programmatic, in-process).

    Convenience wrapper that extracts from the live pipeline object and renders. Requires Haystack and
    the pipeline's dependencies to be importable in the current process; the CLI uses
    :func:`extract_via_subprocess` instead to avoid that requirement.
    """
    extraction = extract_from_pipeline(pipeline, Path(project_root))
    return render_config_yaml(extraction, requirements=requirements, inputs=inputs, outputs=outputs)


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
            raise PipelineTransformError(
                f"Failed to load the pipeline using interpreter '{python}':\n{message}"
            )
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


def _build_dependency_block(dependencies: list, requirements: Optional[Path]) -> str:
    """Build a commented, ready-to-uncomment ``dependencies`` block (a no-op until the feature ships)."""
    if requirements is not None:
        lines = _read_requirements(requirements)
    else:
        lines = list(dependencies)
    if not lines:
        return ""
    body = "\n".join(f"#   - {line}" for line in lines)
    return (
        "# Custom dependencies (not yet supported; uncomment once the feature is live):\n"
        "# dependencies:\n"
        f"{body}\n"
    )


def _read_requirements(requirements: Path) -> list:
    """Read dependency specifiers from a requirements file or a ``pyproject.toml``.

    A ``pyproject.toml`` (matched by file name) is parsed for its PEP 621 ``[project].dependencies``;
    any other file is treated as a plain requirements list (one specifier per line, ``#`` comments and
    blank lines ignored).
    """
    if requirements.name == "pyproject.toml":
        return _read_pyproject_dependencies(requirements)
    return [
        line.strip()
        for line in requirements.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _read_pyproject_dependencies(pyproject: Path) -> list:
    """Extract ``[project].dependencies`` (PEP 621) from a ``pyproject.toml``.

    :raises PipelineTransformError: If the file cannot be parsed as TOML.
    """
    try:
        import tomllib  # noqa: PLC0415  (stdlib from 3.11)
    except ModuleNotFoundError:  # Python 3.9/3.10
        import tomli as tomllib  # noqa: PLC0415

    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as err:
        raise PipelineTransformError(f"Failed to parse '{pyproject}' as TOML: {err}") from err

    dependencies = data.get("project", {}).get("dependencies", [])
    return [str(dep).strip() for dep in dependencies if str(dep).strip()]
