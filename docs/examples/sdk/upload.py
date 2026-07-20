## Authentication
## --------------
## Either explicitly pass an api_key to the `upload` function or set the environment variable
## `API_KEY` to your API key.
## By running `haystack-enterprise login` you can also store your API key globally on your machine.
## This omits the `api_key`` parameter in the following examples.

## Example 1: Upload all files from a folder
## -----------------------------------------
## Uploads all files from a folder to the default workspace.

from pathlib import Path

from haystack_enterprise_sdk.workflows.sync_client.files import upload

upload(
    # workspace_name="my_workspace",  # optional, by default the environment variable "DEFAULT_WORKSPACE_NAME" is used
    paths=[Path("./examples/data")],
    blocking=True,  # optional, by default True
    timeout_s=300,  # optional, by default 300
    show_progress=True,  # optional, by default True
    recursive=False,  # optional, by default False
)


## Example 2: Upload raw texts
## ---------------------------
## Uploads a list of raw texts to the default workspace.
## This is useful if you want to process your text first and upload the content of the files later.

from haystack_enterprise_sdk.workflows.sync_client.files import upload_texts

upload_texts(
    # workspace_name="my_workspace",  # optional, by default the environment variable "DEFAULT_WORKSPACE_NAME" is used
    files=[
        HaystackEnterpriseFile(
            name="example.txt",
            text="this is text",
            meta={"key": "value"},  # optional
        )
    ],
    blocking=True,  # optional, by default True
    timeout_s=300,  # optional, by default 300
)
