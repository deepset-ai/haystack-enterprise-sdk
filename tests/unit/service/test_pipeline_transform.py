"""Tests for the pipeline transform (local .py -> deployable platform YAML)."""

import ast
import os
import sys
import textwrap
from pathlib import Path
from typing import Generator
from unittest.mock import Mock

import pytest
from haystack import Pipeline
from ruamel.yaml import YAML

from haystack_enterprise_sdk._service.pipeline_extract import (
    _classify_origin,
    _sanitize_agent_init_params,
    extract_from_pipeline,
    validate_code_block,
    validate_tool_code_block,
)
from haystack_enterprise_sdk._service.pipeline_transform import (
    CODE_COMPONENT_TYPE,
    ExtractionBundle,
    PipelineTransformError,
    build_config_yaml,
    classify_module,
    detect_project_python,
    extract_via_subprocess,
    load_pipeline_from_file,
    render_config_yaml,
    resolve_io,
    unmapped_mandatory_inputs,
)

FIXTURE_DIR = Path(__file__).parent.parent.parent / "test_data" / "deploy"


def transform_to_config_yaml(pipeline, project_root, inputs=None, outputs=None) -> str:
    """In-process extract → resolve → render, mirroring the production assembly for a live pipeline."""
    bundle = ExtractionBundle.from_dict(extract_from_pipeline(pipeline, Path(project_root)))
    resolved_inputs, resolved_outputs = resolve_io(bundle, inputs, outputs, None)
    return render_config_yaml(bundle, inputs=resolved_inputs, outputs=resolved_outputs)


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
    return YAML().load(config_yaml)


def _component_code(config_yaml: str, comp_name: str) -> str:
    """Return the ``code`` string of a rewritten Code component from rendered YAML."""
    return _load_yaml(config_yaml)["components"][comp_name]["init_parameters"]["code"]


def _class_def(code: str, class_name: str) -> ast.ClassDef:
    """Parse ``code`` and return the named class node (fails if it is not the sole top-level class)."""
    tree = ast.parse(code)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    assert [c.name for c in classes] == [class_name], f"expected exactly one class {class_name}"
    return classes[0]


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

    def test_dependency_block_pins_haystack_version(self) -> None:
        pipeline = load_pipeline_from_file(FIXTURE_DIR / "pipeline.py")
        config_yaml = transform_to_config_yaml(pipeline, project_root=FIXTURE_DIR)
        # an active dependencies block pinning only the executing haystack-ai version
        assert "\ndependencies:\n" in config_yaml
        block = config_yaml.split("\ndependencies:\n")[1]
        assert "  - haystack-ai==" in block
        # user packages are never listed in the dependency block
        assert "requests" not in block

    def test_roundtrips_through_haystack_and_platform_parser(self) -> None:
        pytest.importorskip("deepset_cloud_custom_nodes")
        from deepset_cloud_custom_nodes.utils.haystack_parser import extract_haystack_component

        pipeline = load_pipeline_from_file(FIXTURE_DIR / "pipeline.py")
        config_yaml = transform_to_config_yaml(pipeline, project_root=FIXTURE_DIR)
        base = config_yaml.split("\ndependencies:")[0]
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
# Agent compile step: a bare Agent is wrapped into a single-component Pipeline
# --------------------------------------------------------------------------- #
_HAS_AGENT = True
try:  # pragma: no cover - import guard
    from haystack.components.agents import Agent  # noqa: F401
except ImportError:  # pragma: no cover
    _HAS_AGENT = False

_AGENT_SRC = """
from haystack.components.agents import Agent
from haystack.components.generators.chat import OpenAIChatGenerator

agent = Agent(chat_generator=OpenAIChatGenerator(model="gpt-4o-mini"))
"""


@pytest.mark.skipif(not _HAS_AGENT, reason="haystack Agent not available")
class TestAgentCompile:
    @pytest.fixture(autouse=True)
    def _openai_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # OpenAIChatGenerator reads OPENAI_API_KEY at construction; a dummy value is enough to build it.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def test_compiles_bare_agent_into_pipeline(self, tmp_path: Path) -> None:
        path = _write_project(tmp_path, {"pipeline.py": _AGENT_SRC})
        pipeline = load_pipeline_from_file(path, entrypoint="agent")
        assert isinstance(pipeline, Pipeline)
        assert "agent" in pipeline.to_dict()["components"]

    def test_compiles_agent_factory(self, tmp_path: Path) -> None:
        src = _AGENT_SRC + "\ndef make():\n    return agent\n"
        path = _write_project(tmp_path, {"pipeline.py": src})
        assert isinstance(load_pipeline_from_file(path, entrypoint="make"), Pipeline)

    def test_agent_yaml_maps_messages_and_defaults_to_chat(self, tmp_path: Path) -> None:
        path = _write_project(tmp_path, {"pipeline.py": _AGENT_SRC})
        pipeline = load_pipeline_from_file(path, entrypoint="agent")
        config_yaml = transform_to_config_yaml(pipeline, project_root=tmp_path)
        doc = _load_yaml(config_yaml)
        assert doc["inputs"] == {"messages": ["agent.messages"]}
        assert doc["outputs"] == {"messages": "agent.messages"}
        # A compiled agent is chat-shaped, so the output type defaults to chat (matching the platform).
        assert doc["pipeline_output_type"] == "chat"

    def test_non_agent_pipeline_has_no_suggested_output_type(self, tmp_path: Path) -> None:
        path = _write_project(tmp_path, {"pipeline.py": "from haystack import Pipeline\npipeline = Pipeline()\n"})
        pipeline = load_pipeline_from_file(path)
        bundle = ExtractionBundle.from_dict(extract_from_pipeline(pipeline, tmp_path))
        assert bundle.suggested_pipeline_output_type is None

    def test_emitted_agent_params_are_not_left_at_defaults(self, tmp_path: Path) -> None:
        import inspect

        from haystack.components.agents import Agent

        src = (
            "from haystack.components.agents import Agent\n"
            "from haystack.components.generators.chat import OpenAIChatGenerator\n"
            'agent = Agent(chat_generator=OpenAIChatGenerator(model="gpt-4o-mini"), '
            'system_prompt="You are a helpful assistant.")\n'
        )
        path = _write_project(tmp_path, {"pipeline.py": src})
        pipeline = load_pipeline_from_file(path, entrypoint="agent")
        init = extract_from_pipeline(pipeline, tmp_path)["pipeline"]["components"]["agent"]["init_parameters"]
        defaults = {
            name: p.default
            for name, p in inspect.signature(Agent.__init__).parameters.items()
            if p.default is not inspect.Parameter.empty
        }
        # Nothing the user left at its default should survive into the deployed config (portability
        # across the platform's older Agent); explicitly-set params like system_prompt still do.
        for key, value in init.items():
            if key in defaults:
                assert value != defaults[key], f"param '{key}' was left at its default"
        assert init["system_prompt"] == "You are a helpful assistant."
        assert "chat_generator" in init


_TOOLS_SRC = """
from typing import Annotated
from haystack.tools import tool


def _format(recipient, message):
    return f"to {recipient}: {message}"


@tool
def send_notification(
    recipient: Annotated[str, "email address"],
    message: Annotated[str, "the body"],
) -> str:
    \"\"\"Send a notification.\"\"\"
    return _format(recipient, message)
"""

_AGENT_WITH_TOOL_SRC = """
from haystack.components.agents import Agent
from haystack.components.generators.chat import OpenAIChatGenerator
from tools import send_notification

agent = Agent(
    chat_generator=OpenAIChatGenerator(model="gpt-4o-mini"),
    tools=[send_notification],
)
"""


@pytest.mark.skipif(not _HAS_AGENT, reason="haystack Agent not available")
class TestToolInlining:
    @pytest.fixture(autouse=True)
    def _openai_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def _agent_tools(self, tmp_path: Path) -> list:
        path = _write_project(tmp_path, {"pipeline.py": _AGENT_WITH_TOOL_SRC, "tools.py": _TOOLS_SRC})
        pipeline = load_pipeline_from_file(path, entrypoint="agent")
        bundle = extract_from_pipeline(pipeline, tmp_path)
        return bundle["pipeline"]["components"]["agent"]["init_parameters"]["tools"]

    def test_local_tool_rewritten_to_code_tool(self, tmp_path: Path) -> None:
        tools = self._agent_tools(tmp_path)
        assert len(tools) == 1
        tool = tools[0]
        assert tool["type"] == "deepset_cloud_custom_nodes.tools.code_tool.CodeTool"
        assert tool["data"]["name"] == "send_notification"
        assert tool["data"]["description"] == "Send a notification."
        # A local import path must no longer leak into the config.
        assert "function" not in tool["data"]

    def test_code_tool_inlines_source_and_transitive_helper(self, tmp_path: Path) -> None:
        code = self._agent_tools(tmp_path)[0]["data"]["code"]
        assert "@tool" in code
        assert "def send_notification" in code
        # The transitive local helper is pulled in; the local import is dropped.
        assert "def _format" in code
        assert "from tools import" not in code

    def test_non_local_tool_left_untouched(self, tmp_path: Path) -> None:
        # A tool whose function resolves to an installed package (not the project) is not rewritten.
        path = _write_project(
            tmp_path,
            {
                "pipeline.py": _AGENT_WITH_TOOL_SRC.replace(
                    "from tools import send_notification", "from haystack.tools import Tool"
                ).replace(
                    "tools=[send_notification],",
                    "tools=[Tool(name='noop', description='d', "
                    "function=len, parameters={'type': 'object', 'properties': {}})],",
                )
            },
        )
        pipeline = load_pipeline_from_file(path, entrypoint="agent")
        tools = extract_from_pipeline(pipeline, tmp_path)["pipeline"]["components"]["agent"]["init_parameters"]["tools"]
        # `len` lives in builtins, not the project, so the tool stays a plain Tool (not a CodeTool).
        assert tools[0]["type"] != "deepset_cloud_custom_nodes.tools.code_tool.CodeTool"


class TestValidateToolCodeBlock:
    def test_rejects_no_tool_function(self) -> None:
        with pytest.raises(PipelineTransformError, match="no @tool-decorated function"):
            validate_tool_code_block("t", "def plain():\n    return 1\n")

    def test_rejects_multiple_tool_functions(self) -> None:
        code = "from haystack.tools import tool\n@tool\ndef a():\n    return 1\n@tool\ndef b():\n    return 2\n"
        with pytest.raises(PipelineTransformError, match="multiple @tool functions"):
            validate_tool_code_block("t", code)

    def test_accepts_single_tool_function(self) -> None:
        validate_tool_code_block("t", "from haystack.tools import tool\n@tool\ndef a():\n    return 1\n")


class TestSanitizeAgentInitParams:
    def test_removes_hooks_from_agent(self) -> None:
        components = {
            "agent": {
                "type": "haystack.components.agents.agent.Agent",
                "init_parameters": {"system_prompt": "hi", "hooks": {"before_tool": ["x"]}},
            }
        }
        _sanitize_agent_init_params(components)
        assert "hooks" not in components["agent"]["init_parameters"]
        # An explicitly-set, non-default param is preserved.
        assert components["agent"]["init_parameters"]["system_prompt"] == "hi"

    def test_prunes_default_valued_params(self) -> None:
        # Unknown-to-the-platform param sitting at the authoring Agent's default is dropped; a custom
        # value for the same param would be kept.
        import inspect

        from haystack.components.agents import Agent

        defaults = {
            name: p.default
            for name, p in inspect.signature(Agent.__init__).parameters.items()
            if p.default is not inspect.Parameter.empty
        }
        assert defaults, "expected Agent to have defaulted params"
        some_key, some_default = next(iter(defaults.items()))
        components = {
            "agent": {
                "type": "haystack.components.agents.agent.Agent",
                "init_parameters": {some_key: some_default, "system_prompt": "custom"},
            }
        }
        _sanitize_agent_init_params(components)
        init = components["agent"]["init_parameters"]
        assert some_key not in init
        assert init["system_prompt"] == "custom"

    def test_leaves_non_agent_component_untouched(self) -> None:
        components = {"c": {"type": "haystack.components.foo.Foo", "init_parameters": {"hooks": 1}}}
        _sanitize_agent_init_params(components)
        assert components["c"]["init_parameters"]["hooks"] == 1


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
        bundle = ExtractionBundle.from_dict(extract_from_pipeline(pipeline, project_root=tmp_path))
        assert set(bundle.available_inputs["searcher"]) == {"query", "filters"}
        assert set(bundle.available_outputs["searcher"]) == {"answers", "documents"}
        # Each socket carries display metadata: a stringified type and its mandatory flag.
        query_socket = bundle.available_inputs["searcher"]["query"]
        assert query_socket["type"] == "str"
        assert query_socket["is_mandatory"] is True

    def test_bundle_normalizes_legacy_available_socket_lists(self) -> None:
        # A stale extractor emitting the old list shape must still parse into the typed shape.
        bundle = ExtractionBundle.from_dict({"available_inputs": {"retriever": ["query"]}})
        assert bundle.available_inputs == {"retriever": {"query": {"type": None, "is_mandatory": False}}}

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
        bundle = ExtractionBundle.from_dict(extract_from_pipeline(pipeline, project_root=tmp_path))
        assert bundle.mandatory_inputs["prompt_builder"] == ["question"]

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
        assert any(dep.startswith("haystack-ai==") for dep in extraction.dependencies)
        inputs, outputs = resolve_io(extraction, None, None, None)
        config_yaml = render_config_yaml(extraction, inputs=inputs, outputs=outputs)
        assert CODE_COMPONENT_TYPE in config_yaml
        # Parse the YAML rather than string-match: ruamel line-wraps the code scalar, which can split
        # a token like "class Greeter" across a folded line.
        doc = _load_yaml(config_yaml)
        assert "class Greeter" in doc["components"]["greeter"]["init_parameters"]["code"]
        assert "\ndependencies:\n" in config_yaml
        assert "  - haystack-ai==" in config_yaml

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


class TestBuildConfigYaml:
    """The ``io_resolver`` always gets the final say; empty returns keep the resolved mappings."""

    def _bundle(self, inferred_inputs: dict, inferred_outputs: dict, mandatory_inputs: dict = None) -> ExtractionBundle:
        return ExtractionBundle.from_dict(
            {
                "pipeline": {"components": {}},
                "async_enabled": False,
                "inferred_inputs": inferred_inputs,
                "inferred_outputs": inferred_outputs,
                "available_inputs": {"retriever": ["query"]},
                "available_outputs": {"reader": ["answers"]},
                "mandatory_inputs": mandatory_inputs or {},
                "dependencies": [],
            }
        )

    def test_resolver_invoked_when_io_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from haystack_enterprise_sdk._service import pipeline_transform

        monkeypatch.setattr(pipeline_transform, "extract_via_subprocess", lambda *a, **k: self._bundle({}, {}))
        resolver = Mock(return_value=({"query": ["retriever.query"]}, {"answers": "reader.answers"}))

        yaml = build_config_yaml(FIXTURE_DIR / "pipeline.py", io_resolver=resolver)

        resolver.assert_called_once()
        assert "retriever.query" in yaml
        assert "reader.answers" in yaml

    def test_resolver_called_with_inferred_io_and_empty_return_keeps_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The resolver is always consulted (it decides whether to interact); returning empty dicts
        # keeps the inferred mappings untouched.
        from haystack_enterprise_sdk._service import pipeline_transform

        bundle = self._bundle({"query": ["retriever.query"]}, {"answers": "reader.answers"})
        monkeypatch.setattr(pipeline_transform, "extract_via_subprocess", lambda *a, **k: bundle)
        resolver = Mock(return_value=({}, {}))

        yaml = build_config_yaml(FIXTURE_DIR / "pipeline.py", io_resolver=resolver)

        resolver.assert_called_once()
        _, current_inputs, current_outputs = resolver.call_args.args
        assert current_inputs == {"query": ["retriever.query"]}
        assert current_outputs == {"answers": "reader.answers"}
        assert "retriever.query" in yaml
        assert "reader.answers" in yaml

    def test_resolver_invoked_when_only_outputs_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from haystack_enterprise_sdk._service import pipeline_transform

        bundle = self._bundle({"query": ["retriever.query"]}, {})
        monkeypatch.setattr(pipeline_transform, "extract_via_subprocess", lambda *a, **k: bundle)
        resolver = Mock(return_value=({}, {"answers": "reader.answers"}))

        yaml = build_config_yaml(FIXTURE_DIR / "pipeline.py", io_resolver=resolver)

        resolver.assert_called_once()
        assert "reader.answers" in yaml

    def test_resolver_invoked_when_mandatory_input_unmapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Inference produced inputs and outputs, but a mandatory socket is not routed to any platform
        # input — the resolver must still be consulted (same rule as --dry-run).
        from haystack_enterprise_sdk._service import pipeline_transform

        bundle = self._bundle(
            {"query": ["retriever.query"]},
            {"answers": "reader.answers"},
            mandatory_inputs={"prompt_builder": ["passage"]},
        )
        monkeypatch.setattr(pipeline_transform, "extract_via_subprocess", lambda *a, **k: bundle)
        resolver = Mock(return_value=({"query": ["retriever.query", "prompt_builder.passage"]}, {}))

        yaml = build_config_yaml(FIXTURE_DIR / "pipeline.py", io_resolver=resolver)

        resolver.assert_called_once()
        assert "prompt_builder.passage" in yaml


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


# --------------------------------------------------------------------------- #
# Folding local helpers into the component class (platform Code invariant)
# --------------------------------------------------------------------------- #
class TestHelperFolding:
    """The generated code must have nothing at module level except imports and the one @component class."""

    def test_helpers_folded_into_class(self) -> None:
        pipeline = load_pipeline_from_file(FIXTURE_DIR / "pipeline.py")
        code = _component_code(transform_to_config_yaml(pipeline, project_root=FIXTURE_DIR), "greeter")

        tree = ast.parse(code)
        # Module level: only imports + the single component class — no stray def/class/constant.
        non_import = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        assert [type(n).__name__ for n in non_import] == ["ClassDef"]

        greeter = non_import[0]
        assert greeter.name == "Greeter"
        members = {n.name: n for n in greeter.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        # Helper functions folded as @staticmethod inside the class.
        for helper in ("normalize_name", "make_greeting"):
            assert helper in members
            decorators = {d.id for d in members[helper].decorator_list if isinstance(d, ast.Name)}
            assert "staticmethod" in decorators
        # The constant folded as a class attribute.
        attrs = {t.id for n in greeter.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        assert "GREETING_PREFIX" in attrs

    def test_references_qualified(self) -> None:
        pipeline = load_pipeline_from_file(FIXTURE_DIR / "pipeline.py")
        code = _component_code(transform_to_config_yaml(pipeline, project_root=FIXTURE_DIR), "greeter")
        # References to folded symbols become ClassName.<symbol>, inside run() and inside helpers.
        assert "Greeter.GREETING_PREFIX" in code
        assert "Greeter.make_greeting" in code
        assert "Greeter.normalize_name" in code
        # The external import is used unqualified; its attribute access is untouched.
        assert "requests.utils.default_user_agent()" in code

    def test_generated_code_reparses(self) -> None:
        pipeline = load_pipeline_from_file(FIXTURE_DIR / "pipeline.py")
        code = _component_code(transform_to_config_yaml(pipeline, project_root=FIXTURE_DIR), "greeter")
        ast.parse(code)  # 3.9 ast.unparse smoke: emitted code must be valid Python

    def test_shadowing_kept_bare(self, tmp_path: Path) -> None:
        path = _write_project(
            tmp_path,
            {
                "custom/__init__.py": "",
                "custom/helpers.py": """
                def helper(x):
                    return x + 1
                """,
                "custom/comp.py": """
                from haystack import component
                from custom.helpers import helper

                @component
                class C:
                    @component.output_types(x=int)
                    def run(self, x: int):
                        helper = x * 2   # local shadows the folded helper -> must stay bare
                        return {"x": helper}
                """,
                "pipeline.py": """
                from haystack import Pipeline
                from custom.comp import C

                pipeline = Pipeline()
                pipeline.add_component("c", C())
                """,
            },
        )
        pipeline = load_pipeline_from_file(path)
        code = _component_code(transform_to_config_yaml(pipeline, project_root=tmp_path), "c")
        # The folded def is qualified where called, but the shadowing local assignment stays bare.
        assert "helper = x * 2" in code
        assert "C.helper = x * 2" not in code

    def test_recursion_and_mutual_refs_qualified(self, tmp_path: Path) -> None:
        path = _write_project(
            tmp_path,
            {
                "custom/__init__.py": "",
                "custom/helpers.py": """
                def fib(n):
                    return n if n < 2 else fib(n - 1) + fib(n - 2)
                """,
                "custom/comp.py": """
                from haystack import component
                from custom.helpers import fib

                @component
                class C:
                    @component.output_types(x=int)
                    def run(self, x: int):
                        return {"x": fib(x)}
                """,
                "pipeline.py": """
                from haystack import Pipeline
                from custom.comp import C

                pipeline = Pipeline()
                pipeline.add_component("c", C())
                """,
            },
        )
        pipeline = load_pipeline_from_file(path)
        code = _component_code(transform_to_config_yaml(pipeline, project_root=tmp_path), "c")
        # Self-reference inside the folded staticmethod is qualified.
        assert "C.fib(n - 1)" in code
        assert "C.fib(x)" in code

    def test_global_on_folded_symbol_rejected(self, tmp_path: Path) -> None:
        path = _write_project(
            tmp_path,
            {
                "custom/__init__.py": "",
                "custom/helpers.py": """
                COUNTER = 0

                def bump():
                    global COUNTER
                    COUNTER += 1
                    return COUNTER
                """,
                "custom/comp.py": """
                from haystack import component
                from custom.helpers import bump

                @component
                class C:
                    @component.output_types(x=int)
                    def run(self, x: int):
                        return {"x": bump()}
                """,
                "pipeline.py": """
                from haystack import Pipeline
                from custom.comp import C

                pipeline = Pipeline()
                pipeline.add_component("c", C())
                """,
            },
        )
        pipeline = load_pipeline_from_file(path)
        with pytest.raises(PipelineTransformError, match="global/nonlocal"):
            transform_to_config_yaml(pipeline, project_root=tmp_path)

    def test_helper_name_collision_with_member_rejected(self, tmp_path: Path) -> None:
        path = _write_project(
            tmp_path,
            {
                "custom/__init__.py": "",
                "custom/helpers.py": """
                def prepare(x):
                    return x
                """,
                "custom/comp.py": """
                from haystack import component
                from custom.helpers import prepare

                @component
                class C:
                    def prepare(self):   # collides with the folded helper name
                        return None

                    @component.output_types(x=int)
                    def run(self, x: int):
                        return {"x": prepare(x)}
                """,
                "pipeline.py": """
                from haystack import Pipeline
                from custom.comp import C

                pipeline = Pipeline()
                pipeline.add_component("c", C())
                """,
            },
        )
        pipeline = load_pipeline_from_file(path)
        with pytest.raises(PipelineTransformError, match="collides"):
            transform_to_config_yaml(pipeline, project_root=tmp_path)

    def test_class_scope_helper_reference_rejected(self, tmp_path: Path) -> None:
        path = _write_project(
            tmp_path,
            {
                "custom/__init__.py": "",
                "custom/helpers.py": """
                def default_name():
                    return "anon"
                """,
                "custom/comp.py": """
                from haystack import component
                from custom.helpers import default_name

                @component
                class C:
                    @component.output_types(x=str)
                    def run(self, x: str = default_name()):   # helper called at def time -> unfoldable
                        return {"x": x}
                """,
                "pipeline.py": """
                from haystack import Pipeline
                from custom.comp import C

                pipeline = Pipeline()
                pipeline.add_component("c", C())
                """,
            },
        )
        pipeline = load_pipeline_from_file(path)
        with pytest.raises(PipelineTransformError, match="class-definition time"):
            transform_to_config_yaml(pipeline, project_root=tmp_path)


class TestModuleLevelInvariant:
    """Unit tests for the validate_code_block module-level check."""

    def test_module_level_function_rejected(self) -> None:
        code = textwrap.dedent(
            """
            from haystack import component

            def helper():
                return 1

            @component
            class C:
                @component.output_types(x=int)
                def run(self):
                    return {"x": helper()}
            """
        )
        with pytest.raises(PipelineTransformError, match="module-level definition 'helper'"):
            validate_code_block("c", code)

    def test_clean_single_class_passes(self) -> None:
        code = textwrap.dedent(
            '''
            """A module docstring is allowed."""
            from haystack import component

            @component
            class C:
                CONST = 1

                @staticmethod
                def helper():
                    return C.CONST

                @component.output_types(x=int)
                def run(self):
                    return {"x": C.helper()}
            '''
        )
        validate_code_block("c", code)  # must not raise
