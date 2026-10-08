# Code build demo

The smallest project that exercises a code build end to end, with no LLM and no secrets:

- `app.py`: a `SuperComponent` with a platform-shaped API (`query` in, `answers` out).
- `demo_lib/text.py`: a helper module. Every answer contains "served from a code bundle", which proves
  the platform imported the whole project, not just the entrypoint.

## Prerequisites

- An org with `fleet_enabled`. Other orgs get `403 CODE_BUNDLES_DISABLED`.
- A runtime with code-build support on that org's fleet, and a hub-api with the `code_bundles` endpoints.
- Run the commands from the root of this repository (the SDK checkout with code-build support).

```sh
export API_URL=https://<api host>   # no /api suffix, e.g. https://api-pr-haystack-hub-api-<PR>.dev.cloud.dpst.dev
export API_KEY=<api key of a user in the fleet-enabled org>
```

## 1. Check the bundle locally

```sh
uv run haystack-enterprise deploy examples/code-bundle-demo code-bundle-demo \
  --build code --entrypoint app.py:app --dry-run --output /tmp/demo.tar.gz
tar -tzf /tmp/demo.tar.gz
```

The output shows the settings the platform gets (`query` → `app.query`, `answers` → `app.answers`,
`haystack-ai==3.2.0`) and the bundle sha. The same files always give the same sha.

## 2. Deploy

```sh
uv run haystack-enterprise deploy examples/code-bundle-demo code-bundle-demo \
  --build code --entrypoint app.py:app --workspace-name <workspace>
```

This checks that the platform supports code builds, uploads the bundle if it isn't there yet, then
creates the service and pushes and activates the revision. Deploying again without changes reuses the
uploaded bundle.

## 3. Query it

The demo doesn't stream tokens, so use the search endpoint, which returns the final answers. The chat
completions endpoint only assembles streamed text and returns empty content for it.

```sh
curl -s "$API_URL/api/v1/workspaces/<workspace>/deployments/<service id>/search" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"queries": ["hello fleet"]}'
```

Expected answer: `HELLO FLEET! (served from a code bundle)`.

## Things to check

- The answer text contains the marker from `demo_lib`.
- A trace shows up for the run.
- Changing `demo_lib/text.py` and deploying again gives a new sha, and the new answer shows up (one worker
  per bundle, no stale code).
