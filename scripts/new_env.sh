#!/usr/bin/env bash
# Print an env-file template for a given stack with random secrets filled in.
# Does NOT write to disk. Pipe or copy the output to /etc/osprey/.env.prod
# or /etc/osprey/.env.sandbox, then fill in the placeholders.
#
# Usage:
#   ./scripts/new_env.sh prod     > /tmp/env.prod
#   ./scripts/new_env.sh sandbox  > /tmp/env.sandbox
set -euo pipefail

STACK="${1:-}"
case "$STACK" in
    prod)
        HOST="osprey.phalkon.io"
        DEBUG=0
        IS_SANDBOX=0
        ZENODO_SANDBOX=0
        ;;
    sandbox)
        HOST="ospreysandbox.phalkon.io"
        DEBUG=0
        IS_SANDBOX=1
        ZENODO_SANDBOX=1
        ;;
    *)
        echo "Usage: $0 {prod|sandbox}" >&2
        exit 1
        ;;
esac

SECRET_KEY="$(openssl rand -base64 48 | tr -d '\n')"
PG_PASSWORD="$(openssl rand -base64 32 | tr -d '/+=\n' | cut -c1-32)"

cat <<ENV
DJANGO_SECRET_KEY=${SECRET_KEY}
DJANGO_DEBUG=${DEBUG}
DJANGO_ALLOWED_HOSTS=${HOST}
DJANGO_CSRF_TRUSTED_ORIGINS=https://${HOST}
DJANGO_SESSION_COOKIE_SECURE=1
DJANGO_CSRF_COOKIE_SECURE=1

OSPREY_IS_SANDBOX=${IS_SANDBOX}

POSTGRES_DB=osprey
POSTGRES_USER=osprey
POSTGRES_PASSWORD=${PG_PASSWORD}
DATABASE_URL=postgres://osprey:${PG_PASSWORD}@db:5432/osprey

ORCID_USE_SANDBOX=0
ORCID_CLIENT_ID=<orcid production client id>
ORCID_CLIENT_SECRET=<orcid production client secret>

ZENODO_USE_SANDBOX=${ZENODO_SANDBOX}
ZENODO_ACCESS_TOKEN=<zenodo personal access token>
ZENODO_DEFAULT_COMMUNITY=
ENV
