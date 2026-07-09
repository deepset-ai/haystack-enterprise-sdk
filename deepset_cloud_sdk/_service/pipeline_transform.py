"""Transform a Haystack pipeline defined in a local ``.py`` file into deployable platform YAML.

The transform:

1. Imports the target file and auto-detects the pipeline (a ``Pipeline``/``AsyncPipeline`` instance
   or a zero-argument factory returning one).
2. Dumps the pipeline to YAML and rewrites every component defined in the user's *local* project into
   the platform :class:`Code` component, inlining the class source plus every local helper symbol it
   references transitively into a single self-contained code block.
3. Extracts the *external* (site-packages) dependencies the inlined code needs and emits them as a
   commented, ready-to-uncomment ``dependencies`` block (the platform custom-dependencies feature is
   not live yet, so the block is a no-op today).
4. Optionally infers pipeline inputs/outputs from the open sockets.

The generated YAML is validated locally in two ways during development: every Code block parses via
``deepset_cloud_custom_nodes.utils.haystack_parser.extract_haystack_component`` and the whole config
round-trips through ``haystack.Pipeline.loads`` (see ``tests/unit/service/test_pipeline_transform.py``).
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import importlib.util
import inspect
import sys
from io import StringIO
from pathlib import Path
from typing import Any, Optional

import structlog
from ruamel.yaml import YAML

logger = structlog.get_logger(__name__)

# The platform component that runs arbitrary user code.
CODE_COMPONENT_TYPE = "deepset_cloud_custom_nodes.code.code_component.Code"

# Packages that are part of the runtime base image. Their imports must never be inlined and they must
# never appear in the extracted-dependencies block.
BASE_PACKAGES = {"haystack", "haystack_experimental", "deepset_cloud_custom_nodes"}

# A DocumentWriter is the tell-tale sign of an indexing pipeline, which v1 does not support.
_INDEX_MARKER_SUFFIXES = ("DocumentWriter",)


class PipelineTransformError(Exception):
    """Raised when the pipeline cannot be loaded or transformed into deployable YAML."""


# --------------------------------------------------------------------------- #
# Loading & auto-detection
# --------------------------------------------------------------------------- #
def load_pipeline_from_file(path: Path, entrypoint: Optional[str] = None) -> Any:
    """Import ``path`` and return the Haystack pipeline it defines.

    :param path: Path to a ``.py`` file that defines a pipeline. Its parent directory is added to
        ``sys.path`` so sibling packages (for example ``custom_nodes``) resolve. Importing runs the
        user's module top-level code, so its dependencies must be installed locally.
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

    module_name = path.stem
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PipelineTransformError(f"Could not load a Python module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as err:  # noqa: BLE001 - surface the user's import error verbatim
        raise PipelineTransformError(
            f"Failed to import {path}: {err.__class__.__name__}: {err}. "
            "Make sure the pipeline's dependencies are installed in this environment."
        ) from err

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
        name: obj
        for name, obj in vars(module).items()
        if not name.startswith("_") and isinstance(obj, pipeline_types)
    }
    if len(instances) == 1:
        return next(iter(instances.values()))
    if len(instances) > 1:
        raise PipelineTransformError(
            f"Multiple pipeline instances found ({', '.join(sorted(instances))}). "
            "Disambiguate with --entrypoint."
        )

    factories = _find_factories(module, pipeline_types)
    if len(factories) == 1:
        return next(iter(factories.values()))
    if len(factories) > 1:
        raise PipelineTransformError(
            f"Multiple pipeline factories found ({', '.join(sorted(factories))}). "
            "Disambiguate with --entrypoint."
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
    """Classify ``module_name`` as ``'local'``, ``'external'``, or ``'stdlib'``.

    - ``local``: resolved to a file under ``project_root`` (inline it into the Code block).
    - ``external``: a site-/dist-packages distribution (record it as a dependency).
    - ``stdlib``: part of the standard library (always available; ignore for dependencies).
    """
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
    origin_path = Path(origin).resolve()
    if _is_relative_to(origin_path, project_root.resolve()):
        return "local"
    if "site-packages" in origin_path.parts or "dist-packages" in origin_path.parts:
        return "external"
    return "stdlib"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _distribution_name(module_name: str) -> str:
    """Best-effort import-name -> distribution-name mapping (e.g. ``cv2`` -> ``opencv-python``)."""
    root = module_name.split(".")[0]
    try:
        mapping = importlib.metadata.packages_distributions()
    except Exception:  # noqa: BLE001 - fall back to the import name
        return root
    dists = mapping.get(root)
    return dists[0] if dists else root


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
        self.external_modules: set[str] = set()
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
            if kind == "external":
                self.external_modules.add(mod.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                kind = classify_module(alias.name, project_root)
                if kind == "local":
                    # `import local.module` inside a component is unusual; skip (can't inline a whole module).
                    continue
                line = f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
                self.preserved_import_lines.append(line)
                if kind == "external":
                    self.external_modules.add(alias.name.split(".")[0])


def _referenced_names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _build_code_block(component_type: str, project_root: Path) -> tuple[str, set[str]]:
    """Build a self-contained code string for ``component_type`` (a fully-qualified class name),
    transitively inlining local symbols. Returns ``(code, external_module_roots)``."""
    module_name, _, class_name = component_type.rpartition(".")

    caches: dict[str, _ModuleSymbols] = {}

    def symbols(mod: str) -> _ModuleSymbols:
        if mod not in caches:
            caches[mod] = _ModuleSymbols(mod, project_root)
        return caches[mod]

    preserved_imports: list[str] = []
    external_modules: set[str] = set()
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
        external_modules.update(idx.external_modules)
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
    return code, external_modules


# --------------------------------------------------------------------------- #
# Inputs / outputs inference
# --------------------------------------------------------------------------- #
def _infer_inputs(pipeline: Any) -> dict:
    """Heuristically map open input sockets named ``query``/``filters`` to platform inputs."""
    query: list[str] = []
    filters: list[str] = []
    try:
        open_inputs = pipeline.inputs()
    except Exception:  # noqa: BLE001
        return {}
    for comp_name, sockets in open_inputs.items():
        for socket_name in sockets:
            if socket_name == "query":
                query.append(f"{comp_name}.{socket_name}")
            elif socket_name == "filters":
                filters.append(f"{comp_name}.{socket_name}")
    result: dict[str, list[str]] = {}
    if query:
        result["query"] = query
    if filters:
        result["filters"] = filters
    return result


def _infer_outputs(pipeline: Any) -> dict:
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


# --------------------------------------------------------------------------- #
# Main transform
# --------------------------------------------------------------------------- #
def transform_to_config_yaml(
    pipeline: Any,
    project_root: Path,
    requirements: Optional[Path] = None,
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
) -> str:
    """Transform a Haystack pipeline into deployable platform YAML.

    :param pipeline: A Haystack ``Pipeline``/``AsyncPipeline`` (typically from :func:`load_pipeline_from_file`).
    :param project_root: Directory that roots the user's local modules (used to classify imports).
    :param requirements: Optional requirements file; when given, its lines override the autodetected
        dependency block.
    :param inputs: Optional explicit inputs dict (``{"query": [...], "filters": [...]}``); overrides inference.
    :param outputs: Optional explicit outputs dict (``{"answers": "...", "documents": "..."}``); overrides inference.
    :raises PipelineTransformError: If a local component cannot be turned into a valid Code block.
    :return: The platform-ready ``config_yaml`` string.
    """
    from haystack import AsyncPipeline

    project_root = Path(project_root).resolve()
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=2)
    pipeline_dict = yaml.load(pipeline.dumps())

    external_modules: set[str] = set()
    components = pipeline_dict.get("components", {}) or {}
    for comp_name, comp in components.items():
        comp_type = comp.get("type", "")
        module_name = comp_type.rpartition(".")[0]
        if classify_module(module_name, project_root) != "local":
            continue
        logger.debug("Rewriting local component to Code", component=comp_name, type=comp_type)
        code, deps = _build_code_block(comp_type, project_root)
        _validate_code_block(comp_name, code)
        external_modules.update(deps)
        original_init = comp.get("init_parameters", {}) or {}
        new_init: dict[str, Any] = {"code": code}
        if original_init:
            new_init["init_parameters"] = original_init
        comp["type"] = CODE_COMPONENT_TYPE
        comp["init_parameters"] = new_init

    _apply_inputs_outputs(pipeline_dict, pipeline, inputs, outputs)

    if isinstance(pipeline, AsyncPipeline):
        pipeline_dict["async_enabled"] = True

    buffer = StringIO()
    yaml.dump(pipeline_dict, buffer)
    config_yaml = buffer.getvalue()

    dependency_block = _build_dependency_block(external_modules, requirements)
    if dependency_block:
        config_yaml = f"{config_yaml}\n{dependency_block}"
    return config_yaml


def _validate_code_block(comp_name: str, code: str) -> None:
    """Validate a generated Code block locally, mirroring the platform's parser.

    The platform's ``Code`` component requires exactly one ``@component`` class with no *required*
    ``__init__`` parameters. We replicate that check via AST so a bad transform fails fast locally
    instead of as a ``DEPLOYMENT_FAILED`` after a wasted rollout.
    """
    tree = ast.parse(code)
    component_classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and _has_component_decorator(node)
    ]
    if not component_classes:
        raise PipelineTransformError(
            f"Component '{comp_name}': no @component class found in the generated code block."
        )
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
            required_kwonly = [
                kw.arg for kw, default in zip(args.kwonlyargs, args.kw_defaults) if default is None
            ]
            offending.extend(required_kwonly)
            if offending:
                raise PipelineTransformError(
                    f"Component '{comp_name}': custom component '{class_node.name}' has required "
                    f"__init__ parameters ({', '.join(offending)}). The platform Code component only "
                    "supports components whose __init__ parameters all have defaults. Give them defaults."
                )
            return


def _apply_inputs_outputs(
    pipeline_dict: dict,
    pipeline: Any,
    inputs: Optional[dict],
    outputs: Optional[dict],
) -> None:
    resolved_inputs = inputs if inputs is not None else _infer_inputs(pipeline)
    resolved_outputs = outputs if outputs is not None else _infer_outputs(pipeline)
    if resolved_inputs:
        pipeline_dict["inputs"] = resolved_inputs
    if resolved_outputs:
        pipeline_dict["outputs"] = resolved_outputs
    if not resolved_inputs and not resolved_outputs:
        logger.warning(
            "Could not infer pipeline inputs/outputs from open sockets. Deploying without them; "
            "the Playground query UI will be unavailable. Pass inputs/outputs explicitly to enable it."
        )


def _build_dependency_block(external_modules: set[str], requirements: Optional[Path]) -> str:
    """Build a commented, ready-to-uncomment ``dependencies`` block (a no-op until the feature ships)."""
    if requirements is not None:
        lines = [
            line.strip()
            for line in requirements.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    else:
        distributions = sorted(
            {_distribution_name(mod) for mod in external_modules if mod not in BASE_PACKAGES}
        )
        lines = []
        for dist in distributions:
            try:
                lines.append(f"{dist}=={importlib.metadata.version(dist)}")
            except importlib.metadata.PackageNotFoundError:
                logger.warning("Could not pin a version for dependency; listing it unpinned.", dependency=dist)
                lines.append(dist)
    if not lines:
        return ""
    body = "\n".join(f"#   - {line}" for line in lines)
    return (
        "# Custom dependencies (not yet supported; uncomment once the feature is live):\n"
        "# dependencies:\n"
        f"{body}\n"
    )
