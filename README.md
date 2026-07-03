# ruoyu-costrouter

`ruoyu-costrouter` packages the Hermes `cost_router` delegation tool so another Hermes user can reproduce a routing-first multi-model setup.

The tool adds a model-facing `cost_router(...)` function that routes a subtask through a named Hermes worker profile, reads that profile's `config.yaml`, resolves provider/model credentials, and then dispatches through Hermes' existing `delegate_task` runtime.

## What it is

- A small Hermes core patch for the agent-loop tool path.
- Worker profile templates for common routing lanes.
- Install and uninstall scripts that apply or reverse the patch against a local Hermes checkout.
- Usage examples and verification commands.

## Why this is a patch, not a pure plugin

`cost_router` must call `delegate_task` with the live `parent_agent` object. In current Hermes, that object is available only inside the agent loop, so the tool needs three core integration points:

1. register `cost_router` in `tools/delegate_tool.py`;
2. dispatch it through `AIAgent._dispatch_cost_router(...)` in `run_agent.py`;
3. route agent-loop execution through the same middleware path as `delegate_task`.

Until Hermes exposes a stable third-party API for agent-loop tools that need `parent_agent`, this repo installs as a reproducible patch bundle.

## Requirements

- macOS or Linux shell environment.
- A local Hermes Agent source checkout.
- Python 3.11+.
- `git` available on `PATH`.
- Worker profiles under `<HERMES_HOME>/profiles/<worker>/config.yaml`.

## Quick install

```bash
git clone https://github.com/Allenbayern/ruoyu-costrouter.git
cd ruoyu-costrouter
./scripts/install.sh /path/to/hermes-agent
```

If your Hermes checkout is the default source install, this is often:

```bash
./scripts/install.sh ~/.hermes/hermes-agent
```

The installer creates a best-effort backup branch named `ruoyu-costrouter-backup-<timestamp>` before applying the patch.

## Configure worker profiles

Copy the templates you need:

```bash
mkdir -p ~/.hermes/profiles
cp -R profiles/worker-dsflash ~/.hermes/profiles/
cp -R profiles/worker-dspro ~/.hermes/profiles/
cp -R profiles/worker-gpt54 ~/.hermes/profiles/
cp -R profiles/worker-gpt55 ~/.hermes/profiles/
```

Then edit each `config.yaml` to use your actual provider, model, API base URL, and secret source.

Do not commit real API keys. Prefer environment variables or your existing Hermes provider configuration.

## Example controller policy

Use `examples/controller-policy.md` as a system/developer-policy snippet. It maps task classes to worker profiles:

- `worker-dsflash`: rough filtering, clustering, table/checklist extraction.
- `worker-dspro`: bounded technical analysis and narrow source judgment.
- `worker-gpt54`: high-quality prose drafts and Chinese rewrites.
- `worker-gpt55`: hard execution slices that need top-tier reasoning.

## Tool call shape

Single task:

```json
{
  "profile": "worker-dspro",
  "goal": "Inspect this module and identify the narrow root cause.",
  "context": "Include file paths, constraints, observed errors, and requested output shape.",
  "role": "leaf"
}
```

Batch tasks through the same worker profile:

```json
{
  "profile": "worker-dsflash",
  "tasks": [
    {"goal": "Cluster these warnings", "context": "Paste or reference logs."},
    {"goal": "Extract fields into a checklist", "context": "Paste source text."}
  ]
}
```

## Verify after install

From the Hermes checkout:

```bash
python -m pytest tests/tools/test_delegate.py::TestCostRouter -q
python -m pytest tests/run_agent/test_run_agent.py -k cost_router -q
```

If this Hermes checkout uses a project test runner, use that runner instead of raw `pytest`.

## Uninstall

```bash
./scripts/uninstall.sh /path/to/hermes-agent
```

This reverses `patches/cost-router.patch` with `git apply -R`.

## Security notes

- The tool never asks the model to pass provider credentials directly.
- Runtime credential values are resolved from named worker profile config.
- Keep `profiles/*/config.yaml` templates free of real secrets.
- Review `git diff` before committing an installed Hermes checkout.

## Status

This package targets Hermes builds whose delegation implementation is structurally compatible with the included patch. If the patch fails, inspect the rejected hunks and port the same integration points manually.
