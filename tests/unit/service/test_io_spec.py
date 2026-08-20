"""Tests for the declarative platform I/O key spec and the io-config renderer."""

from haystack_enterprise_sdk._service.io_spec import (
    PIPELINE_SETTINGS,
    PLATFORM_SERVING_SPEC,
    render_io_config,
)
from haystack_enterprise_sdk._service.pipeline_extract import (
    STANDARD_INPUT_KEYS,
    STANDARD_OUTPUT_KEYS,
)
from haystack_enterprise_sdk._service.pipeline_transform import KNOWN_SETTING_KEYS, PipelineSettings


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

    def test_setting_stubs_match_the_settings_the_loader_accepts(self) -> None:
        # Two independent lists that have to agree: what a generated io-config documents, and what
        # `_load_io_config` recognises as a setting rather than passing through as an unknown key. A
        # setting missing from either side is invisible or undocumented, so pin them together.
        assert {setting.name for setting in PIPELINE_SETTINGS} == KNOWN_SETTING_KEYS

    def test_every_setting_stub_loads_when_uncommented(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # A stub exists to be uncommented, so its example value has to pass the loader's validation of
        # that key — an example the loader rejects is worse than no example.
        from haystack_enterprise_sdk.cli import _load_io_config

        for setting in PIPELINE_SETTINGS:
            path = tmp_path / f"{setting.name}.io.yaml"
            path.write_text(f"{setting.name}: {setting.example}\n", encoding="utf-8")
            declared = getattr(_load_io_config(path).settings, setting.name)
            assert declared is not None, setting.name


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
        for setting in PIPELINE_SETTINGS:
            assert f"# {setting.name}: {setting.example}" in content

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
        # Every setting is a commented-out stub, so none of them is declared.
        assert cfg_io.settings == PipelineSettings()

    def test_rendered_config_with_nothing_mapped_loads_as_absent(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # All keys commented out: the sections parse as empty and must count as absent, not error.
        from haystack_enterprise_sdk.cli import _load_io_config

        path = tmp_path / "pipeline.io.yaml"
        path.write_text(render_io_config(PLATFORM_SERVING_SPEC, {}, {}), encoding="utf-8")
        cfg_io = _load_io_config(path)
        assert cfg_io.inputs is None
        assert cfg_io.outputs is None
        assert cfg_io.settings == PipelineSettings()
