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

## Agent with a skill

`translate.py` holds an Agent that translates text into Klingon-ish. It follows instructions from the
`translate` skill, which ships inside the bundle as `skills/translate/SKILL.md`. `SkillToolset` lists the
skill in the agent's `load_skill` tool, and the agent loads it when it needs it. Set the OpenAI key as a
service secret; it is only read on the first call.

```sh
uv sync --project examples/code-bundle-function-demo   # haystack-ai 3.2 (skills), so the CLI can import it
export OPENAI_API_KEY=sk-...
```

### As a chat service (`translate.py:agent`)

The entrypoint is the Agent itself, so the platform maps chat `messages` to it and the chat endpoints
work, including OpenAI-compatible chat completions:

```sh
uv run haystack-enterprise deploy examples/code-bundle-function-demo klingon-chat \
  --entrypoint translate.py:agent --env OPENAI_API_KEY
uv run haystack-enterprise invoke klingon-chat --query "Hello warrior, the coffee is ready"

curl -s "$API_URL/api/v1/workspaces/<workspace>/deployments/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "<workspace>/<deployment id>", "messages": [{"role": "user", "content": "Hello warrior"}]}'
```

Any OpenAI client works too: `base_url=$API_URL/api/v1/workspaces/<workspace>/deployments/v1`,
`model="<workspace>/<deployment id>"`.

### As a function (`translate.py:run`)

```sh
uv run haystack-enterprise deploy examples/code-bundle-function-demo klingon \
  --entrypoint translate.py:run --env OPENAI_API_KEY
uv run haystack-enterprise invoke klingon --inputs '{"text": "Hello warrior, the coffee is ready"}'
```

A function build only serves `/run`; chat completions answer 400.

## Sync job (`sync.py:run`)

Plain Python, no Haystack and no secrets: fetch from an API, write to a data sink, return `"success"`.
Both ends are dummy functions, so swap in real calls when you need them.

```sh
uv run haystack-enterprise deploy examples/code-bundle-function-demo sync-job --entrypoint sync.py:run
uv run haystack-enterprise invoke sync-job --inputs '{"source": "orders"}'
```

```json
"success"
```

## Other libraries

Nothing here is Haystack-specific. LangChain or anything else works the same way: list it in
`deepset.yaml`, which replaces the dependencies read from `pyproject.toml`:

```yaml
entrypoint: app.py:run
dependencies: [haystack-ai==3.2.0, langchain==0.3.0]
```

Keep `haystack-ai` in the list: the runtime that wraps the function needs it.
