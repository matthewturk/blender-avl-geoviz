#!/usr/bin/env bash
# Install a built AVL GeoViz zip into a throwaway Blender profile and run
# tests/smoke_test.py against it.
#   tests/run_smoke.sh <blender> <zip> [--network]
set -euo pipefail
BLENDER=$1; ZIP=$(realpath "$2"); shift 2
export BLENDER_USER_RESOURCES=$(mktemp -d); trap 'rm -rf "$BLENDER_USER_RESOURCES"' EXIT
"$BLENDER" --command extension install-file -r user_default -e "$ZIP"
"$BLENDER" --background --python-exit-code 1 --python "$(dirname "$0")/smoke_test.py" -- "$@"
