# 4. Hosting and deployment guide

## Recommended hosting

- **Where:** a cloud VM in **Singapore** (or **Hong Kong**), from a provider with good connectivity
  into mainland China. Confirm the choice with the one-week latency test from both offices
  (plan step 0.7).
- **Size:** 2 vCPU, 4 GB RAM, 40 GB disk. Use 8 GB RAM if LibreTranslate runs on the same machine.
- **OS:** Ubuntu 24.04 LTS.
- **Why not mainland China:** hosting there requires ICP filing by a Chinese entity and puts the data
  under Chinese jurisdiction.
- **Why not a PC in the office:** power cuts, no public IP, no redundancy
  (see [evaluation §5](01-critical-evaluation.md#5-hosting-a-local-computer)).
- **Local copy:** the nightly encrypted backup is also copied to a NAS at the Oversight office in
  Pakistan.

What runs on the VM (all from [`deploy/docker-compose.yml`](../deploy/docker-compose.yml)):

| Container | Purpose |
|---|---|
| `caddy` | HTTPS front door; obtains and renews certificates automatically; HTTP → HTTPS |
| `web` | the portal (Django + gunicorn); applies database migrations on start |
| `scheduler` | reminders, escalations and translation retries every 15 minutes |
| `db` | PostgreSQL 16 |
| `backup` | nightly database + documents archive, encrypted, rotated, copied off-site |
| `libretranslate` *(optional profile)* | self-hosted machine translation for remarks |
| `clamav` *(optional profile)* | virus scanning of uploaded files |

This stack was built and tested end to end:

- image build;
- Compose start-up on PostgreSQL;
- HTTPS through Caddy with an HTTP → HTTPS redirect;
- security headers (HSTS, CSP, X-Frame-Options, nosniff, Permissions-Policy);
- secure, HttpOnly session cookies;
- browser login and scan-to-receive over HTTPS;
- encrypted backup, and a restore into a fresh database (all consignments, items and audit
  records recovered).

## Step by step

### 1. Server and DNS
1. Create the VM. Point a DNS **A record** (e.g. `portal.yourcompany.com`) at its IP address.
2. Harden the server:
   ```bash
   adduser ops && usermod -aG sudo ops              # personal admin account
   # copy your SSH public key to ~ops/.ssh/authorized_keys, then in /etc/ssh/sshd_config:
   #   PasswordAuthentication no
   #   PermitRootLogin no
   sudo systemctl restart ssh
   sudo apt update && sudo apt install -y ufw fail2ban unattended-upgrades
   sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw allow 443/udp
   sudo ufw enable
   sudo dpkg-reconfigure -plow unattended-upgrades   # automatic security updates
   ```
   If the offices have static IP addresses, restrict SSH to them (`ufw allow from <ip> to any port 22`).
3. Install Docker Engine and the Compose plugin (official instructions for Ubuntu at docs.docker.com).

### 2. Configure
```bash
git clone <this repository> portal && cd portal/deploy
cp .env.example .env
openssl rand -hex 32   # → DJANGO_SECRET_KEY
openssl rand -hex 24   # → POSTGRES_PASSWORD
openssl rand -hex 24   # → BACKUP_PASSPHRASE  (store it offline too: without it backups cannot be read)
nano .env              # domain, secrets, SMTP, MT provider, backup settings
```

### 3. Start
```bash
docker compose up -d --build
docker compose ps                                  # web and db should be "healthy"
docker compose exec web python manage.py createsuperuser
```
Open `https://portal.yourcompany.com`. The certificate is issued automatically within about a
minute.

### 4. Set up organisations and people
In `https://…/admin/` (system administrator only):

1. **Organisations:** one Shipper (default language 简体中文, optional WeCom webhook), one Receiver,
   one Oversight.
2. **Users:** a profile for each person with organisation, access level (Manager / Operator /
   Viewer), language and time zone (Asia/Shanghai, Asia/Karachi). Use each person's own e-mail
   address for reminders.
3. **Item types:** the item master (codes, three-language names, serialised or batch, controlled flag).
4. **Document types:** adjust who uploads each, which are required by default, and the due
   milestone and offset.

With `PORTAL_REQUIRE_2FA=1`, every user sets up an authenticator app at first login.

### 5. Notifications
- **E-mail:** set `SMTP_*` and `PORTAL_FROM_EMAIL`. Publish SPF, DKIM and DMARC records for the
  sending domain; without them mail to QQ, 163 or corporate Chinese mailboxes may land in spam.
- **WeCom (企业微信):** in the Shipper's WeCom group choose *Add group robot*, copy the webhook URL,
  and paste it into the Shipper organisation in the admin. Reminders then appear in that group in
  Chinese.
- **Test:** `docker compose exec web python manage.py run_scheduler --once`

### 6. Backups (do not skip)
- They run automatically every 24 hours into `deploy/backups/`, encrypted with `BACKUP_PASSPHRASE`,
  and are kept for `BACKUP_KEEP_DAYS` days.
- **Off-site copy:** create `deploy/rclone/rclone.conf` with a remote (for example an SFTP remote
  pointing at the NAS in the Oversight office, or any S3-compatible bucket) and set `RCLONE_REMOTE`,
  e.g. `pk-nas:portal-backups`.
- **Run one now:** `docker compose run --rm backup once`
- **Restore drill (monthly):**
  ```bash
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in portal-YYYYMMDD-HHMMSS.tar.enc -out restore.tar
  tar -xf restore.tar                      # → db.dump, media.tar.gz
  docker compose stop web scheduler
  docker compose cp db.dump db:/tmp/db.dump
  docker compose exec db pg_restore -U portal -d portal --clean --if-exists --no-owner /tmp/db.dump
  docker compose run --rm -v "$PWD":/restore --entrypoint sh web -c "tar -xzf /restore/media.tar.gz -C /data"
  docker compose start web scheduler
  docker compose exec web python manage.py verify_audit
  ```

### 7. Optional services
- **Self-hosted translation:**
  - set `MT_PROVIDER=libretranslate`, then run `docker compose --profile translate up -d`;
  - the first start downloads the English, Chinese and Urdu models.
- **Claude API translation:**
  - set `MT_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`;
  - remark text is sent to Anthropic for translation, and originals are always kept.
- **Virus scanning:**
  - set `PORTAL_CLAMAV=clamav:3310`, then run `docker compose --profile antivirus up -d`;
  - uploads are refused while the scanner is unavailable.

### 8. Monitoring
- Point an external uptime monitor at `https://…/login/`. Check it from Pakistan *and* China if the
  monitor supports that.
- Watch disk space (`df -h`) and container state (`docker compose ps`).
- Logs: `docker compose logs --tail 200 web scheduler caddy backup`.
- Quarterly: `docker compose exec web python manage.py verify_audit`. It detects edited or deleted
  audit history.

### 9. Updating
```bash
cd portal && git pull
cd deploy && docker compose run --rm backup once     # safety copy first
docker compose up -d --build                         # migrations run automatically
docker compose ps && docker compose logs --tail 50 web
```

## Security checklist

- [ ] HTTPS only (Caddy), HSTS enabled; `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` set to the real domain
- [ ] `PORTAL_REQUIRE_2FA=1`; admin accounts personal (no shared logins); leavers removed the same day
- [ ] SSH key-only, firewall allows 22/80/443 only, automatic security updates on
- [ ] `.env` readable by the admin only (`chmod 600 deploy/.env`); secrets never committed
- [ ] Backups encrypted, copied off-site, restore drill done this month
- [ ] Virus scanning on if external parties upload files routinely
- [ ] Machine-translation provider approved for the sensitivity of the data
- [ ] Outer labels do not show contents (default)
- [ ] Privacy notice given to users in both countries (business contact data only)

## Running the demo locally (no Docker)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cd src
export DJANGO_DEBUG=1
python manage.py migrate
python manage.py seed_demo          # demo organisations, users and 5 consignments
python manage.py runserver          # http://127.0.0.1:8000  – users listed by seed_demo
python manage.py test               # 64 tests
```

## China-specific notes
- The portal loads nothing from Google, CDNs or external font services. Do not add analytics or
  embeds that do.
- There is no need for ICP filing, because the server is outside mainland China. Speed from China
  depends on the provider's routes, which is why the latency test comes first.
- The camera scanner falls back to a bundled decoder, so it works on Chinese Android phones without
  Google services and in in-app browsers.
- Chinese Excel saves CSV as GBK/GB18030; the packing-list import accepts it as well as UTF-8.
