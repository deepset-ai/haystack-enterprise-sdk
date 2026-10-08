"""Code build demo: a SuperComponent with a platform-shaped API (`query` in, `answers` out).

Deploy with:
    haystack-enterprise deploy . code-bundle-demo --build code --entrypoint app.py:app
"""

from haystack import Pipeline, SuperComponent, component
from haystack.dataclasses import GeneratedAnswer

from demo_lib.text import shout


@component
class Shouter:
    """Answers every query by shouting it back, so a result proves the bundle's own code ran."""

    @component.output_types(answers=list[GeneratedAnswer])
    def run(self, query: str) -> dict:
        return {"answers": [GeneratedAnswer(data=shout(query), query=query, documents=[])]}


def _pipeline() -> Pipeline:
    pipeline = Pipeline()
    pipeline.add_component("shouter", Shouter())
    return pipeline


app = SuperComponent(
    pipeline=_pipeline(),
    input_mapping={"query": ["shouter.query"]},
    output_mapping={"shouter.answers": "answers"},
)
