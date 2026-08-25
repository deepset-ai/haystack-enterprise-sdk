# Troubleshooting

What the SDK's errors mean, and what usually fixes them.

Put `--verbose` (or `-v`) before the command name — `haystack-enterprise --verbose deploy …` — to see
the SDK's INFO and DEBUG logs. That is the first thing to try when a message is not specific enough. It
is a global option, so it is rejected when placed after the command name.

## Configuration

### "API key is required"

Nothing supplied an `API_KEY`. Run `haystack-enterprise login`, set the environment variable, or pass
`--api-key`. See [Configuration](../get-started/configuration.md).

If you *have* logged in and still see this, a local `.env` in the directory you are running from may be
shadowing the global one — the local file takes precedence.

### Commands hit the wrong environment

`API_URL` resolved to something unexpected. The same precedence applies: an `API_URL` in your shell
beats a local `.env`, which beats the global one. A trailing `/api/v1` is stripped automatically, so
pasting a full endpoint URL is safe.

### `haystack-enterprise: command not found`

The scripts directory of the environment you installed into is not on your PATH. Use
`python -m haystack_enterprise_sdk.cli` instead — see [Install](../get-started/install.md).

## Pipelines

### `PipelineTransformError`

Your pipeline could not be loaded or turned into deployable YAML. Usually one of:

- **The file does not import cleanly.** It is loaded in your project's interpreter, so any import error,
  missing dependency or missing secret surfaces here. A project `.env` is loaded first for exactly this
  reason, but only variables that are not already set.
- **`ModuleNotFoundError: No module named 'haystack'`.** No `.venv` or `venv` was found above the pipeline
  file, so the CLI fell back to its own interpreter, which does not have Haystack. Pass `--python` with
  the interpreter that does, or install the `deploy` extra — see [Install](../get-started/install.md).
- **The file defines more than one pipeline.** Pick one with `--entrypoint`.
- **The wrong interpreter was auto-detected.** Point at the right one with `--python`.

### `PipelineValidationError`

The platform found blocking (ERROR) issues in the generated YAML. The message lists them. Run
`haystack-enterprise validate pipeline.py` to iterate quickly without deploying.

A common cause is a component that exists in one Haystack version but not another. Validation runs
against the `haystack-ai` version *your pipeline pins*; if the platform declines that version you get a
warning saying the pin went unhonored, which means version-specific problems may have been missed.

### "You need to connect at least one of the inputs (`query` or `messages`)"

The platform will not serve a pipeline it cannot route a query into. Either rename your sockets to
something conventional (`query`, `answers`, `replies`, `documents`), which the SDK maps by inference, or
pin the mapping in a `<pipeline>.io.yaml`. See
[Deploy a pipeline](deploy-a-pipeline.md#the-io-config-file).

### `PipelineRunError`

The sandbox run failed — bad configuration, bad inputs, or a server error. The message carries what the
platform returned.

Note that `run` is **not** pinned to your pipeline's `haystack-ai` version; the sandbox uses the
platform's own Haystack. So a version-specific failure can appear in `run` but not in a deployed
service, or the reverse. Use `validate` for the version-accurate check.

Transient failures (network errors, timeouts, 429, 5xx) are retried twice by default. Configuration and
input errors always fail immediately. `--retries 0` disables retrying.

## Deployments

### `ServiceNotFoundError`

The named service does not exist and you passed `--no-create`. Check the name with
`haystack-enterprise service-status <name>`, or drop `--no-create` to have it created.

### `DeploymentFailedError`

The rollout reached `DEPLOYMENT_FAILED`. The API does not expose a failure reason, so the error points
you at the service in Haystack Enterprise Platform, where the deployment logs are.

The usual cause is a dependency that installs locally but not in the deployed revision. Check the
`dependencies:` pin in your io-config — it *replaces* the automatic `haystack-ai` pin rather than adding
to it, so if you set it you must include `haystack-ai` yourself.

If you interrupted the command with Ctrl-C, the rollout continued on the platform; check with
`service-status`.

### `FailedToCreateSharedPrototypeError`

The share link could not be created. `--share` needs a deployed, active service, so it cannot be
combined with `--skip-activation`.

If the link is created but the CLI warns the chat UI may not render output well, the platform did not
classify your pipeline as a chat pipeline. Set `pipeline_output_type: chat` in the io-config.

### An io-config key was ignored

Unrecognised top-level keys are passed through to the pipeline config unchanged, with a note. That is
deliberate — it lets you set a platform key newer than your installed SDK. But it also means a typo like
`session_storge: true` passes through silently. The note is the only signal, so read it.

## Uploads

### Files uploaded but do not appear

Ingestion lags the upload. Check the session rather than guessing:

```shell
haystack-enterprise list-upload-sessions
haystack-enterprise get-upload-session <session-id>
```

An upload session left open expires after 24 hours, and files only start ingesting when it closes.

### Some files were skipped

Only `.txt` and `.pdf` are uploaded by default, and unsupported types are skipped rather than failing
the run. Name the types you want with `--use-type`. See [Upload files](upload-files.md#file-types).

### Duplicates in the workspace

The default write mode is `KEEP`, which uploads a same-named file alongside the existing one. Use
`--write-mode OVERWRITE` to replace instead.
