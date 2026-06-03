# Setting up an OSPREY server

This is the runbook I use to bring up an OSPREY VPS from scratch. It's written
for Ubuntu 24.04 on Hetzner, but anything Debian-flavored should be close. The
stack is Postgres and Django under Docker Compose, with nginx and certbot
running on the host (not in containers).

## What's in the repo

A few scripts you'll touch on the server:

- `scripts/host_setup.sh` brings a fresh host up to baseline: apt packages,
  ufw, docker, the `/srv/osprey-*` directories. Run it once as root after
  you've cloned the repo. It's safe to re-run if you want to confirm the host
  still matches baseline.
- `scripts/new_env.sh {prod|sandbox}` prints an env-file template with random
  `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD` already filled in. Pipe the
  output into `/etc/osprey/.env.prod` or `.env.sandbox` and fill in the
  remaining placeholders (ORCID, Zenodo).
- `scripts/deploy.sh {prod|sandbox} [ref]` deploys one stack. With no ref it
  fast-forwards the current branch; with a ref it checks out that tag or
  commit before rebuilding.
- `deploy.sh` at the repo root is a backwards-compatible wrapper that runs
  `scripts/deploy.sh prod`.

## The three environments

OSPREY runs in three places, all driven by the same `docker-compose.yml` plus
an override file and an env file. The pattern is the same in each, just with
different inputs.

**Local dev** is what you run on your laptop:

- Compose: `docker-compose.yml` only, no override.
- Env file: repo-local `.env` (gitignored). Named directly by
  `docker-compose.yml`, so plain `docker compose up` picks it up.
- Postgres data, static, and media live in the repo's `media/` and a Docker
  volume. `DJANGO_DEBUG=1`, weak secret, ORCID pointed at sandbox or a
  Cloudflare-tunneled dev hostname.

**Production** is what serves `osprey.phalkon.io`:

- Compose: `docker-compose.yml` plus `docker-compose.prod.yml`.
- Env file: `/etc/osprey/.env.prod` on the server, owned by `osprey`, mode 640.
  No `.env` on the server.
- Compose project name `osprey-prod`. Web bound to `127.0.0.1:8000`. Postgres
  data, static, and media in `/srv/osprey-prod/`. `DJANGO_DEBUG=0`, real
  secret, real ORCID credentials.

**Sandbox** is what serves `sandbox.osprey.phalkon.io` on the same VPS:

- Compose: `docker-compose.yml` plus `docker-compose.sandbox.yml`.
- Env file: `/etc/osprey/.env.sandbox`. Same shape as prod, *different*
  `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD`, sandbox hostnames.
- Compose project name `osprey-sandbox`. Web bound to `127.0.0.1:8001`.
  Postgres data, static, and media in `/srv/osprey-sandbox/`.

`scripts/deploy.sh` handles prod and sandbox by stitching together the right
project name, env file, and override file:

```bash
# prod
docker compose -p osprey-prod --env-file /etc/osprey/.env.prod \
  -f docker-compose.yml -f docker-compose.prod.yml ...

# sandbox
docker compose -p osprey-sandbox --env-file /etc/osprey/.env.sandbox \
  -f docker-compose.yml -f docker-compose.sandbox.yml ...
```

One nuance worth knowing because it bit me: Compose's `--env-file` and a
service's `env_file:` directive are two different things. `--env-file` tells
the *Compose CLI* what variables exist while it's parsing the YAML, so
`${POSTGRES_PASSWORD}` resolves. `env_file:` inside a service injects those
same variables into the *running container*. Prod and sandbox use both, and
they both point at the same file.

## Bringing up a fresh server

Roughly six steps. Only one of them is a single script; the rest is manual
because it involves secrets, per-host decisions, or interactive prompts.

### 1. Make a deploy user (as root)

After Hetzner hands you a fresh box and you've SSH'd in as root:

```bash
adduser osprey
usermod -aG sudo osprey
rsync --archive --chown=osprey:osprey ~/.ssh /home/osprey/
```

Then edit `/etc/ssh/sshd_config` to disable root login and password auth and
reload sshd. From this point on you should be logging in as `osprey` with your
SSH key.

### 2. Clone the repo (as osprey)

Generate a deploy key and add the public half to GitHub under the repo's
Settings → Deploy keys. Read-only is fine.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/osprey_deploy -C "osprey-vps-deploy"
cat ~/.ssh/osprey_deploy.pub
```

Once GitHub knows about it:

```bash
git clone git@github.com:Phalkon-Industries/osprey.git ~/osprey
```

### 3. Run the host baseline script (as root)

```bash
sudo ~/osprey/scripts/host_setup.sh
```

This installs ufw, nginx, certbot, and Docker, opens firewall ports 22/80/443,
adds `osprey` to the `docker` group, creates the `/srv/osprey-prod/` and
`/srv/osprey-sandbox/` directory trees, and creates `/etc/osprey/` for the env
files.

Log out and back in as `osprey` afterwards so the new docker group membership
actually applies to your shell.

### 4. Write the env files

The host_setup script can't do this for you because it's secrets. There's a
helper that prints a template with random `DJANGO_SECRET_KEY` and
`POSTGRES_PASSWORD` already filled in. Pipe it into place and edit in the
ORCID and Zenodo values:

```bash
~/osprey/scripts/new_env.sh prod | sudo -u osprey tee /etc/osprey/.env.prod > /dev/null
sudo chmod 640 /etc/osprey/.env.prod
sudo nano /etc/osprey/.env.prod   # fill in ORCID + Zenodo placeholders
```

The template covers:

- `DJANGO_*` — secret key, debug off, allowed hosts, CSRF origins, secure
  cookies.
- `POSTGRES_*` and `DATABASE_URL` — local Postgres in the compose stack. The
  password is generated; `DATABASE_URL` already substitutes it in.
- `ORCID_*` — placeholders. Fill in with credentials from your production
  ORCID API client (see the ORCID section below).
- `ZENODO_*` — placeholders. `ZENODO_USE_SANDBOX=0` points at real Zenodo;
  `ZENODO_ACCESS_TOKEN` is a personal access token from
  [zenodo.org/account/settings/applications/](https://zenodo.org/account/settings/applications/).
  Leave `ZENODO_DEFAULT_COMMUNITY` empty unless you've made an OSPREY
  community on Zenodo.

For a sandbox stack on the same VPS:

```bash
~/osprey/scripts/new_env.sh sandbox | sudo -u osprey tee /etc/osprey/.env.sandbox > /dev/null
sudo chmod 640 /etc/osprey/.env.sandbox
sudo nano /etc/osprey/.env.sandbox
```

The helper regenerates a different `DJANGO_SECRET_KEY` and
`POSTGRES_PASSWORD` for sandbox, so the two stacks never share secrets. If
you don't have a separate ORCID sandbox client, real ORCID credentials are
fine for sign-in testing; just keep `ORCID_USE_SANDBOX=0`.

### 5. nginx and TLS

```bash
sudo cp ~/osprey/deploy/nginx.conf /etc/nginx/sites-available/osprey
sudo ln -s /etc/nginx/sites-available/osprey /etc/nginx/sites-enabled/osprey
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Then ask certbot for certs. One hostname:

```bash
sudo certbot --nginx -d osprey.phalkon.io
```

Or both, if you're running sandbox too:

```bash
sudo certbot --nginx -d osprey.phalkon.io -d sandbox.osprey.phalkon.io
```

certbot installs its own renewal timer, so this is roughly set-and-forget.
I still check `sudo certbot renew --dry-run` once after install to confirm.

### 6. First deploy

```bash
cd ~/osprey && ./scripts/deploy.sh prod
```

If you're running sandbox too:

```bash
./scripts/deploy.sh sandbox main
```

That's a full server. If it worked you should be able to hit the public
hostname in a browser and get a working OSPREY.

## Running prod and sandbox side by side

One repo checkout, two stacks. `host_setup.sh` already made both directory
trees. The rest is making sure these three things are different at the same
time:

- Compose project name: `-p osprey-prod` vs `-p osprey-sandbox`.
- Host paths and env file: `/srv/osprey-prod/` + `.env.prod` vs
  `/srv/osprey-sandbox/` + `.env.sandbox`.
- Host bind port: `127.0.0.1:8000` for prod, `127.0.0.1:8001` for sandbox.

If any one of those collides between the two stacks, things break in weird
ways.

What you actually need on the box:

1. `/etc/osprey/.env.sandbox` populated as described above.
2. The shipped [deploy/nginx.conf](../deploy/nginx.conf) already has a second
   server block for `sandbox.osprey.phalkon.io` pointing at `127.0.0.1:8001`.
3. The shipped `docker-compose.sandbox.yml` already binds the sandbox web
   container to port 8001 and mounts `/srv/osprey-sandbox/`.
4. Deploy with `./scripts/deploy.sh sandbox`.

## Routine deploys

The way I do this: tag a release on GitHub, then ssh to the server and point
it at the tag. Prod sits on a named version rather than whatever happened to
be on `main` when I last pulled.

From a dev machine on a clean `main`:

```bash
git pull --ff-only
git tag -a v0.1.0 -m "v0.1.0: short note about what changed"
git push origin v0.1.0
```

Then go to GitHub → Releases → Draft a new release, pick the tag, and write
the real release notes there. That's the public log of what's in prod.

Move prod to it:

```bash
ssh osprey@osprey.phalkon.io 'cd ~/osprey && ./scripts/deploy.sh prod v0.1.0'
```

`scripts/deploy.sh` takes an optional ref as the second argument. With no ref
it fast-forwards the current branch, which is handy when you've already
pushed to `main` and just want the server to catch up. With a tag it checks
the tag out in detached-HEAD mode. With a branch name it forces the server to
that branch's tip, useful for rolling forward past a hotfix.

Under the hood the script does the same five things every time: git fetch,
checkout, build, migrate, collectstatic, up -d.

Never edit anything on the server. If you find yourself wanting to, that's a
sign the deploy script is missing something; fix it there.

### Rolling back

```bash
ssh osprey@osprey.phalkon.io 'cd ~/osprey && ./scripts/deploy.sh prod v0.0.9'
```

Honest caveat: if a migration between v0.0.9 and v0.1.0 dropped or renamed a
column, this rollback won't bring the data back. Irreversible migrations need
a database restore from the most recent backup.

## Autostart after reboot

Two things have to be true. Both are already set up by the rest of the
runbook:

- `docker.service` and `containerd.service` start at boot. `host_setup.sh`
  enables them.
- The app containers use `restart: unless-stopped`, set in the prod and
  sandbox compose overrides.

After your first `up -d` it's worth confirming:

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}'
docker inspect $(docker ps -q) --format '{{.Name}} -> {{.HostConfig.RestartPolicy.Name}}'
```

You should see `unless-stopped` for the app containers. To actually verify
autostart works, reboot the box:

```bash
sudo reboot
# wait about a minute, reconnect
docker ps
```

If both stacks were up before the reboot, both should be back.

One subtlety worth knowing: `unless-stopped` only restarts containers Docker
stopped on its own. If you `docker stop` something by hand, it stays stopped
until you start it again, which is usually what you want during maintenance.

## ORCID

OSPREY uses ORCID through django-allauth for sign-in. The longer design notes
are in [planning/features/orcid-signin.md](../planning/features/orcid-signin.md).
The short version: ORCID iDs come in through OAuth only, nobody types them
into a form.

### Testing locally

ORCID doesn't always accept `localhost` as a callback host, so for repeat
local testing the easiest thing is a Cloudflare tunnel pointing a public
hostname at `http://localhost:8000`. Register this exact callback URL with
ORCID:

```text
https://ospreydev.phalkon.io/accounts/orcid/login/callback/
```

Then in your local `.env`:

```env
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,ospreydev.phalkon.io
DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8000,https://ospreydev.phalkon.io
ORCID_USE_SANDBOX=0
ORCID_CLIENT_ID=<orcid application id>
ORCID_CLIENT_SECRET=<orcid secret>
```

Only set `ORCID_USE_SANDBOX=1` if you're actually using sandbox credentials.
Real ORCID is fine for sign-in testing because OSPREY only reads the iD; it
doesn't write to anyone's ORCID record.

### Production ORCID

1. Register a public API client at https://orcid.org/.
2. Add the production callback URL:
   `https://osprey.phalkon.io/accounts/orcid/login/callback/`. Don't add
   hostnames that aren't actually going to serve OSPREY.
3. Put the client ID and secret in `/etc/osprey/.env.prod`, with
   `ORCID_USE_SANDBOX=0`.
4. Make sure that file also lists the public hostname in
   `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`.
5. Deploy: `ssh osprey@osprey.phalkon.io 'cd ~/osprey && ./scripts/deploy.sh prod'`.
6. Test sign-in from a private browser window. The OAuth authorize URL should
   be on `orcid.org`, not `sandbox.orcid.org`.

## Backups

A nightly cron job for the `osprey` user. Single stack:

```cron
15 4 * * * cd /home/osprey/osprey && docker compose -p osprey-prod --env-file /etc/osprey/.env.prod -f docker-compose.yml -f docker-compose.prod.yml exec -T db pg_dump -U osprey osprey | gzip > /home/osprey/backups/osprey-prod-$(date +\%Y\%m\%d).sql.gz
```

Dual stack (offset the times and use distinct filenames so a restore isn't
ambiguous):

```cron
15 4 * * * cd /home/osprey/osprey && docker compose -p osprey-prod --env-file /etc/osprey/.env.prod -f docker-compose.yml -f docker-compose.prod.yml exec -T db pg_dump -U osprey osprey | gzip > /home/osprey/backups/osprey-prod-$(date +\%Y\%m\%d).sql.gz
45 4 * * * cd /home/osprey/osprey && docker compose -p osprey-sandbox --env-file /etc/osprey/.env.sandbox -f docker-compose.yml -f docker-compose.sandbox.yml exec -T db pg_dump -U osprey osprey | gzip > /home/osprey/backups/osprey-sandbox-$(date +\%Y\%m\%d).sql.gz
```

A dump sitting on the same VPS isn't really a backup, so push `~/backups/`
off-box. I use Backblaze B2; Tailscale plus rsync to a NAS works fine too.

## Restore

```bash
gunzip -c /path/to/dump.sql.gz | \
  docker compose -p osprey-prod --env-file /etc/osprey/.env.prod \
    -f docker-compose.yml -f docker-compose.prod.yml exec -T db \
    psql -U osprey -d osprey
```

If you're restoring across schema versions, deploy the matching code first so
the database schema matches what `pg_dump` actually produced.
