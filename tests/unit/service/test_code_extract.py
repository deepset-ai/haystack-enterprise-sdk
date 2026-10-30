"""Tests for the extractor's code mode (describe a code build's entrypoint, rewrite nothing)."""

import sys
import textwrap
from pathlib import Path
from typing import Generator

import pytest

from haystack_enterprise_sdk._service.pipeline_extract import PipelineTransformError, extract_code_entrypoint
from haystack_enterprise_sdk._service.pipeline_transform import extract_code_via_subprocess


@pytest.fixture(autouse=True)
def clean_import_state() -> Generator[None, None, None]:
    """Isolate sys.path / sys.modules mutations made by importing user files."""
    original_path = list(sys.path)
    original_modules = set(sys.modules)
    yield
    sys.path[:] = original_path
    for name in set(sys.modules) - original_modules:
        del sys.modules[name]


def _write(root: Path, src: str, name: str = "app.py") -> Path:
    path = root / name
    path.write_text(textwrap.dedent(src), encoding="utf-8")
    return path


_PIPELINE_SRC = """
from haystack import Pipeline
from haystack.components.builders import PromptBuilder

pipe = Pipeline()
pipe.add_component("prompt", PromptBuilder("{{query}}", required_variables=["query"]))
"""

_SUPER_SRC = """
from haystack import Pipeline, SuperComponent, super_component
from haystack.components.builders import PromptBuilder

def _inner():
    p = Pipeline()
    p.add_component("prompt", PromptBuilder("{{query}}", required_variables=["query"]))
    return p

app = SuperComponent(_inner(), input_mapping={"query": ["prompt.query"]}, output_mapping={"prompt.prompt": "answers"})

@super_component
class Decorated:
    def __init__(self):
        self.pipeline = _inner()
        self.input_mapping = {"query": ["prompt.query"]}
        self.output_mapping = {"prompt.prompt": "answers"}
"""

_AGENT_SRC = """
from haystack.components.agents import Agent
from haystack.components.generators.chat import OpenAIChatGenerator

def make_agent():
    return Agent(chat_generator=OpenAIChatGenerator(model="gpt-4o-mini"))
"""


class TestCodeMode:
    def test_pipeline_kind_reuses_yaml_inference(self, tmp_path: Path) -> None:
        bundle = extract_code_entrypoint(_write(tmp_path, _PIPELINE_SRC))

        assert bundle["kind"] == "pipeline"
        assert bundle["entrypoint_attr"] == "pipe"
        assert bundle["inferred_inputs"] == {"query": ["prompt.query"]}
        assert bundle["inferred_outputs"] == {}
        assert "pipeline" not in bundle  # nothing rewritten, nothing rendered
        assert bundle["input_schema"]["properties"]["prompt"]["properties"]["query"] == {}
        assert bundle["input_schema"]["properties"]["prompt"]["required"] == ["query"]
        assert "prompt" in bundle["graph"]["components"]

    def test_super_component_maps_public_names_onto_app(self, tmp_path: Path) -> None:
        bundle = extract_code_entrypoint(_write(tmp_path, _SUPER_SRC), "app")

        assert bundle["kind"] == "super_component"
        assert bundle["inferred_inputs"] == {"query": ["app.query"]}
        assert bundle["inferred_outputs"] == {"answers": "app.answers"}
        assert set(bundle["input_schema"]["properties"]) == {"query"}  # flat payload
        assert bundle["suggested_pipeline_output_type"] is None
        assert "prompt" in bundle["graph"]["components"]  # the inner pipeline, not the wrapper

    def test_decorated_super_component_class_is_instantiated(self, tmp_path: Path) -> None:
        bundle = extract_code_entrypoint(_write(tmp_path, _SUPER_SRC), "Decorated")

        assert bundle["kind"] == "super_component"
        assert bundle["entrypoint_attr"] == "Decorated"
        assert bundle["inferred_outputs"] == {"answers": "app.answers"}

    def test_agent_factory_maps_messages_and_defaults_to_chat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        bundle = extract_code_entrypoint(_write(tmp_path, _AGENT_SRC))

        assert bundle["kind"] == "agent"
        assert bundle["entrypoint_attr"] == "make_agent"
        assert bundle["inferred_inputs"]["messages"] == ["agent.messages"]
        assert bundle["inferred_outputs"]["messages"] == "agent.messages"
        assert bundle["suggested_pipeline_output_type"] == "chat"
        assert bundle["input_schema"]["properties"]["messages"]["type"] == "array"

    def test_subprocess_roundtrip(self, tmp_path: Path) -> None:
        extraction, code = extract_code_via_subprocess(
            _write(tmp_path, _SUPER_SRC), "app", python_executable=sys.executable, project_root=tmp_path
        )

        assert code.kind == "super_component"
        assert code.attr == "app"
        assert extraction.pipeline == {}
        assert extraction.inferred_inputs == {"query": ["app.query"]}
        assert extraction.dependencies and extraction.dependencies[0].startswith("haystack-ai==")


_FUNCTION_SRC = """
from haystack import Pipeline

def run(name: str, times: int = 2) -> dict:
    return {"message": name * times}

def hello():
    return "hello"

def make_pipeline():
    return Pipeline()

def anything(**kwargs):
    return kwargs

class Greeter:
    def __call__(self, name: str) -> str:
        return name

greeter = Greeter()
"""


class TestFunctionKind:
    def test_function_with_parameters_is_a_function(self, tmp_path: Path) -> None:
        bundle = extract_code_entrypoint(_write(tmp_path, _FUNCTION_SRC), "run")

        assert bundle["kind"] == "function"
        assert bundle["entrypoint_attr"] == "run"
        assert bundle["inferred_inputs"] == {"payload": ["app.payload"]}
        assert bundle["inferred_outputs"] == {"result": "app.result"}
        assert "suggested_pipeline_output_type" not in bundle
        schema = bundle["input_schema"]
        assert schema["required"] == ["name"]
        assert schema["properties"]["name"]["type"] == "string"
        assert schema["properties"]["times"] == {"default": 2, "title": "Times", "type": "integer"}

    def test_zero_arg_function_returning_a_value_is_a_function(self, tmp_path: Path) -> None:
        bundle = extract_code_entrypoint(_write(tmp_path, _FUNCTION_SRC), "hello")
        assert bundle["kind"] == "function"
        assert bundle["input_schema"] == {"type": "object"}

    def test_zero_arg_function_returning_a_pipeline_is_a_factory(self, tmp_path: Path) -> None:
        bundle = extract_code_entrypoint(_write(tmp_path, _FUNCTION_SRC), "make_pipeline")
        assert bundle["kind"] == "pipeline"

    def test_kwargs_only_accepts_any_object(self, tmp_path: Path) -> None:
        assert extract_code_entrypoint(_write(tmp_path, _FUNCTION_SRC), "anything")["input_schema"] == {
            "type": "object"
        }

    def test_callable_object_is_a_function(self, tmp_path: Path) -> None:
        bundle = extract_code_entrypoint(_write(tmp_path, _FUNCTION_SRC), "greeter")
        assert bundle["kind"] == "function"
        assert bundle["input_schema"]["required"] == ["name"]

    def test_declared_kind_overrides_detection(self, tmp_path: Path) -> None:
        bundle = extract_code_entrypoint(_write(tmp_path, _FUNCTION_SRC), "make_pipeline", kind="function")
        assert bundle["kind"] == "function"

    def test_declared_kind_must_match(self, tmp_path: Path) -> None:
        with pytest.raises(PipelineTransformError, match="kind: agent"):
            extract_code_entrypoint(_write(tmp_path, _FUNCTION_SRC), "make_pipeline", kind="agent")

    def test_auto_detection_never_picks_a_function(self, tmp_path: Path) -> None:
        with pytest.raises(PipelineTransformError, match="needs an entrypoint"):
            extract_code_entrypoint(_write(tmp_path, _FUNCTION_SRC), kind="function")
