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

The SDK is not published to a package registry yet. Install it directly from this repository (see the [README](README.md)).

## Software design

Have a look at this [README](/haystack_enterprise_sdk/README.md) to get an overview of the software design.
