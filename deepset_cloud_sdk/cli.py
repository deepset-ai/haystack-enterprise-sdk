"""The CLI for the deepset AI Platform SDK."""

import functools
import json
import sys
from importlib.metadata import version
from pathlib import Path
from typing import List, Optional, Tuple
from uuid import UUID

import click
import typer
from tabulate import tabulate
from yaspin import yaspin

__version__ = version("deepset-cloud-sdk")
from deepset_cloud_sdk._api.config import DEFAULT_WORKSPACE_NAME, ENV_FILE_PATH
from deepset_cloud_sdk._api.deployments import (
    DeploymentServiceLevel,
    PipelineValidationError,
)
from deepset_cloud_sdk._api.pipeline_run import PipelineRunError
from deepset_cloud_sdk._api.shared_prototypes import FailedToCreateSharedPrototypeError
from deepset_cloud_sdk._api.upload_sessions import WriteMode
from deepset_cloud_sdk._service.deployment_service import (
    CreateOptions,
    DeploymentFailedError,
    ServiceNotFoundError,
    ShareOptions,
)
from deepset_cloud_sdk._service.pipeline_transform import (
    ExtractionBundle,
    PipelineTransformError,
    build_config_yaml,
    flatten_sockets,
    unmapped_mandatory_inputs,
    unmapped_mandatory_warning,
)
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
    skip_activation: bool = False,
    create: bool = False,
    entrypoint: Optional[str] = None,
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
    skip_io_validation: bool = False,
    skip_validation: bool = False,
    share: Optional[bool] = typer.Option(None, "--share/--no-share"),
    share_expiration_days: int = 30,
    share_login_required: Optional[bool] = typer.Option(None, "--share-login-required/--no-share-login-required"),
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
) -> None:
    """Deploy a Haystack pipeline defined in a local Python file to a service deployment.

    Transforms the pipeline (rewriting local custom components into the platform Code component),
    then pushes it as a new revision of the given service.

    The pipeline is loaded in your project's Python environment (auto-detected virtualenv, or the
    interpreter given by --python), so this CLI's own environment does not need your pipeline's
    dependencies installed.

    :param target: Path to the Python file that defines the pipeline.
    :param service_name: Name of the target service deployment.
    :param skip_activation: Push the revision without activating it (skips the rollout and wait).
        By default the new revision is activated and the CLI waits for the rollout to finish.
    :param create: Create the service if it does not exist (Development sizing unless overridden).
    :param entrypoint: Name of the pipeline instance or factory when the file defines more than one.
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
    :param skip_io_validation: Deploy even if mandatory pipeline inputs aren't mapped to platform
        inputs; skips the interactive input/output prompt and deploys with whatever was inferred.
        Use for arbitrary pipelines you invoke directly rather than via the shared prototype chat UI.
    :param skip_validation: Skip validating the generated YAML against the platform before deploying.
        By default the YAML is validated and the deploy is aborted on blocking (ERROR) issues.
    :param share: Create a shareable prototype link (opens a chat UI) after deploying. Omit to be
        prompted on an interactive terminal; pass --share/--no-share to force the choice.
    :param share_expiration_days: Days until the shared prototype link expires (default 30).
    :param share_login_required: Whether recipients must log in to open the shared link. Omit to be
        prompted when sharing interactively (default require login).
    :param api_key: deepset API key to use for authentication.
    :param api_url: API URL to use for authentication.
    :param workspace_name: Workspace to deploy into. Uses the workspace from the .ENV file by default.

    Example (activates and waits for the rollout by default):
    `deepset-cloud deploy pipeline.py my-service --create`

    Push a revision without rolling it out:
    `deepset-cloud deploy pipeline.py my-service --skip-activation`

    Preview the transformed YAML without deploying:
    `deepset-cloud deploy pipeline.py my-service --dry-run --output out.yaml`
    """
    if dry_run:
        _deploy_dry_run(target, entrypoint, python, output, skip_io_validation)
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

    activate = not skip_activation

    # A shared prototype requires the service to be deployed, so it is only offered when we
    # activate. Deciding this up front also drives whether we resolve the pipeline inputs/outputs
    # (needed for the chat UI) into the deployed YAML.
    if skip_activation:
        if share:
            typer.echo("--share requires activation; drop --skip-activation to share a prototype.")
            raise typer.Exit(1)
        do_share = False
    else:
        do_share = share if share is not None else _prompt_share()
    io_resolver = functools.partial(_resolve_io_for_share, skip_validation=skip_io_validation) if do_share else None

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
                    io_resolver=io_resolver,
                    python_executable=python,
                    validate=not skip_validation,
                    on_status=_on_status,
                )
        else:
            result = client.deploy(
                target,
                service_name,
                create=create,
                create_options=create_options,
                entrypoint=entrypoint,
                io_resolver=io_resolver,
                python_executable=python,
                validate=not skip_validation,
            )
    except KeyboardInterrupt:
        typer.echo(
            f"\nDetached. The rollout continues on the platform. "
            f"Check with `deepset-cloud service-status {service_name}`."
        )
        raise typer.Exit(0)  # noqa: B904
    except (DeploymentFailedError, ServiceNotFoundError, PipelineTransformError, PipelineValidationError) as err:
        typer.echo(str(err))
        raise typer.Exit(1)  # noqa: B904

    if not activate:
        typer.echo(
            f"Pushed revision {result.revision.revision_id} to '{service_name}' (status: PENDING). "
            f"Re-run without --skip-activation or activate it in the platform to roll it out."
        )
    elif result.timed_out:
        typer.echo(
            f"Activation of '{service_name}' is still in progress. Detached; the rollout continues. "
            f"Check with `deepset-cloud service-status {service_name}`."
        )
    else:
        typer.echo(f"Service '{service_name}' is now {result.deployment.status.value}.")

    if do_share:
        if not result.is_deployed:
            typer.echo(
                f"Skipped shared prototype: '{service_name}' is not deployed yet "
                f"(status: {result.deployment.status.value}). Create the link in the platform "
                f"once it is deployed."
            )
        else:
            login_required = share_login_required if share_login_required is not None else _prompt_login_required()
            try:
                prototype = client.create_shared_prototype(
                    service_name,
                    ShareOptions(expiration_days=share_expiration_days, login_required=login_required),
                )
            except FailedToCreateSharedPrototypeError as err:
                # The deploy itself succeeded, so this is a warning, not a failure exit.
                typer.echo(f"Deployed, but could not create the shared prototype link: {err}")
            else:
                typer.echo(f"Shared prototype link: {prototype.link}")


@cli_app.command()
def validate(
    target: Path,
    entrypoint: Optional[str] = None,
    python: Optional[str] = None,
    skip_io_validation: bool = False,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
) -> None:
    """Validate the YAML generated from a local pipeline against the platform, without deploying.

    Runs the same transform the deploy uses, then checks the result against the platform and reports
    any issues. Exits non-zero if there are blocking (ERROR) issues.

    :param target: Path to the Python file that defines the pipeline.
    :param entrypoint: Name of the pipeline instance or factory when the file defines more than one.
    :param python: Path to the Python interpreter used to load your pipeline (defaults to an
        auto-detected virtualenv near the target file, else the current interpreter).
    :param skip_io_validation: Skip the interactive input/output prompt and use whatever was inferred.
    :param api_key: deepset API key to use for authentication.
    :param api_url: API URL to use for authentication.
    :param workspace_name: Workspace to validate against. Uses the workspace from the .ENV file by default.

    Example:
    `deepset-cloud validate pipeline.py`
    """
    client = DeploymentClient(api_key=api_key, api_url=api_url, workspace_name=workspace_name)
    io_resolver = functools.partial(_resolve_io_for_share, skip_validation=skip_io_validation)
    try:
        result = client.validate(target, entrypoint=entrypoint, io_resolver=io_resolver, python_executable=python)
    except PipelineTransformError as err:
        typer.echo(str(err))
        raise typer.Exit(1)  # noqa: B904

    for issue in result.warnings:
        typer.echo(str(issue))
    for issue in result.errors:
        typer.echo(str(issue))

    if result.has_errors:
        typer.echo(f"Pipeline is invalid: {len(result.errors)} error(s).")
        raise typer.Exit(1)
    typer.echo("Pipeline is valid.")


@cli_app.command()
def run(  # pylint: disable=too-many-arguments,too-many-locals
    target: Path,
    query: Optional[str] = None,
    inputs: Optional[str] = None,
    include_outputs_from: Optional[List[str]] = None,
    entrypoint: Optional[str] = None,
    python: Optional[str] = None,
    skip_io_validation: bool = False,
    output: Optional[Path] = None,
    api_key: Optional[str] = None,
    api_url: Optional[str] = None,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
) -> None:
    """Run a Haystack pipeline defined in a local Python file in the platform sandbox, without deploying.

    Transforms the pipeline the same way ``deploy`` does (rewriting local custom components into the
    platform Code component), then executes the resulting YAML on the platform with the given inputs
    and prints the pipeline output. This is the same "run without deploying" the builder/playground does.

    The pipeline is loaded in your project's Python environment (auto-detected virtualenv, or the
    interpreter given by --python), so this CLI's own environment does not need your pipeline's
    dependencies installed.

    :param target: Path to the Python file that defines the pipeline.
    :param query: Query text routed to the sockets mapped under the pipeline's 'query' input. Convenient
        for the common case; on an interactive terminal you are prompted for it when neither --query nor
        --inputs is given.
    :param inputs: Explicit run inputs as JSON, either a literal JSON string or '@path/to/file.json'.
        Shape is the Haystack run inputs dict, '{"component": {"socket": value}}'. Merged over (and
        wins against) any inputs derived from --query.
    :param include_outputs_from: Component name(s) whose outputs to include in the result. Repeat the
        option to pass several. Defaults to all components.
    :param entrypoint: Name of the pipeline instance or factory when the file defines more than one.
    :param python: Path to the Python interpreter used to load your pipeline (defaults to an
        auto-detected virtualenv near the target file, else the current interpreter).
    :param skip_io_validation: Skip the interactive input/output prompt and use whatever was inferred.
    :param output: Write the pipeline output JSON to this file instead of printing it to stdout.
    :param api_key: deepset API key to use for authentication.
    :param api_url: API URL to use for authentication.
    :param workspace_name: Workspace to run in. Uses the workspace from the .ENV file by default.

    Example:
    `deepset-cloud run pipeline.py --query "What is deepset?"`

    With explicit inputs from a file:
    `deepset-cloud run pipeline.py --inputs @inputs.json`
    """
    extra_inputs = _parse_inputs_option(inputs)

    if query is None and extra_inputs is None and _stdin_is_tty():
        query = typer.prompt("Query")

    client = DeploymentClient(api_key=api_key, api_url=api_url, workspace_name=workspace_name)
    io_resolver = functools.partial(_resolve_io_for_share, skip_validation=skip_io_validation)
    try:
        result = client.run(
            target,
            entrypoint=entrypoint,
            io_resolver=io_resolver,
            python_executable=python,
            query=query,
            extra_inputs=extra_inputs,
            include_outputs_from=include_outputs_from or None,
        )
    except (PipelineTransformError, PipelineRunError) as err:
        typer.echo(str(err))
        raise typer.Exit(1)  # noqa: B904

    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    if output is not None:
        output.write_text(rendered, encoding="utf-8")
        typer.echo(f"Wrote pipeline output to {output}.")
    else:
        typer.echo(rendered)


def _parse_inputs_option(inputs: Optional[str]) -> Optional[dict]:
    """Parse the ``--inputs`` option into a dict: a literal JSON string, or ``@path`` to a JSON file.

    Returns ``None`` when no ``--inputs`` was given. Exits with an error on invalid JSON or a missing file.
    """
    if inputs is None:
        return None
    if inputs.startswith("@"):
        path = Path(inputs[1:])
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as err:
            typer.echo(f"Could not read --inputs file '{path}': {err}")
            raise typer.Exit(1)  # noqa: B904
    else:
        raw = inputs
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        typer.echo(f"--inputs is not valid JSON: {err}")
        raise typer.Exit(1)  # noqa: B904
    if not isinstance(parsed, dict):
        typer.echo("--inputs must be a JSON object mapping component names to their inputs.")
        raise typer.Exit(1)
    return parsed


def _deploy_dry_run(
    target: Path,
    entrypoint: Optional[str],
    python: Optional[str],
    output: Optional[Path],
    skip_io_validation: bool = False,
) -> None:
    """Transform the pipeline and print/write the YAML without contacting the API.

    Uses the same extract → resolve → render path as the real deploy (:func:`build_config_yaml`).
    """
    io_resolver = functools.partial(_resolve_io_for_share, skip_validation=skip_io_validation)
    try:
        config_yaml = build_config_yaml(
            target,
            entrypoint=entrypoint,
            io_resolver=io_resolver,
            python_executable=python,
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


def _prompt_share() -> bool:
    """Ask whether to create a shareable prototype link. Returns False on a non-interactive terminal."""
    if not _stdin_is_tty():
        return False
    return typer.confirm("Create a shareable prototype link (opens a chat UI) for this service?", default=False)


def _prompt_login_required() -> bool:
    """Ask whether the shared link should require login. Returns True (the safe default) on non-TTY."""
    if not _stdin_is_tty():
        return True
    return typer.confirm("Require login to open the shared link?", default=True)


def _resolve_io_for_share(extraction: ExtractionBundle, *, skip_validation: bool = False) -> Tuple[dict, dict]:
    """Resolve the pipeline inputs/outputs the shared prototype (chat UI) needs.

    Used as the ``io_resolver`` for the deploy flow when the user chose to share (and in --dry-run);
    :func:`resolve_io` only calls it when resolution is incomplete. It returns the ``(inputs, outputs)``
    dicts to use. On an interactive TTY it prompts the user to map the query/filters and
    answers/documents sockets — including any *mandatory* socket inference missed, which would
    otherwise crash the shared prototype at query time. Off a TTY it returns whatever was inferred,
    warning loudly about unmapped mandatory sockets.

    :param skip_validation: When True (``--skip-io-validation``), skip all prompting and mandatory-input
        enforcement and return whatever inference produced. For arbitrary pipelines invoked directly
        rather than via the shared prototype chat UI.
    """
    inferred_inputs = extraction.inferred_inputs
    inferred_outputs = extraction.inferred_outputs
    if skip_validation:
        return inferred_inputs, inferred_outputs
    unmapped = unmapped_mandatory_inputs(extraction.mandatory_inputs, inferred_inputs)

    if not _stdin_is_tty():
        if unmapped:
            typer.echo(f"Warning: {unmapped_mandatory_warning(unmapped)}")
        return inferred_inputs, inferred_outputs

    typer.echo(
        "\nCould not fully infer the pipeline inputs/outputs required for the shared prototype "
        "(the chat UI). Please select them."
    )

    inputs = {key: list(sockets) for key, sockets in inferred_inputs.items()}
    input_sockets = flatten_sockets(extraction.available_inputs)
    if not inputs.get("query"):
        query = _select_socket(input_sockets, "Which socket is the 'query' input?", required=True)
        if query:
            inputs.setdefault("query", []).append(query)
    if not inputs.get("filters"):
        filters = _select_socket(input_sockets, "Which socket is the 'filters' input?", required=False)
        if filters:
            inputs.setdefault("filters", []).append(filters)

    # Any mandatory socket still unmapped would crash the prototype at query time — map each one.
    for socket in unmapped_mandatory_inputs(extraction.mandatory_inputs, inputs):
        key = _select_input_key_for_socket(socket)
        if socket not in inputs.setdefault(key, []):
            inputs[key].append(socket)

    outputs = dict(inferred_outputs)
    if not outputs:
        output_sockets = flatten_sockets(extraction.available_outputs)
        answers = _select_socket(output_sockets, "Which socket is the 'answers' output?", required=False)
        if answers:
            outputs["answers"] = answers
        documents = _select_socket(output_sockets, "Which socket is the 'documents' output?", required=False)
        if documents:
            outputs["documents"] = documents

    return inputs, outputs


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


def _select_input_key_for_socket(socket: str) -> str:
    """Ask which platform input should feed an otherwise-unmapped mandatory socket.

    Returns the chosen input key (``"query"`` or ``"filters"``); defaults to ``"query"`` since the
    shared prototype's chat box sends the user text under ``query``.
    """
    typer.echo(
        f"Mandatory input '{socket}' is not mapped to any platform input; "
        "the shared prototype would fail at query time without it."
    )
    choice = _select_socket(["query", "filters"], f"  Which input feeds '{socket}'?", required=True)
    return choice or "query"


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
