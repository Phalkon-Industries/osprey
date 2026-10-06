#!/usr/bin/env bash
set -euo pipefail

USER_SPEC="${OSPREY_TEST_USER:-1000:1000}"
SERVICE="${OSPREY_TEST_SERVICE:-web}"
COMPOSE=(docker compose)
EXEC=("${COMPOSE[@]}" exec -T --user "$USER_SPEC" -w /app "$SERVICE")

"${EXEC[@]}" python manage.py check
"${EXEC[@]}" python manage.py makemigrations --check --dry-run
"${EXEC[@]}" coverage erase
"${EXEC[@]}" coverage run manage.py test --parallel "${OSPREY_TEST_PARALLEL:-4}" "$@"
"${EXEC[@]}" coverage combine
"${EXEC[@]}" coverage report
