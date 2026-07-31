"""The CLI for the Haystack Enterprise Platform SDK."""

import functools
import json
import logging
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, Union
from uuid import UUID

import structlog
import typer
from tabulate import tabulate
from yaspin import yaspin

__version__ = version("haystack-enterprise-sdk")
from haystack_enterprise_sdk._api.config import (
    DEFAULT_WORKSPACE_NAME,
    ENV_FILE_PATH,
    PLATFORM_URL,
    normalize_base_url,
)
from haystack_enterprise_sdk._api.deployments import (
    DeploymentMode,
    DeploymentServiceLevel,
    PipelineValidationError,
)
from haystack_enterprise_sdk._api.pipeline_run import PipelineRunError
from haystack_enterprise_sdk._api.shared_prototypes import FailedToCreateSharedPrototypeError
from haystack_enterprise_sdk._api.upload_sessions import WriteMode
from haystack_enterprise_sdk._service.deployment_service import (
    CreateOptions,
    DeploymentFailedError,
    DeployResult,
    ServiceNotFoundError,
    ShareOptions,
)
from haystack_enterprise_sdk._service.io_spec import (
    PLATFORM_SERVING_SPEC,
    IntegrationIoSpec,
    render_io_config,
)
from haystack_enterprise_sdk._service.pipeline_transform import (
    STANDARD_INPUT_KEYS,
    STANDARD_OUTPUT_KEYS,
    ExtractionBundle,
    IoResolver,
    PipelineTransformError,
    SocketOption,
    build_config_yaml,
    socket_options,
    unmapped_mandatory_inputs,
    unmapped_mandatory_warning,
)
from haystack_enterprise_sdk.models import PipelineOutputType
from haystack_enterprise_sdk.workflows.sync_client.deployment_client import DeploymentClient
from haystack_enterprise_sdk.workflows.sync_client.files import download as sync_download
from haystack_enterprise_sdk.workflows.sync_client.files import (
    get_upload_session as sync_get_upload_session,
)
from haystack_enterprise_sdk.workflows.sync_client.files import list_files as sync_list_files
from haystack_enterprise_sdk.workflows.sync_client.files import (
    list_upload_sessions as sync_list_upload_sessions,
)
from haystack_enterprise_sdk.workflows.sync_client.files import upload as sync_upload

cli_app = typer.Typer(pretty_exceptions_show_locals=False)


def _configure_cli_logging(verbose: bool) -> None:
    """Set the log verbosity for a CLI run.

    The SDK configures structlog at ``INFO`` on import (see ``haystack_enterprise_sdk/__init__.py``),
    which prints diagnostic log lines from the ``_service``/``_api``/``workflows`` layers over the
    CLI's own output. For the CLI we want a clean console by default, so we override that here:
    hide ``INFO``/``DEBUG`` unless ``--verbose`` is given (``WARNING``+ always shows).

    Only the CLI entry path calls this, so the library default is left unchanged for SDK users.

    :param verbose: When True, show ``INFO``/``DEBUG`` logs; otherwise only ``WARNING`` and above.
    """
    level = logging.DEBUG if verbose else logging.WARNING

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(),
        ],
        # Pin the factory: importing ``haystack`` reconfigures structlog globally to route through
        # stdlib logging, which would send our already-rendered lines through the root logger
        # instead of the console. The CLI owns its own output, so print directly.
        logger_factory=structlog.PrintLoggerFactory(),
    )

    # Cover the one module that uses stdlib logging (``_service/pipeline_extract.py``). Without a
    # handler its sub-WARNING records are dropped, so add one only in verbose mode.
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if verbose and not any(getattr(h, "_deepset_cli", False) for h in root_logger.handlers):
        handler = logging.StreamHandler()
        handler._deepset_cli = True  # type: ignore[attr-defined]  # marker to avoid duplicate handlers
        root_logger.addHandler(handler)


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
    """Upload a folder to Haystack Enterprise Platform.

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
    :param blocking: Whether to wait for the files to be uploaded and displayed in Haystack Enterprise Platform.
    :param timeout_s: Timeout in seconds for the `blocking` parameter.
    :param show_progress: Shows the upload progress.
    :param recursive: Uploads files from subfolders as well.
    :param use_type: A comma-separated string of allowed file types to upload.
    :param enable_parallel_processing: If `True`, Haystack Enterprise Platform ingests the files in parallel.
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
    """Download files from Haystack Enterprise Platform to your local machine.

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
    """Log in to Haystack Enterprise Platform.

    Run `haystack-enterprise login` before performing any tasks in Haystack Enterprise Platform using the SDK or CLI,
    unless you already created the .ENV file.

    This command guides you through creating a global .env file at ~/.haystack-enterprise/.env with your
    Haystack Enterprise Platform `API_KEY`, `API_URL` and `DEFAULT_WORKSPACE_NAME` used for all operations.

    The SDK uses a cascading configuration model with the following precedence:
    1. Explicit parameters (passed via code or CLI)
    2. Environment variables
    3. Local .env file in project root
    4. Global ~/.haystack-enterprise/.env file (supplements local .env)
    5. Built-in defaults
    """
    typer.echo("Log in to Haystack Enterprise Platform")

    # Check for local .env file in the current directory
    local_env = Path.cwd() / ".env"
    if local_env.is_file():
        typer.echo(f"\nNote: Found .env file in the current directory ({local_env}).")
        typer.echo(
            "This local configuration will take precedence over the global configuration you're about to create."
        )

    if typer.confirm(f"Use the deepset platform URL ({PLATFORM_URL})?", default=True):
        api_url = PLATFORM_URL
    else:
        api_url = typer.prompt("Enter the base API URL")

    # Store the bare base URL; the SDK appends the API version when building requests.
    api_url = normalize_base_url(api_url)

    passed_api_key = typer.prompt("Your Haystack Enterprise Platform API_KEY", hide_input=True)
    passed_default_workspace_name = typer.prompt("Your DEFAULT_WORKSPACE_NAME", default="default")

    env_content = f"API_KEY={passed_api_key}\nAPI_URL={api_url}\nDEFAULT_WORKSPACE_NAME={passed_default_workspace_name}"

    ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE_PATH.write_text(env_content, encoding="utf-8")

    typer.echo(f"Global configuration file created at {ENV_FILE_PATH}.")


@cli_app.command()
def logout() -> None:
    """Log out of Haystack Enterprise Platform. This command deletes the .ENV file created during login.

    Example:
    `haystack-enterprise logout`
    """
    typer.echo("Log out of Haystack Enterprise Platform.")
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
    `haystack-enterprise list-files --batch-size 10`

    Example using an odata filter to show only files whose category is "news":
    `haystack-enterprise list-files --odata-filter 'category eq "news"'`
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
    `haystack-enterprise list-upload-sessions --workspace-name default`
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
    """Fetch an upload session from Haystack Enterprise Platform. This method is useful for checking
    the status of an upload session after uploading files to deepset.

    :param session_id: ID of the upload session whose status you want to check.
    :param api_key: deepset API key to use for authentication.
    :param api_url: API URL to use for authentication.
    :param workspace_name: Name of the workspace where you upload your files. Uses the workspace from the .ENV file by default.

    Example:
    `haystack-enterprise get-upload-session --workspace-name default`
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
    create: Optional[bool] = typer.Option(None, "--create/--no-create"),
    managed: bool = False,
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
    io_config: Optional[Path] = None,
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
    :param create: By default the service is reused when it exists and created (serverless unless
        --managed is passed) when it does not. Pass --create to require that it does not exist yet,
        or --no-create to require that it already exists; either way the command fails fast.
    :param managed: Create the service as a managed (provisioned) deployment instead of a serverless
        one. Required to use any of the sizing options below, which serverless ignores. Only applies
        when the service is created.
    :param entrypoint: Name of the pipeline instance or factory when the file defines more than one.
    :param service_level: Service sizing tier when creating the service (with --managed).
    :param min_replicas: Minimum query replicas (with --managed).
    :param max_replicas: Maximum query replicas (with --managed).
    :param cpu: CPU limit, e.g. '1' (with --managed).
    :param memory: Memory limit, e.g. '2Gi' (with --managed).
    :param gpu: GPU memory limit in gigabytes (with --managed).
    :param idle_timeout: Idle timeout in seconds before scale-down (with --managed).
    :param python: Path to the Python interpreter used to load your pipeline (defaults to an
        auto-detected virtualenv near the target file, else the current interpreter).
    :param dry_run: Transform the pipeline and print/write the resulting YAML without deploying. No
        API credentials are needed.
    :param output: With --dry-run, write the transformed YAML to this file instead of stdout.
    :param io_config: Path to a YAML/JSON file with explicit `inputs:`/`outputs:` sections (and an
        optional `pipeline_output_type`) that replace inference and skip the interactive mapping
        review. Defaults to `<target>.io.yaml` next to the pipeline file when that exists (the file
        the interactive review offers to save).
    :param skip_io_validation: Deploy even if mandatory pipeline inputs aren't mapped to platform
        inputs; skips the interactive input/output mapping review and deploys with whatever was
        inferred. Use for arbitrary pipelines you invoke directly rather than via the Playground or
        shared prototype chat UI.
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

    Example (reuses the service, or creates it serverless when missing, and activates the revision):
    `deepset-cloud deploy pipeline.py my-service`

    Create a managed service with explicit sizing (activates and waits for the rollout):
    `deepset-cloud deploy pipeline.py my-service --managed --service-level PRODUCTION --cpu 2`

    Push a revision without rolling it out:
    `deepset-cloud deploy pipeline.py my-service --skip-activation`

    Preview the transformed YAML without deploying:
    `deepset-cloud deploy pipeline.py my-service --dry-run --output out.yaml`
    """
    io_config_path = _resolve_io_config_path(target, io_config)
    io_inputs, io_outputs, io_output_type = (
        _load_io_config(io_config_path) if io_config_path is not None else (None, None, None)
    )

    if dry_run:
        _deploy_dry_run(target, entrypoint, python, output, skip_io_validation, io_inputs, io_outputs, io_output_type)
        return

    # The sizing flags only mean something for a managed service being created, so a combination that
    # would silently drop them is rejected before anything is created on the platform.
    sizing_flags = {
        "--service-level": service_level,
        "--idle-timeout": idle_timeout,
        "--min-replicas": min_replicas,
        "--max-replicas": max_replicas,
        "--cpu": cpu,
        "--memory": memory,
        "--gpu": gpu,
    }
    given_sizing = [flag for flag, value in sizing_flags.items() if value is not None]
    if given_sizing and not managed:
        applies = "only applies" if len(given_sizing) == 1 else "only apply"
        typer.echo(
            f"{', '.join(given_sizing)} {applies} to managed services. "
            f"Add --managed to size the service, or drop the flag to create a serverless one."
        )
        raise typer.Exit(1)

    client = DeploymentClient(api_key=api_key, api_url=api_url, workspace_name=workspace_name)

    # Resolving the service is the first thing we do: a name clash with --create, a missing service
    # with --no-create, or creation-only flags on an existing service are all reported before the
    # pipeline is loaded and transformed. The service itself is only created later, after validation.
    existing = client.find_service(service_name)
    if create is True and existing is not None:
        typer.echo(
            f"Service '{service_name}' already exists in workspace '{workspace_name}'. "
            f"Drop --create to deploy a new revision to it."
        )
        raise typer.Exit(1)
    if create is False and existing is None:
        typer.echo(f"No service named '{service_name}' in workspace '{workspace_name}'. Drop --no-create to create it.")
        raise typer.Exit(1)
    if existing is not None and (managed or given_sizing):
        creation_flags = (["--managed"] if managed else []) + given_sizing
        applies = "only applies" if len(creation_flags) == 1 else "only apply"
        typer.echo(
            f"Service '{service_name}' already exists; {', '.join(creation_flags)} {applies} when a "
            f"service is created. Drop the flag to deploy to the existing service."
        )
        raise typer.Exit(1)

    if existing is not None:
        create_options = None
    elif managed:
        create_options = CreateOptions(
            deployment_mode=DeploymentMode.MANAGED,
            service_level=service_level,
            idle_timeout_in_seconds=idle_timeout,
            min_query_replica_count=min_replicas,
            max_query_replica_count=max_replicas,
            cpu_limit=cpu,
            memory_limit=memory,
            gpu_limit_gigabyte=gpu,
        )
    else:
        create_options = CreateOptions(deployment_mode=DeploymentMode.SERVERLESS)

    activate = not skip_activation

    # A shared prototype requires the service to be deployed, so it is only offered when we activate.
    # The share questions themselves are asked after the deploy (see _offer_shared_prototype), so all
    # of them sit together at the end of the run instead of straddling the rollout.
    if skip_activation and share:
        typer.echo("--share requires activation; drop --skip-activation to share a prototype.")
        raise typer.Exit(1)

    # The I/O mapping is reviewed interactively on every deploy — unless an io-config (explicit or
    # auto-detected) already pins it, which bypasses the review entirely.
    if io_inputs is not None or io_outputs is not None:
        io_resolver = None
    else:
        io_resolver = functools.partial(
            _resolve_io_interactive,
            skip_validation=skip_io_validation,
            mode="review",
            save_path=Path(target).with_suffix(".io.yaml"),
        )

    try:
        if activate:
            with yaspin().arc as spinner:
                spinner.text = f"Deploying '{service_name}'."

                def _on_status(status: object) -> None:
                    spinner.text = f"Rolling out '{service_name}' ({getattr(status, 'value', status)})."

                # The I/O mapping review is interactive and runs mid-deploy; hide the spinner while it
                # prompts so the mapping and "Press Enter to accept" prompt aren't drawn over.
                spinner_resolver = _spinner_paused_resolver(spinner, io_resolver) if io_resolver else None

                result = client.deploy(
                    target,
                    service_name,
                    activate=True,
                    create=create is not False,
                    create_options=create_options,
                    entrypoint=entrypoint,
                    inputs=io_inputs,
                    outputs=io_outputs,
                    pipeline_output_type=io_output_type,
                    io_resolver=spinner_resolver,
                    python_executable=python,
                    validate=not skip_validation,
                    on_status=_on_status,
                )
        else:
            result = client.deploy(
                target,
                service_name,
                create=create is not False,
                create_options=create_options,
                entrypoint=entrypoint,
                inputs=io_inputs,
                outputs=io_outputs,
                pipeline_output_type=io_output_type,
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

    if existing is None:
        # Highlighted: creating a service is a side effect the user did not explicitly ask for, so it
        # should stand out from the regular deploy output.
        typer.secho(
            f"Created {result.deployment.deployment_mode.value.lower()} service '{service_name}' "
            f"in workspace '{workspace_name}'.",
            fg=typer.colors.GREEN,
            bold=True,
        )

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
    elif result.deployment.deployment_mode is DeploymentMode.SERVERLESS:
        # A serverless service has no rollout status to report; the activated revision is what runs.
        typer.echo(f"Serverless service '{service_name}' is serving revision {result.revision.revision_id}.")
    else:
        typer.echo(f"Service '{service_name}' is now {result.deployment.status.value}.")

    if activate:
        _offer_shared_prototype(
            client,
            service_name,
            result,
            share=share,
            expiration_days=share_expiration_days,
            login_required=share_login_required,
        )


@cli_app.command()
def validate(
    target: Path,
    entrypoint: Optional[str] = None,
    python: Optional[str] = None,
    io_config: Optional[Path] = None,
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
    :param io_config: Path to a YAML/JSON file with explicit `inputs:`/`outputs:` sections (and an
        optional `pipeline_output_type`) that replace inference and skip the interactive mapping
        prompt. Defaults to `<target>.io.yaml` next to the pipeline file when that exists.
    :param skip_io_validation: Skip the interactive input/output prompt and use whatever was inferred.
    :param api_key: deepset API key to use for authentication.
    :param api_url: API URL to use for authentication.
    :param workspace_name: Workspace to validate against. Uses the workspace from the .ENV file by default.

    Example:
    `deepset-cloud validate pipeline.py`
    """
    io_config_path = _resolve_io_config_path(target, io_config)
    io_inputs, io_outputs, io_output_type = (
        _load_io_config(io_config_path) if io_config_path is not None else (None, None, None)
    )
    client = DeploymentClient(api_key=api_key, api_url=api_url, workspace_name=workspace_name)
    io_resolver = functools.partial(_resolve_io_interactive, skip_validation=skip_io_validation)
    try:
        result = client.validate(
            target,
            entrypoint=entrypoint,
            inputs=io_inputs,
            outputs=io_outputs,
            pipeline_output_type=io_output_type,
            io_resolver=io_resolver,
            python_executable=python,
        )
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
    io_config: Optional[Path] = None,
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
    :param io_config: Path to a YAML/JSON file with explicit `inputs:`/`outputs:` sections that
        replace inference and skip the interactive mapping prompt. Defaults to `<target>.io.yaml`
        next to the pipeline file when that exists.
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
    io_config_path = _resolve_io_config_path(target, io_config)
    io_inputs, io_outputs, _ = _load_io_config(io_config_path) if io_config_path is not None else (None, None, None)

    if query is None and extra_inputs is None and _stdin_is_tty():
        query = typer.prompt("Query")

    client = DeploymentClient(api_key=api_key, api_url=api_url, workspace_name=workspace_name)
    io_resolver = functools.partial(_resolve_io_interactive, skip_validation=skip_io_validation)
    try:
        result = client.run(
            target,
            entrypoint=entrypoint,
            inputs=io_inputs,
            outputs=io_outputs,
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
    inputs: Optional[dict] = None,
    outputs: Optional[dict] = None,
    pipeline_output_type: Optional[str] = None,
) -> None:
    """Transform the pipeline and print/write the YAML without contacting the API.

    Uses the same extract → resolve → render path as the real deploy (:func:`build_config_yaml`).
    Prompts only for mapping gaps (``mode="gaps"``) so scripted dry runs stay quiet.
    """
    io_resolver = functools.partial(_resolve_io_interactive, skip_validation=skip_io_validation)
    try:
        config_yaml = build_config_yaml(
            target,
            entrypoint=entrypoint,
            inputs=inputs,
            outputs=outputs,
            pipeline_output_type=pipeline_output_type,
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


def _offer_shared_prototype(
    client: DeploymentClient,
    service_name: str,
    result: DeployResult,
    *,
    share: Optional[bool],
    expiration_days: int,
    login_required: Optional[bool],
) -> None:
    """Offer a shared prototype link for a just-deployed service and create it if wanted.

    Runs at the very end of a deploy, so both share questions ("create a link?" and "require login?")
    are asked back to back rather than one before the rollout and one after it.

    :param client: Client used to create the link.
    :param service_name: Name of the deployed service.
    :param result: The deploy result; the link is only offered once the service is serving.
    :param share: ``--share/--no-share`` if given, else None to prompt.
    :param expiration_days: Days until the link expires.
    :param login_required: ``--share-login-required/--no-...`` if given, else None to prompt.
    """
    if not result.is_deployed:
        # Nothing to share yet, so there is nothing to ask about either; only an explicit --share
        # deserves a note that it was skipped.
        if share:
            typer.echo(
                f"Skipped shared prototype: '{service_name}' is not deployed yet "
                f"(status: {result.deployment.status.value}). Create the link in the platform "
                f"once it is deployed."
            )
        return

    if not (share if share is not None else _prompt_share()):
        return

    require_login = login_required if login_required is not None else _prompt_login_required()
    try:
        prototype = client.create_shared_prototype(
            service_name,
            ShareOptions(expiration_days=expiration_days, login_required=require_login),
        )
    except FailedToCreateSharedPrototypeError as err:
        # The deploy itself succeeded, so this is a warning, not a failure exit.
        typer.echo(f"Deployed, but could not create the shared prototype link: {err}")
    else:
        typer.echo(f"Shared prototype link: {prototype.link}")


# Sentinel returned by _select_socket when the user keeps the current mapping (Enter in edit mode).
_KEEP_CURRENT = object()


def _spinner_paused_resolver(spinner: Any, resolver: IoResolver) -> IoResolver:
    """Wrap an interactive ``io_resolver`` so the deploy spinner is hidden while it prompts.

    The I/O mapping review runs mid-deploy, inside the ``yaspin`` spinner context. Without this the
    spinner animation is drawn over the review summary and the "Press Enter to accept" prompt, making
    the deploy look stuck while it silently waits on stdin. Hiding the spinner for the duration of the
    resolver lets the mapping and prompt render cleanly; it resumes once the resolver returns.
    """

    def wrapped(bundle: ExtractionBundle, inputs: dict, outputs: dict) -> Tuple[dict, dict]:
        with spinner.hidden():  # type: ignore[attr-defined]
            return resolver(bundle, inputs, outputs)

    # Expose the wrapped resolver so callers/tests can introspect the underlying configuration.
    wrapped.__wrapped__ = resolver  # type: ignore[attr-defined]
    return wrapped


def _resolve_io_interactive(
    extraction: ExtractionBundle,
    current_inputs: dict,
    current_outputs: dict,
    *,
    skip_validation: bool = False,
    mode: str = "gaps",
    save_path: Optional[Path] = None,
) -> Tuple[dict, dict]:
    """Interactively resolve the pipeline's platform input/output mapping.

    Used as the ``io_resolver`` for the deploy/validate/run flows (:func:`resolve_io` always calls it
    when set). It receives the already-resolved ``current_inputs``/``current_outputs`` and, depending
    on ``mode``:

    - ``"review"`` (the real deploy): always shows the full mapping with per-key descriptions; Enter
      accepts, ``e`` edits key by key.
    - ``"gaps"`` (validate/run/dry-run): silent when the mapping is complete; prompts only for the
      standard keys still missing.

    In both modes, any *mandatory* socket left unmapped is prompted for afterwards — it would crash
    the pipeline at query time. Off a TTY it returns the current mappings unchanged, warning loudly
    about unmapped mandatory sockets, so CI never blocks.

    :param skip_validation: When True (``--skip-io-validation``), skip all prompting and mandatory-input
        enforcement and return the current mappings.
    :param mode: ``"review"`` or ``"gaps"`` (see above).
    :param save_path: When set (review mode), offer to save the confirmed mapping to this io-config
        file so future deploys pick it up automatically.
    """
    spec = PLATFORM_SERVING_SPEC
    inputs = {key: list(sockets) for key, sockets in current_inputs.items()}
    outputs = dict(current_outputs)
    if skip_validation:
        return inputs, outputs
    unmapped = unmapped_mandatory_inputs(extraction.mandatory_inputs, inputs)

    if not _stdin_is_tty():
        if unmapped:
            typer.echo(f"Warning: {unmapped_mandatory_warning(unmapped)}")
        return inputs, outputs

    if mode == "review":
        inputs, outputs = _review_io_mapping(spec, extraction, inputs, outputs)
    else:
        if not (not inputs or not outputs or unmapped):
            return inputs, outputs
        inputs, outputs = _fill_io_gaps(spec, extraction, inputs, outputs)

    # Any mandatory socket still unmapped would crash the pipeline at query time — map each one.
    for socket in unmapped_mandatory_inputs(extraction.mandatory_inputs, inputs):
        key = _select_input_key_for_socket(socket)
        if socket not in inputs.setdefault(key, []):
            inputs[key].append(socket)
        typer.echo(f"  Mapped mandatory input '{socket}' to '{key}'.")

    if mode == "review" and save_path is not None:
        _offer_io_config_save(spec, save_path, inputs, outputs)

    return inputs, outputs


def _review_io_mapping(
    spec: IntegrationIoSpec, extraction: ExtractionBundle, inputs: dict, outputs: dict
) -> Tuple[dict, dict]:
    """Show the resolved I/O mapping summary and let the user accept it or edit it key by key."""
    _echo_io_summary(spec, inputs, outputs)
    while True:
        choice = typer.prompt("Press Enter to accept, or 'e' to edit", default="accept", show_default=False)
        normalized = choice.strip().lower()
        if normalized in ("", "accept", "a", "y", "yes"):
            return inputs, outputs
        if normalized in ("e", "edit"):
            inputs, outputs = _edit_io_mapping(spec, extraction, inputs, outputs)
            _echo_io_summary(spec, inputs, outputs)
            continue
        typer.echo("  Press Enter to accept, or type 'e' to edit.")


def _echo_io_summary(spec: IntegrationIoSpec, inputs: dict, outputs: dict) -> None:
    """Print the I/O mapping summary: every platform key, its mapped sockets, and its description."""
    width = max(len(key.name) for key in (*spec.inputs, *spec.outputs))
    typer.echo("\nI/O mapping (how the platform talks to your pipeline):")
    typer.echo("\n  Inputs")
    for key in spec.inputs:
        sockets = inputs.get(key.name) or []
        mapped = ", ".join(sockets) if sockets else "(not mapped)"
        typer.echo(f"    {key.name:<{width}}  →  {mapped}")
        typer.echo(f"    {'':<{width}}     {key.description} ({key.type_hint})")
    typer.echo("\n  Outputs")
    for key in spec.outputs:
        socket = outputs.get(key.name)
        typer.echo(f"    {key.name:<{width}}  →  {socket or '(not mapped)'}")
        typer.echo(f"    {'':<{width}}     {key.description} ({key.type_hint})")
    typer.echo("")


def _edit_io_mapping(
    spec: IntegrationIoSpec, extraction: ExtractionBundle, inputs: dict, outputs: dict
) -> Tuple[dict, dict]:
    """Walk every platform key, letting the user remap, keep, or unmap it."""
    input_options = socket_options(extraction.available_inputs)
    output_options = socket_options(extraction.available_outputs)
    for key in spec.inputs:
        current = inputs.get(key.name) or []
        choice = _select_socket(
            input_options,
            f"{key.name} — {key.description} ({key.type_hint})",
            required=False,
            current=current,
        )
        if choice is _KEEP_CURRENT:
            continue
        if choice is None:
            inputs.pop(key.name, None)
        else:
            inputs[key.name] = [choice]
    for key in spec.outputs:
        current_socket = outputs.get(key.name)
        choice = _select_socket(
            output_options,
            f"{key.name} — {key.description} ({key.type_hint})",
            required=False,
            current=[current_socket] if current_socket else [],
        )
        if choice is _KEEP_CURRENT:
            continue
        if choice is None:
            outputs.pop(key.name, None)
        else:
            outputs[key.name] = choice
    return inputs, outputs


def _fill_io_gaps(
    spec: IntegrationIoSpec, extraction: ExtractionBundle, inputs: dict, outputs: dict
) -> Tuple[dict, dict]:
    """Prompt only for the standard platform keys that are still unmapped."""
    typer.echo(
        "\nCould not fully determine the pipeline inputs/outputs (needed for the Playground and "
        "shared prototype). Please select them:"
    )
    input_options = socket_options(extraction.available_inputs)
    for key in spec.inputs:
        if inputs.get(key.name):
            continue
        socket = _select_socket(
            input_options, f"Which socket is the '{key.name}' input? ({key.description})", required=False
        )
        if socket:
            inputs.setdefault(key.name, []).append(socket)
    output_options = socket_options(extraction.available_outputs)
    for key in spec.outputs:
        if outputs.get(key.name):
            continue
        socket = _select_socket(
            output_options, f"Which socket is the '{key.name}' output? ({key.description})", required=False
        )
        if socket:
            outputs[key.name] = socket
    return inputs, outputs


def _offer_io_config_save(spec: IntegrationIoSpec, save_path: Path, inputs: dict, outputs: dict) -> None:
    """Offer to persist the confirmed mapping as an io-config file next to the pipeline."""
    if not typer.confirm(
        f"Save this mapping to {save_path.name} so future deploys use it automatically?", default=True
    ):
        return
    try:
        save_path.write_text(render_io_config(spec, inputs, outputs), encoding="utf-8")
    except OSError as err:
        typer.echo(f"Could not write {save_path}: {err}")
        return
    typer.echo(f"Saved {save_path}. Edit it freely, or delete it to map interactively again.")


def _resolve_io_config_path(target: Path, io_config: Optional[Path]) -> Optional[Path]:
    """Pick the io-config file to use: an explicit ``--io-config`` wins, else ``<target>.io.yaml``.

    Announces the auto-detected file so a saved mapping never changes behavior silently.
    """
    if io_config is not None:
        return io_config
    candidate = Path(target).with_suffix(".io.yaml")
    if candidate.is_file():
        typer.echo(f"Using I/O mapping from {candidate} (pass --io-config to override, or delete the file to re-map).")
        return candidate
    return None


def _load_io_config(path: Path) -> Tuple[Optional[dict], Optional[dict], Optional[str]]:
    """Load an explicit pipeline I/O mapping from a YAML or JSON io-config file.

    The file may contain optional ``inputs:``, ``outputs:``, and ``pipeline_output_type:`` sections.
    Input values may be a string or a list of strings (coerced to a list); output values are single
    ``"component.socket"`` strings. Keys beyond the standard platform keys are passed through with a
    note. Returns ``(inputs, outputs, pipeline_output_type)`` with ``None`` for absent sections (a
    section that is present but empty — e.g. fully commented out — also counts as absent).

    :raises typer.Exit: On a missing/unreadable file, invalid YAML/JSON, or a bad shape.
    """
    from ruamel.yaml import YAML  # local import to keep CLI startup light
    from ruamel.yaml.error import YAMLError

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as err:
        typer.echo(f"Could not read --io-config file '{path}': {err}")
        raise typer.Exit(1)  # noqa: B904
    try:
        data = YAML(typ="safe").load(raw)
    except YAMLError as err:
        typer.echo(f"--io-config is not valid YAML/JSON: {err}")
        raise typer.Exit(1)  # noqa: B904
    if not isinstance(data, dict):
        typer.echo("--io-config must be a mapping with optional 'inputs' and 'outputs' sections.")
        raise typer.Exit(1)

    inputs: Optional[dict] = None
    outputs: Optional[dict] = None
    if data.get("inputs"):
        if not isinstance(data["inputs"], dict):
            typer.echo("--io-config 'inputs' must be a mapping of input keys to socket paths.")
            raise typer.Exit(1)
        inputs = {}
        for key, value in dict(data["inputs"]).items():
            sockets = [value] if isinstance(value, str) else value
            if not isinstance(sockets, list) or not all(isinstance(socket, str) for socket in sockets):
                typer.echo(f"--io-config 'inputs.{key}' must be a socket path or a list of socket paths.")
                raise typer.Exit(1)
            if key not in STANDARD_INPUT_KEYS:
                typer.echo(f"Note: '{key}' is not a standard platform input; passing it through as-is.")
            inputs[key] = sockets
    if data.get("outputs"):
        if not isinstance(data["outputs"], dict):
            typer.echo("--io-config 'outputs' must be a mapping of output keys to socket paths.")
            raise typer.Exit(1)
        outputs = {}
        for key, value in dict(data["outputs"]).items():
            if not isinstance(value, str):
                typer.echo(f"--io-config 'outputs.{key}' must be a single socket path string.")
                raise typer.Exit(1)
            if key not in STANDARD_OUTPUT_KEYS:
                typer.echo(f"Note: '{key}' is not a standard platform output; passing it through as-is.")
            outputs[key] = value

    output_type: Optional[str] = data.get("pipeline_output_type")
    if output_type is not None:
        valid = [item.value for item in PipelineOutputType]
        if output_type not in valid:
            typer.echo(f"--io-config 'pipeline_output_type' must be one of: {', '.join(valid)}.")
            raise typer.Exit(1)
    return inputs, outputs, output_type


def _socket_menu_label(option: Union[str, SocketOption]) -> str:
    """Render one menu entry: the socket path plus its type and mandatory flag when known."""
    if isinstance(option, str):
        return option
    details = [detail for detail in (option.type_str, "mandatory" if option.is_mandatory else None) if detail]
    return f"{option.path} ({', '.join(details)})" if details else option.path


def _select_socket(
    options: Sequence[Union[str, SocketOption]],
    prompt: str,
    *,
    required: bool,
    current: Optional[List[str]] = None,
) -> Any:
    """Prompt the user to pick one socket from a numbered menu.

    :param options: The available sockets (plain paths or :class:`SocketOption` with display metadata).
    :param prompt: The question to show above the menu.
    :param required: If True the user must pick one; if False a ``0`` unmaps/skips (returns None).
    :param current: The currently mapped socket path(s), marked in the menu. When set, Enter keeps
        them and :data:`_KEEP_CURRENT` is returned.
    :return: The chosen socket path; None if skipped/unmapped; :data:`_KEEP_CURRENT` if kept.
    """
    if not options:
        return _KEEP_CURRENT if current else None
    values = [option if isinstance(option, str) else option.path for option in options]
    typer.echo(prompt)
    for index, option in enumerate(options, start=1):
        marker = "   [current]" if current and values[index - 1] in current else ""
        typer.echo(f"  {index}. {_socket_menu_label(option)}{marker}")
    if not required:
        typer.echo("  0. not mapped")
    if current:
        default = "keep"
        hint = "enter a number, Enter keeps current" if required else "enter a number, 0 to unmap, Enter keeps current"
    else:
        default = "1" if required else "0"
        hint = "enter a number" if required else "enter a number, or 0 to skip"
    while True:
        choice = typer.prompt(f"  Choice ({hint})", default=default, show_default=False)
        if current and choice.strip().lower() in ("", "keep"):
            return _KEEP_CURRENT
        try:
            number = int(choice)
        except ValueError:
            typer.echo("  Please enter a number.")
            continue
        if number == 0 and not required:
            return None
        if 1 <= number <= len(values):
            return values[number - 1]
        typer.echo("  Out of range.")


def _select_input_key_for_socket(socket: str) -> str:
    """Ask which platform input should feed an otherwise-unmapped mandatory socket.

    Returns the chosen input key (one of the standard inputs); defaults to ``"query"`` since the
    Playground's chat box sends the user text under ``query``.
    """
    typer.echo(
        f"Mandatory input '{socket}' is not mapped to any platform input; "
        "the pipeline would fail at query time without it."
    )
    choice = _select_socket(list(STANDARD_INPUT_KEYS), f"  Which input feeds '{socket}'?", required=True)
    return choice if isinstance(choice, str) else "query"


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
                "deployment_mode": deployment.deployment_mode.value,
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
    `haystack-enterprise --version`
    """
    if value:
        typer.echo(f"Haystack Enterprise Platform SDK version: {__version__}")
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
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show INFO/DEBUG logs from the SDK. By default only warnings and errors are shown.",
    ),
) -> None:  # noqa
    """The CLI for the Haystack Enterprise Platform SDK.

    This documentation uses Python type hints to provide information about the arguments and return values.
    Typer turns these type hints into a CLI interface. To see how these arguments are used in the CLI, check the
    Typer documentation: https://typer.tiangolo.com/tutorial/arguments/optional or run
    `haystack-enterprise <command> --help` to see the arguments for a specific command.

    Boolean values are converted to `-no-<variable>` or `-<variable>` flags in the CLI. For example, to disable
    the progress bar, use `--no-show-progress`.

    Lists can be passed by using the same flag multiple times. For example, to scan only `.txt` and `.pdf` files,
    when uploading use `--use-type .txt --use-type .pdf`.

    Pass `--verbose` (or `-v`) to any command to see the SDK's INFO/DEBUG logs.
    """
    _configure_cli_logging(verbose)


def run_packaged() -> None:
    """Run the packaged CLI.

    This is the entrypoint for the package to enable running the CLI using typer.

    Example:
    `haystack-enterprise run-packaged`
    """
    cli_app()


if __name__ == "__main__":
    cli_app()
