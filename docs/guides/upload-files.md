# Upload files

Get documents and their metadata into a Haystack Enterprise Platform workspace, from the CLI or from
Python. This is the fastest route when you have many files.

Before you start, log in — see [Configuration](../get-started/configuration.md).

## Upload a folder

```shell
haystack-enterprise upload ./my-files
```

By default this uploads the `.txt` and `.pdf` files in the folder, but not its subfolders. To include
subfolders, add `--recursive`. For other file types, see [File types](#file-types).

Uploads happen through a **session**: the SDK opens one, sends your files, and closes it, which is what
starts ingestion. A session you leave open expires after 24 hours.

After the upload finishes, files take a little while to appear in the platform. If you already have a
pipeline deployed, it may not see the new files immediately.

## Folder structure

No particular structure is required. If several files share a name, all of them are uploaded by
default. Change that with `--write-mode`:

| Mode | Effect |
| --- | --- |
| `KEEP` (default) | Uploads the file and keeps both copies in the workspace. |
| `OVERWRITE` | Replaces the file already in the workspace. |
| `FAIL` | Fails the upload rather than creating a duplicate. |

```shell
haystack-enterprise upload ./my-files --write-mode OVERWRITE
```

`OVERWRITE` is the one to use when you want the workspace to mirror a local folder, without deleting
files by hand first.

## File types

Haystack Enterprise Platform supports `.csv`, `.docx`, `.html`, `.json`, `.md`, `.txt`, `.pdf`,
`.pptx`, `.xlsx` and `.xml`. Only `.txt` and `.pdf` are uploaded unless you say otherwise. Name the
types you want with `--use-type`, once per type:

```shell
haystack-enterprise upload ./my-files --use-type .md --use-type .pdf --use-type .docx
```

Unsupported file types in the folder are skipped rather than failing the upload.

## Metadata

To attach metadata to a file, put a JSON file next to it with the same name plus `.meta.json`:

```
my-files/
  report.pdf
  report.pdf.meta.json
  notes.txt
  notes.txt.meta.json
```

Each metadata file holds a flat JSON object:

```json
{"meta_key1": "value1", "meta_key2": "value2"}
```

See [`example.txt.meta.json`](../examples/data/example.txt.meta.json) for a working example.

## Upload from Python

### From a folder

```python
from pathlib import Path

from haystack_enterprise_sdk.workflows.sync_client.files import upload

upload(
    paths=[Path("./my-files")],
    # workspace_name="my_workspace",  # defaults to DEFAULT_WORKSPACE_NAME
    blocking=True,       # wait until the files show up in the platform
    timeout_s=300,       # how long to wait when blocking
    show_progress=True,
    recursive=True,      # include subfolders
    desired_file_types=[".csv", ".docx", ".html", ".json", ".md", ".txt", ".pdf", ".pptx", ".xlsx", ".xml"],
)
```

### Raw text

Useful when you process text in Python and want to upload the result rather than a file on disk:

```python
from haystack_enterprise_sdk.models import HaystackEnterpriseFile
from haystack_enterprise_sdk.workflows.sync_client.files import upload_texts

upload_texts(
    files=[
        HaystackEnterpriseFile(
            name="example.txt",
            text="this is text",
            meta={"key": "value"},  # optional
        )
    ],
    blocking=True,
    timeout_s=300,
)
```

Authentication follows the usual cascade: pass `api_key=` explicitly, set the `API_KEY` environment
variable, or run `haystack-enterprise login` once. See
[Configuration](../get-started/configuration.md).

A runnable version of both examples is in
[`examples/sdk/upload.py`](https://github.com/deepset-ai/haystack-enterprise-sdk/blob/main/docs/examples/sdk/upload.py).

## Check on an upload

```shell
haystack-enterprise list-upload-sessions          # every session, including closed ones
haystack-enterprise get-upload-session <id>       # one session's status
haystack-enterprise list-files                    # what is actually in the workspace
```

See the [CLI reference](../reference/cli.md) for the filters `list-files` accepts.
