# Haystack Enterprise SDK Guidelines for AI Agents

This file covers two jobs: driving the SDK and its CLI on someone's behalf, and contributing to this repo.

## Authentication

Never run `haystack-enterprise login`. It prompts four times, has no flags to bypass those prompts,
and aborts with exit code 1 when nobody is there to answer.

Set these instead — the same keys `login` would write to `~/.haystack-enterprise/.env`:

```bash
API_KEY
API_URL                  # optional, defaults to https://api.cloud.deepset.ai
DEFAULT_WORKSPACE_NAME
```

Precedence, highest first: an explicit `--api-key` / `--workspace-name` argument, then a real
environment variable, then `./.env`, then `~/.haystack-enterprise/.env`.

`DEFAULT_WORKSPACE_NAME` is read once at import and never again, unlike `API_KEY` and `API_URL`.
Setting `os.environ["DEFAULT_WORKSPACE_NAME"]` after `import haystack_enterprise_sdk` has no effect.
Export it before the process starts, or pass `workspace_name=` explicitly.

## Driving the CLI

`deploy`, `validate`, and `run` take a path to a local Python file. They load it with your project's
interpreter — a `.venv` or `venv` found by walking up from that file, or `--python` — so this CLI's
own environment never needs your pipeline's dependencies installed.

Work outward from the cheapest check:

```bash
haystack-enterprise deploy pipeline.py SERVICE --dry-run --output out.yaml   # no credentials needed
haystack-enterprise validate pipeline.py                                     # exit 1 on any ERROR
haystack-enterprise run pipeline.py --query "..."                            # JSON on stdout
haystack-enterprise deploy pipeline.py SERVICE
```

Redirect with `--output`, never with `>`. The CLI's logger writes to stdout, so a shell redirect
interleaves warning lines into the YAML or JSON and the result no longer parses.

Exit codes worth knowing: 1 for validation, transform, and API errors; 130 when `run` is interrupted;
and 0 when a `deploy` rollout is detached or times out. A zero exit from `deploy` does not mean the
service is serving — confirm with `haystack-enterprise service-status SERVICE`.

Set `CI=true` to replace spinners and progress bars with plain line output.

### Two commands you cannot run unattended

`list-files` and `list-upload-sessions` ask "Print more results?" after every non-empty page, without
checking whether anyone is there to answer. With no TTY they print one page and abort with exit 1.
Use the generators in `haystack_enterprise_sdk.workflows.sync_client.files` instead.

## The I/O mapping is your job

Off a TTY the CLI stops asking how your pipeline's sockets map to platform inputs and outputs, and
deploys whatever it inferred. Nothing fails at that point. The mistake surfaces later, as a platform
validation error or as "Missing mandatory input" on the first real query.

Inference is name matching on open sockets:

- inputs: `query` (also `question`), `filters`, `files` (also `sources`), `messages`
- outputs: `answers`, `documents`, `messages` (also `replies`)

A mandatory socket named anything else is not mapped. Write the mapping to `<target>.io.yaml` next to
the pipeline file; `deploy`, `validate`, and `run` pick it up automatically:

```yaml
inputs:
  query:
    - retriever.query
outputs:
  answers: reader.answers
```

The platform needs at least one query input (`query` or `messages`) and at least one output. The same
file carries `pipeline_output_type`, `session_storage`, `dependencies`, and `async_enabled`; see the
io-config section of [docs/cli_command_flow.md](docs/cli_command_flow.md).

`--skip-io-validation` silences the warning without fixing the mapping.

## What a pipeline file must contain

Discovery is by type, not by name. Any module-level `Pipeline`, `AsyncPipeline`, or bare `Agent` whose
name does not start with `_` is a candidate, and exactly one must match. Two or more is an error —
pass `--entrypoint NAME`, a bare attribute name, to choose.

Importing the file runs all of its top-level code. If no instance is found, every zero-argument
callable in the module is then *called* to see whether it returns a pipeline. Keep import-time side
effects out of these files.

Indexing pipelines — anything containing a `DocumentWriter` — are rejected in this SDK version.
Deploy a query pipeline.

## Custom components are rewritten, their dependencies are not

A component defined in your own project is inlined into the platform's `Code` component: helper
functions become static methods, module constants become class attributes, and the platform runs the
result with `exec(code, {})` on an empty namespace. Any name not bound inside that block and not a
builtin is a `NameError` — at deploy time if it is used at class-definition time, or on the first
query if it is used inside a method body.

Two consequences: no aliased imports (`from x import y as z`), and no annotations naming a folded helper.

Only `haystack-ai` is pinned automatically. A third-party import in your component is carried into
the generated code but never into `dependencies:`, so it raises `ImportError` on the platform until
you list it yourself in the io-config.

## Using the SDK from Python

Import from `haystack_enterprise_sdk` only. `_api`, `_service`, and `_s3` are private.

Two clients that are not two ways to do the same thing:

- `DeploymentClient` deploys from a **file path** and creates a service deployment.
- `PipelineClient.import_into_platform` takes a **live `Pipeline` object** and imports a platform
  pipeline or index.

`DeploymentClient` and `AsyncDeploymentClient` are absent from the generated API docs; read their
docstrings.

## Contributing to this repo

This repo uses **uv** for environment and dependency management.

Do not run `python` or `pip` directly.

Before running code on this project, you must be able to run `uv --version`.

### Sync dependencies

uv sync --all-groups

### Run the CLI from source

uv run haystack-enterprise --help

### Run unit tests

make tests-unit

### Run integration tests

make tests-integration

### Type checking with mypy

make types

### Format and lint

make all-fix

`tests/` is type-checked as well, under `disallow_untyped_defs`. Every test function needs a return annotation.

Every async test needs an explicit `@pytest.mark.asyncio`. Unit and integration tests are split by directory.

Docstrings are reStructuredText (`:param:`, `:return:`, `:raises:`), despite the `google` convention
left in the ruff config. Match the surrounding file.

Use the [conventional commit specification](https://www.conventionalcommits.org/en/v1.0.0/) for PR
titles. A dependency change needs `uv lock` and CI runs `uv lock --check`.

## Where to look next

- [docs/cli_command_flow.md](docs/cli_command_flow.md) — the full build, validate, run, and deploy flow, the io-config key table, and notes on CI
- [haystack_enterprise_sdk/README.md](haystack_enterprise_sdk/README.md) — the four-layer design, i.e. where a change belongs
- [CONTRIBUTING.md](CONTRIBUTING.md) — setup and CI
