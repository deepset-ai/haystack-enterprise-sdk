"""This is the entrypoint for the package."""

import logging

import structlog

from haystack_enterprise_sdk._api.deployments import (
    DeploymentMode,
    DeploymentServiceLevel,
    PipelineValidationError,
    PipelineValidationIssue,
    PipelineValidationResult,
)
from haystack_enterprise_sdk._api.pipeline_run import PipelineRunError
from haystack_enterprise_sdk._api.shared_prototypes import (
    FailedToCreateSharedPrototypeError,
    SharedPrototype,
)
from haystack_enterprise_sdk._service.deployment_service import (
    CreateOptions,
    DeploymentFailedError,
    DeployResult,
    ServiceNotFoundError,
    ShareOptions,
)
from haystack_enterprise_sdk._service.pipeline_service import (
    ErrorDetail,
    HaystackEnterpriseValidationError,
)
from haystack_enterprise_sdk._service.pipeline_transform import (
    PipelineSettings,
    PipelineTransformError,
)
from haystack_enterprise_sdk.models import (
    BaseConfig,
    IndexConfig,
    IndexInputs,
    IndexOutputs,
    PipelineConfig,
    PipelineInputs,
    PipelineOutputs,
    PipelineOutputType,
)
from haystack_enterprise_sdk.workflows.async_client.async_pipeline_client import (
    AsyncPipelineClient,
)
from haystack_enterprise_sdk.workflows.async_client.deployment_client import (
    AsyncDeploymentClient,
)
from haystack_enterprise_sdk.workflows.sync_client.deployment_client import DeploymentClient
from haystack_enterprise_sdk.workflows.sync_client.pipeline_client import PipelineClient

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)

log = structlog.get_logger()

__all__ = [
    "AsyncPipelineClient",
    "BaseConfig",
    "ErrorDetail",
    "PipelineClient",
    "PipelineConfig",
    "PipelineInputs",
    "PipelineOutputs",
    "IndexConfig",
    "IndexInputs",
    "IndexOutputs",
    "HaystackEnterpriseValidationError",
    "PipelineOutputType",
    "DeploymentClient",
    "AsyncDeploymentClient",
    "CreateOptions",
    "DeploymentMode",
    "DeploymentServiceLevel",
    "DeployResult",
    "ShareOptions",
    "SharedPrototype",
    "DeploymentFailedError",
    "ServiceNotFoundError",
    "FailedToCreateSharedPrototypeError",
    "PipelineSettings",
    "PipelineTransformError",
    "PipelineValidationError",
    "PipelineValidationResult",
    "PipelineValidationIssue",
    "PipelineRunError",
]
