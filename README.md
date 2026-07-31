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

# Run a local pipeline in the platform sandbox (shows a spinner with the elapsed time while it waits)
haystack-enterprise run ./pipeline.py

# Transient failures (network errors, timeouts, 429, 5xx) are retried twice by default; 0 disables it.
# Config and input errors always fail immediately.
haystack-enterprise run ./pipeline.py --retries 0

# Deploy a local pipeline as a service deployment (reused if it exists, otherwise created serverless)
haystack-enterprise deploy ./pipeline.py my-service

# Create a managed (provisioned) service instead, with explicit sizing
haystack-enterprise deploy ./pipeline.py my-service --managed --cpu 2 --memory 4Gi

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
