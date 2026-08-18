"""Tests for the declarative platform I/O key spec and the io-config renderer."""

from haystack_enterprise_sdk._service.io_spec import PLATFORM_SERVING_SPEC, render_io_config
from haystack_enterprise_sdk._service.pipeline_extract import (
    STANDARD_INPUT_KEYS,
    STANDARD_OUTPUT_KEYS,
)


class TestSpecSync:
    def test_spec_keys_match_extractor_standard_keys(self) -> None:
        # The extractor module runs standalone in the pipeline's interpreter and cannot import the
        # spec, so it keeps its own key tuples — this pins them together.
        assert PLATFORM_SERVING_SPEC.input_keys() == STANDARD_INPUT_KEYS
        assert PLATFORM_SERVING_SPEC.output_keys() == STANDARD_OUTPUT_KEYS

    def test_every_key_has_a_description(self) -> None:
        for key in (*PLATFORM_SERVING_SPEC.inputs, *PLATFORM_SERVING_SPEC.outputs):
            assert key.description
            assert key.type_hint


class TestRenderIoConfig:
    def test_renders_mapped_keys_and_commented_stubs(self) -> None:
        content = render_io_config(
            PLATFORM_SERVING_SPEC,
            {"query": ["retriever.query", "prompt_builder.question"]},
            {"answers": "reader.answers"},
        )
        assert "  query:\n    - retriever.query\n    - prompt_builder.question" in content
        assert "  answers: reader.answers" in content
        # Unmapped keys appear as commented, self-documenting stubs.
        assert "  # filters:" in content
        assert "  # documents: <component.socket>" in content
        assert "# pipeline_output_type: generative" in content
        assert "# session_storage: true" in content

    def test_rendered_config_roundtrips_through_loader(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from haystack_enterprise_sdk.cli import _load_io_config

        path = tmp_path / "pipeline.io.yaml"
        path.write_text(
            render_io_config(PLATFORM_SERVING_SPEC, {"query": ["retriever.query"]}, {"answers": "reader.answers"}),
            encoding="utf-8",
        )
        cfg_io = _load_io_config(path)
        assert cfg_io.inputs == {"query": ["retriever.query"]}
        assert cfg_io.outputs == {"answers": "reader.answers"}
        assert cfg_io.pipeline_output_type is None

    def test_rendered_config_with_nothing_mapped_loads_as_absent(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # All keys commented out: the sections parse as empty and must count as absent, not error.
        from haystack_enterprise_sdk.cli import _load_io_config

        path = tmp_path / "pipeline.io.yaml"
        path.write_text(render_io_config(PLATFORM_SERVING_SPEC, {}, {}), encoding="utf-8")
        cfg_io = _load_io_config(path)
        assert cfg_io.inputs is None
        assert cfg_io.outputs is None
        assert cfg_io.pipeline_output_type is None
