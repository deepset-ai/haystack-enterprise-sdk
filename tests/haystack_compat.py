"""Compatibility helpers for running the suite against both supported Haystack lines.

Haystack 3.0 folded ``AsyncPipeline`` into ``Pipeline`` and removed the class. The SDK
supports both lines (``haystack-ai>=2.13.2``) and ``PipelineService`` already tolerates
the class being absent, so the tests that specifically exercise the async-pipeline path
are skipped on 3.x rather than deleted -- they still have to pass on 2.x.
"""

from typing import Any

import haystack
import pytest

# Resolved with getattr rather than imported so this module type-checks against both
# lines: on 3.x, ``from haystack import AsyncPipeline`` is an attr-defined error.
AsyncPipeline: Any = getattr(haystack, "AsyncPipeline", None)

HAS_ASYNC_PIPELINE = AsyncPipeline is not None

requires_async_pipeline = pytest.mark.skipif(
    not HAS_ASYNC_PIPELINE,
    reason="AsyncPipeline was removed in Haystack 3.0 (folded into Pipeline)",
)
