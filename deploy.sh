#!/usr/bin/env bash
# Run on the VPS as the `osprey` user. Pull, build, migrate, collectstatic, up.
set -euo pipefail

cd /home/osprey/osprey

git pull --ff-only

docker compose -f docker-compose.yml -f docker-compose.prod.yml build web
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm web \
    python manage.py migrate --noinput
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm web \
    python manage.py collectstatic --noinput
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
