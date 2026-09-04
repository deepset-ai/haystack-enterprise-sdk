# Install

The Haystack Enterprise SDK needs Python 3.10 or newer.

## Install the CLI

The SDK is published to PyPI as
[`haystack-enterprise-sdk`](https://pypi.org/project/haystack-enterprise-sdk/). Install the CLI as a
standalone tool with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install haystack-enterprise-sdk
```

That puts the `haystack-enterprise` command on your PATH. Check it:

```bash
haystack-enterprise --version
```

## Add it to a project

To use the Python API rather than the CLI, add the SDK as a dependency instead:

```bash
uv add haystack-enterprise-sdk
```

`pip install haystack-enterprise-sdk` works too.

The import name is `haystack_enterprise_sdk`:

```python
from haystack_enterprise_sdk import PipelineClient
```

## Do you need Haystack installed too?

Usually not. `validate`, `run` and `deploy` load your pipeline in a subprocess using **your project's**
interpreter — an auto-detected virtualenv near the pipeline file, or whatever you pass to `--python`. So
Haystack has to be installed where your pipeline lives, not where the CLI lives.

The exception is when there is no separate project environment to detect, and the CLI environment *is*
the pipeline environment. Install the `deploy` extra for that case:

```bash
uv tool install "haystack-enterprise-sdk[deploy]"
```

If the CLI is installed in an environment without Haystack and no project environment is found next to
the pipeline file, `validate` fails with `ModuleNotFoundError: No module named 'haystack'`. Either install
the extra, or point the CLI at the interpreter that has your pipeline's dependencies with
`--python /path/to/python`.

## If `haystack-enterprise` is not found

The command lives in the scripts directory of the environment you installed into. If that directory is
not on your PATH — most often on Windows — call the CLI through the interpreter instead:

```shell
python -m haystack_enterprise_sdk.cli --help
```

Everything in these docs works the same way: replace `haystack-enterprise` with
`python -m haystack_enterprise_sdk.cli`.

## Next steps

- [Quickstart](quickstart.md) — log in and deploy your first pipeline.
- [Configuration](configuration.md) — API keys, workspaces and `.env` precedence.

## Install for development

To work on the SDK itself, clone the repository and sync all dependency groups:

```bash
git clone https://github.com/deepset-ai/haystack-enterprise-sdk.git
cd haystack-enterprise-sdk
uv sync --all-groups
```

Run the CLI from source with:

```bash
uv run haystack-enterprise --help
```

See [CONTRIBUTING.md](https://github.com/deepset-ai/haystack-enterprise-sdk/blob/main/CONTRIBUTING.md)
for the full contributor workflow.
