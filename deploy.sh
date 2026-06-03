#!/usr/bin/env bash
set -euo pipefail

# Backwards-compatible entrypoint for production deploys.
exec "$(dirname "$0")/scripts/deploy.sh" prod "$@"
