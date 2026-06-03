#!/usr/bin/env bash
# Apply the OSPREY VPS host baseline.
#
# Run as root, after the repo has been cloned to the deploy user's home.
# Idempotent: safe to re-run to bring a host back to baseline.
#
# This script does NOT create the deploy user, install SSH keys, write env
# files, install the nginx site, or run certbot. Those are inherently per-host
# or secret-bearing and live in docs/deployment.md as manual steps.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root." >&2
    exit 1
fi

OSPREY_USER="${OSPREY_USER:-osprey}"

if ! id -u "${OSPREY_USER}" >/dev/null 2>&1; then
    echo "User '${OSPREY_USER}' does not exist. Create it first (see docs/deployment.md Phase A)." >&2
    exit 1
fi

apt update
apt install -y ufw nginx certbot python3-certbot-nginx

ufw allow 22
ufw allow 80
ufw allow 443
ufw --force enable

if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
fi
usermod -aG docker "${OSPREY_USER}"

mkdir -p \
  /srv/osprey-prod/postgres /srv/osprey-prod/staticfiles /srv/osprey-prod/media \
  /srv/osprey-sandbox/postgres /srv/osprey-sandbox/staticfiles /srv/osprey-sandbox/media \
  /etc/osprey
chown -R "${OSPREY_USER}:${OSPREY_USER}" /srv/osprey-prod /srv/osprey-sandbox /etc/osprey
# Stack dirs need world-execute so nginx (www-data) can traverse into
# staticfiles/ and media/ to serve assets. The subdirs and files keep their
# default 755/644 perms, which are already world-readable.
chmod 711 /srv/osprey-prod /srv/osprey-sandbox
# /etc/osprey holds env files with secrets — keep it group-readable only.
chmod 750 /etc/osprey

systemctl enable docker
systemctl enable containerd
systemctl start docker

echo "Host baseline applied."
echo "Next: see docs/deployment.md Phase C (secrets + nginx + certbot) and Phase D (first deploy)."
