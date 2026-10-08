"""A plain function entrypoint. The platform calls it as run(**payload) and returns {"result": ...}."""

from greeting import MARKER, greet


def run(name: str = "world") -> dict:
    return {"message": greet(name), "marker": MARKER}
