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
Integration tests against prod are run manually, via the *CD - Prod Integration Tests* workflow.

## Releasing

The package is published to PyPI as `haystack-enterprise-sdk` by the *PyPI Release* workflow, using
[trusted publishing](https://docs.pypi.org/trusted-publishers/) — there is no API token anywhere in CI.

The version lives in exactly one place, `[project] version` in `pyproject.toml`. Between releases, `main`
carries the last released version.

1. Open a PR titled `chore: release X.Y.Z` that sets `[project] version` in `pyproject.toml` and runs
   `uv lock`, so the root entry in `uv.lock` matches. Write the version in normalised PEP 440 form
   (`1.2.3rc1`, not `1.2.3-rc1`).
2. Merge to `main` through the merge queue.
3. Create a GitHub Release with a new tag `vX.Y.Z` on the bump commit, and click *Generate release notes*.
4. Watch the *PyPI Release* run. It refuses to upload if `uv.lock` is stale, if the tag disagrees with
   `pyproject.toml`, or if the built wheel does not install and run.
5. Verify: `uvx --from haystack-enterprise-sdk haystack-enterprise --version`.

To rehearse without touching PyPI, run the *PyPI Release* workflow manually (Actions → PyPI Release → Run
workflow). A manual run publishes a throwaway `.devN` build to TestPyPI and can never reach PyPI.

**Rollback:** a version can never be re-uploaded to PyPI. Yank it (Manage → Yank on the PyPI project) and
release `X.Y.Z+1`.

## Software design

Have a look at this [README](/haystack_enterprise_sdk/README.md) to get an overview of the software design.
