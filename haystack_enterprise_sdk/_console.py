"""The SDK's animated terminal output, and what it degrades to in CI.

A spinner or progress bar repaints one line by returning the cursor to its start. A CI log viewer has
no cursor to move, so every repaint lands as a new line: a multi-minute `deploy` turns into thousands
of near-identical lines in a GitHub Actions log.

This module is the seam. It is the only place in the SDK that imports ``yaspin`` or ``tqdm`` (one
grep enforces that), so CI-awareness is decided once, here, rather than at each render site. Each
constructor below hands back either the animated thing or the degraded form a caller already has a
branch for -- a printing stand-in, ``None``, a plain ``gather`` -- so no call site asks whether it is
in CI, and a render site added later is CI-aware for free.
"""

import asyncio
import os
import sys
from contextlib import contextmanager
from typing import Any, Awaitable, Iterator, List, Optional, Sequence, TypeVar

from tqdm import tqdm
from tqdm.asyncio import tqdm as async_tqdm
from yaspin import yaspin

# Values that mean "not CI" -- so `CI=false` turns the animations back on.
_CI_OFF = ("", "0", "false")

T = TypeVar("T")


def animations_enabled() -> bool:
    """Whether this process may draw a spinner or progress bar over its own output.

    False in CI, which GitHub Actions, GitLab CI and Jenkins all announce by setting ``CI``. Kept as
    one predicate so every constructor in this module agrees on the answer.
    """
    return os.environ.get("CI", "").strip().lower() in _CI_OFF


def status_spinner() -> Any:
    """A live status line: an animated spinner normally, plain printed lines in CI.

    Use as ``with status_spinner() as spinner: spinner.text = "..."``.
    """
    return yaspin().arc if animations_enabled() else _PlainStatus()


def progress_bar(*, total: Optional[int], desc: str) -> Optional[Any]:
    """A ``tqdm`` progress bar over ``total`` items, or ``None`` in CI.

    ``None`` rather than a silenced bar, because every caller already branches on it for
    ``show_progress=False`` -- so in CI the progress falls into that same path (log lines) instead of
    disappearing.
    """
    return tqdm(total=total, desc=desc) if animations_enabled() else None


async def gather_with_progress(tasks: Sequence[Awaitable[T]], *, desc: str) -> List[T]:
    """``asyncio.gather`` with a bar that advances as tasks finish -- a plain gather in CI.

    The bar redraws its line once per completed task, which is the same one-line-per-repaint problem
    as a spinner, so in CI we just gather.
    """
    if not animations_enabled():
        return list(await asyncio.gather(*tasks))
    gathered: List[T] = await async_tqdm.gather(*tasks, desc=desc)
    return gathered


class _PlainStatus:
    """Non-animated stand-in for a yaspin spinner: prints each distinct status once, to stderr.

    Exposes only what the SDK uses from yaspin -- the context manager, ``text`` and ``hidden()`` -- so
    call sites need no branch of their own. Assigning ``text`` the value it already has is dropped:
    the deploy poller reports the same status every few seconds, and without a cursor to overwrite
    that repaint would be a fresh log line.

    Progress goes to stderr because it is diagnostic chatter; that keeps a piped stdout (`run` writes
    JSON there) parseable.
    """

    def __init__(self) -> None:
        self._text = ""

    def __enter__(self) -> "_PlainStatus":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    @property
    def text(self) -> str:
        """The status last printed."""
        return self._text

    @text.setter
    def text(self, value: str) -> None:
        if value == self._text:
            return
        self._text = value
        print(value, file=sys.stderr, flush=True)

    @contextmanager
    def hidden(self) -> Iterator[None]:
        """No-op counterpart to ``yaspin.hidden()``: nothing is animated, so nothing needs hiding."""
        yield
