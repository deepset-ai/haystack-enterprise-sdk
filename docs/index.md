<p align="center">
  <a href="https://cloud.deepset.ai/"><img src="_images/logo.svg" alt="Haystack Enterprise SDK" width="420"></a>
</p>

[![Coverage badge](https://github.com/deepset-ai/haystack-enterprise-sdk/raw/python-coverage-comment-action-data/badge.svg)](https://github.com/deepset-ai/haystack-enterprise-sdk/tree/python-coverage-comment-action-data)
[![Tests](https://github.com/deepset-ai/haystack-enterprise-sdk/actions/workflows/continuous-integration.yml/badge.svg)](https://github.com/deepset-ai/haystack-enterprise-sdk/actions/workflows/continuous-integration.yml)
[![Compliance Checks](https://github.com/deepset-ai/haystack-enterprise-sdk/actions/workflows/compliance.yml/badge.svg)](https://github.com/deepset-ai/haystack-enterprise-sdk/actions/workflows/compliance.yml)

The **Haystack Enterprise SDK** takes the Haystack pipelines and agents you build locally and moves them
onto [Haystack Enterprise Platform](https://docs.cloud.deepset.ai/) — validated, run and deployed from
your terminal, without leaving your editor. The platform is where you build production-ready AI
applications and manage them across the full lifecycle, from prototyping to large-scale production.

> **Experimental.** This SDK is under active development. APIs and CLI commands may change without
> notice.

## What it does

**Deploy pipelines.** Build a pipeline locally with Haystack, then check it, run it and serve it with
three commands that all read the same file and apply the same transform — so what you validate is what
you run, and what you run is what you deploy.

```shell
haystack-enterprise validate pipeline.py
haystack-enterprise run pipeline.py --query "What is deepset?"
haystack-enterprise deploy pipeline.py my-service
```

A deployed service is served over an OpenAI-compatible chat-completions endpoint, so any OpenAI client
can call it. Add `--share` to get a chat UI link you can send to someone.

**Move files.** Upload documents and their metadata into a workspace in bulk, from the CLI or from
Python.

```shell
haystack-enterprise upload ./my-files
```

## Get started

- **[Install](get-started/install.md)** — get the `haystack-enterprise` command.
- **[Quickstart](get-started/quickstart.md)** — deploy your first pipeline in five minutes.
- **[Configuration](get-started/configuration.md)** — API keys, workspaces and `.env` precedence.

## Guides

- **[Deploy a pipeline](guides/deploy-a-pipeline.md)** — the full flow, managed vs serverless services,
  prototype links, and the `io.yaml` file that pins input/output mapping.
- **[Upload files](guides/upload-files.md)** — upload sessions, folder structure and file metadata.
- **[Troubleshooting](guides/troubleshooting.md)** — what the SDK's errors mean and what to do about them.
- **[CLI reference](reference/cli.md)** — every command and flag.

## Coming from `deepset-cloud-sdk`?

The package, the CLI command and the config file path all changed. See
[Migrating from deepset-cloud-sdk](about/migrating.md).

## Interested in Haystack?

Haystack Enterprise Platform is powered by [Haystack](https://haystack.deepset.ai/), the open source
framework for building end-to-end AI pipelines and agents
([GitHub](https://github.com/deepset-ai/haystack)).
