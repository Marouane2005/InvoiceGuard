# InvoiceGuard

InvoiceGuard reads your invoices, checks the numbers, and puts anything doubtful in a short list for a person to confirm. Nothing is paid or sent without you.

It is built for startups and small to mid-size businesses that receive PDF or photographed invoices and want fewer typing mistakes in their books.

## How it works

1. **Read.** An invoice (PDF, PNG, JPEG, GIF, WebP) is read into vendor, dates, currency, line items and totals. With `INVOICEGUARD_READER=claude` (the default) that call goes to Claude, including scans. With `INVOICEGUARD_READER=local` a rule-based reader uses text already inside the PDF, and images only when the `tesseract` program is installed. Anything unclear is recorded as `null` with a warning. A date like `12/05/23` stays `null`. A missing amount stays `null` and is not stored as `0`. Numbers are copied, not adjusted so the totals add up.
2. **Check.** Code, not AI, checks every number: quantity x price per line, line sum vs subtotal, subtotal + tax vs total. It also enforces your rules (currency, vendor and number present, due date after issue date) and spots the same invoice arriving twice.
3. **Route.** Clean invoices are ready for export. Doubtful ones go to a review queue with the exact reasons. A reviewer sees the original next to the fields, fixes them, and resolves.
4. **Export.** Checked and reviewed invoices download as CSV for your accounting tool.

Before each release run `make rehearsal`: it walks the whole customer journey against the real server with a stand-in AI provider (no key, no cost).

Re-uploading the same file returns the earlier result: no second AI call, no second charge. See `docs/ARCHITECTURE.md`.

## Try it locally

No API key and no charge. The local reader is for digital PDFs with a text layer, and for images when `tesseract` is installed. It is not a measured accuracy figure.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export INVOICEGUARD_READER=local
export INVOICEGUARD_DATA_DIR=./data
export INVOICEGUARD_PUBLIC_URL=http://127.0.0.1:8741
invoiceguard doctor
invoiceguard create-tenant "Demo Co" --currency USD   # the key is printed once
invoiceguard serve --host 127.0.0.1 --port 8741       # open http://127.0.0.1:8741/ and paste the key
```

`doctor` may warn that the local reader is not a measured model and that the address is only for this machine. Those warnings are expected here. To use Claude instead, leave `INVOICEGUARD_READER` unset, put `ANTHROPIC_API_KEY` in `.env`, and use an `https://` public URL before you expose the service.

Invented PDFs for a walk-through (not customer documents):

```bash
python scripts/make_demo_invoices.py /tmp/invoiceguard-samples
```

## Put it on your domain

Follow `docs/RUNBOOK.md`. In short: point your domain at a server, fill in `.env` (including `DOMAIN`), then

```bash
docker compose -f docker-compose.prod.yml run --rm invoiceguard invoiceguard doctor --online
docker compose -f docker-compose.prod.yml up -d --build
```

Caddy gets and renews the HTTPS certificate automatically. Only ports 80 and 443 are exposed; the app itself is not reachable directly.

## Commands

| Command | What it does |
|---|---|
| `invoiceguard serve` | Start the service |
| `invoiceguard doctor [--online] [--backup-dir DIR]` | Check this installation before going live |
| `invoiceguard onboard NAME [--currency] [--monthly-limit]` | Create a customer, print the key once, draft the welcome message (nothing is sent) |
| `invoiceguard create-tenant NAME` | Create a customer without the message |
| `invoiceguard process FILE --tenant ID` | Read one invoice file |
| `invoiceguard process-folder DIR --tenant ID --yes [--concurrency 3] [--report out.csv]` | Read a whole folder (uses paid quota; safe to run again; stops if the quota runs out) |
| `invoiceguard template-check FILE` | Preview a custom CSV export layout with invented sample data |
| `invoiceguard set-export-template --tenant ID --file FILE` | Give a customer a CSV layout that matches their accounting software (`--clear` to undo) |
| `invoiceguard billing --price-per-document P [--included N] [--base-fee F] [--currency C] [--csv]` | Draft monthly statements per customer from usage. Drafts only: nothing is sent or charged |
| `invoiceguard show --tenant ID --document PREFIX` | Print one stored invoice and its review state (customer data) |
| `invoiceguard export-case --tenant ID --document PREFIX --permission-confirmed` | Turn a reviewer-confirmed invoice into an accuracy test case |
| `invoiceguard trace LOGFILE --request-id ID` | Find a request in the server log by the id a customer quotes |
| `invoiceguard validate FILE.json` | Check the math of extracted data |
| `invoiceguard usage [--month] [--csv]` | Documents and tokens per customer, for invoicing |
| `invoiceguard economics --input-price-per-mtok X --output-price-per-mtok Y` | Provider cost per invoice and the price that reaches your margin |
| `invoiceguard backup --dest DIR [--keep N]` | One backup file with every database and upload |
| `invoiceguard restore FILE --into EMPTY_DIR` | Restore (never overwrites) |
| `invoiceguard eval --dir DIR --yes` | Measure accuracy on your own labelled invoices (uses paid quota) |
| `invoiceguard purge --tenant ID --older-than-days N [--confirm]` | Delete old documents (dry run unless confirmed) |
| `invoiceguard erase-tenant --tenant ID [--confirm]` | Erase one customer's invoices and switch the account off (dry run unless confirmed) |

## API

| Method and path | What it does |
|---|---|
| `POST /v1/invoices` | Upload and read one invoice (`201` new, `200` already processed) |
| `GET /v1/invoices?q=&status=&month=&limit=&offset=`, `/v1/invoices/{id}`, `/v1/invoices/{id}/file` | Search and page through processed invoices, fetch one, download the original |
| `GET /v1/invoices/changes?cursor=` | Change feed for syncing into another system (see below) |
| `GET /v1/review-queue`, `GET /v1/review-queue/{id}` | Items waiting, with reasons and the extracted invoice |
| `POST /v1/review-queue/{id}/claim`, `/release` | Take or hand back an item |
| `POST /v1/review-queue/{id}/resolve` | Close with a note; optional corrected invoice (must pass every check) |
| `POST /v1/review-queue/{id}/dismiss` | Reject with a note |
| `POST /v1/notifications/dispatch` | Send Slack messages for new items. Only runs when called |
| `GET /v1/export.csv` | Checked and reviewed invoices (`?month=2026-10`, `?status=all`). Uses the customer's template if set; `?template=default` forces the standard layout |
| `GET /v1/usage`, `GET`/`PATCH /v1/settings`, `POST /v1/settings/rotate-key` | Usage this month; default currency, Slack webhook, custom CSV export template; replace your own key |
| `POST /admin/tenants`, `.../rotate-key`, `.../deactivate`, `GET /admin/usage`, `GET /admin/metrics` | Run customer accounts; Prometheus metrics (needs `X-Admin-Token`; closed to the internet by default in the production setup) |
| `GET /readyz`, `GET /healthz` | Readiness and liveness |

Errors always look like `{"error": {"code", "message", "hint"}}` where the hint says what to do next. Interactive documentation is at `/docs`.

## Syncing with your accounting or ERP system

Poll the change feed and upsert by `document_id`:

```bash
curl -s -H "Authorization: Bearer $KEY" "$URL/v1/invoices/changes?limit=100"                    # first page
curl -s -H "Authorization: Bearer $KEY" "$URL/v1/invoices/changes?limit=100&cursor=$NEXT"       # then pass next_cursor back
```

Only checked invoices (`clean` or `reviewed`) appear unless you add `include=all`. A record that changes later (for example after review) appears again, so always upsert. The cursor is opaque and safe to paste into a URL; an empty page returns the same cursor, so poll again later with it. Each invoice carries the extracted fields, status and timestamps. For CSV imports, set a layout that matches your accounting tool (`set-export-template`).

## Configuration

See `.env.example`. Every setting the code reads is listed there (a test checks this).

## Before you charge anyone: measure accuracy

The one number that matters is how often a **wrong** invoice passes every check. Label 30 to 50 real invoices (`eval_cases/README.md`), then:

```bash
invoiceguard eval --dir eval_cases --out accuracy.md --yes --fail-on-false-clean
```

Re-run it after every prompt or model change. No accuracy figure is claimed in this repository, because none has been measured on real invoices.

## What is verified, and what is not

Checked on Python 3.12.3. Python 3.11 is the minimum in `pyproject.toml` and was not installed for this run.

- `python -m pytest`: 397 passed, 0 failed, on the tree that still included the agent skill copies. Those copies are no longer in this tree. Product tests, including `tests/test_validator.py`, remain.
- `node --test tests/ui/dashboard.test.js`: 17 passed, 0 failed. That run uses jsdom. It does not check browser layout. The separate Node validator script lived only in the skill folder and is no longer in this tree.
- `pip-audit -r constraints.txt` on 8 November 2026: no known vulnerabilities in the pinned set.

A local stack with `INVOICEGUARD_READER=local` was rebuilt from this tree and exercised with invented invoices. A missing tax line stayed null and exported as an empty CSV cell. `12/05/23` stayed null and went to review. A printed total that did not add up stayed as printed until a person saved a correction, and that correction cleared the old issue text. A backup of the live data restored the same tenant, invoice and file counts, and refused to write into a folder that already had a file. Access logs from that run did not contain invoice text or API keys.

`docker compose -f docker-compose.prod.yml` was started with `DOMAIN=invoiceguard.test`. The app became healthy, Caddy waited for that healthcheck, and port 80 answered with a redirect to https. No certificate was issued: the request to Let's Encrypt timed out, and HTTPS on that name failed the TLS handshake. `scripts/proxy_check.py` then ran the shipped Caddyfile on local ports. It passed: admin is closed from other addresses, a 13 MB upload is refused, HSTS is set, and a spoofed client address cannot dodge the lockout.

The Claude reader was not called. No accuracy figure on real invoices was measured. `make rehearsal` was not re-run.

**Still required before you expose this to a customer:**
1. **Claude, if you use it.** Set `ANTHROPIC_API_KEY`. Then label 30 to 50 real invoices and run `invoiceguard eval`. The local reader is rules for text already in the file, plus `tesseract` for images.
2. **A lawyer.** `legal/` holds templates. Each one says it is not legal advice. There is no signed DPA here.
3. **A second copy of the archives.** The daily loop keeps 14 archives on the same disk. An empty data directory is retried in about a minute and is not called a failed backup. A real failure prints `BACKUP FAILED` and is retried in about five minutes. That line is a log, not an alert. `INVOICEGUARD_OFFSITE_BACKUP_DIR` copies an archive to a path you choose; leave it empty and nothing is copied. It is another machine only when you mount it that way.
4. **A checker you run against `GET /readyz`.** Set `INVOICEGUARD_MONITOR_URL` if you want `invoiceguard doctor` to request that checker. Nothing here pages you.
5. **A DNS name you control, with ports 80 and 443 reachable,** and `DOMAIN` set, before Caddy can obtain a certificate.
6. **Billing and Slack.** `invoiceguard billing` writes drafts and charges nothing. Slack is sent only when you call dispatch, and it was not connected.

## Known limits

- SQLite on one machine; one process. No replicas.
- One API key per customer: no individual users, roles, or single sign-on.
- Currency codes are checked for format, not against the official list.
- Provider file limits apply; very large PDFs fail with a clear error.
- No online payments: bill from `invoiceguard billing` drafts at first.
- The local reader does not OCR a scanned PDF that has no text layer. It leaves `12/05/23` as `null`, leaves a missing tax line as `null` (not `0`), and does not rewrite a total that does not add up. The Docker image includes `tesseract-ocr` for images. A customer's default currency is filled in when the document has no currency code, and that invoice still goes to review when the reader raised a warning.
- No measured accuracy, no off-site backups, no monitoring product, and no legal sign-off ship with this code.
- Export layouts are built from a sample import file the customer's accountant provides. No accounting product's format is built in, because none could be verified.

## Putting it on GitHub and releasing

`docs/GITHUB_SETUP.md` has the steps that need your GitHub login: create a **private** repository, push, check that the `ci` jobs are green (this is the first time the Docker image is built), switch on secret scanning and push protection, protect `main`. A release is a tag you push (`git tag v1.4.0 && git push origin v1.4.0`): the `release` workflow then verifies everything and publishes a private image and a GitHub Release. Nothing is published without that tag.

## Before you sell this

- **Legal:** `legal/` has templates for terms, privacy policy and a data processing agreement. They are templates, not legal advice. A lawyer must review them. No license file is included on purpose: that choice is yours.
- **Pricing:** use `economics` with your measured usage and the provider's current prices.
- **Support and operations:** `docs/RUNBOOK.md`.
- **Order of work:** `docs/LAUNCH_PLAN.md`.
