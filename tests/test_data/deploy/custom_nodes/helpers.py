"""Local helper symbols used by the custom component.

These live in a sibling module of the component and must be transitively inlined
into the generated Code block, because the deployed Code component cannot import
from the user's local project.
"""

# A module-level constant the component references.
GREETING_PREFIX = ">> "


def normalize_name(name: str) -> str:
    """Trim and title-case a name. Referenced transitively by the component."""
    return name.strip().title()


def make_greeting(name: str) -> str:
    """Build a greeting. References `normalize_name` (a second level of local inlining)."""
    return f"Hello, {normalize_name(name)}!"
