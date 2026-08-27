# CLI Command Flow

The `haystack-enterprise` CLI takes a Haystack pipeline or agent that you build **locally** and
moves it, step by step, onto the Haystack Enterprise Platform. Each step builds on the previous one and
runs the **same transform** under the hood, so what you validate is what you run, and what you
run is what you deploy.

The flow is meant to be **iterative**: change your pipeline locally, re-validate, re-run, and
re-deploy as often as you like.

```mermaid
flowchart LR
    A["1. Build<br/>locally with Haystack"] --> B["2. Validate<br/>against the platform"]
    B --> C["3. Run<br/>in the platform sandbox"]
    C --> D["4. Deploy<br/>to a service"]
    B -. "fix & iterate" .-> A
    C -. "fix & iterate" .-> A
    D -. "fix & iterate" .-> A
    D --> E["Optional:<br/>share a prototype link"]
```

All commands below take the **same first argument**: the path to the local Python file that
defines your pipeline or agent (`pipeline.py` in the examples). When a file defines more than one
pipeline instance or factory, use `--entrypoint` to pick one.

> **Note on the Python environment.** Your pipeline is loaded in your project's Python
> environment (an auto-detected virtualenv near the file, or the interpreter you pass with
> `--python`). The CLI's own environment does **not** need your pipeline's dependencies installed.

---

## 1. Build your pipeline or agent locally

Build and test your pipeline or agent locally with [Haystack](https://haystack.deepset.ai/),
exactly as you normally would. No `haystack-enterprise` command is involved at this stage — you
write plain Haystack code:

```python
# pipeline.py
from haystack import Pipeline
# ... define components and wire them together ...

pipeline = Pipeline()
# pipeline.add_component(...)
# pipeline.connect(...)
```

Iterate here until the pipeline runs the way you want. Everything that follows takes this file as
its input.

---

## 2. Validate

`validate` checks that your local pipeline is **deployable**. It runs the same transform the deploy
uses (rewriting local custom components into the platform `Code` component), then validates the
resulting YAML against the platform — without deploying anything.

```shell
haystack-enterprise validate pipeline.py
```

- Prints any warnings and errors.
- Exits **non-zero** if there are blocking (ERROR) issues.
- If it reports `Pipeline is valid.`, the pipeline is deployable.

The check runs against the **`haystack-ai` version your pipeline pins**, not the platform host's own
Haystack: the transform records the version it loaded your pipeline under, and `validate` asks the
platform to run the check in a worker built for exactly that version. So a component that exists in
one version but not the other is judged by the version that will actually run your pipeline.

The first check against a given version is slow: the platform builds an environment for it (roughly
20-30s), which can outlast its own validation timeout. `validate` retries once when that happens — the
build leaves its downloads cached, so the second attempt usually lands. Later checks against the same
version are fast.

The platform only validates against a Haystack version it lists as a compatibility target, though it
will *serve* any version you pin. So if it declines your version — or the build still does not finish
in time — `validate` does not fail the pipeline: it re-runs the check against the platform's own
Haystack and prints a **warning** that the pin went unhonored, so you know version-specific problems
may have been missed.

`validate` never prompts — reporting problems is the point. It uses the input/output mapping inferred
from your socket names, so it also tells you when that mapping is not servable ("You need to connect at
least one of the inputs (`query` or `messages`)…"); `deploy` asks you to fix that interactively. You can
pin the mapping in a config file (`<target>.io.yaml`, picked up automatically) or supply your own with
`--io-config` — see [The io-config file](#the-io-config-file).

```shell
# validate a specific entrypoint with an explicit interpreter and IO config
haystack-enterprise validate pipeline.py --entrypoint my_pipeline --python .venv/bin/python --io-config pipeline.io.yaml
```

---

## 3. Run

`run` executes your local pipeline **in the platform sandbox** and prints the results back in your
terminal — again, without deploying. This is the same "run without deploying" the builder/playground
offers, so you can check real output before committing to a service.

```shell
haystack-enterprise run pipeline.py --query "What is deepset?"
```

- `--query` routes text to the sockets mapped under the pipeline's `query` input. On an interactive
  terminal you are prompted for it if you pass neither `--query` nor `--inputs`. If the mapping does not
  say where a query goes, you are asked once which socket receives it — the only mapping question `run`
  ever asks, and it is skipped entirely when you pass only `--inputs`.
- `--inputs` passes explicit run inputs as JSON — a literal string or `@path/to/file.json`. The
  shape is the Haystack run inputs dict, `{"component": {"socket": value}}`. Explicit inputs are
  merged over (and win against) anything derived from `--query`.
- `--include-outputs-from` limits the result to specific components (repeat the option for several);
  defaults to all components.
- `--output` writes the result JSON to a file instead of printing it.

Unlike `validate` and `deploy`, **`run` is not pinned to the `haystack-ai` version your pipeline
declares** — the sandbox executes against the platform's own Haystack. The YAML it runs is the same one
a deploy would push, but the Haystack running it may not be, so a version-specific failure can appear
here and not in a deployed service (or the reverse). Use `validate` for the version-accurate check.

```shell
# run with explicit inputs from a file and save the output
haystack-enterprise run pipeline.py --inputs @inputs.json --output result.json
```

---

## 4. Deploy to a service

`deploy` pushes your pipeline as a new **revision** of a service deployment. By default it activates
the revision, and for a managed service it also waits for the rollout to finish.

```shell
# reuse the service (or create it serverless if it doesn't exist), then activate the revision
haystack-enterprise deploy pipeline.py my-service
```

Useful variants:

```shell
# create a managed (provisioned) service with explicit sizing
haystack-enterprise deploy pipeline.py my-service --managed --service-level PRODUCTION --cpu 2

# describe what changed in this revision
haystack-enterprise deploy pipeline.py my-service -m "Bump embedder model to bge-large"

# push a revision without rolling it out
haystack-enterprise deploy pipeline.py my-service --skip-activation

# preview the transformed YAML without deploying (no API credentials needed)
haystack-enterprise deploy pipeline.py my-service --dry-run --output out.yaml
```

Common options:

- The service is looked up by name first: if it exists the revision is pushed to it, otherwise the
  service is created and the CLI tells you so. New services are **serverless**: they provision no
  workload and run the active revision per request, so there is no rollout to wait for.
- `--create` requires that the service does *not* exist yet; the command fails if the name is already
  taken. `--no-create` is the opposite: it requires the service to exist and fails if it is missing
  (useful in CI to catch a typo'd service name instead of provisioning a new service).
- `--managed` creates a managed (provisioned) service instead. Only managed services can be sized, so
  `--service-level`, `--min-replicas`, `--max-replicas`, `--cpu`, `--memory`, `--gpu` and
  `--idle-timeout` require `--managed`; passing them without `--managed`, or passing creation-only
  flags for a service that already exists, is an error rather than a silently ignored flag.
- `--comment` / `-m` is the comment stored on the revision, so you can tell revisions apart in the
  platform UI. Every revision needs one; when you omit the flag the CLI generates a comment naming the
  pipeline file and, if it sits in a git repository, the current branch, commit and a link to the commit
  on GitHub/GitLab/Bitbucket — e.g.
  `Deployed pipeline.py via haystack-enterprise CLI (main@a1b2c3d) https://github.com/org/repo/commit/…`.
- `--skip-activation` pushes the revision as `PENDING` without rolling it out.
- `--skip-validation` skips the pre-deploy YAML validation (validation runs by default and aborts on
  blocking issues).
- `--io-config` / `--skip-io-validation` control the input/output mapping, same as in `validate`.

The platform requires a servable query pipeline to map **a query input** (`query` or `messages`) and **at
least one output**; without them it rejects the deploy. So a plain deploy asks at most those two
questions, and only for whichever one the socket names did not already answer:

```
Which socket receives the query?
  1. greeter.name (str, mandatory)
  0. not mapped
> 1
```

A pipeline with conventionally named sockets (`query`, `answers`, `replies`, `documents`) is mapped by
inference and asks nothing. Pin the mapping in `<pipeline>.io.yaml` (or `--io-config`) to skip the
questions entirely; `--share` reviews the whole mapping instead (see below).

If you interrupt the command (Ctrl-C) during rollout, the rollout continues on the platform. Check
progress any time with:

```shell
haystack-enterprise service-status my-service
```

### Calling the deployed service

Once the service is serving, `deploy` prints its OpenAI-compatible chat-completions endpoint:

```
POST https://api.cloud.deepset.ai/api/v1/workspaces/<workspace>/deployments/<deployment-id>/chat/completions
```

Send `{"model": "<workspace>/<service-name>", "messages": [...]}` with an `Authorization: Bearer` API
key and you get back a server-sent-event stream of `chat.completion.chunk` objects. Any OpenAI client
works: use everything up to and including `/deployments/<deployment-id>` as its `base_url`.

### Optional: share a prototype link

A **shareable prototype link** opens a chat UI for your pipeline. It is never created unless you ask:

```shell
# deploy and create a share link that expires in 7 days, no login required
haystack-enterprise deploy pipeline.py my-service --share --share-expiration-days 7 --no-share-login-required
```

- `--share` creates the link. Because the chat UI routes through the pipeline's input/output mapping,
  this is also what asks you to review that mapping (and offers to save it as `<pipeline>.io.yaml`).
- `--share-expiration-days` sets how long the link stays valid (default 30).
- `--share-login-required` / `--no-share-login-required` control whether recipients must log in; you are
  asked after the rollout if neither is given.

Sharing requires the service to be deployed, so `--share` cannot be combined with `--skip-activation`.
If the platform does not classify your pipeline as a chat pipeline, the link is still created but the
CLI warns that the chat UI may not render the output well — set `pipeline_output_type: chat` in the
io-config if that classification is wrong.

---

## The io-config file

`<target>.io.yaml` (picked up automatically, or passed with `--io-config`) pins everything the CLI would
otherwise infer or ask about. `deploy --share` offers to write it for you, fully commented — that
generated file is the reference; this is the summary.

Two kinds of thing live in it. The `inputs:`/`outputs:` sections are the **mapping** between the
platform's named keys and your pipeline's sockets. Everything else is a **top-level pipeline setting**,
written straight through to the deployed config:

| Key | Meaning | Default when absent |
| --- | ------- | ------------------- |
| `inputs:` | Platform input key → one or more `component.socket` paths | Inferred from your socket names |
| `outputs:` | Platform output key → a single `component.socket` path | Inferred from your socket names |
| `pipeline_output_type:` | How the Playground renders results: `generative`, `chat`, `extractive`, `document` | Inferred from the pipeline shape |
| `session_storage:` | `true` gives the pipeline a per-session workspace, so files a tool writes survive to the next run in the same search session | Off |
| `async_enabled:` | `true` runs the graph with `Pipeline.run_async`, so independent branches overlap instead of running one at a time. **Only accepted alongside a `dependencies:` pin of `haystack-ai==3.0` or later** — below that the pipeline class says it (build an `AsyncPipeline`), so the key is rejected rather than competing with the class. Optional under such a pin: the pin unlocks the key, it does not require it | Off (the platform's own default), or inferred from the pipeline class below 3.0 |
| `dependencies:` | pip pins the deployed revision installs. **Replaces** the automatic pin rather than adding to it, so include `haystack-ai` yourself if you still want it; `[]` ships no pins at all | The `haystack-ai` version of the interpreter that loaded your pipeline |

```yaml
# pipeline.io.yaml
inputs:
  query:
    - retriever.query
outputs:
  answers: answer_builder.answers
pipeline_output_type: chat
session_storage: true
async_enabled: true       # accepted because the pin below is 3.x
dependencies:
  - haystack-ai==3.0.0
  - my-private-lib==1.4
```

`async_enabled` is the one setting whose acceptance depends on another key. Async execution is a
property of the pipeline, and up to Haystack 2.x the pipeline *class* is where it lives — `deploy`
reads it off an `AsyncPipeline` instance. Haystack 3.0 folded that class into `Pipeline`, leaving the
inference nothing to read, which is the only reason the key exists. Gating it on the pin keeps one
source of truth per version instead of two that can disagree. An io-config with no `haystack-ai` pin
counts as 2.x: the automatic pin is read off whichever interpreter loaded your pipeline, which is not
always the version the revision installs.

Pinning 3.x does not oblige you to say anything about async. Leave `async_enabled` out and the key is
left out of the deployed config too, where the platform applies its own default of off — which is what
almost every pipeline wants. Writing `async_enabled: false` says exactly the same thing.

Any other top-level key is **passed through to the pipeline config unchanged**, with a note telling you
it was not recognised. That is deliberate: it means a platform config key newer than your installed SDK
can still be set from here. The note is the only signal that the SDK did not validate it, so read it —
a typo like `session_storge: true` passes through exactly the same way. Keys the transform derives
itself (`components`, `connections`, `metadata`, and the two mapping sections) are rejected instead of
passed through.

`run` uses only the mapping. The settings that describe a *deployed revision* — the sandbox installs
nothing, has no search session, and renders no Playground result — are stripped by `run`.
`async_enabled` is not one of them: it changes how the graph executes, which a sandbox run does too.

---

## Iterating

Because every step reads the same local file and applies the same transform, iterating is just
re-running a command:

1. Edit `pipeline.py` locally.
2. `haystack-enterprise validate pipeline.py` — is it still deployable?
3. `haystack-enterprise run pipeline.py --query "..."` — do the results look right?
4. `haystack-enterprise deploy pipeline.py my-service` — push a new revision.

Repeat as needed. Each deploy creates a new revision of the service, so you can roll changes out
incrementally.

---

## Running in CI

Spinners and progress bars are drawn by repainting one line, which a CI log viewer cannot do — it
appends a new line per repaint, so a multi-minute rollout becomes thousands of log lines. So when
`CI` is set — GitHub Actions, GitLab CI and Jenkins all set it — the CLI stops animating:

- `deploy` prints each rollout status once, as a plain line, instead of animating a spinner:

  ```
  Deploying 'my-service'.
  Rolling out 'my-service' (DEPLOYMENT_SCHEDULED).
  Rolling out 'my-service' (DEPLOYED).
  ```

- `run` prints nothing while it waits — its spinner shows the elapsed seconds, which as plain lines
  would be one line per second. The JSON result is still written to stdout.
- Progress bars (`upload`, `download`) are dropped. `haystack-enterprise --verbose upload …` logs the
  ingestion progress as ordinary INFO lines instead.

Set `CI=false` (or unset it) to force the animations back on. Independently of `CI`, `run` never
animates when its stdout is redirected, so a piped JSON payload stays parseable.

---

## Command reference

| Step | Command | What it does | Talks to the platform? |
| ---- | ------- | ------------ | ---------------------- |
| 1. Build | *(Haystack, no CLI)* | Build & test your pipeline/agent locally | No |
| 2. Validate | `haystack-enterprise validate pipeline.py` | Transform + validate the YAML; confirm it's deployable | Yes |
| 3. Run | `haystack-enterprise run pipeline.py --query "..."` | Transform + run in the platform sandbox, results in your terminal | Yes |
| 4. Deploy | `haystack-enterprise deploy pipeline.py my-service` | Reuse or create the service (serverless, or `--managed`), push & activate a revision; optional share link | Yes |
| — | `haystack-enterprise service-status my-service` | Check a service's current status | Yes |

> On Windows, replace `haystack-enterprise` with `python -m haystack_enterprise_sdk.cli`.

For the full list of options on any command, run it with `--help`:

```shell
haystack-enterprise deploy --help
```
