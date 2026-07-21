# ruoyu-cost-router

Private local Hermes plugin providing `ruoyu_cost_router` for controller-first routing of bounded work. This bundle is intentionally outside Hermes Core and is designed to be copied by an operator into `~/.hermes/plugins/ruoyu-cost-router` only after review.

## Contract

- Routes are configuration, not Hermes profile names or profile lookups.
- The fixed tiers are `luna`, `luna_economy`, `terra`, and `sol`, with immutable pairs: `luna=custom/gpt-5.6-luna`, `luna_economy=deepseek/deepseek-v4-flash`, `terra=custom/gpt-5.6-terra`, and `sol=custom/gpt-5.6-sol`.
- Each route calls the host-owned `ctx.llm.complete`; the plugin never invokes the Hermes CLI, subprocesses, or worker profiles.
- The result is structured and redacts returned text and errors before serialization.
- The plugin never accepts, publishes, deploys, merges, or promotes work. `controller_decision_required` is always true.
- Model/provider overrides fail closed before `ctx.llm.complete` under Hermes's `plugins.entries.ruoyu-cost-router.llm` trust gate. Both explicit non-empty narrow allowlists must contain the selected immutable pair; missing, empty, malformed, wildcard, unknown, or incomplete values deny without calling the host. Route configuration may set `enabled: false`, but it cannot change a tier's pair.

## Operator installation

1. Copy this directory to `~/.hermes/plugins/ruoyu-cost-router`.
2. Merge `config.template.yaml` into the operator's `config.yaml` and explicitly enable the plugin.
3. Start a new Hermes session so plugin discovery and tool schemas reload.

The plugin bundle contains no credentials and does not write config itself.

## Tool inputs

`goal` is required. `route` is optional; route selection precedence is explicit route, `task_type`, keyword, then `terra`. A route can also be selected by a `project.route` value, but project data is not treated as a profile lookup.

Supported `task_type` values: `router`, `classify`, `dedupe`, `bulk_preprocess`, `rag_answer`, `draft`, `coding`, `final_review`, and `architecture`.

## Tests

From the Hermes repository root:

```sh
scripts/run_tests.sh delivery/tests/test_ruoyu_cost_router.py
```

The tests copy this bundle into a temporary `HERMES_HOME`, exercise real plugin discovery/registration, verify trust denial, and prove that denied routes do not call a provider.
