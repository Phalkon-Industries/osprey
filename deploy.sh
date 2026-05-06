#!/usr/bin/env bash
# Run on the VPS as the `osprey` user. Pull, build, migrate, collectstatic, up.
set -euo pipefail

cd /home/osprey/osprey

git pull --ff-only

COMPOSE=(docker compose --env-file /etc/osprey/.env.prod -f docker-compose.yml -f docker-compose.prod.yml)

"${COMPOSE[@]}" build web
"${COMPOSE[@]}" run --rm web \
    python manage.py migrate --noinput
"${COMPOSE[@]}" run --rm web \
    python manage.py collectstatic --noinput
"${COMPOSE[@]}" up -d
