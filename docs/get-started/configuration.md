# Configuration

The SDK needs three things to talk to Haystack Enterprise Platform: an API key, an API URL, and a
default workspace. The easiest way to set all three is `haystack-enterprise login`.

## Log in

```shell
haystack-enterprise login
```

It asks for the platform URL (press enter for `https://api.cloud.deepset.ai`), your API key, and a
default workspace name (press enter for `default`), then writes them to a global config file at
`~/.haystack-enterprise/.env`.

Get an API key from [API Keys](https://cloud.deepset.ai/settings/api-keys) in Haystack Enterprise
Platform.

To remove that file again:

```shell
haystack-enterprise logout
```

## Settings

| Variable | What it is | Default |
| --- | --- | --- |
| `API_KEY` | Your Haystack Enterprise Platform API key. **Required** — there is no default, and every command fails without it. | — |
| `API_URL` | Base URL of the platform API. A trailing version segment (`/api/v1`, `/v2`) is stripped, so pasting a full URL works. | `https://api.cloud.deepset.ai` |
| `DEFAULT_WORKSPACE_NAME` | Workspace used when a command takes no `--workspace-name`. | — (`login` suggests `default`) |
| `ASYNC_CLIENT_TIMEOUT` | Timeout in seconds for the async client. | `300` |

## Where settings come from

Settings are resolved in this order — the first one that supplies a value wins:

1. **Explicit arguments** — a CLI flag such as `--workspace-name`, or a parameter passed in Python.
2. **Environment variables** set in your shell.
3. **A local `.env`** in the directory you run the command from.
4. **The global `~/.haystack-enterprise/.env`** written by `login`. This *supplements* the local file
   rather than replacing it: a key missing from the local `.env` is still picked up from the global one.
5. **Built-in defaults**, for the two settings that have them.

So a project can override just the workspace in its own `.env` and keep using the API key from `login`.
If a local `.env` exists when you run `login`, the CLI points out that it will take precedence over the
global file it is about to write.

## Configuring in Python

Pass credentials directly and the environment is not consulted at all:

```python
from haystack_enterprise_sdk import PipelineClient

client = PipelineClient(api_key="...", api_url="https://api.cloud.deepset.ai")
```

Omit them and the same cascade above applies.

## Using a different environment

To point at a non-production platform, set `API_URL` — in your shell, in a local `.env`, or by
answering `n` to the URL question during `login`:

```bash
export API_URL=https://api.dev.cloud.dpst.dev
```
