# ruoyu-cost-router release layout

This directory is the prepared standalone repository root for the private
`ruoyu-cost-router` plugin. It is intentionally an untracked task artifact:
it is not an edit to Hermes Core and it has not been installed, configured,
committed, pushed, or published.

## Contents

- `ruoyu-cost-router/` — the installable plugin directory. Copy this directory
  as `~/.hermes/plugins/ruoyu-cost-router` only after controller approval.
- `tests/test_ruoyu_cost_router.py` — isolated verification. It copies the
  installable directory into a temporary `HERMES_HOME` before plugin discovery.

## Prepared release boundary

If a controller later chooses to publish the separate repository, publish the
**contents of this directory** as that repository's root. That produces this
layout:

```text
ruoyu-cost-router/
  __init__.py
  plugin.yaml
  config.template.yaml
  README.md
tests/
  test_ruoyu_cost_router.py
```

In that separate repository, execute the focused verification from its root:

```sh
scripts/run_tests.sh tests/test_ruoyu_cost_router.py
```

No obsolete core patch or profile-template material belongs in this release
layout. Controller retains every install, configuration, remote repository,
unarchive, publication, merge, and acceptance decision.