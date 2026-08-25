<p align="center">
  <a href="https://cloud.deepset.ai/"><img src="_images/logo.svg" alt="Haystack Enterprise SDK" width="420"></a>
</p>

[![Coverage badge](https://github.com/deepset-ai/haystack-enterprise-sdk/raw/python-coverage-comment-action-data/badge.svg)](https://github.com/deepset-ai/haystack-enterprise-sdk/tree/python-coverage-comment-action-data)
[![Tests](https://github.com/deepset-ai/haystack-enterprise-sdk/actions/workflows/continuous-integration.yml/badge.svg)](https://github.com/deepset-ai/haystack-enterprise-sdk/actions/workflows/continuous-integration.yml)
[![Compliance Checks](https://github.com/deepset-ai/haystack-enterprise-sdk/actions/workflows/compliance.yml/badge.svg)](https://github.com/deepset-ai/haystack-enterprise-sdk/actions/workflows/compliance.yml)

The Haystack Enterprise SDK is an open source software development kit that provides convenient access and integration with Haystack Enterprise Platform, a powerful cloud offering for various natural language processing (NLP) tasks. To learn more about Haystack Enterprise Platform, please have a look at the [official Documentation](https://docs.cloud.deepset.ai/).

# Supported Features
The following examples demonstrate how to use the Haystack Enterprise SDK to interact with Haystack Enterprise Platform using Python.
You can use the Haystack Enterprise SDK in the command line as well. For more information, see the [CLI documentation](/haystack-enterprise-sdk/examples/cli).
- [SDK Examples - Upload datasets](/haystack-enterprise-sdk/examples/sdk)
- [CLI Examples - Upload datasets](/haystack-enterprise-sdk/examples/cli/)

## Installation
The SDK is not published to a package registry yet. Install it directly from the repository with [uv](https://docs.astral.sh/uv/):
```bash
uv tool install git+https://github.com/deepset-ai/haystack-enterprise-sdk.git
```

After installing the Haystack Enterprise SDK, you can use it to interact with Haystack Enterprise Platform. It comes with a command line interface (CLI), that you can use by calling:
```bash
haystack-enterprise --help
```

### Development Installation
To install the Haystack Enterprise SDK for development, clone the repository and install the package in editable mode:
```bash
pip install hatch==1.7.0
hatch build
```

Instead of calling the cli from the build package, you can call it directly from the source code:
```bash
python3 -m haystack_enterprise_sdk.cli --help
```

---
## Interested in Haystack Enterprise Platform?
If you are interested in exploring Haystack Enterprise Platform, visit cloud.deepset.ai.
Haystack Enterprise Platform provides a range of NLP capabilities and services to help you build and deploy powerful
natural language processing applications.

## Interested in Haystack?
Haystack Enterprise Platform is powered by Haystack, an open source framework for building end-to-end NLP pipelines.
 - [Project website](https://haystack.deepset.ai/)
 - [GitHub repository](https://github.com/deepset-ai/haystack)
