"""Tests for packing a project into a code bundle."""

import hashlib
import io
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from haystack_enterprise_sdk._service.code_bundle import (
    CodeBundleError,
    ProjectFile,
    build_code_bundle,
    find_project_root,
    has_project_file,
    list_project_files,
    load_project_file,
    pack,
    project_dependencies,
)
from haystack_enterprise_sdk._service.pipeline_transform import PipelineSettings, PipelineTransformError


def _tree(root: Path, files: dict) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")
    return root


_PROJECT = {
    "pyproject.toml": '[project]\nname = "demo"\nversion = "0"\ndependencies = ["requests>=2", "rich[jupyter]; python_version >= \'3.9\'"]\n',
    "uv.lock": '[[package]]\nname = "requests"\nversion = "2.32.3"\n\n[[package]]\nname = "rich"\nversion = "13.9.4"\n',
    "app.py": "x = 1\n",
    "pkg/__init__.py": "",
    "pkg/prompt.txt": "Answer {{query}}",
    ".env": "SECRET=1",
    ".env.local": "SECRET=1",
    "key.pem": "-----",
    ".venv/lib/x.py": "",
    "pkg/__pycache__/x.pyc": "",
    "data/big.bin": "x",
    ".deepsetignore": "# comment\ndata/\n*.log\n",
    "run.log": "noise",
}


class TestFiles:
    def test_walk_drops_secrets_envs_and_ignored(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, _PROJECT)
        assert list_project_files(root) == [
            ".deepsetignore",
            "app.py",
            "pkg/__init__.py",
            "pkg/prompt.txt",
            "pyproject.toml",
            "uv.lock",
        ]

    def test_git_listing_respects_gitignore(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {**_PROJECT, ".gitignore": "pkg/prompt.txt\n"})
        subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S603, S607
        files = list_project_files(root)
        assert "pkg/prompt.txt" not in files
        assert ".gitignore" in files and "app.py" in files
        assert not any(f.startswith((".env", ".venv", "data/")) or f.endswith(".pem") for f in files)

    def test_project_root_is_nearest_pyproject(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"pyproject.toml": "", "src/svc/app.py": ""})
        assert find_project_root(root / "src" / "svc") == root
        assert find_project_root(tmp_path / "src") == root


class TestPack:
    def test_same_tree_same_sha(self, tmp_path: Path) -> None:
        one = _tree(tmp_path / "one", _PROJECT)
        two = _tree(tmp_path / "two", _PROJECT)
        (two / "app.py").touch()  # a different mtime must not matter
        files = list_project_files(one)
        assert hashlib.sha256(pack(one, files)).digest() == hashlib.sha256(pack(two, files)).digest()

    def test_entries_are_normalized(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"b.py": "", "a.py": ""})
        with tarfile.open(fileobj=io.BytesIO(pack(root, ["b.py", "a.py"]))) as tar:
            members = tar.getmembers()
        assert [m.name for m in members] == ["a.py", "b.py"]
        assert {(m.mtime, m.uid, m.gid, m.uname, m.gname, m.mode) for m in members} == {(0, 0, 0, "", "", 0o644)}


class TestDependencies:
    def test_pyproject_pinned_from_uv_lock(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, _PROJECT)
        assert project_dependencies(root) == ["requests==2.32.3", "rich[jupyter]==13.9.4; python_version >= '3.9'"]

    def test_requirements_txt_fallback(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"requirements.txt": "# deps\n-r other.txt\ntrafilatura==2.0.0  # html\n\n"})
        assert project_dependencies(root) == ["trafilatura==2.0.0"]

    def test_none_without_declared_deps(self, tmp_path: Path) -> None:
        assert project_dependencies(_tree(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n"})) is None


_SUPER_APP = """
from haystack import Pipeline, SuperComponent
from haystack.components.builders import PromptBuilder

_inner = Pipeline()
_inner.add_component("prompt", PromptBuilder("{{query}}", required_variables=["query"]))
app = SuperComponent(_inner, input_mapping={"query": ["prompt.query"]}, output_mapping={"prompt.prompt": "answers"})
"""


class TestBuild:
    def test_directory_target_renders_settings_only(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {**_PROJECT, "app.py": _SUPER_APP})
        code_bundle = build_code_bundle(root, entrypoint="app.py:app", python_executable=sys.executable)

        assert code_bundle.kind == "super_component"
        assert code_bundle.entrypoint == "app.py:app"
        assert code_bundle.sha256 == hashlib.sha256(code_bundle.archive).hexdigest()
        config = YAML(typ="safe").load(code_bundle.config_yaml)
        assert config == {
            "inputs": {"query": ["app.query"]},
            "outputs": {"answers": "app.answers"},
            "dependencies": ["requests==2.32.3", "rich[jupyter]==13.9.4; python_version >= '3.9'"],
        }
        assert set(code_bundle.request_field()) == {"sha256", "entrypoint", "kind", "input_schema", "graph"}

    def test_declared_dependencies_win(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {**_PROJECT, "app.py": _SUPER_APP})
        code_bundle = build_code_bundle(
            root / "app.py", settings=PipelineSettings(dependencies=["x==1"]), python_executable=sys.executable
        )
        assert YAML(typ="safe").load(code_bundle.config_yaml)["dependencies"] == ["x==1"]

    def test_directory_target_needs_entrypoint(self, tmp_path: Path) -> None:
        with pytest.raises(CodeBundleError, match="--entrypoint"):
            build_code_bundle(tmp_path)

    def test_size_cap_names_largest_files(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {**_PROJECT, "app.py": _SUPER_APP})
        with pytest.raises(CodeBundleError, match=r"over the 0 MB limit(.|\n)*app\.py"):
            build_code_bundle(root, entrypoint="app.py:app", python_executable=sys.executable, max_bytes=10)


_FUNCTION_APP = """
def run(name: str = "world") -> dict:
    return {"message": f"hello {name}"}
"""


class TestProjectFile:
    def test_absent_file_declares_nothing(self, tmp_path: Path) -> None:
        assert load_project_file(tmp_path) == ProjectFile()
        assert not has_project_file(tmp_path)

    def test_parses_all_keys(self, tmp_path: Path) -> None:
        _tree(tmp_path, {"deepset.yaml": "entrypoint: app.py:run\nkind: function\ndependencies: [langchain==0.3.0]\n"})
        assert load_project_file(tmp_path) == ProjectFile("app.py:run", "function", ["langchain==0.3.0"])

    def test_found_from_a_file_in_the_project(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"pyproject.toml": "", "deepset.yaml": "", "src/app.py": ""})
        assert has_project_file(root / "src" / "app.py")

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ("entrypoint: app.py:run\nimage: x\n", "Unknown key.*image.*Allowed keys: entrypoint, kind, dependencies"),
            ("entrypoint: app.py\n", "path.py:attr"),
            ("kind: lambda\n", "must be one of pipeline, agent, super_component, function"),
            ("dependencies: langchain\n", "list of requirement strings"),
            ("- a\n", "must be a mapping"),
        ],
    )
    def test_invalid_file_is_rejected(self, tmp_path: Path, content: str, message: str) -> None:
        _tree(tmp_path, {"deepset.yaml": content})
        with pytest.raises(CodeBundleError, match=message):
            load_project_file(tmp_path)


class TestFunctionBuild:
    def test_project_file_names_entrypoint_and_dependencies(self, tmp_path: Path) -> None:
        root = _tree(
            tmp_path,
            {**_PROJECT, "app.py": _FUNCTION_APP, "deepset.yaml": "entrypoint: app.py:run\ndependencies: [x==1]\n"},
        )
        code_bundle = build_code_bundle(root, python_executable=sys.executable)

        assert code_bundle.kind == "function"
        assert code_bundle.entrypoint == "app.py:run"
        assert "deepset.yaml" in code_bundle.files
        assert YAML(typ="safe").load(code_bundle.config_yaml) == {
            "inputs": {"payload": ["app.payload"]},
            "outputs": {"result": "app.result"},
            "dependencies": ["x==1"],
        }
        assert code_bundle.input_schema is not None
        assert code_bundle.input_schema["properties"]["name"]["default"] == "world"
        assert set(code_bundle.request_field()) == {"sha256", "entrypoint", "kind", "input_schema"}

    def test_mapping_and_output_type_are_fixed_for_a_function(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"app.py": _FUNCTION_APP, "deepset.yaml": "entrypoint: app.py:run\n"})
        code_bundle = build_code_bundle(
            root,
            inputs={"query": ["app.name"]},
            settings=PipelineSettings(pipeline_output_type="chat"),
            python_executable=sys.executable,
        )
        config = YAML(typ="safe").load(code_bundle.config_yaml)
        assert config["inputs"] == {"payload": ["app.payload"]}
        assert "pipeline_output_type" not in config

    def test_cli_entrypoint_beats_project_file(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {**_PROJECT, "app.py": _SUPER_APP, "deepset.yaml": "entrypoint: other.py:run\n"})
        code_bundle = build_code_bundle(root, entrypoint="app.py:app", python_executable=sys.executable)
        assert code_bundle.kind == "super_component"

    def test_project_file_kind_reaches_the_extractor(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, {"app.py": _SUPER_APP, "deepset.yaml": "entrypoint: app.py:app\nkind: agent\n"})
        with pytest.raises(PipelineTransformError, match="kind: agent"):
            build_code_bundle(root, python_executable=sys.executable)
