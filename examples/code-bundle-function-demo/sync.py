"""A plain-Python job: fetch from an API, write to a data sink, report success.

Both ends are dummies, so it runs without network access or secrets. Deploy with
`--entrypoint sync.py:run` and call `/run` with `{"source": "orders"}`.
"""


def fetch_from_api(source: str) -> str:
    """Stands in for an HTTP call, e.g. `httpx.get(f"https://api.example.com/{source}").text`."""
    return f"3 records from {source}"


def ingest(data: str) -> str:
    """Stands in for a write to a warehouse, vector store or queue."""
    return "success"


def run(source: str = "orders") -> str:
    return ingest(fetch_from_api(source))
