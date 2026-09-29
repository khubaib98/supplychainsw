# supplychainsw: CN–PK Consignment Portal

A shared portal for a China → Pakistan supply chain. The **Shipper** (China), the **Receiver**
(Pakistan) and **Oversight** (top management) use it to track consignments down to the individual
item, exchange documents, and raise and resolve issues. It works in **English, 简体中文 and اردو**.

This repository contains:

1. **A critical evaluation of the original proposal**, with a recommended design:
   [docs/01-critical-evaluation.md](docs/01-critical-evaluation.md)
2. **A working prototype** implementing that design (Django, PostgreSQL, Docker), with demo data
   based on the 57-carton / 40 × DCJM example.
3. **A step-by-step plan to develop and deploy** it:
   [docs/03-delivery-plan.md](docs/03-delivery-plan.md)
4. **A tested deployment kit and runbook** (HTTPS, backups, restore):
   [docs/04-deployment-guide.md](docs/04-deployment-guide.md)

![Oversight dashboard](docs/img/dashboard-oversight.png)

## What it does

- **Item-level traceability.** Consignment `CSG-2026-0042`, unit `CSG-2026-0042-CTN-017` and item
  `CSG-2026-0042-DCJM-017` are generated automatically. IDs never contain the current carton; the
  location is a record, and every move, split and receipt is logged. Units nest to any depth:
  container/truck › pallet › crate › carton › inner box › item.
- **Layer-by-layer map.** Every item and unit shows its full chain (container › pallet › carton ›
  item) with the neighbours at each layer. A nested packing view covers the whole consignment.
- **QR labels.** Package labels are 100×150 mm and item labels 50×30 mm. Scan with a phone camera or
  a handheld scanner to look up, move or receive. Shipper packing lists can be imported from
  Excel/CSV, and packing lists are generated from the data.
- **Milestones and documents.** 11 milestones, each recorded by the party that owns it. Required
  documents depend on the transport mode (B/L, AWB or road note), and controlled items automatically
  add export-licence and end-user NOC requirements. Every upload is versioned and SHA-256
  fingerprinted, and nothing is deleted.
- **Reminders that escalate.** Missing documents trigger a reminder, a second reminder, then an
  escalation to Oversight, by e-mail, in-app and WeCom.
- **Remarks and queries.** Either party raises damage, discrepancy, payment or document issues and
  follows them through open › answered › resolved. Remarks can be machine-translated, with the
  original always shown.
- **Three languages.** The whole interface is translated (Urdu right-to-left with a Nastaliq font).
  Each user's own time zone is shown on every timestamp.
- **Oversight.** Dashboard with KPIs, stage chart and a needs-attention list; read-only data tables
  with CSV export; a hash-chained audit log.
- **Security.** Organisation × access-level roles with ownership and custody rules, two-factor login,
  lock-out, strict security headers, and no third-party CDNs (important for access from China).

## Try it in two minutes

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cd src && export DJANGO_DEBUG=1
python manage.py migrate && python manage.py seed_demo
python manage.py runserver        # open http://127.0.0.1:8000
```

Demo users (password `Demo-Portal-2026`):

| Username | Role | Language |
|---|---|---|
| `shipper.manager` / `shipper.ops` | Shipper | 中文 |
| `receiver.manager` | Receiver | English |
| `receiver.ops` | Receiver | اردو |
| `oversight` | Oversight (read-only) | English |
| `admin` | System administrator (`/admin/`) | English |

Look at `CSG-2026-0002`: 57 cartons on 5 pallets in container MSKU1234565, with 40 × DCJM in
CTN-001…003 and DCJM-017 moved at export inspection. Its payment documents are overdue and
escalated, and CTN-017 arrived damaged.

## Production

Follow [docs/04-deployment-guide.md](docs/04-deployment-guide.md). In short:

```bash
cd deploy && cp .env.example .env    # fill in domain and secrets
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

## Repository layout

```
src/                 Django project
  core/              IDs, audit chain, translation, notifications
  accounts/          organisations, profiles, login + 2FA
  logistics/         consignments, packing, documents, remarks, scanner, dashboard
  templates/ static/ locale/   UI, CSS/JS (no CDNs), EN / 中文 / اردو catalogues
deploy/              docker-compose, Caddy (HTTPS), backup job, .env template
docs/                evaluation, design, delivery plan, deployment guide
```

## Development

```bash
cd src && python manage.py test      # 64 tests
ruff check src deploy
python ../deploy/compile_po.py locale   # after editing translations in locale/*/django.po
```

CI (`.github/workflows/ci.yml`) runs lint, migration checks, the tests and a Docker build on every push.
