#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /path/to/hermes-agent" >&2
  exit 2
fi

HERMES_ROOT="$1"
PATCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/patches"
PATCH_FILE="$PATCH_DIR/cost-router.patch"

if [[ ! -d "$HERMES_ROOT/.git" ]]; then
  echo "error: $HERMES_ROOT is not a git checkout" >&2
  exit 1
fi

for required in tools/delegate_tool.py run_agent.py agent/agent_runtime_helpers.py agent/tool_executor.py model_tools.py toolsets.py; do
  if [[ ! -f "$HERMES_ROOT/$required" ]]; then
    echo "error: missing Hermes file: $required" >&2
    exit 1
  fi
done

cd "$HERMES_ROOT"
backup="ruoyu-costrouter-backup-$(date +%Y%m%d%H%M%S)"
git branch "$backup" >/dev/null 2>&1 || true

git apply --check "$PATCH_FILE"
git apply "$PATCH_FILE"

echo "Applied ruoyu-costrouter patch to $HERMES_ROOT"
echo "Backup branch: $backup"
echo "Next: run Hermes cost_router tests, then copy worker profiles from this repo."
