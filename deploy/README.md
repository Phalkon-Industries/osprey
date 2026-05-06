# OSPREY deployment runbook

Single-VPS Hetzner deploy. Ubuntu 24.04. nginx + certbot on the host, Postgres
and the Django app under Docker Compose. See [planning/demo-architecture.md](../planning/demo-architecture.md) §11
for the design rationale; this file is the executable runbook.

## One-time VPS setup

```bash
# As root immediately after VPS provisioning:
adduser osprey
usermod -aG sudo osprey
rsync --archive --chown=osprey:osprey ~/.ssh /home/osprey/
# Switch to the new user; disable root + password SSH afterwards.

# As the osprey user:
sudo apt update && sudo apt install -y ufw nginx certbot python3-certbot-nginx
sudo ufw allow 22 && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable

curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker osprey
# log out and back in for group membership

# Read-only deploy key for the repo: generate, register on GitHub, then:
git clone git@github.com:<org>/osprey.git ~/osprey

sudo mkdir -p /srv/osprey/postgres /srv/osprey/staticfiles /srv/osprey/media /etc/osprey
sudo chown -R osprey:osprey /srv/osprey /etc/osprey
sudo chmod 750 /srv/osprey /srv/osprey/postgres /srv/osprey/staticfiles /srv/osprey/media
sudo chmod 750 /etc/osprey

# Production env file (never committed):
sudo -u osprey tee /etc/osprey/.env.prod > /dev/null <<'ENV'
DJANGO_SECRET_KEY=<generate a long random string>
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=osprey.phalkon.io
DJANGO_CSRF_TRUSTED_ORIGINS=https://osprey.phalkon.io
DJANGO_SESSION_COOKIE_SECURE=1
DJANGO_CSRF_COOKIE_SECURE=1
POSTGRES_DB=osprey
POSTGRES_USER=osprey
POSTGRES_PASSWORD=<long random>
DATABASE_URL=postgres://osprey:<long random>@db:5432/osprey
ORCID_USE_SANDBOX=0
ORCID_CLIENT_ID=<orcid production client id>
ORCID_CLIENT_SECRET=<orcid production client secret>
ENV
sudo chmod 640 /etc/osprey/.env.prod

# Production Docker does not use ~/osprey/.env. deploy.sh passes
# /etc/osprey/.env.prod to Docker Compose with --env-file, and the prod compose
# override also injects it into the containers that need those variables.

# nginx site:
sudo cp ~/osprey/deploy/nginx.conf /etc/nginx/sites-available/osprey
sudo ln -s /etc/nginx/sites-available/osprey /etc/nginx/sites-enabled/osprey
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# Issue TLS cert (also installs auto-renew cron):
sudo certbot --nginx -d osprey.phalkon.io

# First deploy:
cd ~/osprey && ./deploy.sh
```

## Routine deploy

From a dev machine:

```bash
git push
ssh osprey@osprey.phalkon.io 'cd ~/osprey && ./deploy.sh'
```

No editing on the server. Ever.

## ORCID setup

The app is wired for ORCID through django-allauth. The longer design and setup
notes live in [planning/features/orcid-signin.md](../planning/features/orcid-signin.md).
The key point: ORCID iDs are collected through ORCID sign-in, not typed into
OSPREY forms.

### How Docker sees the env file

There are two different env-file ideas in Docker Compose, and this is where the
setup can get confusing:

- `docker compose --env-file /etc/osprey/.env.prod ...` tells the Compose CLI
  what variables to use while it reads the compose files. This is how
  `${POSTGRES_PASSWORD}` and friends get resolved for the database service.
- `env_file: /etc/osprey/.env.prod` in `docker-compose.prod.yml` injects those
  variables into a running container.

Production needs the server file at `/etc/osprey/.env.prod`. It does not need a
repo-local `.env` file. Local development still uses the repo-local `.env` file
because [docker-compose.yml](../docker-compose.yml) names that file directly.

`deploy.sh` already runs Compose with the production env file:

```bash
docker compose --env-file /etc/osprey/.env.prod -f docker-compose.yml -f docker-compose.prod.yml ...
```

### Local ORCID test

ORCID may reject `localhost` redirect URIs. For repeat local testing, use the
Cloudflare tunnel hostname and register this full callback URL with ORCID:

```text
https://ospreydev.phalkon.io/accounts/orcid/login/callback/
```

The tunnel should point that public hostname at local Django, usually
`http://localhost:8000`, not `https://localhost:8000`. Cloudflare provides the
public HTTPS endpoint; the local Django development server speaks HTTP. Put
matching credentials in local `.env` and include the tunnel hostname in
`DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`:

```env
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,ospreydev.phalkon.io
DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8000,https://ospreydev.phalkon.io
ORCID_USE_SANDBOX=0
ORCID_CLIENT_ID=<orcid application id>
ORCID_CLIENT_SECRET=<orcid secret>
```

Use `ORCID_USE_SANDBOX=1` only with sandbox credentials. Production ORCID
credentials are okay for beta sign-in testing because OSPREY asks only for
authentication and does not write to anyone's ORCID record.

### Production ORCID

1. Sign into a real ORCID account at `https://orcid.org/` and register a
   public API client there.
2. Add the production callback URL:

```text
https://osprey.phalkon.io/accounts/orcid/login/callback/
```

Only add other callback URLs if those hostnames will actually serve OSPREY.

3. In `/etc/osprey/.env.prod`, set:

```env
ORCID_USE_SANDBOX=0
ORCID_CLIENT_ID=<production application id>
ORCID_CLIENT_SECRET=<production secret>
```

4. Confirm the production env file also has the public host names:

```env
DJANGO_ALLOWED_HOSTS=osprey.phalkon.io
DJANGO_CSRF_TRUSTED_ORIGINS=https://osprey.phalkon.io
```

5. Deploy and restart the app:

```bash
ssh osprey@osprey.phalkon.io 'cd ~/osprey && ./deploy.sh'
```

6. Test sign-in from a private browser window. The authorize URL should be on
  `orcid.org`, not `sandbox.orcid.org`.

For the first beta, OSPREY only uses ORCID for sign-in and stores the ORCID iD
on the profile. It does not write to a user's ORCID record.

## Backups

Add a cron job for the `osprey` user:

```cron
15 4 * * * cd /home/osprey/osprey && docker compose --env-file /etc/osprey/.env.prod -f docker-compose.yml -f docker-compose.prod.yml exec -T db pg_dump -U osprey osprey | gzip > /home/osprey/backups/osprey-$(date +\%Y\%m\%d).sql.gz
```

Then sync `~/backups/` off-VPS (Backblaze B2, Tailscale rsync, etc.).

## Restore

```bash
gunzip -c /path/to/dump.sql.gz | \
  docker compose --env-file /etc/osprey/.env.prod -f docker-compose.yml -f docker-compose.prod.yml exec -T db \
    psql -U osprey -d osprey
```
