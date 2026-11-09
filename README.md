# InvoiceGuard

InvoiceGuard is a free, open-source, self-hosted service that reads invoices, checks the numbers, and puts anything doubtful in a short list for a person to confirm. Nothing is paid or sent without you.

It is meant for small teams that receive PDF or photographed invoices and want fewer typing mistakes in their books. It is released under the MIT License (see `LICENSE`).

## How it works

1. **Read.** An invoice (PDF, PNG, JPEG, GIF, WebP) is read into vendor, dates, currency, line items and totals. Two readers are available. `INVOICEGUARD_READER=claude` (the default) uses an optional LLM-based extraction provider (Anthropic's API, needs `ANTHROPIC_API_KEY`) and also handles scans. `INVOICEGUARD_READER=local` is a rule-based reader that needs no key and uses text already inside the PDF, and images only when the `tesseract` program is installed. Anything unclear is recorded as `null` with a warning. A date like `12/05/23` stays `null`. A missing amount stays `null` and is not stored as `0`. Numbers are copied, not adjusted so the totals add up.
2. **Check.** Plain code checks every number: quantity x price per line, line sum vs subtotal, subtotal + tax vs total. It also enforces your rules (currency, vendor and number present, due date after issue date) and spots the same invoice arriving twice.
3. **Route.** Clean invoices are ready for export. Doubtful ones go to a review queue with the exact reasons. A reviewer sees the original next to the fields, fixes them, and resolves.
4. **Export.** Checked and reviewed invoices download as CSV for your accounting tool.

Re-uploading the same file returns the earlier result without reading it again. See `docs/ARCHITECTURE.md`.

## Install and try it locally

Requires Python 3.11 or newer. No API key is needed. The local reader is for digital PDFs with a text layer, and for images when `tesseract` is installed. It is not a measured accuracy figure.

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

`doctor` may warn that the local reader is not a measured model and that the address is only for this machine. Those warnings are expected here. To use the hosted LLM reader instead, leave `INVOICEGUARD_READER` unset, put `ANTHROPIC_API_KEY` in `.env`, and use an `https://` public URL before you expose the service.

Invented PDFs for a walk-through:

```bash
python scripts/make_demo_invoices.py /tmp/invoiceguard-samples
```

## Run it on your own server

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
| `invoiceguard onboard NAME [--currency] [--monthly-limit]` | Create an account, print the key once, draft a welcome note (nothing is sent) |
| `invoiceguard create-tenant NAME` | Create an account without the note |
| `invoiceguard process FILE --tenant ID` | Read one invoice file |
| `invoiceguard process-folder DIR --tenant ID --yes [--concurrency 3] [--report out.csv]` | Read a whole folder (safe to run again; stops if the monthly limit is reached) |
| `invoiceguard template-check FILE` | Preview a custom CSV export layout with invented sample data |
| `invoiceguard set-export-template --tenant ID --file FILE` | Give an account a CSV layout that matches its accounting software (`--clear` to undo) |
| `invoiceguard show --tenant ID --document PREFIX` | Print one stored invoice and its review state |
| `invoiceguard export-case --tenant ID --document PREFIX --permission-confirmed` | Turn a reviewer-confirmed invoice into an accuracy test case |
| `invoiceguard trace LOGFILE --request-id ID` | Find a request in the server log by its request id |
| `invoiceguard validate FILE.json` | Check the math of extracted data |
| `invoiceguard usage [--month] [--csv]` | Documents and tokens per account |
| `invoiceguard backup --dest DIR [--keep N]` | One backup file with every database and upload |
| `invoiceguard restore FILE --into EMPTY_DIR` | Restore (never overwrites) |
| `invoiceguard eval --dir DIR --yes` | Measure accuracy on your own labelled invoices |
| `invoiceguard purge --tenant ID --older-than-days N [--confirm]` | Delete old documents (dry run unless confirmed) |
| `invoiceguard erase-tenant --tenant ID [--confirm]` | Erase one account's invoices and switch it off (dry run unless confirmed) |

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
| `GET /v1/export.csv` | Checked and reviewed invoices (`?month=2026-10`, `?status=all`). Uses the account's template if set; `?template=default` forces the standard layout |
| `GET /v1/usage`, `GET`/`PATCH /v1/settings`, `POST /v1/settings/rotate-key` | Usage this month; default currency, Slack webhook, custom CSV export template; replace your own key |
| `POST /admin/tenants`, `.../rotate-key`, `.../deactivate`, `GET /admin/usage`, `GET /admin/metrics` | Manage accounts; Prometheus metrics (needs `X-Admin-Token`; closed to the internet by default in the production setup) |
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

## Measuring accuracy

The number that matters most is how often a **wrong** invoice passes every check. Label 30 to 50 of your own invoices (`eval_cases/README.md`), then:

```bash
invoiceguard eval --dir eval_cases --out accuracy.md --yes --fail-on-false-clean
```

Re-run it after every prompt or model change. No accuracy figure is claimed in this repository.

## Development

```bash
pip install -e ".[dev]"
make test          # API contract check and the Python test suite
make ui-test       # dashboard tests (Node 22, jsdom)
make rehearsal     # full end-to-end run against the real server with a stand-in provider, no key needed
make security      # automated security checks and pip-audit
```

## Known limits

- SQLite on one machine; one process. No replicas.
- One API key per account: no individual users, roles, or single sign-on.
- Currency codes are checked for format, not against the official list.
- Provider file limits apply; very large PDFs fail with a clear error.
- The local reader does not OCR a scanned PDF that has no text layer. It leaves `12/05/23` as `null`, leaves a missing tax line as `null` (not `0`), and does not rewrite a total that does not add up. The Docker image includes `tesseract-ocr` for images.
- No off-site backups or monitoring are included. See `docs/RUNBOOK.md` for what to set up yourself.
- Export layouts are built from a sample import file from your accounting tool. No accounting product's format is built in.

## Contributing

Issues and pull requests are welcome. Please:

1. Open an issue first for larger changes.
2. Keep pull requests small and focused, with tests for new behaviour.
3. Run `make test` (and `make ui-test` if you touched the dashboard) before opening the pull request.
4. Never attach real invoices or other personal data to issues or pull requests.

Security problems: see `SECURITY.md`.

## License

MIT. See `LICENSE`.
