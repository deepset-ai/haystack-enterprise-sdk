"""Build to code: pack a local project into a code bundle, a deterministic and content-addressed tar.gz.

A code build ships the project as-is instead of converting it to YAML. The revision's ``config_yaml``
then carries only the settings (``inputs``, ``outputs``, ``dependencies``, ``pipeline_output_type``)
and the platform runs the entrypoint out of the bundle. "Bundle" alone already means
:class:`~haystack_enterprise_sdk._service.pipeline_transform.ExtractionBundle`, hence ``code_bundle``.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from haystack_enterprise_sdk._service import deployment_service, pipeline_transform
from haystack_enterprise_sdk._service.pipeline_extract import CODE_KINDS
from haystack_enterprise_sdk._service.pipeline_transform import PipelineTransformError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

logger = structlog.get_logger(__name__)

#: Largest archive the CLI uploads, before the platform's own limit gets a say.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024

#: gitignore-style patterns, one per line, applied on top of git's own ignores.
IGNORE_FILE = ".deepsetignore"

#: Optional project file in the project root: ``entrypoint``, ``kind`` and ``dependencies``.
PROJECT_FILE = "deepset.yaml"

# Never shipped, whatever git or the ignore file says: environments, VCS data, bytecode.
_ALWAYS_EXCLUDED_DIRS = frozenset({".git", ".venv", "venv", "__pycache__"})
# Also skipped when walking a project that is not a git repository (git would ignore them via .gitignore).
_WALK_EXCLUDED_DIRS = _ALWAYS_EXCLUDED_DIRS | {"node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox"}

_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?")


class CodeBundleError(PipelineTransformError):
    """Raised when the project cannot be packed into a code bundle."""


@dataclass(frozen=True)
class CodeBundle:
    """A packed code build: the archive plus everything the revision push needs."""

    archive: bytes
    sha256: str
    files: List[str]
    config_yaml: str
    entrypoint: str  # "path/relative/to/root.py:attr"
    kind: str
    input_schema: Optional[dict] = None
    graph: Optional[dict] = None

    def request_field(self) -> Dict[str, Any]:
        """The ``code_bundle`` field of the push-revision request, without the fields that are unset."""
        fields = {
            "sha256": self.sha256,
            "entrypoint": self.entrypoint,
            "kind": self.kind,
            "input_schema": self.input_schema,
            "graph": self.graph,
        }
        return {key: value for key, value in fields.items() if value is not None}


@dataclass(frozen=True)
class ProjectFile:
    """The settings of a project's ``deepset.yaml``; ``None`` means not declared."""

    entrypoint: Optional[str] = None  # "path/relative/to/root.py:attr"
    kind: Optional[str] = None
    dependencies: Optional[List[str]] = None


def load_project_file(root: Path) -> ProjectFile:
    """Read ``deepset.yaml`` from ``root``; an empty :class:`ProjectFile` when there is none.

    :raises CodeBundleError: If the file is not valid YAML, has unknown keys or a value of the wrong shape.
    """
    path = Path(root) / PROJECT_FILE
    if not path.is_file():
        return ProjectFile()
    try:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
    except (OSError, YAMLError) as err:
        raise CodeBundleError(f"Could not read '{path}': {err}") from err
    allowed = ", ".join(field.name for field in dataclasses.fields(ProjectFile))
    if not isinstance(data, dict):
        raise CodeBundleError(f"'{path}' must be a mapping with any of: {allowed}.")
    unknown = sorted(set(data) - {field.name for field in dataclasses.fields(ProjectFile)})
    if unknown:
        raise CodeBundleError(f"Unknown key(s) in '{path}': {', '.join(unknown)}. Allowed keys: {allowed}.")
    entrypoint, kind, dependencies = data.get("entrypoint"), data.get("kind"), data.get("dependencies")
    if entrypoint is not None and not (isinstance(entrypoint, str) and ":" in entrypoint):
        raise CodeBundleError(f"'entrypoint' in '{path}' must be 'path.py:attr', got {entrypoint!r}.")
    if kind is not None and kind not in CODE_KINDS:
        raise CodeBundleError(f"'kind' in '{path}' must be one of {', '.join(CODE_KINDS)}, got {kind!r}.")
    if dependencies is not None and not (
        isinstance(dependencies, list) and all(isinstance(dep, str) for dep in dependencies)
    ):
        raise CodeBundleError(f"'dependencies' in '{path}' must be a list of requirement strings.")
    return ProjectFile(entrypoint=entrypoint, kind=kind, dependencies=dependencies)


def has_project_file(target: Path) -> bool:
    """Whether the project ``target`` (a directory or a file in it) belongs to has a ``deepset.yaml``."""
    target = Path(target).resolve()
    return (find_project_root(target if target.is_dir() else target.parent) / PROJECT_FILE).is_file()


def build_code_bundle(
    target: Path,
    *,
    entrypoint: Optional[str] = None,
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
    settings: Optional[pipeline_transform.PipelineSettings] = None,
    io_resolver: Optional[pipeline_transform.IoResolver] = None,
    python_executable: Optional[str] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> CodeBundle:
    """Describe the entrypoint, pack the project and render the settings-only ``config_yaml``. No API calls.

    The project's ``deepset.yaml`` (see :func:`load_project_file`) fills in what the arguments leave
    open: its ``entrypoint`` (relative to the project root), its ``kind``, and its ``dependencies``,
    which replace the ones read from ``pyproject.toml``/``requirements.txt``. Declared ``settings`` still
    win over all of them. A ``function`` entrypoint always gets the fixed ``payload``/``result`` mapping.

    :param target: A project directory or the entrypoint's ``.py`` file.
    :param entrypoint: ``path.py:attr`` (relative to a directory target, or to the file's directory),
        or a bare attribute name for a file target. Auto-detected in a file target when omitted.
    :param inputs: Explicit platform inputs (override inference).
    :param outputs: Explicit platform outputs (override inference).
    :param settings: The declared top-level settings; declared ``dependencies`` win over the project's.
    :param io_resolver: Final say on the inputs/outputs, see :func:`pipeline_transform.resolve_io`.
    :param python_executable: Interpreter to import the entrypoint with.
    :param max_bytes: Size cap for the archive.
    :raises CodeBundleError: If the entrypoint is missing or outside the project, or the archive is too large.
    :raises PipelineTransformError: If the entrypoint cannot be imported.
    :return: The packed code build.
    """
    target = Path(target).resolve()
    base = target if target.is_dir() else target.parent
    root = find_project_root(base)
    project = load_project_file(root)
    # A file target names the entrypoint file itself, so deepset.yaml's entrypoint only applies to it
    # when it points at that same file.
    if not entrypoint and project.entrypoint:
        declared_file = (root / project.entrypoint.rpartition(":")[0]).resolve()
        if target.is_dir() or declared_file == target:
            entrypoint, base = project.entrypoint, root
    if entrypoint and ":" in entrypoint:
        file_part, _, attr = entrypoint.rpartition(":")
        entry_file, entry_attr = (base / file_part).resolve(), attr or None
    elif target.is_dir():
        raise CodeBundleError(
            f"'{target}' is a directory: pass --entrypoint path.py:attr or set 'entrypoint:' in {PROJECT_FILE}."
        )
    else:
        entry_file, entry_attr = target, entrypoint

    try:
        entry_rel = entry_file.relative_to(root).as_posix()
    except ValueError:
        raise CodeBundleError(f"Entrypoint '{entry_file}' is outside the project root '{root}'.") from None

    files = list_project_files(root)
    if entry_rel not in files:
        raise CodeBundleError(f"Entrypoint '{entry_rel}' is excluded from the code bundle (git or {IGNORE_FILE}).")

    extraction, code = pipeline_transform.extract_code_via_subprocess(
        entry_file, entry_attr, python_executable, project_root=root, kind=project.kind
    )
    dependencies = project.dependencies if project.dependencies is not None else project_dependencies(root)
    if dependencies is not None:
        extraction = dataclasses.replace(extraction, dependencies=dependencies)
    if code.kind == "function":
        # The runtime's contract for a function: one ``payload`` dict in, one ``result`` out, no output type.
        resolved_inputs, resolved_outputs = extraction.inferred_inputs, extraction.inferred_outputs
        settings = dataclasses.replace(settings or pipeline_transform.PipelineSettings(), pipeline_output_type=None)
    else:
        resolved_inputs, resolved_outputs = pipeline_transform.resolve_io(extraction, inputs, outputs, io_resolver)
    config_yaml = pipeline_transform.render_config_yaml(
        extraction, inputs=resolved_inputs, outputs=resolved_outputs, settings=settings
    )

    archive = pack(root, files)
    if len(archive) > max_bytes:
        raise CodeBundleError(_too_large_message(root, files, len(archive), max_bytes))
    return CodeBundle(
        archive=archive,
        sha256=hashlib.sha256(archive).hexdigest(),
        files=files,
        config_yaml=config_yaml,
        entrypoint=f"{entry_rel}:{code.attr}",
        kind=code.kind,
        input_schema=code.input_schema,
        graph=code.graph,
    )


def find_project_root(start: Path) -> Path:
    """Return the nearest directory at or above ``start`` holding a ``pyproject.toml`` or ``.git``, else ``start``."""
    start = Path(start).resolve()
    for directory in [start, *start.parents]:
        if (directory / "pyproject.toml").is_file() or (directory / ".git").exists():
            return directory
    return start


def list_project_files(root: Path) -> List[str]:
    """List the files to ship, as sorted POSIX paths relative to ``root``.

    git's view (tracked plus untracked-but-not-ignored) when ``root`` is in a repository, else a walk
    with default ignores; then the ``.deepsetignore`` patterns and the always-excluded secrets and
    environments are dropped.
    """
    listed = deployment_service._git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed is not None:
        candidates = [path for path in listed.split("\0") if path]
    else:
        candidates = []
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in _WALK_EXCLUDED_DIRS]
            rel_dir = Path(directory).relative_to(root)
            candidates += [(rel_dir / name).as_posix() for name in filenames]

    patterns = _ignore_patterns(root)
    return sorted(
        {
            path
            for path in candidates
            if (root / path).is_file() and not _always_excluded(path) and not _ignored(path, patterns)
        }
    )


def _always_excluded(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return any(part in _ALWAYS_EXCLUDED_DIRS for part in parts) or name.startswith(".env") or name.endswith(".pem")


def _ignore_patterns(root: Path) -> List[str]:
    ignore_file = root / IGNORE_FILE
    if not ignore_file.is_file():
        return []
    lines = (line.strip() for line in ignore_file.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _ignored(path: str, patterns: List[str]) -> bool:
    """gitignore-ish matching: a pattern matches the path, a leading directory of it, or any one name in it.

    ponytail: no ``!`` negation and no ``**``; use a real gitignore matcher if people need them.
    """
    parts = path.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for pattern in patterns:
        pattern = pattern.rstrip("/")
        if pattern.startswith("/"):
            if any(fnmatch.fnmatchcase(prefix, pattern[1:]) for prefix in prefixes):
                return True
        elif any(fnmatch.fnmatchcase(prefix, pattern) for prefix in prefixes) or any(
            fnmatch.fnmatchcase(part, pattern) for part in parts
        ):
            return True
    return False


def pack(root: Path, files: List[str]) -> bytes:
    """Pack ``files`` (relative to ``root``) into a tar.gz whose bytes depend only on names, content and modes.

    Sorted entries, mtime 0, owner 0/0 with empty names, modes normalized to 0644/0755, and a gzip
    header without mtime or file name: the same tree always gives the same sha256.
    """
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for rel in sorted(files):
                path = root / rel
                data = path.read_bytes()
                info = tarfile.TarInfo(rel)
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def _too_large_message(root: Path, files: List[str], size: int, max_bytes: int) -> str:
    largest = sorted(files, key=lambda rel: (root / rel).stat().st_size, reverse=True)[:10]
    lines = "\n".join(f"  {(root / rel).stat().st_size / 1e6:8.2f} MB  {rel}" for rel in largest)
    return (
        f"The code bundle is {size / 1e6:.1f} MB, over the {max_bytes / 1e6:.0f} MB limit. Largest files:\n{lines}\n"
        f"Exclude what the service does not need in {IGNORE_FILE} (or .gitignore)."
    )


def project_dependencies(root: Path) -> Optional[List[str]]:
    """The project's dependencies for the revision's ``dependencies`` block.

    ``[project.dependencies]`` from ``pyproject.toml``, each pinned to its ``uv.lock`` version when there
    is one; else the lines of ``requirements.txt``; else ``None`` (the caller keeps the haystack pin).

    :raises CodeBundleError: If ``pyproject.toml`` or ``uv.lock`` is not valid TOML.
    """
    pyproject = root / "pyproject.toml"
    declared = _load_toml(pyproject).get("project", {}).get("dependencies") if pyproject.is_file() else None
    if declared:
        lock = root / "uv.lock"
        packages = _load_toml(lock).get("package", []) if lock.is_file() else []
        versions = {_normalize(pkg["name"]): pkg["version"] for pkg in packages if "name" in pkg and "version" in pkg}
        return [_pin(requirement, versions) for requirement in declared]

    requirements = root / "requirements.txt"
    if requirements.is_file():
        lines = (line.split(" #")[0].strip() for line in requirements.read_text(encoding="utf-8").splitlines())
        return [line for line in lines if line and not line.startswith(("#", "-"))] or None
    return None


def _load_toml(path: Path) -> dict:
    try:
        data: dict = tomllib.loads(path.read_text(encoding="utf-8"))
        return data
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise CodeBundleError(f"Could not read '{path}': {err}") from err


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pin(requirement: str, versions: Dict[str, str]) -> str:
    """Pin ``requirement`` to its locked version, keeping extras and markers. URL requirements stay as they are."""
    spec, sep, marker = requirement.partition(";")
    match = _REQUIREMENT_NAME.match(spec)
    if match is None or "@" in spec or _normalize(match.group(1)) not in versions:
        return requirement.strip()
    pinned = f"{match.group(1)}{match.group(2) or ''}=={versions[_normalize(match.group(1))]}"
    return f"{pinned}; {marker.strip()}" if sep else pinned
