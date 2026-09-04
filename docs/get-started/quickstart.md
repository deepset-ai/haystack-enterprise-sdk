# Quickstart

Take a Haystack pipeline you already have on your machine and get it serving on Haystack Enterprise
Platform. Five minutes, four commands.

You need the CLI ([Install](install.md)) and a local Python file that defines a Haystack pipeline.

The pipeline is loaded with the interpreter of the nearest `.venv` or `venv` directory above the file.
If your project's environment lives somewhere else, add `--python /path/to/python` to the `validate`,
`run` and `deploy` commands below.

## 1. Log in

```shell
haystack-enterprise login
```

Paste your API key and pick a default workspace. This is stored once — see
[Configuration](configuration.md).

## 2. Check it is deployable

```shell
haystack-enterprise validate pipeline.py
```

`validate` applies the same transform a deploy would, then asks the platform to check the result —
without deploying anything. It prints warnings and errors, and exits non-zero if any are blocking. When
it says `Pipeline is valid.`, you are good.

The first check against a given `haystack-ai` version is slow, because the platform builds an
environment for it. Later checks are fast.

## 3. See real output

```shell
haystack-enterprise run pipeline.py --query "What is deepset?"
```

`run` executes the pipeline in the platform sandbox and prints the results in your terminal, still
without deploying. Use it to confirm the output looks right before you commit to a service.

## 4. Deploy

```shell
haystack-enterprise deploy pipeline.py my-service
```

This creates the service if it does not exist, pushes your pipeline as a new revision, and activates
it. You may be asked which socket receives the query and which is the main output — and nothing at all
if your socket names already make that obvious.

When it is serving, `deploy` prints an OpenAI-compatible chat-completions endpoint:

```
POST https://api.cloud.deepset.ai/api/v1/workspaces/<workspace>/deployments/<deployment-id>/chat/completions
```

Any OpenAI client works — point its `base_url` at everything up to and including
`/deployments/<deployment-id>`.

Check on it any time:

```shell
haystack-enterprise service-status my-service
```

## Then iterate

Edit `pipeline.py`, re-run steps 2–4. Each deploy creates a new revision, so you can roll changes out
incrementally.

## Next steps

- [Deploy a pipeline](../guides/deploy-a-pipeline.md) — the full flow, including managed services,
  sharing a prototype link, and the `io.yaml` file that pins input/output mapping.
- [Upload files](../guides/upload-files.md) — get documents into a workspace.
- [CLI reference](../reference/cli.md) — every command and flag.
