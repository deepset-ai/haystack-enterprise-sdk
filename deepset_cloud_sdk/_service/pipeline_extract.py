"""Standalone pipeline extractor — runs in the *pipeline's* Python environment.

This module deliberately depends only on the standard library plus Haystack (imported lazily) and
PyYAML (a Haystack dependency). It must NOT import anything from ``deepset_cloud_sdk`` so it can be
executed by the user's project interpreter — which has Haystack and the pipeline's dependencies, but
not necessarily the SDK.

It loads a pipeline from a ``.py`` file, rewrites every locally-defined custom component into the
platform ``Code`` component (inlining the class source and its transitive local helpers), infers
inputs/outputs, and records the executing ``haystack-ai`` version (rendered as a commented-out,
currently inactive ``dependencies`` block — see the renderer). The result is a JSON-serializable
"extraction bundle" that the SDK renders into deployable YAML.

Run as a script (used by the SDK via a subprocess):

    python pipeline_extract.py <target.py> --out <bundle.json> [--entrypoint NAME]

The bundle is written to ``--out`` (not stdout) so that arbitrary prints from the user's module can't
corrupt it. On failure the process exits non-zero with a message on stderr.
"""

from __future__ import annotations

import argparse
import ast
import copy
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
        self.def_nodes: dict[str, ast.stmt] = {}
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


def _referenced_names(node: ast.AST) -> list[str]:
    # Ordered and deduplicated: a set would make the inlined-helper order (and thus the rendered
    # YAML and its config hash) vary across runs with string hash randomization.
    return list(dict.fromkeys(n.id for n in ast.walk(node) if isinstance(n, ast.Name)))


# --------------------------------------------------------------------------- #
# Folding helpers into the component class
# --------------------------------------------------------------------------- #
# The platform's Code component keeps ONLY the single ``@component`` class at runtime; any other
# module-level ``def``/``class``/constant is inaccessible and breaks the deployed pipeline. So every
# inlined helper is folded *into* the component class (functions -> @staticmethod, constants -> class
# attributes, helper classes -> nested classes) and every reference to a folded symbol is rewritten to
# the class-qualified form ``ClassName.<symbol>``.

# Nodes that open a new (non-module, non-class) name scope. A function's locals are decided by
# scanning its body WITHOUT descending into these — Python's whole-function locality rule.
_SCOPE_BOUNDARIES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _iter_same_scope(nodes: Any) -> Any:
    """Yield ``nodes`` and their descendants without crossing into a nested scope.

    A scope-boundary node (nested function/class/lambda/comprehension) is yielded but not descended
    into, so its inner bindings do not leak into the enclosing scope's analysis.
    """
    for node in nodes:
        yield node
        if isinstance(node, _SCOPE_BOUNDARIES):
            continue
        yield from _iter_same_scope(ast.iter_child_nodes(node))


def _arg_names(args: ast.arguments) -> set[str]:
    """Return every parameter name declared by ``args`` (positional, keyword-only, *args, **kwargs)."""
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _local_bindings(fn: Any) -> set[str]:
    """Return the names bound locally in a function scope (so a matching symbol ref must stay bare).

    Covers parameters plus every name assigned anywhere in the body — assignment targets,
    ``for``/``with as``/``except as`` targets, walrus targets, imports, and nested def/class names —
    minus names declared ``global``/``nonlocal`` (which do not create a local binding).
    """
    bound = _arg_names(fn.args)
    declared_global: set[str] = set()
    for node in _iter_same_scope(fn.body):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared_global.update(node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
    return bound - declared_global


def _lambda_bindings(node: ast.Lambda) -> set[str]:
    """Return names bound in a lambda scope (parameters plus any walrus targets in its body)."""
    bound = _arg_names(node.args)
    for child in _iter_same_scope([node.body]):
        if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
            bound.add(child.target.id)
    return bound


def _comprehension_bindings(node: ast.AST) -> set[str]:
    """Return names bound in a comprehension scope (``for`` targets plus walrus targets)."""
    bound: set[str] = set()
    for gen in node.generators:  # type: ignore[attr-defined]
        for name in _iter_same_scope([gen.target]):
            if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                bound.add(name.id)
    for child in ast.walk(node):
        if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
            bound.add(child.target.id)
    return bound


class _QualifyReferences(ast.NodeTransformer):
    """Rewrite bare references to folded symbols into ``ClassName.<symbol>``.

    Only references that execute at *call time* (inside a function body) are qualified — at that point
    the class exists and the class namespace is not in lexical scope. References that execute at
    *class-definition time* (constant values, method decorators/defaults/annotations, nested-class
    bases) are left bare, because ``ClassName`` is not yet bound there.
    """

    def __init__(self, entry_class: str, symbols: set[str]) -> None:
        self.entry_class = entry_class
        self.symbols = symbols
        self.func_depth = 0
        self.scopes: list[set[str]] = []

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if (
            isinstance(node.ctx, ast.Load)
            and node.id in self.symbols
            and self.func_depth > 0
            and not any(node.id in scope for scope in self.scopes)
        ):
            qualified = ast.Attribute(
                value=ast.Name(id=self.entry_class, ctx=ast.Load()),
                attr=node.id,
                ctx=ast.Load(),
            )
            return ast.copy_location(qualified, node)
        return node

    def _visit_signature(self, args: ast.arguments, returns: Optional[ast.AST]) -> None:
        # Defaults and annotations are evaluated in the ENCLOSING scope, so visit them at the current
        # depth (before the new function scope is opened by the caller).
        args.defaults = [self.visit(d) for d in args.defaults]
        args.kw_defaults = [self.visit(d) if d is not None else None for d in args.kw_defaults]
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
            if arg is not None and arg.annotation is not None:
                arg.annotation = self.visit(arg.annotation)
        if returns is not None:
            self.visit(returns)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        self._visit_signature(node.args, None)
        if node.returns is not None:
            node.returns = self.visit(node.returns)
        self.func_depth += 1
        self.scopes.append(_local_bindings(node))
        node.body = [self.visit(stmt) for stmt in node.body]
        self.scopes.pop()
        self.func_depth -= 1
        return node

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node: ast.Lambda) -> ast.AST:
        self._visit_signature(node.args, None)
        self.func_depth += 1
        self.scopes.append(_lambda_bindings(node))
        node.body = self.visit(node.body)
        self.scopes.pop()
        self.func_depth -= 1
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        # A class body is its own (non-function) scope: its names are not visible to nested method
        # bodies, so we do not push a shadowing scope and do not treat it as a function body.
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        node.bases = [self.visit(b) for b in node.bases]
        node.keywords = [self.visit(k) for k in node.keywords]
        node.body = [self.visit(stmt) for stmt in node.body]
        return node

    def _visit_comprehension(self, node: ast.AST) -> ast.AST:
        self.func_depth += 1
        self.scopes.append(_comprehension_bindings(node))
        self.generic_visit(node)
        self.scopes.pop()
        self.func_depth -= 1
        return node

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_DictComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension


def _class_scope_load_names(class_node: ast.ClassDef) -> list[str]:
    """Return names *loaded at class-definition time* in ``class_node`` (excluding function bodies).

    These are references in constant values, method decorators/defaults/annotations, and nested-class
    bases/decorators — the places where a folded-symbol reference can neither stay bare (defined later
    in the body) nor be class-qualified (the class is not yet bound). Used to reject un-transformable
    inputs early.
    """
    names: list[str] = []

    def collect(node: ast.AST) -> None:
        for child in _iter_same_scope([node]):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                names.append(child.id)

    for stmt in class_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in stmt.decorator_list:
                collect(dec)
            for default in (*stmt.args.defaults, *stmt.args.kw_defaults):
                if default is not None:
                    collect(default)
            for arg in (
                *stmt.args.posonlyargs,
                *stmt.args.args,
                *stmt.args.kwonlyargs,
                stmt.args.vararg,
                stmt.args.kwarg,
            ):
                if arg is not None and arg.annotation is not None:
                    collect(arg.annotation)
            if stmt.returns is not None:
                collect(stmt.returns)
        elif isinstance(stmt, ast.ClassDef):
            for dec in stmt.decorator_list:
                collect(dec)
            for base in stmt.bases:
                collect(base)
            for kw in stmt.keywords:
                collect(kw.value)
            names.extend(_class_scope_load_names(stmt))
        else:
            collect(stmt)
    return names


def _build_code_block(component_type: str, project_root: Path) -> str:
    """Build a self-contained code string for ``component_type`` (a fully-qualified class name).

    Transitively collects the component's local helpers and folds them INTO the component class so the
    generated code has nothing at module level except imports and the single ``@component`` class (the
    only shape the platform Code component runs correctly).
    """
    module_name, _, class_name = component_type.rpartition(".")

    caches: dict[str, _ModuleSymbols] = {}

    def module_symbols(mod: str) -> _ModuleSymbols:
        if mod not in caches:
            caches[mod] = _ModuleSymbols(mod, project_root)
        return caches[mod]

    preserved_imports: list[str] = []
    # BFS closure over local symbols, keeping AST nodes (not source text) so we can restructure.
    helpers: list[tuple[str, ast.stmt]] = []  # (symbol name, AST node), in discovery order
    entry_node: Optional[ast.ClassDef] = None
    by_name: dict[str, tuple[str, str]] = {}  # symbol name -> (module, symbol) that first claimed it
    seen: set[tuple[str, str]] = set()
    worklist: list[tuple[str, str]] = [(module_name, class_name)]

    while worklist:
        mod, sym = worklist.pop(0)
        if (mod, sym) in seen:
            continue
        seen.add((mod, sym))
        idx = module_symbols(mod)
        for line in idx.preserved_import_lines:
            if line and line not in preserved_imports:
                preserved_imports.append(line)
        if sym not in idx.defs:
            continue
        node = copy.deepcopy(idx.def_nodes[sym])
        for ref in _referenced_names(idx.def_nodes[sym]):
            if ref == sym:
                continue
            if ref in idx.local_import_bindings:
                worklist.append(idx.local_import_bindings[ref])
            elif ref in idx.defs:
                worklist.append((mod, ref))
        if mod == module_name and sym == class_name and isinstance(node, ast.ClassDef):
            entry_node = node
            continue
        if sym in by_name and by_name[sym] != (mod, sym):
            raise PipelineTransformError(
                f"Component '{class_name}': two different local helpers named '{sym}' were pulled in "
                f"(from '{by_name[sym][0]}' and '{mod}'). Rename one so they can be folded into the "
                "component class without colliding."
            )
        by_name[sym] = (mod, sym)
        helpers.append((sym, node))

    if entry_node is None:
        raise PipelineTransformError(
            f"Could not find the component class '{class_name}' to inline for type '{component_type}'."
        )

    code = _fold_helpers_into_class(class_name, entry_node, helpers)

    parts: list[str] = []
    if preserved_imports:
        parts.append("\n".join(preserved_imports))
    parts.append(code)
    return "\n\n\n".join(parts) + "\n"


def _fold_helpers_into_class(
    class_name: str,
    entry_node: ast.ClassDef,
    helpers: list[tuple[str, ast.stmt]],
) -> str:
    """Fold ``helpers`` into ``entry_node`` and return the rendered class source.

    Functions become ``@staticmethod`` methods, constants become class attributes, helper classes
    become nested classes, and every reference to a folded symbol (including inside the component's own
    methods, and helper self/mutual references) is rewritten to ``ClassName.<symbol>``.
    """
    symbol_names = {name for name, _ in helpers}
    existing_members = _class_member_names(entry_node)

    constants: list[ast.stmt] = []
    nested_classes: list[ast.stmt] = []
    static_methods: list[ast.stmt] = []

    for name, node in helpers:
        if name in existing_members:
            raise PipelineTransformError(
                f"Component '{class_name}': local helper '{name}' collides with a member the component "
                "class already defines. Rename the helper."
            )
        if isinstance(node, ast.ClassDef):
            if _has_component_decorator(node):
                raise PipelineTransformError(
                    f"Component '{class_name}': inlining would define multiple @component classes "
                    f"('{class_name}' and '{node.name}'). A Code component must wrap exactly one. "
                    "Split them into separate components."
                )
            nested_classes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _reject_global_symbol_rebinding(class_name, node, symbol_names)
            node.decorator_list = [ast.Name(id="staticmethod", ctx=ast.Load()), *node.decorator_list]
            static_methods.append(node)
        else:  # Assign / AnnAssign constant
            constants.append(node)

    new_class = copy.deepcopy(entry_node)
    # Keep a leading class docstring first so it stays a docstring rather than a stray string expr.
    original_body = new_class.body
    docstring: list[ast.stmt] = []
    if (
        original_body
        and isinstance(original_body[0], ast.Expr)
        and isinstance(original_body[0].value, ast.Constant)
        and isinstance(original_body[0].value.value, str)
    ):
        docstring, original_body = original_body[:1], original_body[1:]
    new_class.body = [*docstring, *constants, *nested_classes, *static_methods, *original_body]

    # Names that resolve at class-definition time cannot be class-qualified; reject references there to
    # folded functions/classes (a constant value or decorator that calls a helper).
    func_class_symbols = {
        name for name, node in helpers if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for loaded in _class_scope_load_names(new_class):
        if loaded in func_class_symbols:
            raise PipelineTransformError(
                f"Component '{class_name}': local helper '{loaded}' is referenced at class-definition "
                "time (e.g. in a decorator, default value, or class-level constant). Such helpers cannot "
                "be folded into the component class. Move the reference inside a method."
            )

    _QualifyReferences(class_name, symbol_names).visit(new_class)
    module = ast.Module(body=[new_class], type_ignores=[])
    ast.fix_missing_locations(module)
    return ast.unparse(module)


def _class_member_names(class_node: ast.ClassDef) -> set[str]:
    """Return the top-level member names a class defines (methods, nested classes, attributes)."""
    names: set[str] = set()
    for stmt in class_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            names.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
    return names


def _reject_global_symbol_rebinding(class_name: str, fn: Any, symbol_names: set[str]) -> None:
    """Raise if ``fn`` declares ``global``/``nonlocal`` on a folded symbol (unrepresentable as an attr)."""
    for node in _iter_same_scope(fn.body):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            offending = symbol_names.intersection(node.names)
            if offending:
                raise PipelineTransformError(
                    f"Component '{class_name}': helper '{fn.name}' uses global/nonlocal on "
                    f"{', '.join(sorted(offending))}, which cannot be folded into the component class."
                )


# --------------------------------------------------------------------------- #
# Code-block validation (mirrors the platform parser)
# --------------------------------------------------------------------------- #
def validate_code_block(comp_name: str, code: str) -> None:
    """Validate a generated Code block locally, mirroring the platform's parser.

    The platform's ``Code`` component requires exactly one ``@component`` class with no *required*
    ``__init__`` parameters, and keeps ONLY that class at runtime — so nothing else may live at module
    level. We replicate those checks via AST so a bad transform fails fast locally instead of as a
    ``DEPLOYMENT_FAILED`` after a wasted rollout.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as err:
        raise PipelineTransformError(
            f"Component '{comp_name}': the generated code block is not valid Python ({err})."
        ) from err
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
    _reject_module_level_definitions(comp_name, tree, component_classes[0])
    _reject_required_init_params(comp_name, component_classes[0])


def _reject_module_level_definitions(comp_name: str, tree: ast.Module, component_class: ast.ClassDef) -> None:
    """Raise if the code has any module-level definition besides the single ``@component`` class.

    The platform Code component keeps only that class at runtime; a stray module-level
    ``def``/``class``/constant would be inaccessible and break the deployed pipeline. Imports and a
    module docstring are allowed.
    """
    for node in tree.body:
        if node is component_class:
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # module docstring
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            offender = node.name
        elif isinstance(node, ast.Assign):
            offender = ", ".join(t.id for t in node.targets if isinstance(t, ast.Name)) or "<assignment>"
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            offender = node.target.id
        else:
            offender = type(node).__name__
        raise PipelineTransformError(
            f"Component '{comp_name}': the generated code has a module-level definition '{offender}' "
            "outside the @component class. The platform Code component keeps only the single @component "
            "class, so helpers must be folded into it."
        )


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
# Name-based inference: open sockets are mapped to platform I/O keys by name. Several socket names
# are conventional synonyms for the same platform key — most importantly ``question``, the variable Haystack's own ``PromptBuilder``
# templates use for the user query. Mapping these synonyms is what lets the shared prototype (which
# only ever sends ``query``) reach them. Ordered so rendering is deterministic.
_INPUT_SOCKET_KEYS = {
    "query": "query",
    "question": "query",
    "filters": "filters",
    "files": "files",
    "sources": "files",
    "messages": "messages",
}
_OUTPUT_SOCKET_KEYS = {
    "answers": "answers",
    "documents": "documents",
    "replies": "messages",
    "messages": "messages",
}
# Canonical platform key ordering, reused by the CLI's interactive mapping. Kept here (not in
# io_spec) because this module runs standalone in the pipeline's own interpreter; a test asserts it
# stays in sync with the SDK-side PLATFORM_SERVING_SPEC.
STANDARD_INPUT_KEYS = ("query", "filters", "files", "messages")
STANDARD_OUTPUT_KEYS = ("answers", "documents", "messages")


def _open_sockets(pipeline: Any, direction: str) -> dict:
    """Return the pipeline's open ``inputs()``/``outputs()``, or ``{}`` when inspection fails.

    :param direction: ``"inputs"`` or ``"outputs"``.
    """
    try:
        return getattr(pipeline, direction)() or {}
    except Exception:  # noqa: BLE001
        return {}


def infer_inputs(pipeline: Any) -> dict:
    """Heuristically map open input sockets to platform inputs by socket name.

    Sockets named ``query`` or its synonym ``question`` route to the platform ``query`` input;
    ``filters`` to ``filters``; ``files``/``sources`` to ``files``; ``messages`` to ``messages``
    (see :data:`_INPUT_SOCKET_KEYS`).
    """
    result: dict[str, list[str]] = {}
    for key in STANDARD_INPUT_KEYS:
        for comp_name, sockets in _open_sockets(pipeline, "inputs").items():
            for socket_name in sockets:
                if _INPUT_SOCKET_KEYS.get(socket_name) == key:
                    result.setdefault(key, []).append(f"{comp_name}.{socket_name}")
    return result


def mandatory_inputs(pipeline: Any) -> dict:
    """Return the open input sockets that are *mandatory*, as ``{component_name: [socket_name, ...]}``.

    A mandatory socket has no default and is not fed by another component, so the deployed pipeline
    cannot run unless a platform input maps to it. Callers compare this against the resolved inputs to
    catch pipelines that would fail at query time with "Missing mandatory input '<socket>'".
    """
    result: dict[str, list[str]] = {}
    for comp_name, sockets in _open_sockets(pipeline, "inputs").items():
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
    """Heuristically map open output sockets to platform outputs by socket name.

    Sockets named ``answers``/``documents`` map to the platform output of the same name; ``replies``
    or ``messages`` map to ``messages`` (see :data:`_OUTPUT_SOCKET_KEYS`). First match per key wins.
    """
    result: dict[str, str] = {}
    for comp_name, sockets in _open_sockets(pipeline, "outputs").items():
        for socket_name in sockets:
            key = _OUTPUT_SOCKET_KEYS.get(socket_name)
            if key is not None and key not in result:
                result[key] = f"{comp_name}.{socket_name}"
    return result


def _type_display(type_: Any) -> Optional[str]:
    """Best-effort human-readable name for a socket type, e.g. ``List[GeneratedAnswer]``.

    Runs in the pipeline's interpreter where the type objects are live. Never raises — type display
    is cosmetic, so any failure just drops the annotation.
    """
    try:
        try:
            from haystack.utils.type_serialization import serialize_type

            name = serialize_type(type_)
        except Exception:  # noqa: BLE001 - serialize_type may not exist or may choke on exotic generics
            name = getattr(type_, "__name__", None) or str(type_)
        # Shorten dotted module paths everywhere they appear (also inside generics):
        # "typing.List[haystack.dataclasses.answer.GeneratedAnswer]" -> "List[GeneratedAnswer]".
        import re

        return re.sub(
            r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", lambda m: m.group(0).rsplit(".", 1)[-1], name
        )
    except Exception:  # noqa: BLE001
        return None


def _available_sockets(pipeline: Any, direction: str) -> dict:
    """Return every open socket in ``direction`` with display metadata.

    Shape: ``{component_name: {socket_name: {"type": "str"|None, "is_mandatory": bool}}}``. Unlike
    inference, this does not filter by socket name — it exposes all open sockets so a caller (e.g.
    the CLI) can offer them for interactive mapping. Types are stringified here because the bundle
    crosses a subprocess JSON boundary.
    """
    result: dict[str, dict] = {}
    for comp_name, sockets in _open_sockets(pipeline, direction).items():
        if not sockets:
            continue
        entry: dict[str, dict] = {}
        for socket_name in sockets:
            info = sockets.get(socket_name) if isinstance(sockets, dict) else None
            info = info if isinstance(info, dict) else {}
            entry[socket_name] = {
                "type": _type_display(info["type"]) if "type" in info else None,
                "is_mandatory": bool(info.get("is_mandatory")),
            }
        result[comp_name] = entry
    return result


def available_inputs(pipeline: Any) -> dict:
    """Return every open input socket with display metadata (see :func:`_available_sockets`)."""
    return _available_sockets(pipeline, "inputs")


def available_outputs(pipeline: Any) -> dict:
    """Return every open output socket with display metadata (see :func:`_available_sockets`)."""
    return _available_sockets(pipeline, "outputs")


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
