# ruoyu-cost-router release layout

This is the standalone repository root for the private `ruoyu-cost-router`
plugin. Installation and runtime configuration are operator-controlled; this
repository contains no credentials and does not modify Hermes Core.

## Contents

- `ruoyu-cost-router/`: installable plugin directory.
- `tests/`: standalone router-to-Kanban submission contract tests.
- `pyproject.toml`: package metadata and test dependency range.
- `scripts/run_tests.sh`: reproducible test entry point.

## Validation After Clone

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
scripts/run_tests.sh
```

Tests do not need a Hermes Core checkout. They validate that a selected route
creates a `kanban_create` request for the bound worker profile and workspace,
not a bare model call. A real installation requires a running Kanban dispatcher
and configured `worker-luna`, `worker-luna-economy`, `worker-terra`, and
`worker-sol` profiles; inspect the resulting task ID, worker runs, and logs to
verify actual execution.

Controller retains install, configuration, remote repository, publication,
merge, and acceptance decisions.
