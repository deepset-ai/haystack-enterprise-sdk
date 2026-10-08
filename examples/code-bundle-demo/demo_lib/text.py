"""A helper module, to prove the platform imports the whole project and not only the entrypoint file."""

MARKER = "served from a code bundle"


def shout(text: str) -> str:
    return f"{text.upper()}! ({MARKER})"
