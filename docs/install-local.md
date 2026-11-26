# Install in the shop (local server)

One computer in the shop runs the whole system: the app, the MySQL database,
nightly backups and the M-Pesa and SMS jobs. Every other till opens it in a
browser over the shop network. Sales keep working when the internet is down;
only M-Pesa prompts, SMS and remote access need the internet.

The same package runs on a rented server instead: see [install-vps.md](install-vps.md).

## 1. The server PC

| | Minimum | Better |
|---|---|---|
| Machine | any PC or mini-PC from the last 8 years | mini-PC with an SSD (Intel N100 class) |
| Memory | 4 GB | 8 GB |
| Disk | 64 GB SSD | 256 GB SSD |
| Network | cable to the router | cable to the router |
| Power | UPS | UPS |

- **Use a cable**, not Wi-Fi, between the server and the router.
- **Put the server and the router on a UPS.** A power cut during a sale is
  harmless; a power cut while MySQL writes to disk can damage the database.
  A small 650 VA UPS gives 10 to 20 minutes, enough to finish the sale.
- **Ubuntu Server 24.04 LTS** is the recommended system. Windows 10/11 with
  Docker Desktop also works (steps below).
- Set the BIOS to **power on after a power loss** ("Restore on AC power loss:
  Power On") so the server comes back by itself.

## 2. Give the server a fixed address

The tills open the server by its address, so it must never change. In the
router's admin page, find **DHCP reservation** (also called "static lease" or
"address binding") and reserve an address for the server, for example
`192.168.1.10`. That is better than typing a static address on the server,
because the router then never gives that address to a phone.

## 3. Install

### Ubuntu

```bash
# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER        # log out and in again

# The app
git clone https://github.com/Markkimotho/make-an-order.git
cd make-an-order/deploy
cp .env.example .env
nano .env                            # see "Settings" below
docker compose up -d --build
```

### Windows

1. Install **Docker Desktop** (docker.com), and in its settings tick
   "Start Docker Desktop when you sign in".
2. Install **Git for Windows** (it includes Git Bash for the scripts).
3. In Git Bash:

```bash
git clone https://github.com/Markkimotho/make-an-order.git
cd make-an-order/deploy
cp .env.example .env
notepad .env
docker compose up -d --build
```

4. Windows Firewall: allow Docker Desktop on **private networks** when asked,
   or the tills cannot reach port 80. Set the shop network to "Private" in
   Settings > Network.
5. Turn off sleep: Settings > System > Power > Sleep: Never.

### Settings (deploy/.env)

At least:

| Setting | Value |
|---|---|
| `APP_SECRET_KEY` | `openssl rand -hex 32` |
| `MYSQL_PASSWORD`, `MYSQL_ROOT_PASSWORD` | `openssl rand -hex 24` each (letters and digits only) |
| `TENANT_SECRETS_KEY` | see the comment in the file; **keep a copy offline** |
| `APP_URL` | `http://192.168.1.10` (your server's address), or the tunnel address once you add one |
| `SINGLE_BUSINESS_INSTALL` | `true` |

Every setting has a comment in `deploy/.env.example`.

### First start

```bash
docker compose ps                      # db healthy, app running
docker compose logs -f app             # migrations, then gunicorn
scripts/lan-address.sh                 # prints http://192.168.1.10
```

Open that address on the server, register the business, and log in as the
owner.

## 4. The tills

On each till:

1. Open Chrome at the address from `scripts/lan-address.sh`, e.g.
   `http://192.168.1.10`.
2. Pin the tab, or make it the start page (Settings > On start-up > Open a
   specific page).
3. Log in as that till's cashier.

Tills need nothing installed except Chrome (or Edge).

## 5. Receipt printers (80 mm thermal)

Most 80 mm USB thermal printers (Xprinter, Epson TM-T20, Rongta) install as a
normal Windows or Linux printer.

1. Install the printer's driver on the till and print a test page.
2. In the driver's preferences set paper to **80 mm x receipt** (or "Roll
   paper 80 x 297") and margins to 0.
3. Make it the till's default printer.
4. For one-click receipts with no print dialog, start Chrome in kiosk-printing
   mode: make a shortcut with

   ```
   "C:\Program Files\Google\Chrome\Application\chrome.exe" --kiosk-printing http://192.168.1.10/pos-ui
   ```

   Chrome then prints straight to the default printer. In the first print
   dialog (without the flag) set Margins: None and untick Headers and footers;
   Chrome remembers it.

The receipt page is laid out for 80 mm (`/pos-ui/receipt/<id>?print=1`).

## 6. Backups

The `backup` service backs up the database once a day. It checks every hour
and backs up when the newest backup is older than 20 hours, so a server that
is switched off at night still gets its backup soon after it is switched on.
After each backup it checks the file and deletes old ones: it keeps the
newest backup of each of the last 14 days and of each of the last 8 weeks.

**Settings > Backup** in the app shows the last backup, has a "Back up now"
button and lets the owner download backups. The page and the dashboard warn
when the last backup is older than 26 hours.

A backup on the same disk does not survive the disk dying. Copy them off the
server, one of these ways:

**USB disk (always plugged in).** Format it ext4 (Ubuntu) and mount it at a
fixed place, e.g. `/media/usb`, then:

```bash
sudo mkdir -p /media/usb/mao-backups && sudo chown 1000:1000 /media/usb/mao-backups
# deploy/.env
BACKUP_VOLUME=/media/usb/mao-backups
docker compose up -d
```

**Cloud (Google Drive, Backblaze B2, Dropbox) with rclone.** On any computer
run `rclone config` to create a remote (e.g. `gdrive`), copy the resulting
`rclone.conf` to `deploy/rclone/rclone.conf` on the server, then:

```bash
# deploy/.env
BACKUP_RCLONE_REMOTE=gdrive:mao-backups
docker compose up -d
```

Every new backup is then copied there. A failed copy is logged and does not
stop the backup.

**By hand.** Download the newest backup from Settings > Backup once a week
onto a flash disk you take home.

Useful commands (in `deploy/`):

```bash
docker compose exec app flask backup list
docker compose exec app flask backup create
docker compose exec app flask backup verify latest
```

`flask backup verify` checks the file's checksum. To also restore it into a
scratch database and compare row counts, set `BACKUP_VERIFY_MYSQL_URL` (see
`.env.example`).

## 7. Remote access and M-Pesa callbacks (Cloudflare Tunnel)

M-Pesa must reach the server to confirm payments, and the owner may want to
check sales from home. A Cloudflare Tunnel does both with no port forwarding
and no fixed public IP.

1. Put a domain on Cloudflare (free plan).
2. Cloudflare dashboard > Zero Trust > Networks > Tunnels > Create a tunnel,
   type "Cloudflared". Copy the **token**.
3. Add a **public hostname**, e.g. `shop.example.co.ke`, service
   `http://app:8000`.
4. In `deploy/.env`:

   ```
   COMPOSE_PROFILES=tunnel
   CLOUDFLARE_TUNNEL_TOKEN=<the token>
   APP_URL=https://shop.example.co.ke
   PUBLIC_BASE_URL=https://shop.example.co.ke
   ```

5. `docker compose up -d`. Open `https://shop.example.co.ke` from a phone on
   mobile data to check.
6. In Settings, use the callback URLs it shows (they now start with your
   https address) and press "Register Paybill URLs" for a Paybill.

Tills in the shop keep using the LAN address, which works without internet.

Leave `PROXY_FIX_X_FOR=0` here. The tills reach the app directly, so trusting
`X-Forwarded-For` would let anyone on the shop network fake their address and
get round the sign-in rate limit. Remote sign-ins through the tunnel then share
one rate limit (10 a minute); account lockout still applies per person.

## 8. Updates

```bash
cd make-an-order
git pull
cd deploy
docker compose up -d --build           # migrations run when the app starts
```

Take a backup first: `docker compose exec app flask backup create`.

## 9. When the server PC dies

What you need: a backup file (USB disk, cloud, or a download) and the
`deploy/.env` file (keep a copy with the backups, somewhere safe: it holds
`TENANT_SECRETS_KEY`, without which saved M-Pesa keys cannot be read).

1. Set up the new PC (sections 1 to 3) with the **same** `deploy/.env`.
   Give it the old server's reserved address in the router (change the
   reservation to the new PC), so the tills need no change.
2. `docker compose up -d --build` and wait until the app is running.
3. Restore:

   ```bash
   cd make-an-order/deploy
   scripts/restore.sh /media/usb/mao-backups/mao-20261126-031500.sql.gz
   ```

   It asks you to type the database name, stops the app, saves the empty new
   database as a "prerestore" backup, restores, and starts the app (which
   applies any newer migrations).
4. Log in and check the last sales. Sales made after the backup are not in
   it; enter them again from paper receipts or M-Pesa statements.

The same script undoes a mistake on a working server: pick the backup from
before the mistake. `scripts/restore.sh` with no file lists the backups.

On Windows without Git Bash, the same steps by hand:

```bash
docker compose stop app backup jobs
docker compose run --rm app flask backup restore mao-20261126-031500.sql.gz
docker compose up -d
```
