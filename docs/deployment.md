# OSPREY deployment runbook

Single-VPS Hetzner deploy. Ubuntu 24.04. nginx + certbot on the host, Postgres
and the Django app under Docker Compose. See [planning/demo-architecture.md](../planning/demo-architecture.md) §11
for the design rationale; this file is the executable runbook.

## Script entrypoints

From the repo root on the VPS:

- `./scripts/host_setup.sh` — apply the host baseline (apt packages, ufw, docker, `/srv/osprey-*` dirs). Run as root, after the repo is cloned. Idempotent.
- `./scripts/deploy.sh {prod|sandbox} [ref]` — deploy the named stack at `ref` (default: tip of current branch).
- `./deploy.sh [ref]` — backwards-compatible wrapper for `./scripts/deploy.sh prod`.

## How Compose env files work

Two different env-file ideas in Docker Compose, and this is where things get
confusing:

- `docker compose --env-file /etc/osprey/.env.prod ...` tells the Compose CLI
  what variables to use while it reads the compose files. This is how
  `${POSTGRES_PASSWORD}` and friends get resolved.
- `env_file: /etc/osprey/.env.prod` inside a service injects those variables
  into the running container.

Production uses `/etc/osprey/.env.prod`, never a repo-local `.env`. Local
development still uses the repo-local `.env` because `docker-compose.yml` names
that file directly. `scripts/deploy.sh prod` runs Compose like this:

```bash
docker compose -p osprey-prod --env-file /etc/osprey/.env.prod \
  -f docker-compose.yml -f docker-compose.prod.yml ...
```

The `-p osprey-prod` is what keeps the prod and sandbox stacks from colliding
(Compose prefixes container/network/volume names with the project name).

## First deploy

Four phases. Phase A is genuinely manual; Phase B is one script; Phase C is
secrets and per-host config that can't be scripted safely; Phase D is the
deploy.

### Phase A — pre-clone (as root on the fresh VPS)

```bash
adduser osprey
usermod -aG sudo osprey
rsync --archive --chown=osprey:osprey ~/.ssh /home/osprey/
# Disable root SSH and password auth in /etc/ssh/sshd_config, then reload sshd.
```

Then, as the `osprey` user:

```bash
# Generate a deploy key for the repo, register the public half on GitHub:
ssh-keygen -t ed25519 -f ~/.ssh/osprey_deploy -C "osprey-vps-deploy"
cat ~/.ssh/osprey_deploy.pub
# Add it to GitHub → repo → Settings → Deploy keys (read-only is fine).

git clone git@github.com:<org>/osprey.git ~/osprey
```

### Phase B — host baseline (one command, as root)

```bash
sudo ~/osprey/scripts/host_setup.sh
```

Installs apt packages (`ufw`, `nginx`, `certbot`), opens firewall ports 22/80/443,
installs Docker, adds `osprey` to the `docker` group, creates `/srv/osprey-prod/`
and `/srv/osprey-sandbox/` directories with the right ownership, creates
`/etc/osprey/`, and enables Docker at boot.

After this, log out and back in as `osprey` so the new docker group membership
takes effect.

### Phase C — secrets, env files, nginx, TLS (manual, per-host)

Write the prod env file:

```bash
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
```

If you also want a sandbox stack on the same VPS, write
`/etc/osprey/.env.sandbox` the same way, with a sandbox hostname and a
different `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD`:

```env
DJANGO_ALLOWED_HOSTS=sandbox.osprey.phalkon.io
DJANGO_CSRF_TRUSTED_ORIGINS=https://sandbox.osprey.phalkon.io
```

Install the nginx site and issue TLS certs:

```bash
sudo cp ~/osprey/deploy/nginx.conf /etc/nginx/sites-available/osprey
sudo ln -s /etc/nginx/sites-available/osprey /etc/nginx/sites-enabled/osprey
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# Prod only:
sudo certbot --nginx -d osprey.phalkon.io
# Prod + sandbox:
sudo certbot --nginx -d osprey.phalkon.io -d sandbox.osprey.phalkon.io
```

### Phase D — first deploy

```bash
cd ~/osprey && ./scripts/deploy.sh prod
# Sandbox too, if running both:
./scripts/deploy.sh sandbox main
```

## Running prod and sandbox on one VPS

One repo checkout can run both stacks. The host_setup script already creates
both directory trees, so the only extras are:

1. `/etc/osprey/.env.sandbox` with sandbox values (see Phase C).
2. A second nginx server block for `sandbox.osprey.phalkon.io` proxying to
   `127.0.0.1:8001` with static/media under `/srv/osprey-sandbox/`. The
   shipped [deploy/nginx.conf](../deploy/nginx.conf) already includes this.
3. `docker-compose.sandbox.yml` in the repo root. The shipped file binds the
   sandbox web container to `127.0.0.1:8001`, mounts `/srv/osprey-sandbox/`,
   and reads `/etc/osprey/.env.sandbox`.
4. `./scripts/deploy.sh sandbox` to deploy it.

Separation comes from three things being different at the same time:

1. Compose project name (`-p osprey-prod` vs `-p osprey-sandbox`)
2. Host paths and env files (`/srv/osprey-prod/...` vs `/srv/osprey-sandbox/...`)
3. Host bind port (`127.0.0.1:8000` vs `127.0.0.1:8001`)

If any of those is shared, the stacks collide.

## Routine deploys

The recommended flow is to **tag a release on GitHub, then ssh in and tell the
server to move to that tag**. This keeps prod on a named, reproducible point in
history rather than whatever happens to be on `main`.

From a dev machine, on a clean `main`:

```bash
git pull --ff-only
git tag -a v0.1.0 -m "v0.1.0: short note about what changed"
git push origin v0.1.0
```

Then on GitHub, **Releases → Draft a new release**, pick the tag, and write
the release notes there.

Move prod to that tag:

```bash
ssh osprey@osprey.phalkon.io 'cd ~/osprey && ./scripts/deploy.sh prod v0.1.0'
```

`deploy.sh` accepts an optional ref as the second argument:

- `./scripts/deploy.sh prod` — fast-forward the current branch to its remote
  tip (still works for emergency hotfixes).
- `./scripts/deploy.sh prod v0.1.0` — check out that tag in detached-HEAD mode.
- `./scripts/deploy.sh prod main` — force prod to the tip of `main` (useful
  for rolling forward after a hotfix tag).

In every case the script:

1. `git fetch --tags --prune origin`
2. Checks out the requested ref
3. `docker compose ... build web`
4. `python manage.py migrate --noinput`
5. `python manage.py collectstatic --noinput`
6. `docker compose ... up -d`

No editing on the server. Ever.

### Rollback

```bash
ssh osprey@osprey.phalkon.io 'cd ~/osprey && ./scripts/deploy.sh prod v0.0.9'
```

Rolling back across a migration that drops or renames a column will not restore
data on its own. Irreversible migrations need a database restore from the most
recent backup.

## Autostart after reboot

Two things must both be true for the stack to come back automatically:

1. The Docker daemon starts at boot. `host_setup.sh` enables `docker.service`
   and `containerd.service`, so this is already done.
2. App containers use `restart: unless-stopped`, which the prod and sandbox
   compose overrides already set.

### Verify after first `up -d`

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}'
docker inspect $(docker ps -q) --format '{{.Name}} -> {{.HostConfig.RestartPolicy.Name}}'
```

You should see `unless-stopped` for the app containers.

### Reboot test

```bash
sudo reboot
# reconnect after ~1 minute
docker ps --format 'table {{.Names}}\t{{.Status}}'
```

For a dual-stack host, both project namespaces should be back (`osprey-prod_*`
and `osprey-sandbox_*`) if both were running before reboot.

`unless-stopped` means reboot/daemon restart brings containers back, but a
manual `docker stop` keeps them stopped until you explicitly start them. That's
usually what you want for maintenance windows.

## ORCID setup

The app is wired for ORCID through django-allauth. Longer design notes live in
[planning/features/orcid-signin.md](../planning/features/orcid-signin.md).
ORCID iDs are collected through ORCID sign-in, never typed into OSPREY forms.

### Local ORCID test

ORCID may reject `localhost` redirect URIs. For repeat local testing, use the
Cloudflare tunnel hostname and register this full callback URL with ORCID:

```text
https://ospreydev.phalkon.io/accounts/orcid/login/callback/
```

The tunnel should point that public hostname at local Django, usually
`http://localhost:8000` (the local dev server speaks HTTP; Cloudflare provides
the public HTTPS endpoint). Put matching credentials in local `.env` and include
the tunnel hostname in `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`:

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

4. Confirm the production env file has the public host names:

   ```env
   DJANGO_ALLOWED_HOSTS=osprey.phalkon.io
   DJANGO_CSRF_TRUSTED_ORIGINS=https://osprey.phalkon.io
   ```

5. Deploy and restart:

   ```bash
   ssh osprey@osprey.phalkon.io 'cd ~/osprey && ./scripts/deploy.sh prod'
   ```

6. Test sign-in from a private browser window. The authorize URL should be on
   `orcid.org`, not `sandbox.orcid.org`.

## Backups

Cron job for the `osprey` user. Single-stack host:

```cron
15 4 * * * cd /home/osprey/osprey && docker compose -p osprey-prod --env-file /etc/osprey/.env.prod -f docker-compose.yml -f docker-compose.prod.yml exec -T db pg_dump -U osprey osprey | gzip > /home/osprey/backups/osprey-prod-$(date +\%Y\%m\%d).sql.gz
```

Dual-stack host (use distinct times and filenames so restores are unambiguous):

```cron
15 4 * * * cd /home/osprey/osprey && docker compose -p osprey-prod --env-file /etc/osprey/.env.prod -f docker-compose.yml -f docker-compose.prod.yml exec -T db pg_dump -U osprey osprey | gzip > /home/osprey/backups/osprey-prod-$(date +\%Y\%m\%d).sql.gz
45 4 * * * cd /home/osprey/osprey && docker compose -p osprey-sandbox --env-file /etc/osprey/.env.sandbox -f docker-compose.yml -f docker-compose.sandbox.yml exec -T db pg_dump -U osprey osprey | gzip > /home/osprey/backups/osprey-sandbox-$(date +\%Y\%m\%d).sql.gz
```

Then sync `~/backups/` off-VPS (Backblaze B2, Tailscale rsync, etc.).

## Restore

```bash
gunzip -c /path/to/dump.sql.gz | \
  docker compose -p osprey-prod --env-file /etc/osprey/.env.prod \
    -f docker-compose.yml -f docker-compose.prod.yml exec -T db \
    psql -U osprey -d osprey
```
