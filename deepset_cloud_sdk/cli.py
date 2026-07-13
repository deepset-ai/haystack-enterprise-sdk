"""The CLI for the deepset AI Platform SDK."""

import json
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import UUID

import click
import typer
from tabulate import tabulate
from yaspin import yaspin

__version__ = version("deepset-cloud-sdk")
from deepset_cloud_sdk._api.config import DEFAULT_WORKSPACE_NAME, ENV_FILE_PATH
from deepset_cloud_sdk._api.deployments import DeploymentServiceLevel
from deepset_cloud_sdk._api.upload_sessions import WriteMode
from deepset_cloud_sdk._service.deployment_service import (
    CreateOptions,
    DeploymentFailedError,
    ServiceNotFoundError,
)
from deepset_cloud_sdk._service.pipeline_transform import PipelineTransformError
from deepset_cloud_sdk.workflows.sync_client.deployment_client import DeploymentClient
from deepset_cloud_sdk.workflows.sync_client.files import download as sync_download
from deepset_cloud_sdk.workflows.sync_client.files import (
    get_upload_session as sync_get_upload_session,
)
from deepset_cloud_sdk.workflows.sync_client.files import list_files as sync_list_files
from deepset_cloud_sdk.workflows.sync_client.files import (
    list_upload_sessions as sync_list_upload_sessions,
)
from deepset_cloud_sdk.workflows.sync_client.files import upload as sync_upload

cli_app = typer.Typer(pretty_exceptions_show_locals=False)


# cli commands
@cli_app.command()
def upload(  # pylint: disable=too-many-arguments
    paths: List[Path],
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
    write_mode: WriteMode = WriteMode.KEEP,
    blocking: bool = True,
    timeout_s: Optional[int] = None,
    show_progress: bool = True,
    recursive: bool = False,
    use_type: Optional[List[str]] = None,
    enable_parallel_processing: bool = False,
    safe_mode: bool = False,
) -> None:
    """Upload a folder to deepset AI Platform.

    :param paths: Path to the folder to upload. If the folder contains unsupported file types, they're skipped.
    deepset supports CSV, DOCX, HTML, JSON, MD, TXT, PDF, PPTX, XLSX, XML.
    :param api_key: deepset API key to use for authentication.
    :param api_url: API URL to use for authentication.
    :param workspace_name: Name of the workspace to upload the files to. It uses the workspace from the .ENV file by default.
    :param write_mode: Specifies what to do when a file with the same name already exists in the workspace.
    Possible options are:
    KEEP - uploads the file with the same name and keeps both files in the workspace.
    OVERWRITE - overwrites the file that is in the workspace.
    FAIL - fails to upload the file with the same name.
    :param blocking: Whether to wait for the files to be uploaded and displayed in deepset AI Platform.
    :param timeout_s: Timeout in seconds for the `blocking` parameter.
    :param show_progress: Shows the upload progress.
    :param recursive: Uploads files from subfolders as well.
    :param use_type: A comma-separated string of allowed file types to upload.
    :param enable_parallel_processing: If `True`, deepset AI Platform ingests the files in parallel.
        Use this to speed up the upload process. Make sure you are not running concurrent uploads for the same files.
    :param safe_mode: If `True`, disables ingesting files in parallel.
    """
    sync_upload(
        paths=paths,
        api_key=api_key,
        api_url=api_url,
        workspace_name=workspace_name,
        write_mode=write_mode,
        blocking=blocking,
        timeout_s=timeout_s,
        show_progress=show_progress,
        recursive=recursive,
        desired_file_types=use_type,
        enable_parallel_processing=enable_parallel_processing,
        safe_mode=safe_mode,
    )


@cli_app.command()
def download(  # pylint: disable=too-many-arguments
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
    file_dir: Optional[str] = None,
    name: Optional[str] = None,
    odata_filter: Optional[str] = None,
    include_meta: bool = True,
    batch_size: int = 50,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    show_progress: bool = True,
    safe_mode: bool = False,
) -> None:
    """Download files from deepset AI Platform to your local machine.

    :param workspace_name: Name of the workspace to download the files from. Uses the workspace from the .ENV file by default.
    :param file_dir: Path to the folder where you want to download the files.
    :param name: Name of the file to odata_filter for.
    :param odata_filter: odata_filter to apply to the file list.
    :param include_meta: Downloads metadata of the files.
    :param batch_size: Batch size for file listing.
    :param api_key: API key to use for authentication.
    :param api_url: API URL to use for authentication.
    :param show_progress: Shows the upload progress.
    :param safe_mode: If `True`, disables ingesting files in parallel.
    """
    sync_download(
        workspace_name=workspace_name,
        file_dir=file_dir,
        name=name,
        odata_filter=odata_filter,
        include_meta=include_meta,
        batch_size=batch_size,
        api_key=api_key,
        api_url=api_url,
        show_progress=show_progress,
        safe_mode=safe_mode,
    )


@cli_app.command()
def login() -> None:
    """Log in to deepset AI Platform.

    Run `deepset-cloud login` before performing any tasks in deepset AI platform using the SDK or CLI,
    unless you already created the .ENV file.

    This command guides you through creating a global .env file at ~/.deepset-cloud/.env with your
    deepset AI Platform `API_KEY`, `API_URL` and `DEFAULT_WORKSPACE_NAME` used for all operations.

    The SDK uses a cascading configuration model with the following precedence:
    1. Explicit parameters (passed via code or CLI)
    2. Environment variables
    3. Local .env file in project root
    4. Global ~/.deepset-cloud/.env file (supplements local .env)
    5. Built-in defaults
    """
    typer.echo("Log in to deepset AI Platform")

    # Check for local .env file in the current directory
    local_env = Path.cwd() / ".env"
    if local_env.is_file():
        typer.echo(f"\nNote: Found .env file in the current directory ({local_env}).")
        typer.echo(
            "This local configuration will take precedence over the global configuration you're about to create."
        )

    environment = typer.prompt(
        "Choose environment",
        type=click.Choice(["eu", "us", "custom"], case_sensitive=False),
        default="eu",
    )

    if environment.lower() == "eu":
        api_url = "https://api.cloud.deepset.ai/api/v1"
    elif environment.lower() == "us":
        api_url = "http://api.us.deepset.ai/api/v1"
    else:
        api_url = typer.prompt("Enter custom API URL")
    passed_api_key = typer.prompt("Your deepset AI Platform API_KEY", hide_input=True)
    passed_default_workspace_name = typer.prompt("Your DEFAULT_WORKSPACE_NAME", default="default")

    env_content = f"API_KEY={passed_api_key}\nAPI_URL={api_url}\nDEFAULT_WORKSPACE_NAME={passed_default_workspace_name}"

    ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE_PATH.write_text(env_content, encoding="utf-8")

    typer.echo(f"Global configuration file created at {ENV_FILE_PATH}.")


@cli_app.command()
def logout() -> None:
    """Log out of deepset AI Platform. This command deletes the .ENV file created during login.

    Example:
    `deepset-cloud logout`
    """
    typer.echo("Log out of deepset AI Platform.")
    if not ENV_FILE_PATH.exists():
        typer.echo("No global configuration file found. Nothing to do!")
        return
    ENV_FILE_PATH.unlink()
    typer.echo(f"Global configuration file {ENV_FILE_PATH} removed successfully.")


@cli_app.command()
def list_files(
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    name: Optional[str] = None,
    odata_filter: Optional[str] = None,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
    batch_size: int = 10,
    timeout_s: Optional[int] = None,
) -> None:
    """List files that exist in the specified deepset workspace.

    :param api_key: deepset API key to use for authentication.
    :param api_url: API URL to use for authentication.
    :param workspace_name: Name of the workspace to list the files from. Uses the workspace from the .ENV file by default.
    :param name: Name of the file to odata_filter for.
    :param odata_filter: odata_filter to apply to the file list.
    :param batch_size: Batch size to use for the file list.
    :param timeout_s: The timeout for this request, in seconds.

    Example:
    `deepset-cloud list-files --batch-size 10`

    Example using an odata filter to show only files whose category is "news":
    `deepset-cloud list-files --odata-filter 'category eq "news"'`
    """
    try:
        headers = [
            "file_id",
            "url",
            "name",
            "size",
            "created_at",
            "meta",
        ]  # Assuming the first row contains the headers
        for files in sync_list_files(api_key, api_url, workspace_name, name, odata_filter, batch_size, timeout_s):
            table = tabulate(files, headers, tablefmt="grid")  # type: ignore
            typer.echo(table)
            if len(files) > 0:
                prompt_input = typer.prompt("Print more results ?", default="y")
                if prompt_input != "y":
                    break
    except TimeoutError:
        typer.echo("Command timed out.")


@cli_app.command()
def list_upload_sessions(
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    is_expired: Optional[bool] = False,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
    batch_size: int = 10,
    timeout_s: Optional[int] = None,
) -> None:
    """List the details of all upload sessions for the specified workspace, including closed sessions.

    :param api_key: deepset API key to use for authentication.
    :param api_url: API URL to use for authentication.
    :param workspace_name: Name of the workspace to list the files from. Uses the workspace from the .ENV file by default.
    :param is_expired: Whether to list expired upload sessions.
    :param batch_size: Batch size to use for the file list.
    :param timeout_s: Timeout in seconds for the API requests.

    Example:
    `deepset-cloud list-upload-sessions --workspace-name default`
    """
    headers: List[str] = [
        "session_id",
        "created_by",
        "created_at",
        "expires_at",
        "write_mode",
        "status",
    ]
    try:
        for upload_sessions in sync_list_upload_sessions(
            api_key=api_key,
            api_url=api_url,
            workspace_name=workspace_name,
            is_expired=is_expired,
            batch_size=batch_size,
            timeout_s=timeout_s,
        ):
            table = tabulate(
                [
                    {
                        "session_id": str(el.session_id),
                        "created_by": f"{el.created_by.given_name} {el.created_by.family_name}",
                        "created_at": str(el.created_at),
                        "expires_at": str(el.expires_at),
                        "write_mode": el.write_mode.name,
                        "status": el.status.name,
                    }
                    for el in upload_sessions
                ],
                dict(enumerate(headers)),  # type: ignore
                tablefmt="grid",
            )
            typer.echo(table)
            if len(upload_sessions) > 0:
                prompt_input = typer.prompt("Print more results?", default="y")
                if prompt_input != "y":
                    break
    except TimeoutError:
        typer.echo("Command timed out. Please try again later.")


@cli_app.command()
def get_upload_session(
    session_id: UUID,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
) -> None:  # noqa: D400, D205
    """Fetch an upload session from deepset AI Platform. This method is useful for checking
    the status of an upload session after uploading files to deepset.

    :param session_id: ID of the upload session whose status you want to check.
    :param api_key: deepset API key to use for authentication.
    :param api_url: API URL to use for authentication.
    :param workspace_name: Name of the workspace where you upload your files. Uses the workspace from the .ENV file by default.

    Example:
    `deepset-cloud get-upload-session --workspace-name default`
    """
    session = sync_get_upload_session(
        session_id=session_id,
        api_key=api_key,
        api_url=api_url,
        workspace_name=workspace_name,
    )
    typer.echo(
        json.dumps(
            {
                "session_id": str(session.session_id),
                "expires_at": str(session.expires_at),
                "documentation_url": str(session.documentation_url),
                "ingestion_status": {
                    "failed_files": session.ingestion_status.failed_files,
                    "finished_files": session.ingestion_status.finished_files,
                },
            },
            indent=4,
        )
    )


@cli_app.command()
def deploy(  # pylint: disable=too-many-arguments,too-many-locals
    target: Path,
    service_name: str,
    activate: bool = False,
    create: bool = False,
    entrypoint: Optional[str] = None,
    requirements: Optional[Path] = None,
    service_level: Optional[DeploymentServiceLevel] = None,
    min_replicas: Optional[int] = None,
    max_replicas: Optional[int] = None,
    cpu: Optional[str] = None,
    memory: Optional[str] = None,
    gpu: Optional[int] = None,
    idle_timeout: Optional[int] = None,
    python: Optional[str] = None,
    dry_run: bool = False,
    output: Optional[Path] = None,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
) -> None:
    """Deploy a Haystack pipeline defined in a local Python file to a service deployment.

    Transforms the pipeline (rewriting local custom components into the platform Code component and
    extracting dependencies), then pushes it as a new revision of the given service.

    The pipeline is loaded in your project's Python environment (auto-detected virtualenv, or the
    interpreter given by --python), so this CLI's own environment does not need your pipeline's
    dependencies installed.

    :param target: Path to the Python file that defines the pipeline.
    :param service_name: Name of the target service deployment.
    :param activate: Activate the new revision and wait for the rollout to finish.
    :param create: Create the service if it does not exist (Development sizing unless overridden).
    :param entrypoint: Name of the pipeline instance or factory when the file defines more than one.
    :param requirements: Requirements file to use instead of autodetecting dependencies.
    :param service_level: Service sizing tier when creating the service.
    :param min_replicas: Minimum query replicas (with --create).
    :param max_replicas: Maximum query replicas (with --create).
    :param cpu: CPU limit, e.g. '1' (with --create).
    :param memory: Memory limit, e.g. '2Gi' (with --create).
    :param gpu: GPU memory limit in gigabytes (with --create).
    :param idle_timeout: Idle timeout in seconds before scale-down (with --create).
    :param python: Path to the Python interpreter used to load your pipeline (defaults to an
        auto-detected virtualenv near the target file, else the current interpreter).
    :param dry_run: Transform the pipeline and print/write the resulting YAML without deploying. No
        API credentials are needed.
    :param output: With --dry-run, write the transformed YAML to this file instead of stdout.
    :param api_key: deepset API key to use for authentication.
    :param api_url: API URL to use for authentication.
    :param workspace_name: Workspace to deploy into. Uses the workspace from the .ENV file by default.

    Example:
    `deepset-cloud deploy pipeline.py my-service --activate --create`

    Preview the transformed YAML without deploying:
    `deepset-cloud deploy pipeline.py my-service --dry-run --output out.yaml`
    """
    if dry_run:
        _deploy_dry_run(target, entrypoint, requirements, python, output)
        return

    client = DeploymentClient(api_key=api_key, api_url=api_url, workspace_name=workspace_name)
    create_options = (
        CreateOptions(
            service_level=service_level,
            idle_timeout_in_seconds=idle_timeout,
            min_query_replica_count=min_replicas,
            max_query_replica_count=max_replicas,
            cpu_limit=cpu,
            memory_limit=memory,
            gpu_limit_gigabyte=gpu,
        )
        if create
        else None
    )

    try:
        if activate:
            with yaspin().arc as spinner:
                spinner.text = f"Deploying '{service_name}'."

                def _on_status(status: object) -> None:
                    spinner.text = f"Rolling out '{service_name}' ({getattr(status, 'value', status)})."

                result = client.deploy(
                    target,
                    service_name,
                    activate=True,
                    create=create,
                    create_options=create_options,
                    entrypoint=entrypoint,
                    requirements=requirements,
                    io_resolver=_resolve_missing_io,
                    python_executable=python,
                    on_status=_on_status,
                )
        else:
            result = client.deploy(
                target,
                service_name,
                create=create,
                create_options=create_options,
                entrypoint=entrypoint,
                requirements=requirements,
                io_resolver=_resolve_missing_io,
                python_executable=python,
            )
    except KeyboardInterrupt:
        typer.echo(
            f"\nDetached. The rollout continues on the platform. "
            f"Check with `deepset-cloud service-status {service_name}`."
        )
        raise typer.Exit(0)  # noqa: B904
    except DeploymentFailedError as err:
        typer.echo(str(err))
        raise typer.Exit(1)  # noqa: B904
    except (ServiceNotFoundError, PipelineTransformError) as err:
        typer.echo(str(err))
        raise typer.Exit(1)  # noqa: B904

    if not activate:
        typer.echo(
            f"Pushed revision {result.revision.revision_id} to '{service_name}' (status: PENDING). "
            f"Re-run with --activate or activate it in the platform to roll it out."
        )
    elif result.timed_out:
        typer.echo(
            f"Activation of '{service_name}' is still in progress. Detached; the rollout continues. "
            f"Check with `deepset-cloud service-status {service_name}`."
        )
    else:
        typer.echo(f"Service '{service_name}' is now {result.deployment.status.value}.")


def _deploy_dry_run(
    target: Path,
    entrypoint: Optional[str],
    requirements: Optional[Path],
    python: Optional[str],
    output: Optional[Path],
) -> None:
    """Transform the pipeline and print/write the YAML without contacting the API."""
    from deepset_cloud_sdk._service import pipeline_transform

    try:
        extraction = pipeline_transform.extract_via_subprocess(target, entrypoint, python)
        inputs, outputs = _resolve_missing_io(extraction)
        config_yaml = pipeline_transform.render_config_yaml(
            extraction,
            requirements=requirements,
            inputs=inputs or None,
            outputs=outputs or None,
        )
    except PipelineTransformError as err:
        typer.echo(str(err))
        raise typer.Exit(1)  # noqa: B904

    if output is not None:
        output.write_text(config_yaml, encoding="utf-8")
        typer.echo(f"Wrote transformed pipeline YAML to {output}.")
    else:
        typer.echo(config_yaml)


def _stdin_is_tty() -> bool:
    """Whether stdin is an interactive terminal (indirection kept simple to patch in tests)."""
    return sys.stdin.isatty()


def _resolve_missing_io(extraction: dict) -> Tuple[dict, dict]:
    """Interactively set the pipeline inputs/outputs the shared prototype (Playground) needs.

    Used as the ``io_resolver`` for the deploy flow (and in --dry-run). It is called with the extraction
    bundle when inputs or outputs could not be inferred from the pipeline's open sockets. It returns the
    ``(inputs, outputs)`` dicts to use; empty dicts mean "leave unset" so the deploy proceeds as before.

    It only prompts on an interactive TTY — in CI or with piped input it returns whatever was inferred,
    keeping the existing warn-and-deploy-without-them behavior.
    """
    inferred_inputs = extraction.get("inferred_inputs") or {}
    inferred_outputs = extraction.get("inferred_outputs") or {}
    if inferred_inputs and inferred_outputs:
        return inferred_inputs, inferred_outputs
    if not _stdin_is_tty():
        return inferred_inputs, inferred_outputs

    typer.echo(
        "\nCould not infer the pipeline inputs/outputs required for the shared prototype "
        "(the Playground query UI)."
    )
    if not typer.confirm("Set them now?", default=False):
        return inferred_inputs, inferred_outputs

    inputs = dict(inferred_inputs)
    if not inputs:
        input_sockets = _flatten_sockets(extraction.get("available_inputs") or {})
        query = _select_socket(input_sockets, "Which socket is the 'query' input?", required=True)
        if query:
            inputs["query"] = [query]
        filters = _select_socket(input_sockets, "Which socket is the 'filters' input?", required=False)
        if filters:
            inputs["filters"] = [filters]

    outputs = dict(inferred_outputs)
    if not outputs:
        output_sockets = _flatten_sockets(extraction.get("available_outputs") or {})
        answers = _select_socket(output_sockets, "Which socket is the 'answers' output?", required=False)
        if answers:
            outputs["answers"] = answers
        documents = _select_socket(output_sockets, "Which socket is the 'documents' output?", required=False)
        if documents:
            outputs["documents"] = documents

    return inputs, outputs


def _flatten_sockets(sockets_by_component: Dict[str, List[str]]) -> List[str]:
    """Flatten ``{component: [socket, ...]}`` into sorted ``"component.socket"`` paths."""
    paths = [f"{comp}.{socket}" for comp, sockets in sockets_by_component.items() for socket in sockets]
    return sorted(paths)


def _select_socket(sockets: List[str], prompt: str, *, required: bool) -> Optional[str]:
    """Prompt the user to pick one socket from a numbered menu.

    :param sockets: The available ``"component.socket"`` paths.
    :param prompt: The question to show above the menu.
    :param required: If True the user must pick one; if False a ``0`` skips (returns None).
    :return: The chosen socket path, or None if skipped or there is nothing to choose from.
    """
    if not sockets:
        return None
    typer.echo(prompt)
    for index, socket in enumerate(sockets, start=1):
        typer.echo(f"  {index}. {socket}")
    default = "1" if required else "0"
    hint = "enter a number" if required else "enter a number, or 0 to skip"
    while True:
        choice = typer.prompt(f"  Choice ({hint})", default=default)
        try:
            number = int(choice)
        except ValueError:
            typer.echo("  Please enter a number.")
            continue
        if number == 0 and not required:
            return None
        if 1 <= number <= len(sockets):
            return sockets[number - 1]
        typer.echo("  Out of range.")


@cli_app.command()
def service_status(
    service_name: str,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
) -> None:
    """Show the current status of a service deployment.

    :param service_name: Name of the service deployment.
    :param api_key: deepset API key to use for authentication.
    :param api_url: API URL to use for authentication.
    :param workspace_name: Workspace of the service. Uses the workspace from the .ENV file by default.

    Example:
    `deepset-cloud service-status my-service`
    """
    client = DeploymentClient(api_key=api_key, api_url=api_url, workspace_name=workspace_name)
    try:
        deployment = client.get_service_status(service_name)
    except ServiceNotFoundError as err:
        typer.echo(str(err))
        raise typer.Exit(1)  # noqa: B904
    typer.echo(
        json.dumps(
            {
                "name": deployment.name,
                "deployment_id": str(deployment.deployment_id),
                "status": deployment.status.value,
                "service_level": deployment.service_level.value,
                "active_revision_id": str(deployment.active_revision_id) if deployment.active_revision_id else None,
                "pending_revision_id": (
                    str(deployment.pending_revision_id) if deployment.pending_revision_id else None
                ),
            },
            indent=4,
        )
    )


def version_callback(value: bool) -> None:
    """Show the SDK version and exit.

    :param value: Value of the version option.

    Example:
    `deepset-cloud --version`
    """
    if value:
        typer.echo(f"deepset SDK version: {__version__}")
        raise typer.Exit()


@cli_app.callback()
def main(
    _: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show the SDK version and exit.",
    ),
) -> None:  # noqa
    """The CLI for the deepset SDK.

    This documentation uses Python type hints to provide information about the arguments and return values.
    Typer turns these type hints into a CLI interface. To see how these arguments are used in the CLI, check the
    Typer documentation: https://typer.tiangolo.com/tutorial/arguments/optional or run
    `deepset-cloud <command> --help` to see the arguments for a specific command.

    Boolean values are converted to `-no-<variable>` or `-<variable>` flags in the CLI. For example, to disable
    the progress bar, use `--no-show-progress`.

    Lists can be passed by using the same flag multiple times. For example, to scan only `.txt` and `.pdf` files,
    when uploading use `--use-type .txt --use-type .pdf`.
    """


def run_packaged() -> None:
    """Run the packaged CLI.

    This is the entrypoint for the package to enable running the CLI using typer.

    Example:
    `deepset cloud run-packaged`
    """
    cli_app()


if __name__ == "__main__":
    cli_app()
