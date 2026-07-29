# Haystack Enterprise SDK

> **Experimental.** This SDK is under active development. APIs and CLI commands may change without notice.

Python SDK and CLI for the Haystack Enterprise Platform.

## Installation

Not published to a package registry yet — install directly from this repository with [uv](https://docs.astral.sh/uv/):

```bash
# Install as a CLI tool
uv tool install git+https://github.com/deepset-ai/haystack-enterprise-sdk.git

# Or add it as a dependency of your project
uv add git+https://github.com/deepset-ai/haystack-enterprise-sdk.git
```

## Usage

```bash
haystack-enterprise --help
```

### Authentication

```bash
haystack-enterprise login
haystack-enterprise logout
```

### Files

```bash
# Upload a folder to a workspace
haystack-enterprise upload ./my-files

# List files in a workspace
haystack-enterprise list-files

# Download files to your local machine
haystack-enterprise download
```

### Pipelines

```bash
# Validate a local pipeline against the platform
haystack-enterprise validate ./pipeline.py

# Run a local pipeline in the platform sandbox
haystack-enterprise run ./pipeline.py

# Deploy a local pipeline as a service deployment
haystack-enterprise deploy ./pipeline.py my-service

# Check the status of a service deployment
haystack-enterprise service-status my-service
```

Pass `--verbose` to any command for INFO/DEBUG logs, and `<command> --help` for its arguments.

## Development

```bash
# Install uv if you don't have it
pip install uv

# Sync all dependencies (including dev dependencies)
uv sync --all-groups

# Run the CLI from source
uv run haystack-enterprise --help
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.

## Licenses

The SDK is licensed under Apache 2.0, see [LICENSE](LICENSE).

Some bundled libraries are licensed under the [MPL 2.0 license](https://www.mozilla.org/en-US/MPL/2.0/):

- [tqdm](https://github.com/tqdm/tqdm) for progress bars
- [pathspec](https://github.com/cpburnz/python-pathspec) for pattern matching file paths
- [certifi](https://github.com/certifi/python-certifi) for validating trustworthiness of SSL certificates
