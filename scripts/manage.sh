#!/usr/bin/env bash
# Run a Django manage.py command inside a stack's web container.
#
# Usage:
#   ./scripts/manage.sh prod promote jonathanapfeifer --superuser
#   ./scripts/manage.sh sandbox createsuperuser
#   ./scripts/manage.sh prod shell
#
# Works from the server (sg docker wrapper) and from a dev laptop (docker
# group already primary).
set -euo pipefail

STACK="${1:-}"
shift || true

case "$STACK" in
    prod)
        PROJECT="osprey-prod"
        ENV_FILE="/etc/osprey/.env.prod"
        OVERRIDE="docker-compose.prod.yml"
        ;;
    sandbox)
        PROJECT="osprey-sandbox"
        ENV_FILE="/etc/osprey/.env.sandbox"
        OVERRIDE="docker-compose.sandbox.yml"
        ;;
    local|dev|"")
        PROJECT="osprey"
        ENV_FILE=""
        OVERRIDE=""
        ;;
    *)
        echo "Usage: $0 {prod|sandbox|local} <manage.py command...>" >&2
        exit 1
        ;;
esac

if [[ $# -eq 0 ]]; then
    echo "Usage: $0 $STACK <manage.py command...>" >&2
    exit 1
fi

# Interactive TTY for things like createsuperuser and shell; -T otherwise.
EXEC_FLAGS=(-T)
case "${1:-}" in
    shell|shell_plus|dbshell|createsuperuser|changepassword)
        EXEC_FLAGS=(-it)
        ;;
esac

CMD=(docker compose -p "$PROJECT")
USER_FLAGS=()
if [[ -n "$ENV_FILE" ]]; then
    CMD+=(--env-file "$ENV_FILE" -f docker-compose.yml -f "$OVERRIDE")
else
    # Local only: the repo is bind-mounted, so files a command writes
    # (makemigrations) should belong to the host user. On prod and
    # sandbox the container runs as root and owns media/; run as root
    # there too, like gunicorn and deploy.sh.
    USER_FLAGS=(--user 1000:1000)
fi
CMD+=(exec "${EXEC_FLAGS[@]}" "${USER_FLAGS[@]}" -w /app web python manage.py "$@")

# If the user isn't in the docker group's primary set, wrap with sg.
if id -nG | tr ' ' '\n' | grep -qx docker; then
    "${CMD[@]}"
else
    sg docker -c "$(printf '%q ' "${CMD[@]}")"
fi
