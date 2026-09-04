# Migrating from `deepset-cloud-sdk`

This SDK was previously published as **`deepset-cloud-sdk`**. The rename changed the package, the
command and the config file path. Nothing about the API surface changed with the rename itself.

## What changed

| | Before | Now |
| --- | --- | --- |
| Product | deepset Cloud SDK | Haystack Enterprise SDK |
| Package | `deepset-cloud-sdk` | `haystack-enterprise-sdk` |
| Import | `deepset_cloud_sdk` | `haystack_enterprise_sdk` |
| CLI command | `deepset-cloud` (or `python -m deepset_cloud_sdk.cli`) | `haystack-enterprise` (or `python -m haystack_enterprise_sdk.cli`) |
| Config file | `~/.deepset-cloud/.env` | `~/.haystack-enterprise/.env` |
| Platform | deepset Cloud, deepset AI Platform | Haystack Enterprise Platform |

## Steps

**1. Remove the old package.**

```bash
pip uninstall deepset-cloud-sdk
# or, if you installed it as a tool
uv tool uninstall deepset-cloud-sdk
```

**2. Install the new one.** See [Install](../get-started/install.md).

**3. Log in again.** The SDK reads `~/.haystack-enterprise/.env`; your old `~/.deepset-cloud/.env` is
ignored.

```shell
haystack-enterprise login
```

Or move the file yourself — the contents are unchanged (`API_KEY`, `API_URL`,
`DEFAULT_WORKSPACE_NAME`):

```bash
mkdir -p ~/.haystack-enterprise
cp ~/.deepset-cloud/.env ~/.haystack-enterprise/.env
```

Project-local `.env` files need no change.

**4. Update your imports.**

```python
# before
from deepset_cloud_sdk.workflows.sync_client.files import upload

# now
from haystack_enterprise_sdk.workflows.sync_client.files import upload
```

**5. Update scripts and CI** that call the old command. The subcommands you already use (`login`,
`upload`, `download`, `list-files`, …) keep their names and flags; only the command in front changes:

```shell
# before
deepset-cloud upload ./my-files

# now
haystack-enterprise upload ./my-files
```

The pipeline commands — `validate`, `run`, `deploy` and `service-status` — are new in the renamed
package and have no `deepset-cloud` equivalent.

## Things that look like the old name but are not

Two identifiers still contain `deepset_cloud`. They are platform wire contract, not branding, and are
supposed to stay:

- `deepset_cloud_custom_nodes.*` — component type paths in generated pipeline YAML.
- `deepset_cloud_version` — a field in the deployment payload.

You will see these in `--dry-run` output. Leave them alone.
