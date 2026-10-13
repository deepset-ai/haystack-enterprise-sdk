---
name: haystack-enterprise-get-started
description: |-
  Hand your coding agent this playbook to set up Haystack Enterprise: install the
  haystack-enterprise CLI, authenticate it, and connect the deepset MCP server once
  for use across projects.
title: 'Onboard your agent to Haystack Enterprise'
url: https://raw.githubusercontent.com/deepset-ai/haystack-enterprise-sdk/main/get-started.md
---

# Set up Haystack Enterprise for your AI coding agent

Use this playbook to prepare the current agent for Haystack Enterprise Platform. Install the CLI, log
in, and connect the MCP server once per machine. Pipelines and services are created later, when a task
needs them.

## How to run

Run sections 1 and 2 once per machine. Run section 3 once per coding agent. If a step is blocked,
record why and continue with the remaining steps.

Perform actions yourself when terminal or file access is available. Global or user-scoped installation
is the default. Pause only for steps the user must do: creating an API key, typing it in, and approving
MCP tools. Do not merely return commands for the user to copy.

Never ask the user to paste an API key into the chat, and never print one back.

## 1. Install the CLI

Check for an existing installation:

```sh
haystack-enterprise --version
```

If the CLI is missing, install it. The script installs [uv](https://docs.astral.sh/uv/) if needed, then
installs the CLI as a uv tool:

```sh
curl -fsSL https://raw.githubusercontent.com/deepset-ai/haystack-enterprise-sdk/main/install.sh | bash
```

On Windows, install uv first, then run `uv tool install haystack-enterprise-sdk`.

To upgrade an existing installation, run `uv tool upgrade haystack-enterprise-sdk`.

If `haystack-enterprise` is still not found in your current shell, call it by its full path,
`"$(uv tool dir --bin)/haystack-enterprise"`, for the rest of this playbook.

## 2. Log in

Check whether the CLI already has working credentials. This command never prompts:

```sh
haystack-enterprise whoami
```

Exit code 0 means the credentials work. Note the API URL and default workspace it prints.

The MCP server in section 3 needs an **API key**. A browser login session does not give one. Check
whether a key is configured: either the `API_KEY` environment variable is set, or
`~/.haystack-enterprise/.env` has an `API_KEY=` line. Check that the line exists without printing its value.

If `whoami` fails or no API key is configured:

1. Ask the user to create an API key at <https://cloud.deepset.ai/settings/api-keys>.
2. Ask the user to run this in their own terminal. In Claude Code, they can type it at the prompt with a
   leading `!`:

   ```sh
   haystack-enterprise login --no-browser
   ```

   It asks for the platform URL, the API key (hidden input), and the default workspace. It then writes
   them to `~/.haystack-enterprise/.env`, readable only by the user.

3. Pause until the user confirms, then run `haystack-enterprise whoami` again.

Do not run `haystack-enterprise login` yourself without flags. It waits several minutes for a human in a
browser.

If the user asks you to use a key they already exported as `API_KEY`, run
`haystack-enterprise login --api-key "$API_KEY" --workspace-name <workspace>` instead. This writes the
file without prompting.

## 3. Connect the deepset MCP server

The [deepset MCP server](https://pypi.org/project/deepset-mcp/) lets the agent search the documentation,
inspect pipelines, indexes, and deployments, read logs, and more. It runs locally over stdio with
`uvx deepset-mcp`. uv downloads the package and a suitable Python on first start.

Collect its settings from the CLI configuration:

| MCP variable | Value |
| --- | --- |
| `DEEPSET_API_KEY` | `API_KEY` from the environment or `~/.haystack-enterprise/.env` |
| `DEEPSET_WORKSPACE` | `DEFAULT_WORKSPACE_NAME` from the same place |
| `DEEPSET_API_URL` | Set it only if `API_URL` is not `https://api.cloud.deepset.ai`, and then to `<API_URL>/api`. The MCP server expects the `/api` suffix and the CLI does not. |

Read these values in a shell. Do not print the key. For example:

```sh
set -a; . ~/.haystack-enterprise/.env; set +a
```

Name the server `deepset`. Before changing any MCP configuration, inspect it. If a `deepset` entry
already exists, update that entry. Merge it in without replacing unrelated settings. Use only one route
per client.

The entry stores the API key in the client's configuration file. Tell the user which file holds it.

### Claude Code

```sh
claude mcp list
claude mcp add deepset --scope user \
  -e DEEPSET_API_KEY="$API_KEY" \
  -e DEEPSET_WORKSPACE="$DEFAULT_WORKSPACE_NAME" \
  -- uvx deepset-mcp
```

### Codex CLI

```sh
codex mcp list
codex mcp add deepset \
  --env DEEPSET_API_KEY="$API_KEY" \
  --env DEEPSET_WORKSPACE="$DEFAULT_WORKSPACE_NAME" \
  -- uvx deepset-mcp
```

### Cursor, Windsurf, Gemini CLI

Merge this into the client's file:

- Cursor: `~/.cursor/mcp.json`
- Windsurf: `~/.codeium/windsurf/mcp_config.json`
- Gemini CLI: `~/.gemini/settings.json`

```json
{
  "mcpServers": {
    "deepset": {
      "command": "uvx",
      "args": ["deepset-mcp"],
      "env": {
        "DEEPSET_API_KEY": "<API_KEY>",
        "DEEPSET_WORKSPACE": "<DEFAULT_WORKSPACE_NAME>"
      }
    }
  }
}
```

After saving, refresh the client's MCP list. In Windsurf, refresh the MCP configuration. In Gemini CLI,
run `/mcp list`.

### VS Code with GitHub Copilot

Run **MCP: Open User Configuration** and merge this into the file it opens:

```json
{
  "servers": {
    "deepset": {
      "type": "stdio",
      "command": "uvx",
      "args": ["deepset-mcp"],
      "env": {
        "DEEPSET_API_KEY": "<API_KEY>",
        "DEEPSET_WORKSPACE": "<DEFAULT_WORKSPACE_NAME>"
      }
    }
  }
}
```

Then run **MCP: List Servers** and start `deepset`.

### OpenCode

Merge this into `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "deepset": {
      "type": "local",
      "command": ["uvx", "deepset-mcp"],
      "environment": {
        "DEEPSET_API_KEY": "<API_KEY>",
        "DEEPSET_WORKSPACE": "<DEFAULT_WORKSPACE_NAME>"
      }
    }
  }
}
```

### Other clients

Configure a stdio server with command `uvx`, arguments `["deepset-mcp"]`, and the environment variables
above.

### Verify

1. Call the `search_docs` tool.
2. Call the read-only `list_pipelines` tool.
3. If either is missing, refresh the client's MCP tools or server list. Restart or reload the client only
   if refreshing does not load the server.

If the server fails to start, run `uvx deepset-mcp --list-tools` in a terminal to see the error.

Keep human confirmation on for every MCP tool that changes something, such as creating, deploying, or
deleting. The server acts with the full access of the API key.

## 4. Before you work with pipelines

Fetch <https://raw.githubusercontent.com/deepset-ai/haystack-enterprise-sdk/main/AGENTS.md> and follow it
when you drive the CLI or SDK. It explains which commands are safe to run unattended. It also explains
why to use `--output` instead of `>`, how to map pipeline inputs and outputs in `<pipeline>.io.yaml`,
and what the exit codes mean.

The cheapest check for a pipeline file needs no credentials:

```sh
haystack-enterprise deploy pipeline.py SERVICE --dry-run --output out.yaml
```

## Completion

Report only state you verified:

```text
Haystack Enterprise agent setup is ready
CLI: <version>, <whoami: API URL, default workspace>
MCP: <connected|blocked: reason>, deepset-mcp via uvx
MCP config: <path|managed by claude mcp|codex mcp>
MCP check: <list_pipelines succeeded|blocked: reason>
Reload: <not needed|completed>
```

Sources:

- https://github.com/deepset-ai/haystack-enterprise-sdk
- https://docs.cloud.deepset.ai/reference/sdk-overview
- https://pypi.org/project/deepset-mcp/
