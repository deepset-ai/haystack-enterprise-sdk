"""A sibling module: its marker in the response proves the platform imported the whole project."""

MARKER = "served from a code bundle"


def greet(name: str) -> str:
    return f"hello {name}"
