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
Publishing a GitHub Release builds the package and publishes it to PyPI, and regenerates the API docs.

## Releasing

The SDK is published to PyPI as [`haystack-enterprise-sdk`](https://pypi.org/project/haystack-enterprise-sdk/).

1. Bump `version` under `[project]` in `pyproject.toml`, run `uv lock`, and merge that to `main`.
2. Publish a GitHub Release tagged `v<version>` — `v0.1.1` for version `0.1.1`.

Publishing the release is the single human action that ships a version. It triggers
`CI_pypi_release.yml`, which checks the tag against `pyproject.toml`, builds the sdist and wheel,
installs the wheel into a throwaway environment to prove the entry point works, and uploads to PyPI via
trusted publishing. The same event triggers `api-docs.yaml`, so the package and the docs site move
together.

To rehearse without shipping, run `CI_pypi_release.yml` manually (`workflow_dispatch`). That path
stamps a throwaway `.devN` version and uploads to TestPyPI; it can never reach PyPI.

Use `v` prefix for tags. For example, tag `v0.1.1` instead of `0.1.1`.

## Software design

Have a look at this [README](/haystack_enterprise_sdk/README.md) to get an overview of the software design.
