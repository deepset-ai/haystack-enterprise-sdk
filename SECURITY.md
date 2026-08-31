# Security Policy

## Reporting a Vulnerability

Please do not report security vulnerabilities through public GitHub issues or
pull requests, and do not include exploit details in a branch or PR title.

Report suspected vulnerabilities in `haystack-enterprise-sdk` — including in the
published `haystack-enterprise-sdk` package — by email to
[opensource-security@deepset.ai](mailto:opensource-security@deepset.ai).

In your message, please include:

1. Reproducible steps to trigger the vulnerability.
2. An explanation of what makes you think there is a vulnerability.
3. The SDK version and, if relevant, the deepset Cloud environment you observed it against.
4. Any information you may have on active exploitation.

## Vulnerability Response

We aim to review your report within 5 business days and do a preliminary analysis
to confirm that the vulnerability is plausible. We will keep you updated on the
status of the issue and coordinate a fix and, where relevant, a release.

## Scope

This SDK is a client for the deepset Cloud API. Findings in the deepset Cloud
service itself are in scope for the same address, but are handled by the service
team rather than through this repository.

Credentials belong in the environment, not in source: the SDK reads its API key
from `API_KEY` (or an `.env` file). Reports that require an attacker to already
control that environment, the machine running the SDK, or the pipeline
definitions it submits are out of scope.
