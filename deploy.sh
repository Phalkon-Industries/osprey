#!/usr/bin/env bash
# Run on the VPS as the `osprey` user.
#
# Usage:
#   ./deploy.sh             # deploy the tip of the default branch
#   ./deploy.sh v0.1.0      # deploy a specific tag (recommended for prod)
#   ./deploy.sh main        # deploy a specific branch
#
# What it does, in order:
#   1. Fetches the latest commits and tags from origin.
#   2. Checks out the requested ref (tag, branch, or commit). With no argument
#      it fast-forwards the current branch.
#   3. Rebuilds the web image, runs migrations and collectstatic, brings the
#      stack up.
set -euo pipefail

cd /home/osprey/osprey

REF="${1:-}"

git fetch --tags --prune origin
if [[ -n "$REF" ]]; then
    echo "Deploying ref: $REF"
    git checkout --detach "$REF"
else
    echo "Deploying tip of current branch"
    git pull --ff-only
fi

git --no-pager log -1 --pretty="format:Now at %h %s%n"

COMPOSE=(docker compose --env-file /etc/osprey/.env.prod -f docker-compose.yml -f docker-compose.prod.yml)

"${COMPOSE[@]}" build web
"${COMPOSE[@]}" run --rm web \
    python manage.py migrate --noinput
"${COMPOSE[@]}" run --rm web \
    python manage.py collectstatic --noinput
"${COMPOSE[@]}" up -d

echo "Deploy complete."
