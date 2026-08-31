# Haystack Enterprise SDK Guidelines for AI Agents

## Environment

The Haystack Enterprise SDK uses **uv** for environment and dependency management.

Do not run `python` or `pip` directly.

Before running code on this project, you must be able to run `uv --version` and get a correct output.

If not, ask the user where uv is or if they want to install it. For installation instructions, refer to https://docs.astral.sh/uv/getting-started/installation/.

### Sync dependencies

uv sync --all-groups

### Run scripts with test dependencies

uv run python SCRIPT.py

### Run the CLI from source

uv run haystack-enterprise --help

### Install pre-commit hooks

make hooks

## Tests

Tests run via uv and support pytest arguments.

Prefer running tests on a specific module or using `-k`, since the full suite is large.

### Run unit tests

make tests-unit

### Run integration tests

make tests-integration

Integration tests run against a live environment and need credentials, so run them only when the user asks for it.

## Quality Checks

### Type checking with mypy
make types

To fix type issues, avoid `type: ignore`, casts, or assertions when possible. If they are necessary, explain why.

### Format and lint
make all-fix

Use `make all` to check formatting, linting, and types without applying fixes.

## Pull Requests

Use the [conventional commit specification](https://www.conventionalcommits.org/en/v1.0.0/) for the PR title, and fill in the [pull request template](.github/pull_request_template.md).

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contribution guidelines.
