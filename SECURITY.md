# Security Policy

## Report a Vulnerability

If you have found a security vulnerability in the Haystack Enterprise SDK, please report via email to
[opensource-security@deepset.ai](mailto:opensource-security@deepset.ai).

In your message, please include:

1. Reproducible steps to trigger the vulnerability.
2. An explanation of what makes you think there is a vulnerability.
3. Any information you may have on active exploitations of the vulnerability (zero-day).
4. An explanation of why you believe the vulnerability is not out of scope. See the Out of Scope section below.

We encourage reports that are meaningful, high-impact, and reviewed by a human before submission. Fully automated or AI-generated reports submitted without human review and validation are unlikely to meet this bar and risk being declined.

## Out of Scope

The Haystack Enterprise SDK is a client library and CLI intended to run inside a trusted execution environment: a developer machine or a CI runner, under the developer's own credentials, against a workspace they already have access to. It assumes that the files, arguments, and configuration it is given come from the operator who runs it, not from an untrusted third party.

Any vulnerability that can only be triggered by passing unsanitized, attacker-controlled input to the SDK is considered out of scope. This reflects a conscious design decision after evaluating the trade-offs and risks: as a developer tool, the SDK cannot and should not enforce input validation on behalf of the operator who invokes it.

The following area has been deliberately scoped out below and we ask that you read it carefully before submitting.

### Loading Local Pipelines

Commands such as `validate`, `run`, and `deploy` take a path to a local pipeline file and load it in a Python interpreter to extract the pipeline it defines. Loading a pipeline executes the module, and it is designed to do so: that is the only way to obtain the pipeline object a user has built in their own code.

**Pointing the SDK at a pipeline file from an untrusted source is unsafe by design.** This is not a hidden weakness but the expected consequence of a tool that runs user-authored code. The security responsibility lies with the operator: pipeline files must be treated as code, stored and transmitted with the same controls applied to source code, and never loaded from untrusted or user-controlled input without review. Reports that demonstrate, for example, arbitrary code execution from a pipeline file that an operator chose to load are out of scope.

However, if you find a way to achieve arbitrary code execution that does *not* rely on an operator loading an untrusted pipeline (for example, an issue in how the SDK processes a response from the platform), that finding is in scope.

---

If you are uncertain whether a finding falls within scope, feel free to reach out before submitting a full report.

## Vulnerability Response

We aim to review your report within 5 business days where we do a preliminary analysis
to confirm that the vulnerability is plausible. Otherwise, we'll decline the report.

We won't disclose any information you share with us but we'll use it to get the issue
fixed or to coordinate a vendor response, as needed.

We'll keep you updated of the status of the issue.

Our goal is to disclose bugs as soon as possible once a user mitigation is available.
Once we get a good understanding of the vulnerability, we'll set a disclosure date after consulting the author of the report and the Haystack Enterprise SDK maintainers.
