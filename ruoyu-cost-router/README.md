# ruoyu-cost-router

Private Hermes plugin that selects a validated budget-aware route and submits a
real Kanban task. It does not perform a bare `ctx.llm.complete()` call. The
Kanban dispatcher launches the selected Hermes worker profile in its assigned
workspace, where it has that profile's actual toolset, including filesystem and
terminal access when enabled for the profile.

## Execution Contract

- `ruoyu_cost_router` validates the operator route catalog, pricing, allowlists,
  token/cost limits, fallback path, and input bounds before any task is created.
- A successful call returns `routing_status: "queued"` only when the Kanban
  result is dispatchable; it always returns a durable `task_id`, actual
  `task_status`, board, selected worker profile, and routing/budget metadata.
  An idempotent retry of an existing completed/blocked card returns
  `routing_status: "existing"` rather than falsely reporting queued work.
- Hermes Kanban dispatches the task to the bound profile:
  `luna -> worker-luna`, `luna_economy -> worker-luna-economy`,
  `terra -> worker-terra`, and `sol -> worker-sol`. A catalog cannot retarget a
  tier to another profile.
- Use `workspace_path` or `project.workdir` for code tasks. This creates a
  `dir` workspace rooted at that absolute path. Without one, the task uses an
  isolated disposable `scratch` workspace.
- The gateway must be running with `kanban.dispatch_in_gateway: true` (default),
  or an operator must run a Kanban dispatcher. A `ready` card is durable queue
  evidence, not proof that a worker has started.
- The plugin never publishes, deploys, merges, changes global configuration, or
  accepts work. `controller_decision_required` remains true.

## Route and Budget Contract

- `catalog_version: 2` is required. A version mismatch or malformed catalog
  fails closed.
- Each route declares an allowlisted provider/model pair, the immutable worker
  profile for its tier, price estimates, output cap, enablement state, and
  permitted cost-fallback tiers.
- `max_cost_usd`, `max_tokens`, and `remaining_budget_usd` may be supplied per
  task or configured as defaults. The plugin estimates bounded prompt input plus
  maximum output before queueing. If the requested tier exceeds a limit it tries
  configured fallbacks and returns the candidates/rejection reasons. If none
  fits, it creates no task.
- Actual costs are not invented by the router. The queued result contains the
  estimate; worker/provider usage must be captured by Hermes execution telemetry
  or a later task-completion integration before it can be reported as actual.
- Free-text keyword routing is disabled by default. Use explicit `route` or the
  constrained `task_type` enum. Operators can opt in to keyword fallback.
- Goal and context are independently capped at 12,000 characters before routing
  and estimation. Before persistence, the final UTF-8 task body is additionally
  capped at the Hermes worker-context contract of 8 KiB; the result and task
  metadata mark any content omitted to preserve that executable brief. Worker output
  is controlled by the worker/profile runtime rather than this queueing tool.

## Operator Installation

1. Copy this directory to `~/.hermes/plugins/ruoyu-cost-router`.
2. Merge `config.template.yaml` into `config.yaml`, including the v2 catalog.
   Review pricing against current provider price sheets and confirm the named
   worker profiles exist with the intended toolsets.
3. Ensure `kanban.dispatch_in_gateway: true`, start/restart the gateway, and
   start a new Hermes session so plugin discovery and tool schemas reload.

The bundle contains no credentials and never writes configuration itself.

## Tool Inputs

`goal` is required. Selection precedence is explicit `route`, constrained
`task_type`, opt-in keyword fallback, then `terra`.

For a repository task, call with an absolute workspace:

```json
{
  "goal": "Run the focused tests and repair the failing regression.",
  "route": "terra",
  "workspace_path": "/absolute/path/to/repository",
  "board": "cost-router-plugin",
  "max_cost_usd": 0.05
}
```

The caller must read the resulting Kanban card/runs/log before treating any
worker claim as complete. `task_id` can be inspected with:

```sh
hermes kanban show TASK_ID --json
hermes kanban log TASK_ID
hermes kanban runs TASK_ID
```

## Standalone Verification

The package includes a test runner and development dependencies. It does not
require a Hermes Core checkout for contract tests; those install small test-only
stubs for the documented plugin config and `dispatch_tool` interfaces.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
scripts/run_tests.sh
```

The suite verifies task submission, tier-to-profile binding, catalog migration
within the strict allowlist, budget fallback/exhaustion, keyword safety,
truncation, idempotency, and task-creation failures. Real worker execution is
verified after installation by observing Kanban `claimed`, `spawned`, runs, and
worker logs.
