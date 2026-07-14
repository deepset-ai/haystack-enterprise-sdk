"""Tests for the pipeline transform (local .py -> deployable platform YAML)."""

import os
import sys
import textwrap
from pathlib import Path
from typing import Generator

import pytest
from haystack import Pipeline
from ruamel.yaml import YAML

from deepset_cloud_sdk._service.pipeline_extract import (
    _classify_origin,
    extract_from_pipeline,
)
from deepset_cloud_sdk._service.pipeline_transform import (
    CODE_COMPONENT_TYPE,
    PipelineTransformError,
    classify_module,
    detect_project_python,
    extract_via_subprocess,
    load_pipeline_from_file,
    render_config_yaml,
    transform_to_config_yaml,
    unmapped_mandatory_inputs,
)

FIXTURE_DIR = Path(__file__).parent.parent.parent / "test_data" / "deploy"


@pytest.fixture(autouse=True)
def clean_import_state() -> Generator[None, None, None]:
    """Isolate sys.path / sys.modules mutations made by importing user pipeline files."""
    original_path = list(sys.path)
    original_modules = set(sys.modules)
    yield
    sys.path[:] = original_path
    for name in set(sys.modules) - original_modules:
        del sys.modules[name]


def _write_project(root: Path, files: dict) -> Path:
    """Write ``{relative_path: content}`` under ``root`` and return ``root / 'pipeline.py'``."""
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content), encoding="utf-8")
    return root / "pipeline.py"


def _load_yaml(config_yaml: str) -> dict:
    base = config_yaml.split("\n# Custom dependencies")[0]
    return YAML().load(base)


# --------------------------------------------------------------------------- #
# Happy path: the committed fixture project
# --------------------------------------------------------------------------- #
class TestTransformFixture:
    def test_local_component_rewritten_to_code(self) -> None:
        pipeline = load_pipeline_from_file(FIXTURE_DIR / "pipeline.py")
        config_yaml = transform_to_config_yaml(pipeline, project_root=FIXTURE_DIR)
        doc = _load_yaml(config_yaml)

        greeter = doc["components"]["greeter"]
        assert greeter["type"] == CODE_COMPONENT_TYPE
        # original init params are passed through nested under `init_parameters`
        assert greeter["init_parameters"]["init_parameters"] == {"shout": True}

        code = greeter["init_parameters"]["code"]
        # transitive local inlining: class + both helper levels + the constant
        assert "@component" in code
        assert "class Greeter" in code
        assert "def make_greeting" in code
        assert "def normalize_name" in code
        assert "GREETING_PREFIX" in code
        # external import preserved, local import dropped
        assert "import requests" in code
        assert "from custom_nodes" not in code

    def test_base_component_untouched(self) -> None:
        pipeline = load_pipeline_from_file(FIXTURE_DIR / "pipeline.py")
        doc = _load_yaml(transform_to_config_yaml(pipeline, project_root=FIXTURE_DIR))
        assert doc["components"]["prompt_builder"]["type"].startswith("haystack.")

    def test_dependency_block_is_commented_and_pinned(self) -> None:
        pipeline = load_pipeline_from_file(FIXTURE_DIR / "pipeline.py")
        config_yaml = transform_to_config_yaml(pipeline, project_root=FIXTURE_DIR)
        assert "# dependencies:" in config_yaml
        assert "#   - requests==" in config_yaml
        # base packages are never listed
        assert "haystack" not in config_yaml.split("# Custom dependencies")[1]

    def test_roundtrips_through_haystack_and_platform_parser(self) -> None:
        pytest.importorskip("deepset_cloud_custom_nodes")
        from deepset_cloud_custom_nodes.utils.haystack_parser import extract_haystack_component

        pipeline = load_pipeline_from_file(FIXTURE_DIR / "pipeline.py")
        config_yaml = transform_to_config_yaml(pipeline, project_root=FIXTURE_DIR)
        base = config_yaml.split("\n# Custom dependencies")[0]
        doc = _load_yaml(config_yaml)
        for name, comp in doc["components"].items():
            if comp["type"] == CODE_COMPONENT_TYPE:
                extract_haystack_component(comp["init_parameters"]["code"])
        Pipeline.loads(base)


# --------------------------------------------------------------------------- #
# Auto-detection
# --------------------------------------------------------------------------- #
class TestDetection:
    def test_detects_single_instance(self, tmp_path: Path) -> None:
        path = _write_project(
            tmp_path,
            {"pipeline.py": "from haystack import Pipeline\npipeline = Pipeline()\n"},
        )
        assert isinstance(load_pipeline_from_file(path), Pipeline)

    def test_detects_zero_arg_factory(self, tmp_path: Path) -> None:
        path = _write_project(
            tmp_path,
            {
                "pipeline.py": """
                from haystack import Pipeline

                def build() -> Pipeline:
                    return Pipeline()
                """
            },
        )
        assert isinstance(load_pipeline_from_file(path), Pipeline)

    def test_multiple_instances_requires_entrypoint(self, tmp_path: Path) -> None:
        path = _write_project(
            tmp_path,
            {"pipeline.py": "from haystack import Pipeline\na = Pipeline()\nb = Pipeline()\n"},
        )
        with pytest.raises(PipelineTransformError, match="Multiple pipeline instances"):
            load_pipeline_from_file(path)

    def test_entrypoint_selects_instance(self, tmp_path: Path) -> None:
        path = _write_project(
            tmp_path,
            {"pipeline.py": "from haystack import Pipeline\na = Pipeline()\nb = Pipeline()\n"},
        )
        assert isinstance(load_pipeline_from_file(path, entrypoint="b"), Pipeline)

    def test_entrypoint_selects_factory(self, tmp_path: Path) -> None:
        path = _write_project(
            tmp_path,
            {
                "pipeline.py": """
                from haystack import Pipeline

                def make() -> Pipeline:
                    return Pipeline()
                """
            },
        )
        assert isinstance(load_pipeline_from_file(path, entrypoint="make"), Pipeline)

    def test_missing_entrypoint_errors(self, tmp_path: Path) -> None:
        path = _write_project(tmp_path, {"pipeline.py": "from haystack import Pipeline\np = Pipeline()\n"})
        with pytest.raises(PipelineTransformError, match="Entrypoint 'nope' not found"):
            load_pipeline_from_file(path, entrypoint="nope")

    def test_no_pipeline_errors(self, tmp_path: Path) -> None:
        path = _write_project(tmp_path, {"pipeline.py": "x = 1\n"})
        with pytest.raises(PipelineTransformError, match="No Pipeline"):
            load_pipeline_from_file(path)

    def test_missing_file_errors(self, tmp_path: Path) -> None:
        with pytest.raises(PipelineTransformError, match="not found"):
            load_pipeline_from_file(tmp_path / "does_not_exist.py")

    def test_import_error_is_surfaced(self, tmp_path: Path) -> None:
        path = _write_project(tmp_path, {"pipeline.py": "import a_module_that_does_not_exist_xyz\n"})
        with pytest.raises(PipelineTransformError, match="Failed to import"):
            load_pipeline_from_file(path)

    def test_index_pipeline_rejected(self, tmp_path: Path) -> None:
        path = _write_project(
            tmp_path,
            {
                "pipeline.py": """
                from haystack import Pipeline
                from haystack.components.writers import DocumentWriter
                from haystack.document_stores.in_memory import InMemoryDocumentStore

                pipeline = Pipeline()
                pipeline.add_component("writer", DocumentWriter(document_store=InMemoryDocumentStore()))
                """
            },
        )
        with pytest.raises(PipelineTransformError, match="indexing pipeline"):
            load_pipeline_from_file(path)


# --------------------------------------------------------------------------- #
# Code-block validation (local, mirrors the platform parser)
# --------------------------------------------------------------------------- #
class TestCodeBlockValidation:
    def test_required_init_param_rejected(self, tmp_path: Path) -> None:
        path = _write_project(
            tmp_path,
            {
                "custom/__init__.py": "",
                "custom/comp.py": """
                from haystack import component

                @component
                class NeedsArg:
                    def __init__(self, model):  # required, no default
                        self.model = model

                    @component.output_types(x=str)
                    def run(self, x: str):
                        return {"x": x}
                """,
                "pipeline.py": """
                from haystack import Pipeline
                from custom.comp import NeedsArg

                pipeline = Pipeline()
                pipeline.add_component("c", NeedsArg(model="m"))
                """,
            },
        )
        pipeline = load_pipeline_from_file(path)
        with pytest.raises(PipelineTransformError, match="required __init__ parameters"):
            transform_to_config_yaml(pipeline, project_root=tmp_path)

    def test_multiple_component_classes_rejected(self, tmp_path: Path) -> None:
        # Component A references component B (both local @component) -> inlining pulls both into one block.
        path = _write_project(
            tmp_path,
            {
                "custom/__init__.py": "",
                "custom/b.py": """
                from haystack import component

                @component
                class B:
                    @component.output_types(y=str)
                    def run(self, y: str):
                        return {"y": y}
                """,
                "custom/a.py": """
                from haystack import component
                from custom.b import B

                @component
                class A:
                    @component.output_types(x=str)
                    def run(self, x: str):
                        _ = B
                        return {"x": x}
                """,
                "pipeline.py": """
                from haystack import Pipeline
                from custom.a import A

                pipeline = Pipeline()
                pipeline.add_component("a", A())
                """,
            },
        )
        pipeline = load_pipeline_from_file(path)
        with pytest.raises(PipelineTransformError, match="multiple @component classes"):
            transform_to_config_yaml(pipeline, project_root=tmp_path)


# --------------------------------------------------------------------------- #
# Inputs / outputs inference & dependencies
# --------------------------------------------------------------------------- #
class TestInputsOutputsAndDeps:
    def _query_project(self, tmp_path: Path) -> Path:
        return _write_project(
            tmp_path,
            {
                "custom/__init__.py": "",
                "custom/searcher.py": """
                from typing import List
                from haystack import component

                @component
                class Searcher:
                    @component.output_types(answers=List[str], documents=List[str])
                    def run(self, query: str, filters: dict = None):
                        return {"answers": [query], "documents": []}
                """,
                "pipeline.py": """
                from haystack import Pipeline
                from custom.searcher import Searcher

                pipeline = Pipeline()
                pipeline.add_component("searcher", Searcher())
                """,
            },
        )

    def test_infers_inputs_and_outputs(self, tmp_path: Path) -> None:
        pipeline = load_pipeline_from_file(self._query_project(tmp_path))
        doc = _load_yaml(transform_to_config_yaml(pipeline, project_root=tmp_path))
        assert doc["inputs"]["query"] == ["searcher.query"]
        assert doc["inputs"]["filters"] == ["searcher.filters"]
        assert doc["outputs"]["answers"] == "searcher.answers"
        assert doc["outputs"]["documents"] == "searcher.documents"

    def test_explicit_inputs_outputs_override_inference(self, tmp_path: Path) -> None:
        pipeline = load_pipeline_from_file(self._query_project(tmp_path))
        doc = _load_yaml(
            transform_to_config_yaml(
                pipeline,
                project_root=tmp_path,
                inputs={"query": ["searcher.query"]},
                outputs={"answers": "searcher.answers"},
            )
        )
        assert "filters" not in doc["inputs"]
        assert "documents" not in doc["outputs"]

    def test_no_inputs_inferred_warns_and_omits(self, tmp_path: Path) -> None:
        path = _write_project(tmp_path, {"pipeline.py": "from haystack import Pipeline\npipeline = Pipeline()\n"})
        pipeline = load_pipeline_from_file(path)
        doc = _load_yaml(transform_to_config_yaml(pipeline, project_root=tmp_path))
        assert "inputs" not in doc
        assert "outputs" not in doc

    def test_bundle_reports_available_sockets(self, tmp_path: Path) -> None:
        pipeline = load_pipeline_from_file(self._query_project(tmp_path))
        bundle = extract_from_pipeline(pipeline, project_root=tmp_path)
        assert set(bundle["available_inputs"]["searcher"]) == {"query", "filters"}
        assert set(bundle["available_outputs"]["searcher"]) == {"answers", "documents"}

    def _prompt_builder_project(self, tmp_path: Path) -> Path:
        """A summarization-style pipeline whose only open input is a ``question`` prompt variable."""
        return _write_project(
            tmp_path,
            {
                "pipeline.py": """
                from haystack import Pipeline
                from haystack.components.builders.prompt_builder import PromptBuilder

                pipeline = Pipeline()
                pipeline.add_component(
                    "prompt_builder",
                    PromptBuilder(template="Passage: {{ question }}", required_variables="*"),
                )
                """,
            },
        )

    def test_infers_question_socket_as_query(self, tmp_path: Path) -> None:
        # Regression: a PromptBuilder using {{ question }} must be routed to the platform `query`
        # input, otherwise the shared prototype fails with "Missing mandatory input 'question'".
        pipeline = load_pipeline_from_file(self._prompt_builder_project(tmp_path))
        doc = _load_yaml(transform_to_config_yaml(pipeline, project_root=tmp_path))
        assert doc["inputs"]["query"] == ["prompt_builder.question"]

    def test_bundle_reports_mandatory_inputs(self, tmp_path: Path) -> None:
        pipeline = load_pipeline_from_file(self._prompt_builder_project(tmp_path))
        bundle = extract_from_pipeline(pipeline, project_root=tmp_path)
        assert bundle["mandatory_inputs"]["prompt_builder"] == ["question"]

    def test_unmapped_mandatory_input_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        # A mandatory socket whose name inference doesn't recognize must be surfaced, not shipped silently.
        path = _write_project(
            tmp_path,
            {
                "custom/__init__.py": "",
                "custom/summarizer.py": """
                from typing import List
                from haystack import component

                @component
                class Summarizer:
                    @component.output_types(answers=List[str])
                    def run(self, passage: str):
                        return {"answers": [passage]}
                """,
                "pipeline.py": """
                from haystack import Pipeline
                from custom.summarizer import Summarizer

                pipeline = Pipeline()
                pipeline.add_component("summarizer", Summarizer())
                """,
            },
        )
        pipeline = load_pipeline_from_file(path)
        doc = _load_yaml(transform_to_config_yaml(pipeline, project_root=tmp_path))
        # `passage` is not recognized as query/filters, so it is left unmapped...
        assert "query" not in doc.get("inputs", {})
        # ...and the deploy warns that it will fail at query time.
        assert "summarizer.passage" in caplog.text

    def test_unmapped_mandatory_inputs_helper(self) -> None:
        mandatory = {"prompt_builder": ["question"], "searcher": ["query"]}
        inputs = {"query": ["searcher.query"]}
        assert unmapped_mandatory_inputs(mandatory, inputs) == ["prompt_builder.question"]

    def test_unmapped_mandatory_inputs_helper_all_mapped(self) -> None:
        mandatory = {"prompt_builder": ["question"]}
        inputs = {"query": ["prompt_builder.question", "answer_builder.query"]}
        assert unmapped_mandatory_inputs(mandatory, inputs) == []

    def test_requirements_file_overrides_autodetect(self, tmp_path: Path) -> None:
        pipeline = load_pipeline_from_file(FIXTURE_DIR / "pipeline.py")
        req = tmp_path / "requirements.txt"
        req.write_text("# a comment\ntrafilatura==1.6.0\nnumpy==1.26.4\n", encoding="utf-8")
        config_yaml = transform_to_config_yaml(pipeline, project_root=FIXTURE_DIR, requirements=req)
        assert "#   - trafilatura==1.6.0" in config_yaml
        assert "#   - numpy==1.26.4" in config_yaml
        assert "requests" not in config_yaml.split("# Custom dependencies")[1]

    def test_pyproject_toml_dependencies_override_autodetect(self, tmp_path: Path) -> None:
        pipeline = load_pipeline_from_file(FIXTURE_DIR / "pipeline.py")
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "demo"\ndependencies = ["trafilatura==1.6.0", "numpy>=1.26"]\n',
            encoding="utf-8",
        )
        config_yaml = transform_to_config_yaml(pipeline, project_root=FIXTURE_DIR, requirements=pyproject)
        assert "#   - trafilatura==1.6.0" in config_yaml
        assert "#   - numpy>=1.26" in config_yaml
        assert "requests" not in config_yaml.split("# Custom dependencies")[1]

    def test_pyproject_toml_without_dependencies_omits_block(self, tmp_path: Path) -> None:
        pipeline = load_pipeline_from_file(FIXTURE_DIR / "pipeline.py")
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "demo"\n', encoding="utf-8")
        config_yaml = transform_to_config_yaml(pipeline, project_root=FIXTURE_DIR, requirements=pyproject)
        assert "# Custom dependencies" not in config_yaml

    def test_malformed_pyproject_toml_raises(self, tmp_path: Path) -> None:
        pipeline = load_pipeline_from_file(FIXTURE_DIR / "pipeline.py")
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project\nname = broken", encoding="utf-8")
        with pytest.raises(PipelineTransformError):
            transform_to_config_yaml(pipeline, project_root=FIXTURE_DIR, requirements=pyproject)


# --------------------------------------------------------------------------- #
# classify_module
# --------------------------------------------------------------------------- #
class TestDotenvLoading:
    """Importing a pipeline auto-loads a project ``.env`` so ``Secret.from_env_var`` pipelines work."""

    _PIPELINE = """
    import os
    from haystack import Pipeline

    # Fails to import unless the variable is present — mirrors a client that needs a key at construction.
    if not os.environ.get("PIPELINE_TEST_SECRET"):
        raise RuntimeError("PIPELINE_TEST_SECRET is not set")

    pipeline = Pipeline()
    """

    def test_loads_env_from_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PIPELINE_TEST_SECRET", raising=False)
        path = _write_project(tmp_path, {"pipeline.py": self._PIPELINE})
        (tmp_path / ".env").write_text('PIPELINE_TEST_SECRET="from-dotenv"\n', encoding="utf-8")

        load_pipeline_from_file(path)  # would raise if the .env was not loaded

        assert os.environ["PIPELINE_TEST_SECRET"] == "from-dotenv"

    def test_existing_env_var_wins_over_dotenv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PIPELINE_TEST_SECRET", "from-shell")
        path = _write_project(tmp_path, {"pipeline.py": self._PIPELINE})
        (tmp_path / ".env").write_text("PIPELINE_TEST_SECRET=from-dotenv\n", encoding="utf-8")

        load_pipeline_from_file(path)

        assert os.environ["PIPELINE_TEST_SECRET"] == "from-shell"


class TestSubprocessExtraction:
    def test_extract_via_subprocess_roundtrips(self) -> None:
        # Uses the current interpreter, which has haystack installed.
        extraction = extract_via_subprocess(FIXTURE_DIR / "pipeline.py", python_executable=sys.executable)
        assert any(dep.startswith("requests==") for dep in extraction["dependencies"])
        config_yaml = render_config_yaml(extraction)
        assert CODE_COMPONENT_TYPE in config_yaml
        assert "class Greeter" in config_yaml
        assert "# dependencies:" in config_yaml

    def test_extract_via_subprocess_missing_dep_errors(self, tmp_path: Path) -> None:
        path = _write_project(tmp_path, {"pipeline.py": "import a_missing_module_xyz\n"})
        with pytest.raises(PipelineTransformError, match="a_missing_module_xyz"):
            extract_via_subprocess(path, python_executable=sys.executable)

    def test_detect_project_python_finds_venv(self, tmp_path: Path) -> None:
        venv_python = tmp_path / "proj" / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("")
        target = tmp_path / "proj" / "pkg" / "pipeline.py"
        target.parent.mkdir(parents=True)
        target.write_text("")
        assert detect_project_python(target) == str(venv_python)

    def test_detect_project_python_falls_back_to_current(self, tmp_path: Path) -> None:
        target = tmp_path / "pipeline.py"
        target.write_text("")
        assert detect_project_python(target) == sys.executable


class TestClassifyOrigin:
    """Regression tests for the path-classification, especially venv-inside-project."""

    def test_site_packages_inside_project_is_external(self, tmp_path: Path) -> None:
        # A project's .venv often lives inside the project dir; installed packages must be external.
        origin = tmp_path / ".venv" / "lib" / "python3.13" / "site-packages" / "tiktoken" / "__init__.py"
        assert _classify_origin(origin, tmp_path) == "external"

    def test_dist_packages_inside_project_is_external(self, tmp_path: Path) -> None:
        origin = tmp_path / "venv" / "lib" / "dist-packages" / "numpy" / "__init__.py"
        assert _classify_origin(origin, tmp_path) == "external"

    def test_local_source_is_local(self, tmp_path: Path) -> None:
        origin = tmp_path / "custom_nodes" / "greeter.py"
        assert _classify_origin(origin, tmp_path) == "local"

    def test_outside_project_is_stdlib(self, tmp_path: Path) -> None:
        origin = Path("/usr/lib/python3.13/json/__init__.py")
        assert _classify_origin(origin, tmp_path) == "stdlib"


class TestClassifyModule:
    def test_stdlib(self, tmp_path: Path) -> None:
        assert classify_module("json", tmp_path) == "stdlib"

    def test_external(self, tmp_path: Path) -> None:
        assert classify_module("haystack", tmp_path) == "external"

    def test_local(self, tmp_path: Path) -> None:
        (tmp_path / "mylib.py").write_text("x = 1\n", encoding="utf-8")
        sys.path.insert(0, str(tmp_path))
        assert classify_module("mylib", tmp_path) == "local"

    def test_unresolvable_is_external(self, tmp_path: Path) -> None:
        assert classify_module("totally_missing_pkg_xyz", tmp_path) == "external"
