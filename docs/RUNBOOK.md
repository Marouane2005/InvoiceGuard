# Runbook

For whoever runs the service. Commands assume `docker compose -f docker-compose.prod.yml` (shortened to `dc` below).
Commands marked (untested) have not been tried on a real server yet; try them once on a throwaway server first. The Caddy configuration itself was tested with the real Caddy (`make proxy-check`); only certificate issuance for your domain needs your server.

## 1. First deploy
1. A small Linux server with Docker installed. Open ports 80 and 443 only. Close everything else (SSH from your address only).
2. DNS: point an A (and AAAA) record for your domain at the server.
3. `git clone` the repository and `cd` into it, then `cp .env.example .env` and fill in: `ANTHROPIC_API_KEY`, `INVOICEGUARD_ADMIN_TOKEN` (24+ random characters), `INVOICEGUARD_PUBLIC_URL=https://your.domain`, `DOMAIN=your.domain`. Leave `ADMIN_ALLOW` empty to manage customers from the server's command line only, or set it to your own address to use `/admin` from your laptop.
4. Create the backup folder the container can write to: `mkdir backups && sudo chown 10001 backups` (the app runs as user 10001, not root; without this the first backup fails with a permission error).
5. Preflight: `dc run --rm invoiceguard invoiceguard doctor --online`. Fix every FAIL. Read every WARN.
6. `dc up -d --build`. Open `https://your.domain/readyz`: it must say `{"status":"ready"}`.
7. Prove the reader works with your key: `ANTHROPIC_API_KEY=... python scripts/live_smoke_test.py`.
8. Schedule backups (section 3) and an uptime monitor on `/readyz`.

## 2. Customer lifecycle
- **Onboard:** `dc exec invoiceguard invoiceguard onboard "Acme Ltd" --currency USD --monthly-limit 500`. It prints the key once and a draft welcome message. Send the message and the key in two separate messages. Nothing is sent for you.
- **Lost or leaked key:** `POST /admin/tenants/{id}/rotate-key` (old key stops working immediately).
- **Offboard:** let the customer download their CSV, then `invoiceguard erase-tenant --tenant <id>` (dry run), then again with `--confirm`. Usage counts are kept; invoice content is deleted.
- **Usage report:** `invoiceguard usage --month 2026-10 --csv`.

## 3. Backups (do this before you have a customer)
- Daily at 02:15 (untested): `15 2 * * * cd /opt/invoiceguard && docker compose -f docker-compose.prod.yml exec -T invoiceguard invoiceguard backup --dest /backups --keep 14`
- The `./backups` folder holds customer data in plain form (file permission 600). Copy it off the server every day, **encrypted** (for example with `age` or `gpg`) to storage you control.
- **Restore drill, once a quarter:** `invoiceguard restore <file> --into /tmp/restore-test`, then `INVOICEGUARD_DATA_DIR=/tmp/restore-test invoiceguard doctor`. A backup you have never restored is not a backup.
- Real restore: stop the service, restore into a NEW empty folder, point the volume at it, start, check `/readyz`.

## 4. Upgrade
1. `invoiceguard backup` first. Schema changes only move forward; the backup is your way back.
2. `git pull`, then `dc run --rm invoiceguard invoiceguard doctor`, then `dc up -d --build`.
3. Check `/readyz`, upload one test invoice, look at the logs.
4. Rollback: stop, check out the previous version, restore the backup into a new folder if the schema changed (a database from a newer version is refused, never silently changed), start.

## 5. When something breaks
| Symptom | What it means | What to do |
|---|---|---|
| Uploads return `extraction_failed` | Provider outage or limits | Nothing is lost. Retry later. Check the provider status page |
| `extraction_failed`: "rejected the server's API key" | Your provider key is wrong or out of credit | Fix `ANTHROPIC_API_KEY`, restart |
| `server_busy` often | More simultaneous uploads than `INVOICEGUARD_MAX_CONCURRENT_READS` | Raise it carefully (check provider limits and cost) |
| Customers get `rate_limited` right after login problems | Failed-key lockout (1 minute) | Wait; check `INVOICEGUARD_TRUST_PROXY=1` is set behind Caddy |
| `/readyz` is 503 | Data folder not writable or a database cannot open | `dc logs invoiceguard`, run `doctor`, check disk space |
| Disk nearly full | Uploads accumulate | Offer customers `purge --older-than-days N` (dry run first), add disk |
| Suspected key leak | | Rotate that customer's key; if the admin token leaked, change it and restart |

## 6. What to watch
`/readyz` up, disk space, backup age (`doctor --backup-dir /backups`), the logs for `"status": 5xx`, and provider spend against `usage`. Logs are one JSON line per request with no invoice content, keys, or query strings.

## 7. Export layout per customer
1. Ask the customer's accountant for the import template or a sample file for their accounting software. Do not rely on memory of any product's format.
2. Write a template from that sample file, then preview it: `invoiceguard template-check template.json`.
3. Apply it: `invoiceguard set-export-template --tenant <id> --file template.json` (`--clear` to undo).
4. Ask the customer to import one exported file and confirm it worked.

## 8. Support
1. Ask the customer for the `X-Request-ID` header of the failed call.
2. `docker compose -f docker-compose.prod.yml logs invoiceguard > server.log`, then `invoiceguard trace server.log --request-id <id>`.
3. Wrong reading: with the customer's permission, `invoiceguard show --tenant <id> --document <first 8+ characters>`. If a wrong invoice passed unflagged, fix it through review, then with their written permission `invoiceguard export-case ... --permission-confirmed` and rerun `invoiceguard eval`.

## 9. Importing a backlog
`invoiceguard process-folder /path/to/their/pdfs --tenant <id>` shows how many documents will be sent and asks for `--yes` (each new file is one provider call). It reads three at a time (`--concurrency`, 1 to 8), stops by itself if the customer's monthly limit is reached, and is safe to run again: files already read are skipped. Add `--report result.csv` for a per-file list. Then have the customer's reviewer work through "Needs review".

## 10. Monitoring
Scrape `/admin/metrics` from the server itself (the production proxy hides `/admin` from the internet unless you set `ADMIN_ALLOW`), for example `docker compose exec invoiceguard python -c "import urllib.request,os;r=urllib.request.Request('http://127.0.0.1:8000/admin/metrics',headers={'X-Admin-Token':os.environ['INVOICEGUARD_ADMIN_TOKEN']});print(urllib.request.urlopen(r).read().decode())"` (untested here).
Worth alerting on:
| Metric | Why |
|---|---|
| `/readyz` not 200 | The service cannot read its data |
| `invoiceguard_errors_total{code="extraction_failed"}` rising | The extraction provider is failing or your key or credit is out |
| `invoiceguard_errors_total{code="server_busy"}` rising | More simultaneous uploads than `INVOICEGUARD_MAX_CONCURRENT_READS` |
| `invoiceguard_errors_total{code="rate_limited"}` rising | A client is misbehaving or someone is guessing keys |
| `invoiceguard_http_requests_total{status_class="5xx"}` rising | A bug: use the request id with `invoiceguard trace` |
| `invoiceguard_invoices_total{result="needs_review"}` share rising | Reading quality changed: run `invoiceguard eval` |

## 11. Updating dependencies
`make constraints` regenerates `constraints.txt` from fresh installs; run `make test ui-test`, then `pip-audit -r constraints.txt`, then commit. CI runs the audit on every push.

## 12. Production checklist

Steps for a server you control. This repository does not start that server, obtain a certificate, or copy backups off the machine.

1. `cp .env.example .env`. Set `DOMAIN` (the name in `deploy/Caddyfile`) and `INVOICEGUARD_ADMIN_TOKEN` (`python -c "import secrets; print(secrets.token_urlsafe(32))"`, at least 16 characters). Set `ANTHROPIC_API_KEY` only if `INVOICEGUARD_READER` stays `claude`. Leave `ADMIN_ALLOW` empty unless `/admin` should answer from a specific address. An empty `INVOICEGUARD_PUBLIC_URL` becomes `https://$DOMAIN`. Compose refuses to start when `DOMAIN` is empty.
2. If the machine is on the internet, allow 22 from your own address, plus 80/tcp, 443/tcp and 443/udp. Do not publish port 8000.
3. `docker compose -f docker-compose.prod.yml up -d --build`. Caddy is declared with `depends_on` condition `service_healthy`, so it waits for the app `/readyz` check. Certificate issuance happens only when DNS for `DOMAIN` points at that machine.
4. The `backup` service runs `invoiceguard backup --dest /backups --keep 14`. A successful backup waits `BACKUP_INTERVAL_SECONDS` (default 86400). An empty data directory exits 2 and is tried again after `BACKUP_EMPTY_RETRY_SECONDS` (default 60). A real failure prints `BACKUP FAILED` and is tried again after `BACKUP_FAILURE_RETRY_SECONDS` (default 300). That line is a log, not an alert. Archives stay in `./backups` on this disk. Set `INVOICEGUARD_OFFSITE_BACKUP_DIR` only when that path is storage on another machine; otherwise leave it empty. Restore with `invoiceguard restore <file> --into <empty-folder>`.
5. `invoiceguard doctor` uses `INVOICEGUARD_BACKUP_DIR` (the prod compose sets `/backups`). Set `INVOICEGUARD_MONITOR_URL` to a checker you run if you want doctor to request it. Nothing in this repo pages you. Poll `GET /readyz` from a checker you operate.
6. Dev compose binds `127.0.0.1` and uses `INVOICEGUARD_HOST_PORT` (default 8000).
