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
DJANGO_ALLOWED_HOSTS=osprey.science,www.osprey.science
DJANGO_CSRF_TRUSTED_ORIGINS=https://osprey.science,https://www.osprey.science
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

# nginx site:
sudo cp ~/osprey/deploy/nginx.conf /etc/nginx/sites-available/osprey
sudo ln -s /etc/nginx/sites-available/osprey /etc/nginx/sites-enabled/osprey
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# Issue TLS cert (also installs auto-renew cron):
sudo certbot --nginx -d osprey.science -d www.osprey.science

# First deploy:
cd ~/osprey && ./deploy.sh
```

## Routine deploy

From a dev machine:

```bash
git push
ssh osprey@osprey.science 'cd ~/osprey && ./deploy.sh'
```

No editing on the server. Ever.

## ORCID setup

The app is wired for ORCID through django-allauth. Development defaults to the
ORCID sandbox; production should use regular `orcid.org` credentials.

### 1. Test with the sandbox

1. Create or sign into a sandbox ORCID account at `https://sandbox.orcid.org/`.
2. Register a sandbox public API client in the ORCID developer tools.
3. Add this redirect URI to the sandbox client:

```text
http://localhost:8000/accounts/orcid/login/callback/
```

4. Put the sandbox credentials in local `.env`:

```env
ORCID_USE_SANDBOX=1
ORCID_CLIENT_ID=<sandbox client id>
ORCID_CLIENT_SECRET=<sandbox client secret>
```

5. Restart the Django container and use the login page's ORCID button. The
  browser should leave OSPREY, go to `sandbox.orcid.org`, and return to
  `/accounts/orcid/login/callback/`.

Sandbox accounts are separate from real ORCID accounts. A real user's normal
ORCID login will not work there.

### 2. Move to production ORCID

1. Sign into a real ORCID account at `https://orcid.org/` and register a
  public API client there.
2. Add the production callback URL:

```text
https://osprey.science/accounts/orcid/login/callback/
```

Add `https://www.osprey.science/accounts/orcid/login/callback/` too if the
`www` host will accept logins.

3. In `/etc/osprey/.env.prod`, set:

```env
ORCID_USE_SANDBOX=0
ORCID_CLIENT_ID=<production client id>
ORCID_CLIENT_SECRET=<production client secret>
```

4. Confirm the production env file also has the public host names:

```env
DJANGO_ALLOWED_HOSTS=osprey.science,www.osprey.science
DJANGO_CSRF_TRUSTED_ORIGINS=https://osprey.science,https://www.osprey.science
```

5. Deploy and restart the app:

```bash
ssh osprey@osprey.science 'cd ~/osprey && ./deploy.sh'
```

6. Test sign-in from a private browser window. The authorize URL should be on
  `orcid.org`, not `sandbox.orcid.org`.

For the first beta, OSPREY only uses ORCID for sign-in and stores the ORCID iD
on the profile. It does not write to a user's ORCID record.

## Backups

Add a cron job for the `osprey` user:

```cron
15 4 * * * cd /home/osprey/osprey && docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db pg_dump -U osprey osprey | gzip > /home/osprey/backups/osprey-$(date +\%Y\%m\%d).sql.gz
```

Then sync `~/backups/` off-VPS (Backblaze B2, Tailscale rsync, etc.).

## Restore

```bash
gunzip -c /path/to/dump.sql.gz | \
  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db \
    psql -U osprey -d osprey
```
