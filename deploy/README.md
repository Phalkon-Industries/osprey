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

sudo mkdir -p /srv/osprey/postgres /etc/osprey
sudo chown osprey:osprey /srv/osprey/postgres /etc/osprey
sudo chmod 750 /srv/osprey /srv/osprey/postgres
sudo chmod 750 /etc/osprey

# Production env file (never committed):
sudo -u osprey tee /etc/osprey/.env.prod > /dev/null <<'ENV'
DJANGO_SECRET_KEY=<generate a long random string>
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=osprey.science,www.osprey.science
POSTGRES_DB=osprey
POSTGRES_USER=osprey
POSTGRES_PASSWORD=<long random>
DATABASE_URL=postgres://osprey:<long random>@db:5432/osprey
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
