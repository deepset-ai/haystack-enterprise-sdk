"""Sync pipeline client for importing pipelines and indexes to Haystack Enterprise Platform."""

import asyncio

import structlog

from haystack_enterprise_sdk._service.pipeline_service import PipelineProtocol
from haystack_enterprise_sdk.models import IndexConfig, PipelineConfig
from haystack_enterprise_sdk.workflows.async_client.async_pipeline_client import (
    AsyncPipelineClient,
)

logger = structlog.get_logger(__name__)


class PipelineClient:  # pylint: disable=too-few-public-methods
    """Sync client for importing Haystack pipelines and indexes to Haystack Enterprise Platform.

    Example for importing a Haystack pipeline or index to Haystack Enterprise Platform:
        ```python
        from haystack_enterprise_sdk import (
            PipelineClient,
            PipelineConfig,
            PipelineInputs,
            PipelineOutputs,
            IndexConfig,
            IndexInputs,
        )
        from haystack import Pipeline

        # Initialize the client with configuration from environment variables (after running `haystack-enterprise login`)
        client = PipelineClient()

        # or initialize the client with explicit configuration
        client = PipelineClient(
            api_key="your-api-key",
            workspace_name="your-workspace",
            api_url="https://api.cloud.deepset.ai"
        )

        # Configure your pipeline
        pipeline = Pipeline()

        # Configure import
        # if importing a pipeline, use PipelineConfig
        config = PipelineConfig(
            name="my-pipeline",
            inputs=PipelineInputs(
                query=["prompt_builder.query"],
                filters=["bm25_retriever.filters", "embedding_retriever.filters"],
            ),
            outputs=PipelineOutputs(
                answers="answers_builder.answers",
                documents="ranker.documents",
            ),
            strict_validation=False,  # Fail on validation errors (default: False, warnings only)
            overwrite=False,  # Overwrite existing pipelines with the same name. If True, creates if it doesn't exist (default: False)
        )

        # if importing an index, use IndexConfig
        config = IndexConfig(
            name="my-index",
            inputs=IndexInputs(files=["file_type_router.sources"]),
            strict_validation=False,  # Fail on validation errors (default: False, warnings only)
            overwrite=False,  # Overwrite existing indexes with the same name. If True, creates if it doesn't exist (default: False)
        )

        # sync execution
        client.import_into_platform(pipeline, config)
        ```
    """

    def __init__(
        self,
        api_key: str | None = None,
        workspace_name: str | None = None,
        api_url: str | None = None,
    ) -> None:
        """Initialize the Pipeline Client.

        The client can be configured in two ways:

        1. Using environment variables (recommended):
           - Run `haystack-enterprise login` to set up the following environment variables:
             - `API_KEY`: Your Haystack Enterprise Platform API key
             - `API_URL`: The URL of the Haystack Enterprise Platform API
             - `DEFAULT_WORKSPACE_NAME`: The workspace name to use.

        2. Using explicit parameters:
           - Provide the values directly to this constructor
           - Any missing parameters will fall back to environment variables

        :param api_key: Your Haystack Enterprise Platform API key. Falls back to `API_KEY` environment variable.
        :param workspace_name: The workspace to use. Falls back to `DEFAULT_WORKSPACE_NAME` environment variable.
        :param api_url: The URL of the Haystack Enterprise Platform API. Falls back to `API_URL` environment variable.
        :raises ValueError: If no api key or workspace name is provided and `API_KEY` or `DEFAULT_WORKSPACE_NAME` is not set in the environment.
        """
        self._async_client = AsyncPipelineClient(
            api_key=api_key,
            workspace_name=workspace_name,
            api_url=api_url,
        )

    def import_into_platform(self, pipeline: PipelineProtocol, config: IndexConfig | PipelineConfig) -> None:
        """Import a Haystack `Pipeline` or `AsyncPipeline` into Haystack Enterprise Platform synchronously.

        The pipeline must be imported as either an index or a pipeline:
        - An index: Processes files and stores them in a document store, making them available for
          pipelines to search.
        - A pipeline: For other use cases, for example, searching through documents stored by index pipelines.

        :param pipeline: The Haystack `Pipeline` or `AsyncPipeline` to import.
        :param config: Configuration for importing into deepset, use either `IndexConfig` or `PipelineConfig`.
            If importing an index, the config argument is expected to be of type `IndexConfig`,
            if importing a pipeline, the config argument is expected to be of type `PipelineConfig`.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("Event loop is closed")
            # do not close if event loop already exists, e.g. in Jupyter notebooks
            should_close = False
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            should_close = True

        try:
            return loop.run_until_complete(self._async_client.import_into_platform(pipeline, config))
        finally:
            if should_close:
                loop.close()
