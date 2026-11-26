# Install on a VPS

The alternative to a server in the shop: a rented Linux server runs the same
package and every till opens it over the internet. Choose it when the shop's
internet is reliable, when several branches share one system, or when one
server hosts several businesses. Tills stop working when the shop's internet
is down, which the [local install](install-local.md) avoids.

## Sizing

| Businesses | VPS | Disk |
|---|---|---|
| 1 to 3 shops | 2 vCPU, 2 GB RAM | 40 GB SSD |
| up to 20 | 2 vCPU, 4 GB RAM | 80 GB SSD |
| more | 4 vCPU, 8 GB RAM, or MySQL on a managed database | 160 GB SSD |

Ubuntu 24.04 LTS. A Nairobi or Johannesburg region keeps the tills fast;
Europe works too.

## Install

```bash
# as root on a new server: a user, Docker, firewall
adduser mao && usermod -aG sudo mao
curl -fsSL https://get.docker.com | sh
usermod -aG docker mao
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp
ufw enable

# as mao
git clone https://github.com/Markkimotho/make-an-order.git
cd make-an-order/deploy
cp .env.example .env
nano .env
```

In `deploy/.env`:

```
COMPOSE_PROFILES=vps
APP_DOMAIN=pos.example.co.ke
APP_URL=https://pos.example.co.ke
PUBLIC_BASE_URL=https://pos.example.co.ke
APP_BIND=127.0.0.1
APP_PORT=8080
SINGLE_BUSINESS_INSTALL=false          # true if only one business uses this server
```

plus the secrets (`APP_SECRET_KEY`, `MYSQL_PASSWORD`, `MYSQL_ROOT_PASSWORD`,
`TENANT_SECRETS_KEY`), as described in the file.

`APP_BIND=127.0.0.1` keeps the app's plain-HTTP port off the internet; only
Caddy (ports 80 and 443) is public. Docker publishes ports around `ufw`, so
this setting, not the firewall, is what keeps 8080 private.

## DNS and HTTPS

1. At your DNS provider, add an **A record** `pos.example.co.ke` pointing at
   the VPS's IP address (and AAAA for IPv6 if the VPS has one).
2. `docker compose up -d --build`.
3. Caddy gets a Let's Encrypt certificate on the first request and renews it
   by itself. Watch it with `docker compose logs -f caddy`.

If the domain is on Cloudflare with the orange-cloud proxy, either turn the
proxy off for this record or use the `tunnel` profile instead of `vps` (see
the local guide, section 7) and close ports 80 and 443.

## Several businesses on one server

Each business registers at `https://pos.example.co.ke/register`. Data is kept
apart per business inside one database. A backup holds every business, so
with `SINGLE_BUSINESS_INSTALL=false` only platform admins
(`PLATFORM_ADMIN_EMAILS` plus `flask create-platform-admin`) can download
backups; business owners still see the backup status.

```bash
docker compose exec app flask create-platform-admin you@example.com
```

## Backups offsite

A VPS disk can fail and a provider can lose a server. Copy every backup off
the VPS with rclone (Backblaze B2, S3, Google Drive):

```bash
mkdir -p rclone
docker run --rm -it -v "$PWD/rclone:/config/rclone" rclone/rclone config
# deploy/.env
BACKUP_RCLONE_REMOTE=b2:my-bucket/mao
docker compose up -d
```

Check it after a day: `docker compose logs backup` shows "offsite copy ...
done". Also turn on the provider's own snapshots if offered.

To test-restore every backup into a scratch database, set
`BACKUP_VERIFY_MYSQL_URL=mysql://root:<MYSQL_ROOT_PASSWORD>@db:3306`.

## Scheduled jobs

The `jobs` service runs `flask payments-sweep` every 10 minutes and
`flask run-daily-triggers` once a day after 06:00 Nairobi time; nothing to set
up. Do not also add them to cron.

## Updates and restore

Same as the local install: `git pull && docker compose up -d --build` to
update (back up first), and `scripts/restore.sh <file>` to restore. See
[install-local.md](install-local.md), sections 8 and 9.
