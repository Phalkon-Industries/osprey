#!/usr/bin/env bash
# Deploy an OSPREY stack on the VPS.
#
# Usage:
#   ./scripts/deploy.sh prod                # prod, tip of current branch
#   ./scripts/deploy.sh prod v0.1.0         # prod, specific tag/ref
#   ./scripts/deploy.sh sandbox             # sandbox, tip of current branch
#   ./scripts/deploy.sh sandbox main        # sandbox, specific ref
set -euo pipefail

STACK="${1:-}"
REF="${2:-}"

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
    *)
        echo "Usage: $0 {prod|sandbox} [ref]" >&2
        exit 1
        ;;
esac

cd /home/osprey/osprey

git fetch --tags --prune origin
if [[ -n "$REF" ]]; then
    echo "Deploying $STACK ref: $REF"
    git checkout --detach "$REF"
else
    echo "Deploying $STACK tip of current branch"
    git pull --ff-only
fi

git --no-pager log -1 --pretty="format:Now at %h %s%n"

COMPOSE=(
    docker compose
    -p "$PROJECT"
    --env-file "$ENV_FILE"
    -f docker-compose.yml
    -f "$OVERRIDE"
)

"${COMPOSE[@]}" build web
"${COMPOSE[@]}" run --rm web python manage.py migrate --noinput
"${COMPOSE[@]}" run --rm web python manage.py collectstatic --noinput
"${COMPOSE[@]}" up -d

echo "$STACK deploy complete."
