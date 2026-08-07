# ruoyu-costrouter — standalone release repository

Standalone repository root for the private **ruoyu-cost-router** Hermes plugin
(flat catalog **v5**, version 0.4.0). Installation and runtime configuration
are operator-controlled; this repository contains no credentials and does not
modify Hermes Core.

## Contents

- `ruoyu-cost-router/`: installable plugin directory (`__init__.py`,
  `plugin.yaml`, `config.template.yaml`, plus the full v5 contract README).
- `tests/`: standalone router-to-Kanban submission contract tests (12 files,
  v5 catalog: `flash` / `luna` / `terra` / `terra_pro` / `sol`).
- `pyproject.toml`: package metadata and test dependency range.
- `scripts/run_tests.sh`: reproducible test entry point.

## Validation after clone

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
scripts/run_tests.sh
```

Contract tests validate that a selected exact route creates a `kanban_create`
request bound to the correct worker profile, provider, and model — never a
bare model call. One integration test
(`test_flat_v4.py::FlatCatalogV5Tests::test_router_reports_queued_then_existing_with_real_host_create_response`)
exercises the queued-vs-existing idempotency contract against a real Hermes
`kanban_create` host response; it requires a Hermes Core checkout and is
skipped automatically when the source tree is not present.

A real installation additionally requires a running Kanban dispatcher and the
configured worker profiles `worker-flash`, `worker-luna`, `worker-terra`, and
`worker-sol`; inspect the resulting task ID, worker runs, and logs to verify
actual execution. See `ruoyu-cost-router/README.md` for the full v5 route
table, task-type binding, and rollback notes.

Controller retains install, configuration, remote repository, publication,
merge, and acceptance decisions.
