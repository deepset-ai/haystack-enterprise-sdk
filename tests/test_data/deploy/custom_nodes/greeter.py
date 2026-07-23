"""A local custom Haystack component.

It references local helper symbols (`make_greeting`, `GREETING_PREFIX`, and
transitively `normalize_name`) plus an external dependency (`requests`). The
transform must inline the local symbols into the Code block and record `requests`
as an extracted (external) dependency.
"""

import requests  # external dependency — should be extracted, not inlined
from haystack import component

from custom_nodes.helpers import GREETING_PREFIX, make_greeting


@component
class Greeter:
    """Greets a name, optionally shouting. No required __init__ params (Code-safe)."""

    def __init__(self, shout: bool = False) -> None:
        self.shout = shout

    @component.output_types(greeting=str, user_agent=str)
    def run(self, name: str) -> dict:
        greeting = GREETING_PREFIX + make_greeting(name)
        if self.shout:
            greeting = greeting.upper()
        # touch the external dep so the import is genuinely used
        user_agent = requests.utils.default_user_agent()
        return {"greeting": greeting, "user_agent": user_agent}
