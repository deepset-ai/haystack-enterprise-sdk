# CLI reference

Every command, with the flags you are most likely to reach for. Run `haystack-enterprise <command>
--help` for the exhaustive list and the current defaults.

> If `haystack-enterprise` is not on your PATH — most often on Windows — replace it with
> `python -m haystack_enterprise_sdk.cli` in every example below. See [Install](../get-started/install.md).

Three flags are accepted by nearly every command: `--api-key`, `--api-url` and `--workspace-name`. They
override whatever [Configuration](../get-started/configuration.md) resolved.

`--verbose` / `-v` turns on the SDK's INFO and DEBUG logs. It is a global option, so it goes *before*
the command name — `haystack-enterprise --verbose upload ./my-files` works,
`haystack-enterprise upload --verbose ./my-files` is rejected.

## Commands at a glance

| Command | What it does |
| --- | --- |
| [`login`](#login) | Store your API key, API URL and default workspace. |
| [`logout`](#logout) | Delete the stored configuration. |
| [`upload`](#upload) | Upload a file or folder to a workspace. |
| [`download`](#download) | Download files from a workspace. |
| [`list-files`](#list-files) | List files in a workspace. |
| [`list-upload-sessions`](#list-upload-sessions) | List upload sessions, including closed ones. |
| [`get-upload-session`](#get-upload-session) | Show one upload session's status. |
| [`validate`](#validate) | Check that a local pipeline is deployable. |
| [`run`](#run) | Run a local pipeline in the platform sandbox. |
| [`deploy`](#deploy) | Deploy a local pipeline as a service. |
| [`service-status`](#service-status) | Show a service deployment's status. |

There is also a top-level `--version`, which prints the installed SDK version and exits, and
`--verbose`, described above.

## Authentication

### `login`

Prompts for the platform URL, your API key and a default workspace, then writes them to
`~/.haystack-enterprise/.env`.

```shell
haystack-enterprise login
```

### `logout`

Deletes that file.

```shell
haystack-enterprise logout
```

## Files

### `upload`

```shell
haystack-enterprise upload ./my-files
```

| Flag | Effect |
| --- | --- |
| `--recursive` | Include subfolders. Off by default. |
| `--use-type` | Which file extensions to upload. Repeat per type. Defaults to `.txt` and `.pdf`. |
| `--write-mode` | `KEEP` (default), `OVERWRITE` or `FAIL` when a name already exists. |
| `--blocking` / `--no-blocking` | Wait until the files appear in the platform. On by default. |
| `--timeout-s` | How long to wait when blocking. |
| `--show-progress` / `--no-show-progress` | Progress bar. On by default. |
| `--enable-parallel-processing` | Upload in parallel. |

See [Upload files](../guides/upload-files.md) for the concepts.

### `download`

```shell
haystack-enterprise download --workspace-name my-workspace
```

| Flag | Effect |
| --- | --- |
| `--file-dir` | Where to write the files locally. |
| `--name` | Only files whose name matches. |
| `--odata-filter` | Only files matching an OData metadata filter. |
| `--include-meta` | Also download each file's metadata. |
| `--batch-size` | Files fetched per request. |

### `list-files`

```shell
haystack-enterprise list-files
haystack-enterprise list-files --name "report.pdf"
haystack-enterprise list-files --odata-filter "key eq 'value'"
```

| Flag | Effect |
| --- | --- |
| `--name` | Filter by file name. |
| `--odata-filter` | Filter by metadata, OData syntax. |
| `--batch-size` | Files fetched per request. |

### `list-upload-sessions`

Every session for the workspace, closed ones included.

```shell
haystack-enterprise list-upload-sessions
haystack-enterprise list-upload-sessions --is-expired
```

### `get-upload-session`

One session's status, by ID.

```shell
haystack-enterprise get-upload-session <session-id>
```

## Pipelines

All three pipeline commands take the path to a local Python file as their first argument, and share
`--entrypoint` (which pipeline in the file), `--python` (which interpreter loads it), `--io-config` and
`--skip-io-validation`.

### `validate`

```shell
haystack-enterprise validate pipeline.py
```

Transforms the pipeline and asks the platform to check the result, without deploying. Exits non-zero on
blocking errors.

### `run`

```shell
haystack-enterprise run pipeline.py --query "What is deepset?"
```

| Flag | Effect |
| --- | --- |
| `--query` | Text routed to the sockets mapped to the `query` input. |
| `--inputs` | Explicit run inputs as JSON, literal or `@file.json`. Wins over `--query`. |
| `--set` | Set a single input value, e.g. `--set token=abc` or `--set prompt=@file.md`. |
| `--include-outputs-from` | Limit results to specific components. Repeatable. |
| `--output` | Write the result JSON to a file. |
| `--retries` | Retries for transient failures. Default 2; `0` disables. |

### `deploy`

```shell
haystack-enterprise deploy pipeline.py my-service
```

| Flag | Effect |
| --- | --- |
| `--managed` | Create a provisioned service rather than serverless. Required for any sizing flag. |
| `--service-level`, `--cpu`, `--memory`, `--gpu`, `--min-replicas`, `--max-replicas`, `--idle-timeout` | Sizing. Managed services only. |
| `--create` / `--no-create` | Require that the service does not / does already exist. |
| `--comment`, `-m` | Revision comment. Auto-generated from git when omitted. |
| `--skip-activation` | Push the revision without rolling it out. |
| `--skip-validation` | Skip the pre-deploy check. |
| `--dry-run`, `--output` | Print or save the transformed YAML without deploying. Needs no credentials. |
| `--share`, `--share-expiration-days`, `--share-login-required` | Create a shareable chat UI link. |

See [Deploy a pipeline](../guides/deploy-a-pipeline.md) for the full flow.

### `service-status`

```shell
haystack-enterprise service-status my-service
```

## Python API

The generated reference for the Python clients is under **API Docs** in the navigation.
