# Contributing

## Setup

### Build from source

Install uv
```bash
pip install uv
```

Sync dependencies
```bash
# Install all dependencies including dev dependencies
uv sync --all-groups

# Or install specific dependency groups
uv sync --group code-quality
uv sync --group test
```

### Install pre-commit hooks
```bash
make hooks
# or
uv run pre-commit install
```

## CI
Code quality checks, unit tests, and integration tests (against dev) are performed on the creation of a PR, and subsequent pushes for that PR.
Code quality checks, unit tests, and integration tests (against dev) are performed on a push to main.
Integration tests are triggered whenever the e2e tests are triggered (environment will be dependent on e2e tests)
Code quality checks, unit tests, and integration tests (against prod) are performed on the publishing of a release tag.

## Releasing

The SDK is not published to a package registry yet. Install it directly from this repository (see the [README](README.md)).

## Naming

One product, one name. Use these consistently in docs, docstrings, CLI help and error messages:

| Concept | Write it as | Not |
| --- | --- | --- |
| The product | Haystack Enterprise SDK | Haystack Enterprise Platform SDK, deepset Cloud SDK, deepset SDK |
| The PyPI package | `haystack-enterprise-sdk` | `deepset-cloud-sdk` |
| The import | `haystack_enterprise_sdk` | — |
| The CLI | the `haystack-enterprise` CLI | the deepset CLI, `deepset-cloud`, `deepset-cloud-cli` |
| The backend | Haystack Enterprise Platform | deepset AI Platform, deepset Cloud |
| The framework | Haystack | — |
| The company | deepset | Deepset, DeepSet |

Two platform-side identifiers look like old names but are wire contract and must **not** be renamed:
`deepset_cloud_custom_nodes.*` (component type paths) and the `deepset_cloud_version` payload key.

Docstrings in `cli.py` are user-visible twice over: they become `--help` output *and* the generated API
reference. Treat them as documentation, not comments.

## Software design

Have a look at this [README](/haystack_enterprise_sdk/README.md) to get an overview of the software design.
