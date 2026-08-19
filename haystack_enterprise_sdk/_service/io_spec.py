"""Declarative description of an integration's pipeline input/output mapping keys.

The platform-level ``inputs:``/``outputs:`` YAML sections are a wrapper around the pipeline: they map
an integration's named keys to pipeline sockets. The first (and currently only) integration is the
deepset AI Platform itself acting as the serving engine (Playground, shared prototypes, query API) —
described by :data:`PLATFORM_SERVING_SPEC`. Future integrations define their own
:class:`IntegrationIoSpec`; the CLI's review/edit UI is spec-driven and needs no changes per
integration.

The key names must stay in sync with ``STANDARD_INPUT_KEYS``/``STANDARD_OUTPUT_KEYS`` in
:mod:`haystack_enterprise_sdk._service.pipeline_extract` (which cannot import this module because it runs
standalone in the pipeline's interpreter); a unit test enforces this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

__all__ = [
    "IntegrationIoSpec",
    "PipelineSetting",
    "PlatformKey",
    "PIPELINE_SETTINGS",
    "PLATFORM_SERVING_SPEC",
    "render_io_config",
]


@dataclass(frozen=True)
class PlatformKey:
    """One named input or output an integration exposes, with user-facing display metadata."""

    name: str  # e.g. "query"
    description: str  # one-liner shown in the CLI summary and edit prompts
    type_hint: str  # human-readable expected type, e.g. "str", "dict", "list"
    direction: str  # "input" | "output"
    multi: bool  # inputs may fan out to several sockets; outputs map to exactly one


@dataclass(frozen=True)
class PipelineSetting:
    """One top-level pipeline setting an io-config may declare, for the generated file's stub lines.

    Data rather than literal lines so a single test can assert this list and the set of keys
    ``cli._load_io_config`` validates (``pipeline_transform.KNOWN_SETTING_KEYS``) never drift: a
    setting the loader accepts but no generated file documents is a setting nobody finds.
    """

    name: str  # e.g. "session_storage"
    description: str  # one-liner written above the stub as a comment
    example: str  # the value shown in the commented-out stub, valid YAML on its own


PIPELINE_SETTINGS = (
    PipelineSetting(
        name="pipeline_output_type",
        description="how the Playground renders results (generative | chat | extractive | document)",
        example="generative",
    ),
    PipelineSetting(
        name="session_storage",
        description="give the pipeline a per-session workspace that keeps files between runs",
        example="true",
    ),
    PipelineSetting(
        name="dependencies",
        description="pip pins the deployed revision installs; replaces the auto-detected haystack-ai pin",
        example="[haystack-ai==2.30.2]",
    ),
)


@dataclass(frozen=True)
class IntegrationIoSpec:
    """The full set of input/output keys one integration understands."""

    name: str
    inputs: Tuple[PlatformKey, ...]
    outputs: Tuple[PlatformKey, ...]

    def input_keys(self) -> Tuple[str, ...]:
        """The input key names, in canonical display order."""
        return tuple(key.name for key in self.inputs)

    def output_keys(self) -> Tuple[str, ...]:
        """The output key names, in canonical display order."""
        return tuple(key.name for key in self.outputs)


PLATFORM_SERVING_SPEC = IntegrationIoSpec(
    name="deepset AI Platform",
    inputs=(
        PlatformKey(
            name="query",
            description="The user's question/text sent by the Playground and chat UI",
            type_hint="str",
            direction="input",
            multi=True,
        ),
        PlatformKey(
            name="filters",
            description="Metadata filters restricting retrieval",
            type_hint="dict",
            direction="input",
            multi=True,
        ),
        PlatformKey(
            name="files",
            description="Files uploaded alongside the query",
            type_hint="list",
            direction="input",
            multi=True,
        ),
        PlatformKey(
            name="messages",
            description="The chat history as ChatMessage objects",
            type_hint="list",
            direction="input",
            multi=True,
        ),
    ),
    outputs=(
        PlatformKey(
            name="answers",
            description="Generated or extracted answers shown as the reply",
            type_hint="list",
            direction="output",
            multi=False,
        ),
        PlatformKey(
            name="documents",
            description="Retrieved documents shown as sources",
            type_hint="list",
            direction="output",
            multi=False,
        ),
        PlatformKey(
            name="messages",
            description="Assistant chat messages (for chat pipelines)",
            type_hint="list",
            direction="output",
            multi=False,
        ),
    ),
)


def render_io_config(
    spec: IntegrationIoSpec,
    inputs: dict,
    outputs: dict,
    *,
    target_name: Optional[str] = None,
) -> str:
    """Render an I/O mapping as a self-documenting, commented io-config YAML string.

    Mapped keys become active YAML with the key's description as a comment; unmapped spec keys appear
    as commented stubs so the file doubles as documentation. The result is loadable by the CLI's
    ``--io-config`` (and its auto-detection).
    """
    lines = []
    header_target = f" for {target_name}" if target_name else ""
    lines.append(f"# I/O mapping{header_target} — {spec.name}.")
    lines.append("# Picked up automatically by `deepset-cloud deploy`; edit freely, delete to re-map interactively.")
    lines.append("# An explicit --io-config <file> overrides this file.")
    lines.append("inputs:")
    for key in spec.inputs:
        lines.append(f"  # {key.description} ({key.type_hint})")
        sockets = inputs.get(key.name) or []
        if sockets:
            lines.append(f"  {key.name}:")
            lines.extend(f"    - {socket}" for socket in sockets)
        else:
            lines.append(f"  # {key.name}:")
            lines.append("  #   - <component.socket>")
    lines.append("outputs:")
    for key in spec.outputs:
        lines.append(f"  # {key.description} ({key.type_hint})")
        socket = outputs.get(key.name)
        if socket:
            lines.append(f"  {key.name}: {socket}")
        else:
            lines.append(f"  # {key.name}: <component.socket>")
    for setting in PIPELINE_SETTINGS:
        lines.append(f"# Optional: {setting.description}")
        lines.append(f"# {setting.name}: {setting.example}")
    return "\n".join(lines) + "\n"
