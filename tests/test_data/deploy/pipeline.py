"""A folder-structured query pipeline for the deploy transform spike/tests.

Exposes both a `Pipeline` instance (`pipeline`) and a zero-arg factory
(`build_pipeline`) so the auto-detection logic can be exercised both ways.
"""

from haystack import Pipeline
from haystack.components.builders import PromptBuilder

from custom_nodes.greeter import Greeter


def build_pipeline() -> Pipeline:
    """Build a small pipeline: a local custom component feeding a prompt builder."""
    pp = Pipeline()
    pp.add_component("greeter", Greeter(shout=True))
    pp.add_component("prompt_builder", PromptBuilder(template="Say: {{ greeting }}"))
    pp.connect("greeter.greeting", "prompt_builder.greeting")
    return pp


pipeline = build_pipeline()
