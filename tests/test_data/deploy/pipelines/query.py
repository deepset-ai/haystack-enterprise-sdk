"""A ``src``-layout pipeline: the pipeline file and its custom component live in sibling directories.

The component (``custom_nodes.greeter``) resolves from ``../``, i.e. outside this file's own directory,
which is exactly the layout that used to be left un-inlined.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_nodes.greeter import Greeter  # noqa: E402
from haystack import Pipeline  # noqa: E402

pipeline = Pipeline()
pipeline.add_component("greeter", Greeter(shout=True))
