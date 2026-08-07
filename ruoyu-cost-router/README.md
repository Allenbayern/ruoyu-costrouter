# ruoyu-cost-router — flat catalog v5

Private Hermes plugin that validates one exact route and submits one Kanban task.
It never calls an LLM directly, changes configuration itself, publishes, deploys,
merges, or accepts work. `controller_decision_required` remains true.

## Exact route contract

`catalog_version: 5` accepts exactly these public routes:

| Route | Worker | Provider/model | Output cap | Role |
| --- | --- | --- | --- | --- |
| `flash` | `worker-flash` | `custom:new-api` / `deepseek-v4-flash` | 65536 | Mechanical low-risk execution |
| `luna` | `worker-luna` | `custom:new-api` / `deepseek-v4-flash` | 12000 | Luna Economy — bounded bulk low-risk preprocessing |
| `terra` | `worker-terra` | `custom:new-api` / `deepseek-v4-flash` | 65536 | Default production execution |
| `terra_pro` | `worker-terra` | `custom:new-api` / `deepseek-v4-pro` | 12000 | Explicit quality upgrade only |
| `sol` | `worker-sol` | `custom:new-api` / `gpt-5.6-sol` | 6000 | Protected independent review |

Task-type binding (v5 roles):

- `flash`: `router`, `classify`, `dedupe`, `normalize`, `fixed_extraction`
- `luna`: `cleanup`
- `terra`: `coding`, `rag_answer`, `evidence_synthesis`, `repair`, `draft`,
  `final_chinese`, plus upgrade types below
- `terra_pro`: reachable **only** by an explicit `route: terra_pro` request
  paired with an upgrade task type (`architecture`, `cross_artifact_analysis`,
  `difficult_debugging`). Production task types never imply automatic upgrades.
- `sol`: `final_review` (protected admission unchanged)

Every `budget_fallbacks` value must be `[]`. Budget exhaustion denies the request;
it never retargets a worker, provider, or model. Keyword fallback is false.
`luna_economy`, `tier1_flash`, `variant` inputs, and any legacy route label are
rejected before task creation. Route/task-source conflicts deny without dispatch.
An explicit `terra_pro` route with a non-upgrade task type is a conflict and is
denied.

`sol` remains protected: ordinary direct Sol route requests are denied unless the
existing review metadata/admission path authorizes protected-final review.

## queued-vs-existing contract

The host's `kanban_create` response does not carry a `created` flag, so the router
derives the result from the task table: a fresh idempotency key means the creating
caller (`queued`), an existing key means replay (`existing`). The lookup is
best-effort — any failure degrades to "not pre-existing" and never blocks task
creation.

## Installation and rollback

Merge `config.template.yaml` into active `config.yaml`, then run:

```sh
hermes config check
hermes plugin reload ruoyu-cost-router
python3 plugins/ruoyu-cost-router/test_flat_v4.py -v
```

Pre-change rollback copies are stored under
`/home/allen/.hermes/artifacts/router-v5-20260806/` as
`*.pre-v5-20260806T131647Z`. Restore only the corresponding file from that
timestamped copy, then re-run config check and the focused tests. Controller
retains configuration promotion decisions.

## Verification scope

Focused contract tests exercise exact catalog validation, direct route worker and
model pinning (flash/luna/terra/terra_pro), the terra_pro explicit-upgrade-only
contract, legacy/variant/conflict/budget zero-dispatch paths, unchanged direct-Sol
denial, and the queued/existing idempotency contract against the real host
`kanban_create` response. Canonical package tests should exclude historical
`*-pre-*` files.

Full-suite baseline (v5, post closure): `python3 -m unittest discover` in the
plugin dir → **62/62 OK** (`test_flat_v4.py` 11/11, `test_reviewer_admission.py`
23/23, `test_concurrent_replay.py` 1/1 — the latter asserts serial replay
single-card + no concurrent denied against a real SQLite host table). Plugin
directory is not git-managed; `sha256sum` baselines for `__init__.py` and test
files are recorded in
`~/.hermes/knowledge/curation-runs/cost-router-audit-20260806/step2-land-verification.md`.
