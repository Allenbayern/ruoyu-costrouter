#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /path/to/hermes-agent" >&2
  exit 2
fi

HERMES_ROOT="$1"
PATCH_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/patches/cost-router.patch"

if [[ ! -d "$HERMES_ROOT/.git" ]]; then
  echo "error: $HERMES_ROOT is not a git checkout" >&2
  exit 1
fi

cd "$HERMES_ROOT"
git apply -R --check "$PATCH_FILE"
git apply -R "$PATCH_FILE"

echo "Removed ruoyu-costrouter patch from $HERMES_ROOT"
