"""Standalone pipeline extractor — runs in the *pipeline's* Python environment.

This module deliberately depends only on the standard library plus Haystack (imported lazily) and
PyYAML (a Haystack dependency). It must NOT import anything from ``deepset_cloud_sdk`` so it can be
executed by the user's project interpreter — which has Haystack and the pipeline's dependencies, but
not necessarily the SDK.

It loads a pipeline from a ``.py`` file, rewrites every locally-defined custom component into the
platform ``Code`` component (inlining the class source and its transitive local helpers), infers
inputs/outputs, and version-pins the external dependencies. The result is a JSON-serializable
"extraction bundle" that the SDK renders into deployable YAML.

Run as a script (used by the SDK via a subprocess):

    python pipeline_extract.py <target.py> --out <bundle.json> [--entrypoint NAME]

The bundle is written to ``--out`` (not stdout) so that arbitrary prints from the user's module can't
corrupt it. On failure the process exits non-zero with a message on stderr.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.metadata
import importlib.util
import inspect
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The platform component that runs arbitrary user code.
CODE_COMPONENT_TYPE = "deepset_cloud_custom_nodes.code.code_component.Code"

# A DocumentWriter is the tell-tale sign of an indexing pipeline, which v1 does not support.
_INDEX_MARKER_SUFFIXES = ("DocumentWriter",)

# Path components that mark an installed package (even when the venv lives inside the project dir).
_INSTALLED_PACKAGE_DIRS = {"site-packages", "dist-packages"}


class PipelineTransformError(Exception):
    """Raised when the pipeline cannot be loaded or transformed into deployable YAML."""


# --------------------------------------------------------------------------- #
# Loading & auto-detection
# --------------------------------------------------------------------------- #
def load_pipeline_from_file(path: Path, entrypoint: Optional[str] = None) -> Any:
    """Import ``path`` and return the Haystack pipeline it defines.

    :param path: Path to a ``.py`` file that defines a pipeline. Its parent directory is added to
        ``sys.path`` so sibling packages (for example ``custom_nodes``) resolve. Importing runs the
        user's module top-level code, so its dependencies must be installed in this environment.
    :param entrypoint: Name of the module-level pipeline instance or zero-arg factory to use. Required
        only when the file exposes more than one candidate.
    :raises PipelineTransformError: If the file can't be imported, no pipeline is found, the choice is
        ambiguous, or the pipeline looks like an index.
    :return: A Haystack ``Pipeline`` or ``AsyncPipeline`` instance.
    """
    from haystack import AsyncPipeline, Pipeline

    path = path.resolve()
    if not path.is_file():
        raise PipelineTransformError(f"Pipeline file not found: {path}")

    project_root = path.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # Importing the module runs its top-level code, which often constructs components (e.g. an
    # OpenAIGenerator) that read secrets via ``Secret.from_env_var`` and fail to build without them.
    # Load a project ``.env`` so those pipelines import cleanly, mirroring the deepset CLI's own .env
    # loading. Already-set environment variables always win.
    _load_project_dotenv(project_root)

    module_name = path.stem
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PipelineTransformError(f"Could not load a Python module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as err:
        raise PipelineTransformError(
            f"Failed to import {path}: missing dependency '{err.name}'. "
            f"Install it in the environment used to load the pipeline (e.g. `pip install {err.name}`), "
            "or point --python at the interpreter where your pipeline's dependencies live."
        ) from err
    except Exception as err:  # noqa: BLE001 - surface the user's import error verbatim
        raise PipelineTransformError(f"Failed to import {path}: {err.__class__.__name__}: {err}.") from err

    pipeline = _resolve_pipeline(module, entrypoint, (Pipeline, AsyncPipeline))
    _reject_index_pipeline(pipeline)
    return pipeline


def _resolve_pipeline(module: Any, entrypoint: Optional[str], pipeline_types: tuple) -> Any:
    """Find the single pipeline instance/factory in ``module``."""
    if entrypoint is not None:
        if not hasattr(module, entrypoint):
            raise PipelineTransformError(f"Entrypoint '{entrypoint}' not found in the pipeline file.")
        return _coerce_to_pipeline(getattr(module, entrypoint), entrypoint, pipeline_types)

    instances = {
        name: obj for name, obj in vars(module).items() if not name.startswith("_") and isinstance(obj, pipeline_types)
    }
    if len(instances) == 1:
        return next(iter(instances.values()))
    if len(instances) > 1:
        raise PipelineTransformError(
            f"Multiple pipeline instances found ({', '.join(sorted(instances))}). Disambiguate with --entrypoint."
        )

    factories = _find_factories(module, pipeline_types)
    if len(factories) == 1:
        return next(iter(factories.values()))
    if len(factories) > 1:
        raise PipelineTransformError(
            f"Multiple pipeline factories found ({', '.join(sorted(factories))}). Disambiguate with --entrypoint."
        )
    raise PipelineTransformError(
        "No Pipeline or AsyncPipeline instance or zero-argument factory found in the file. "
        "Expose the pipeline as a module-level variable or a zero-arg function that returns it."
    )


def _find_factories(module: Any, pipeline_types: tuple) -> dict:
    """Return zero-arg callables that produce a pipeline, keyed by name."""
    factories = {}
    for name, obj in vars(module).items():
        if name.startswith("_") or inspect.isclass(obj) or not callable(obj):
            continue
        try:
            sig = inspect.signature(obj)
        except (ValueError, TypeError):
            continue
        if any(p.default is inspect.Parameter.empty for p in sig.parameters.values()):
            continue
        try:
            result = obj()
        except Exception:  # noqa: BLE001 - a factory that needs setup is simply not a candidate
            continue
        if isinstance(result, pipeline_types):
            factories[name] = result
    return factories


def _coerce_to_pipeline(obj: Any, name: str, pipeline_types: tuple) -> Any:
    """Return ``obj`` if it is (or returns) a pipeline, else raise."""
    if isinstance(obj, pipeline_types):
        return obj
    if callable(obj):
        result = obj()
        if isinstance(result, pipeline_types):
            return result
    raise PipelineTransformError(f"Entrypoint '{name}' is not a pipeline instance or a factory returning one.")


def _load_project_dotenv(start: Path) -> None:
    """Load the nearest ``.env`` (``start`` or a parent) into ``os.environ`` without overriding set vars.

    Walks up from ``start`` and applies the first ``.env`` found. Existing environment variables take
    precedence, so anything the user exported still wins over the file. Best-effort: unreadable files and
    malformed lines are skipped silently.
    """
    start = Path(start).resolve()
    for directory in [start, *start.parents]:
        env_file = directory / ".env"
        if env_file.is_file():
            _apply_env_file(env_file)
            return


def _apply_env_file(env_file: Path) -> None:
    """Parse a minimal ``KEY=VALUE`` ``.env`` file and set any unset keys in ``os.environ``."""
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _reject_index_pipeline(pipeline: Any) -> None:
    """Raise if the pipeline looks like an indexing pipeline (v1 supports query pipelines only)."""
    for _, instance in pipeline.graph.nodes(data="instance"):
        type_name = type(instance).__name__ if instance is not None else ""
        if type_name.endswith(_INDEX_MARKER_SUFFIXES):
            raise PipelineTransformError(
                "This looks like an indexing pipeline (it contains a DocumentWriter). "
                "Index deployment is not yet supported; deploy a query pipeline instead."
            )


# --------------------------------------------------------------------------- #
# Import classification
# --------------------------------------------------------------------------- #
def classify_module(module_name: str, project_root: Path) -> str:
    """Classify ``module_name`` as ``'local'``, ``'external'``, or ``'stdlib'``."""
    if not module_name:
        return "external"
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError, ModuleNotFoundError, AttributeError):
        return "external"
    if spec is None:
        return "external"
    origin = spec.origin
    if origin is None or origin in ("built-in", "frozen"):
        return "stdlib"
    return _classify_origin(Path(origin).resolve(), project_root.resolve())


def _classify_origin(origin_path: Path, project_root: Path) -> str:
    """Classify a resolved module file path.

    ``site-packages``/``dist-packages`` is checked BEFORE ``project_root`` on purpose: a project's
    virtualenv often lives *inside* the project directory (e.g. ``<project>/.venv/.../site-packages``),
    so installed packages resolve under ``project_root`` yet must be treated as external dependencies,
    not inlined as local source.
    """
    if _INSTALLED_PACKAGE_DIRS.intersection(origin_path.parts):
        return "external"
    if _is_relative_to(origin_path, project_root):
        return "local"
    return "stdlib"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Transitive local inlining
# --------------------------------------------------------------------------- #
class _ModuleSymbols:
    """Indexes a local module's top-level definitions and imports for inlining."""

    def __init__(self, module_name: str, project_root: Path) -> None:
        self.module_name = module_name
        spec = importlib.util.find_spec(module_name)
        if spec is None or spec.origin is None:
            raise PipelineTransformError(f"Could not locate source for local module '{module_name}'.")
        self.source = Path(spec.origin).read_text(encoding="utf-8")
        self.source_lines = self.source.splitlines()
        self.tree = ast.parse(self.source)
        self.defs: dict[str, str] = {}
        self.def_nodes: dict[str, ast.AST] = {}
        # bound-name -> (local module, original name) for `from <local> import x`
        self.local_import_bindings: dict[str, tuple[str, str]] = {}
        self.preserved_import_lines: list[str] = []
        self._index(project_root)

    def _segment(self, node: ast.AST) -> str:
        """Source for a top-level node, INCLUDING decorators (which ``ast.get_source_segment`` omits)."""
        start = node.lineno  # type: ignore[attr-defined]
        decorators = getattr(node, "decorator_list", None)
        if decorators:
            start = min(d.lineno for d in decorators)
        return "\n".join(self.source_lines[start - 1 : node.end_lineno])  # type: ignore[attr-defined]

    def _index(self, project_root: Path) -> None:
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defs[node.name] = self._segment(node)
                self.def_nodes[node.name] = node
            elif isinstance(node, ast.Assign):
                segment = self._segment(node)
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.defs[target.id] = segment
                        self.def_nodes[target.id] = node
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self._index_import(node, project_root)

    def _index_import(self, node: ast.AST, project_root: Path) -> None:
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            kind = classify_module(mod, project_root)
            if kind == "local":
                for alias in node.names:
                    self.local_import_bindings[alias.asname or alias.name] = (mod, alias.name)
                return
            self.preserved_import_lines.append(self._segment(node))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                kind = classify_module(alias.name, project_root)
                if kind == "local":
                    # `import local.module` inside a component is unusual; skip (can't inline a whole module).
                    continue
                line = f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
                self.preserved_import_lines.append(line)


def _referenced_names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _build_code_block(component_type: str, project_root: Path) -> str:
    """Build a self-contained code string for ``component_type`` (a fully-qualified class name),
    transitively inlining local symbols."""
    module_name, _, class_name = component_type.rpartition(".")

    caches: dict[str, _ModuleSymbols] = {}

    def symbols(mod: str) -> _ModuleSymbols:
        if mod not in caches:
            caches[mod] = _ModuleSymbols(mod, project_root)
        return caches[mod]

    preserved_imports: list[str] = []
    emitted: list[tuple[str, str, str]] = []  # (module, symbol, source)
    seen: set[tuple[str, str]] = set()
    worklist: list[tuple[str, str]] = [(module_name, class_name)]

    while worklist:
        mod, sym = worklist.pop(0)
        if (mod, sym) in seen:
            continue
        seen.add((mod, sym))
        idx = symbols(mod)
        for line in idx.preserved_import_lines:
            if line and line not in preserved_imports:
                preserved_imports.append(line)
        if sym not in idx.defs:
            continue
        for ref in _referenced_names(idx.def_nodes[sym]):
            if ref == sym:
                continue
            if ref in idx.local_import_bindings:
                worklist.append(idx.local_import_bindings[ref])
            elif ref in idx.defs:
                worklist.append((mod, ref))
        emitted.append((mod, sym, idx.defs[sym]))

    # Emit helpers first, the component class last, de-duplicating identical segments.
    entry_segments = [seg for (m, s, seg) in emitted if m == module_name and s == class_name]
    helper_segments = [seg for (m, s, seg) in emitted if not (m == module_name and s == class_name)]
    ordered: list[str] = []
    for seg in helper_segments + entry_segments:
        if seg not in ordered:
            ordered.append(seg)

    parts: list[str] = []
    if preserved_imports:
        parts.append("\n".join(preserved_imports))
    parts.extend(ordered)
    code = "\n\n\n".join(parts) + "\n"
    return code


# --------------------------------------------------------------------------- #
# Code-block validation (mirrors the platform parser)
# --------------------------------------------------------------------------- #
def validate_code_block(comp_name: str, code: str) -> None:
    """Validate a generated Code block locally, mirroring the platform's parser.

    The platform's ``Code`` component requires exactly one ``@component`` class with no *required*
    ``__init__`` parameters. We replicate that check via AST so a bad transform fails fast locally
    instead of as a ``DEPLOYMENT_FAILED`` after a wasted rollout.
    """
    tree = ast.parse(code)
    component_classes = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and _has_component_decorator(node)
    ]
    if not component_classes:
        raise PipelineTransformError(f"Component '{comp_name}': no @component class found in the generated code block.")
    if len(component_classes) > 1:
        names = ", ".join(c.name for c in component_classes)
        raise PipelineTransformError(
            f"Component '{comp_name}': the generated code block defines multiple @component classes "
            f"({names}). A Code component must wrap exactly one. Split them into separate components."
        )
    _reject_required_init_params(comp_name, component_classes[0])


def _has_component_decorator(node: ast.ClassDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name == "component":
            return True
    return False


def _reject_required_init_params(comp_name: str, class_node: ast.ClassDef) -> None:
    """Raise if the component defines an ``__init__`` with a parameter that has no default."""
    for item in class_node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
            args = item.args
            positional = args.posonlyargs + args.args
            required = positional[1:] if positional else []  # drop `self`
            num_defaults = len(args.defaults)
            required_without_default = required[: len(required) - num_defaults]
            offending = [a.arg for a in required_without_default]
            required_kwonly = [kw.arg for kw, default in zip(args.kwonlyargs, args.kw_defaults) if default is None]
            offending.extend(required_kwonly)
            if offending:
                raise PipelineTransformError(
                    f"Component '{comp_name}': custom component '{class_node.name}' has required "
                    f"__init__ parameters ({', '.join(offending)}). The platform Code component only "
                    "supports components whose __init__ parameters all have defaults. Give them defaults."
                )
            return


# --------------------------------------------------------------------------- #
# Inputs / outputs inference
# --------------------------------------------------------------------------- #
# Open input sockets are mapped to platform inputs by name. Several socket names are
# conventional synonyms for the same platform input — most importantly ``question``, which is
# the variable Haystack's own ``PromptBuilder`` templates use for the user query. Mapping these
# synonyms is what lets the shared prototype (which only ever sends ``query``) reach them.
_QUERY_SOCKET_NAMES = frozenset({"query", "question"})
_FILTERS_SOCKET_NAMES = frozenset({"filters"})


def infer_inputs(pipeline: Any) -> dict:
    """Heuristically map open input sockets to platform ``query``/``filters`` inputs.

    Sockets named ``query`` or its synonyms (see :data:`_QUERY_SOCKET_NAMES`, e.g. ``question``)
    are routed to the platform ``query`` input; ``filters`` sockets to ``filters``.
    """
    query: list[str] = []
    filters: list[str] = []
    try:
        open_inputs = pipeline.inputs()
    except Exception:  # noqa: BLE001
        return {}
    for comp_name, sockets in open_inputs.items():
        for socket_name in sockets:
            if socket_name in _QUERY_SOCKET_NAMES:
                query.append(f"{comp_name}.{socket_name}")
            elif socket_name in _FILTERS_SOCKET_NAMES:
                filters.append(f"{comp_name}.{socket_name}")
    result: dict[str, list[str]] = {}
    if query:
        result["query"] = query
    if filters:
        result["filters"] = filters
    return result


def mandatory_inputs(pipeline: Any) -> dict:
    """Return the open input sockets that are *mandatory*, as ``{component_name: [socket_name, ...]}``.

    A mandatory socket has no default and is not fed by another component, so the deployed pipeline
    cannot run unless a platform input maps to it. Callers compare this against the resolved inputs to
    catch pipelines that would fail at query time with "Missing mandatory input '<socket>'".
    """
    try:
        open_inputs = pipeline.inputs()
    except Exception:  # noqa: BLE001
        return {}
    result: dict[str, list[str]] = {}
    for comp_name, sockets in open_inputs.items():
        required = [
            socket_name
            for socket_name in sockets
            if isinstance(sockets, dict)
            and isinstance(sockets.get(socket_name), dict)
            and sockets[socket_name].get("is_mandatory")
        ]
        if required:
            result[comp_name] = required
    return result


def infer_outputs(pipeline: Any) -> dict:
    """Heuristically map open output sockets named ``answers``/``documents`` to platform outputs."""
    result: dict[str, str] = {}
    try:
        open_outputs = pipeline.outputs()
    except Exception:  # noqa: BLE001
        return {}
    for comp_name, sockets in open_outputs.items():
        for socket_name in sockets:
            if socket_name == "answers" and "answers" not in result:
                result["answers"] = f"{comp_name}.{socket_name}"
            elif socket_name == "documents" and "documents" not in result:
                result["documents"] = f"{comp_name}.{socket_name}"
    return result


def available_inputs(pipeline: Any) -> dict:
    """Return every open input socket as ``{component_name: [socket_name, ...]}``.

    Unlike :func:`infer_inputs`, this does not filter by socket name — it exposes all open sockets so a
    caller (e.g. the CLI) can offer them for interactive mapping when inference finds nothing.
    """
    try:
        open_inputs = pipeline.inputs()
    except Exception:  # noqa: BLE001
        return {}
    return {comp_name: list(sockets) for comp_name, sockets in open_inputs.items() if sockets}


def available_outputs(pipeline: Any) -> dict:
    """Return every open output socket as ``{component_name: [socket_name, ...]}`` (see :func:`available_inputs`)."""
    try:
        open_outputs = pipeline.outputs()
    except Exception:  # noqa: BLE001
        return {}
    return {comp_name: list(sockets) for comp_name, sockets in open_outputs.items() if sockets}


def _haystack_dependency() -> list[str]:
    """Pin the ``haystack-ai`` version the pipeline is executing under, for the ``dependencies`` block.

    Runs in the pipeline's own interpreter, where ``haystack-ai`` is installed. Returns an empty list
    (so no block is rendered) if the version cannot be determined.
    """
    try:
        return [f"haystack-ai=={importlib.metadata.version('haystack-ai')}"]
    except importlib.metadata.PackageNotFoundError:
        logger.warning("Could not determine the installed haystack-ai version; omitting the dependencies block.")
        return []


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def extract_from_pipeline(pipeline: Any, project_root: Path) -> dict:
    """Build a JSON-serializable extraction bundle from a loaded pipeline.

    :param pipeline: A Haystack ``Pipeline``/``AsyncPipeline``.
    :param project_root: Directory that roots the user's local modules (used to classify imports).
    :return: A bundle with keys ``pipeline`` (dict, local components rewritten to Code),
        ``async_enabled``, ``inferred_inputs``, ``inferred_outputs``, ``available_inputs``,
        ``available_outputs``, ``mandatory_inputs``, and ``dependencies`` (the executing
        ``haystack-ai`` version).
    """
    import yaml  # type: ignore[import-untyped]
    from haystack import AsyncPipeline

    project_root = Path(project_root).resolve()
    pipeline_dict = yaml.safe_load(pipeline.dumps())

    components = pipeline_dict.get("components", {}) or {}
    for comp_name, comp in components.items():
        comp_type = comp.get("type", "")
        module_name = comp_type.rpartition(".")[0]
        if classify_module(module_name, project_root) != "local":
            continue
        logger.debug("Rewriting local component '%s' (%s) to Code", comp_name, comp_type)
        code = _build_code_block(comp_type, project_root)
        validate_code_block(comp_name, code)
        original_init = comp.get("init_parameters", {}) or {}
        new_init: dict[str, Any] = {"code": code}
        if original_init:
            new_init["init_parameters"] = original_init
        comp["type"] = CODE_COMPONENT_TYPE
        comp["init_parameters"] = new_init

    return {
        "pipeline": pipeline_dict,
        "async_enabled": isinstance(pipeline, AsyncPipeline),
        "inferred_inputs": infer_inputs(pipeline),
        "inferred_outputs": infer_outputs(pipeline),
        "available_inputs": available_inputs(pipeline),
        "available_outputs": available_outputs(pipeline),
        "mandatory_inputs": mandatory_inputs(pipeline),
        "dependencies": _haystack_dependency(),
    }


def extract_from_file(path: Path, entrypoint: Optional[str] = None) -> dict:
    """Load the pipeline from ``path`` and return its extraction bundle."""
    pipeline = load_pipeline_from_file(path, entrypoint)
    return extract_from_pipeline(pipeline, path.resolve().parent)


def main(argv: Optional[list] = None) -> int:
    """CLI entrypoint: write the extraction bundle for ``target`` to ``--out`` as JSON."""
    parser = argparse.ArgumentParser(description="Extract a deployable pipeline bundle from a .py file.")
    parser.add_argument("target", type=Path, help="Path to the pipeline .py file.")
    parser.add_argument("--out", type=Path, required=True, help="Path to write the JSON bundle to.")
    parser.add_argument("--entrypoint", default=None, help="Pipeline instance/factory name to use.")
    args = parser.parse_args(argv)

    try:
        bundle = extract_from_file(args.target, args.entrypoint)
    except PipelineTransformError as err:
        print(str(err), file=sys.stderr)
        return 2
    except Exception as err:  # noqa: BLE001 - report anything else clearly
        print(f"{err.__class__.__name__}: {err}", file=sys.stderr)
        return 1

    args.out.write_text(json.dumps(bundle), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
