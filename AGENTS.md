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

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contribution guidelines.
