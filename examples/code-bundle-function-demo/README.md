# Function code build demo

A code build whose entrypoint is a plain Python function instead of a Haystack pipeline:

- `deepset.yaml`: names the entrypoint (`app.py:run`). With it, `deploy` on the directory ships the
  project as code; no `--build` or `--entrypoint` needed.
- `app.py`: `run(name: str = "world") -> dict`. The platform calls it as `run(**payload)`.
- `greeting.py`: a sibling module, so the response proves the whole project was shipped.

Prerequisites are the same as in [code-bundle-demo](../code-bundle-demo/README.md). Run the commands
from the root of this repository.

## Check it locally

```sh
uv run haystack-enterprise deploy examples/code-bundle-function-demo hello-fn --dry-run
```

Shows the entrypoint `app.py:run (function)` and the settings: `payload` → `app.payload`,
`result` → `app.result`, `haystack-ai==3.2.0`.

## Deploy

```sh
uv run haystack-enterprise deploy examples/code-bundle-function-demo hello-fn
```

The CLI prints the run endpoint and an example curl when it is done.

## Call it

```sh
curl -s "$API_URL/api/v1/workspaces/<workspace>/deployments/<deployment id>/run" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "fleet"}'
```

```json
{"result": {"message": "hello fleet", "marker": "served from a code bundle"}}
```

## Other libraries

Nothing here is Haystack-specific. LangChain or anything else works the same way: list it in
`deepset.yaml`, which replaces the dependencies read from `pyproject.toml`:

```yaml
entrypoint: app.py:run
dependencies: [haystack-ai==3.2.0, langchain==0.3.0]
```

Keep `haystack-ai` in the list: the runtime that wraps the function needs it.
