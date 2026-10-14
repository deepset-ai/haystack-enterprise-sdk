---
name: haystack-enterprise-get-started
description: |-
  Hand your coding agent this playbook to set up Haystack Enterprise: install the
  haystack-enterprise CLI and log in, so it can develop pipelines locally and
  deploy them as services.
title: 'Onboard your agent to Haystack Enterprise'
url: https://raw.githubusercontent.com/deepset-ai/haystack-enterprise-sdk/main/get-started.md
---

# Set up Haystack Enterprise for your AI coding agent

Use this playbook to prepare the current agent for Haystack Enterprise Platform. Install the CLI and log
in once per machine. Pipelines and services are created later, when a task needs them.

## How to run

Run sections 1 and 2 once per machine. If a step is blocked, record why and continue with the remaining
steps.

Perform actions yourself when terminal or file access is available. Global or user-scoped installation
is the default. Pause only for the step the user must do: logging in in the browser. Do not merely
return commands for the user to copy.

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

Exit code 0 means the credentials work. Note the API URL and default workspace it prints, and skip to
section 3.

If `whoami` fails, ask the user to log in. They run this in their own terminal; in Claude Code, they can
type it at the prompt with a leading `!`:

```sh
haystack-enterprise login
```

It opens the platform in a browser, where they log in and pick a workspace. On a remote machine without a
browser, use `haystack-enterprise login --device` instead; it prints a code to confirm on any device.
Pause until the user confirms, then run `haystack-enterprise whoami` again.

Do not run `haystack-enterprise login` yourself without flags. It waits several minutes for a human in a
browser.

A browser login is for people. For CI and other unattended use, use an API key from
<https://cloud.deepset.ai/settings/api-keys> instead: set `API_KEY`, or run
`haystack-enterprise login --api-key "$API_KEY" --workspace-name <workspace>`, which writes it without
prompting. An API key always wins over a login session.

## 3. Before you work with pipelines

Fetch <https://raw.githubusercontent.com/deepset-ai/haystack-enterprise-sdk/main/AGENTS.md> and follow it
when you drive the CLI or SDK. It explains which commands are safe to run unattended. It also explains
why to use `--output` instead of `>`, how to map pipeline inputs and outputs in `<pipeline>.io.yaml`,
and what the exit codes mean.

Work outward from the cheapest check. The first needs no credentials:

```sh
haystack-enterprise deploy pipeline.py SERVICE --dry-run --output out.yaml
haystack-enterprise validate pipeline.py
haystack-enterprise run pipeline.py --query "..."
haystack-enterprise deploy pipeline.py SERVICE
haystack-enterprise service-status SERVICE
```

A zero exit from `deploy` does not mean the service is serving; `service-status` confirms it.

## Completion

Report only state you verified:

```text
Haystack Enterprise agent setup is ready
CLI: <version>
Login: <whoami: API URL, login session or API key, default workspace|blocked: reason>
```

Sources:

- https://github.com/deepset-ai/haystack-enterprise-sdk
- https://docs.cloud.deepset.ai/reference/sdk-overview
