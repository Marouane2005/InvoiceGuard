# Changelog

## 1.4.2 (2026-11-08)
Checked by running the suite on Python 3.12.3. This is not an accuracy claim.
- An unused internal tooling folder, its sync script, and an old demo report were removed. The optional Claude reader, `ANTHROPIC_API_KEY`, and the deploy configuration are unchanged.
- Fix: `eval_cases/sample-001.expected.json` now includes `ocr_warnings`, so `invoiceguard validate` accepts the shipped sample.
- Fix: accepting a corrected invoice clears the old issue list. The previous text was left in place and could still say the total was the uncorrected number.
- Fix: `invoiceguard doctor` treats `http://localhost` and `http://127.0.0.1` as a local warning. Any other non-https address still fails the check.
- Fix: `invoiceguard doctor --backup-dir` defaults to `INVOICEGUARD_BACKUP_DIR` when the flag is omitted.
- Fix: the secret scan used by tests skips `.venv`, caches, `data/` and `backups/`, so a local environment cannot fail the check.
- Add: `INVOICEGUARD_READER=local` reads text already stored in a PDF, and images when the `tesseract` program is installed. Missing amounts stay null. `12/05/23` stays null and is sent to review. A total that does not add up is stored as printed and marked `needs_review`. No cloud key is used. This reader has not been measured on real invoices.
- Dev compose starts without a `.env` file, binds `127.0.0.1:${INVOICEGUARD_HOST_PORT:-8000}`, and defaults that stack to the local reader. The image installs `tesseract-ocr` so that local image reading works inside the container. There is still no default admin password.
- Opening a customer database from several folder-processing threads retries when SQLite reports the file is locked, instead of failing the run.
- An empty data directory makes `invoiceguard backup` wait (exit 2) instead of counting as a failed backup. The prod loop retries that in about a minute, and retries a real failure in about five minutes. `INVOICEGUARD_OFFSITE_BACKUP_DIR`, when set, receives an extra copy; empty means the archive stays in the local folder. `INVOICEGUARD_MONITOR_URL`, when set, is requested by `doctor`. Neither sends an alert, and a second folder is another machine only if you mounted it that way.
- Prod compose refuses to start when `DOMAIN` is empty, and Caddy waits until the app healthcheck passes. A `backup` service runs `invoiceguard backup --keep 14` once a day into `./backups` on the same machine. It does not copy those files anywhere else and it does not send an alert. `latest_backup_age_hours` skips a filename whose date cannot exist instead of raising. Restore raises `BackupError` when a member cannot be read, instead of `TypeError`. On Python 3.12 the safety check already rejects the member types for which `extractfile` returns `None`; the guard covers that return value if it happens.

## 1.4.1 (2026-10-27)
Verification release: turning "untested" into "tested" wherever the tools could be run.
- **Real-tool checks added:** `scripts/proxy_check.py` runs the shipped `deploy/Caddyfile` in front of the real server (valid configuration, admin area refused from other addresses even with the right token, 12 MB upload limit, a spoofed `X-Forwarded-For` cannot dodge the lockout). CI now also runs `actionlint` on the workflows, `hadolint` on the Dockerfile, and this proxy check.
- **Python 3.11 verified** (370 tests, dashboard tests and rehearsal) alongside 3.12, with the pinned versions; CI uses the pins on both.
- **Dockerfile fixes from `hadolint`:** numeric `USER 10001`; healthcheck in exec form.
- `make security` installs `pip-audit` itself, and says so plainly if it is missing (found when a clean clone failed with a confusing error).

## 1.4.0 (2026-10-27)
Release pipeline and the last planned skills.
- **GitHub pipeline:** a `release` workflow that runs only when you push a version tag (checks that tag, code version and release notes agree, reruns tests and the dependency audit, then publishes a private container image to GitHub's registry and creates a GitHub Release from this changelog); issue and pull-request templates that warn against pasting customer data; `CODEOWNERS`; read-only default permissions in every workflow; actions pinned to major versions; `docs/GITHUB_SETUP.md` for the steps only the account owner can do (private repo, secret scanning, branch protection, first CI run).
- **Security review tool:** `make security` runs 14 automated checks (no keys or databases tracked, non-root container, only the proxy publishes ports, admin closed at the proxy, pinned dependencies, safe defaults, security headers, errors that reveal nothing) plus `pip-audit`.
- **Skills (14 total):** `sales-materials` with a claims checker that flags absolute promises, certifications we do not hold, invented social proof, and numbers not found in a measured report; `design-partner-feedback` with a transparent ranking (different customers, then blocking, then impact; labelled "anecdotes" under 3 customers); `security-review`.
- **Fixes found by tests:** the release-notes script treated version `1.0` as `1.0.0`; the claims checker must not accept 97% when the evidence says 97.5%.

## 1.3.0 (2026-10-27)
Making it usable day to day, and checkable by a customer's IT team.
- **Invoices with discounts, shipping and rounding lines** are now modelled (`discount_amount`, `shipping_amount`, `rounding_adjustment`), so correct invoices of that kind no longer fail the total check. A misread discount is still caught. Older records behave exactly as before.
- **Dashboard rebuilt:** upload many files at once (progress per file, three at a time, up to 50), an **All invoices** view with search, status filter and paging, and a read-only view of finished invoices beside the original. It is now tested by running the real script in a DOM against the real server (16 flows, including a hostile vendor name), stress-tested 30 times under CPU load.
- **Integration:** search and paging on `GET /v1/invoices`; a **change feed** (`GET /v1/invoices/changes`) with an opaque URL-safe cursor that never skips or repeats an invoice; customers can **rotate their own key**; `process-folder` bulk command (asks before spending, stops when the quota runs out, free to repeat).
- **Operations:** Prometheus metrics at `/admin/metrics` (no customer ids or paths in labels); `/admin/*` is closed to the internet by default in the Caddy setup (`ADMIN_ALLOW`); the API is documented with tags, summaries and security schemes at `/docs`, and the contract is committed in `docs/openapi.json` and checked by a test and CI.
- **Supply chain:** dependencies pinned to the versions the tests passed against (`constraints.txt`, used by Docker and CI); `pip-audit` in CI (scan on 2026-10-27: no known vulnerabilities in the 23 pinned packages); Dependabot for pip, npm, Actions and Docker.
- **Data checks:** dates more than a month in the future (misread years) or before 1990 are flagged.
- **Rehearsal** extended to 37 steps (search, change feed with a pasted cursor, metrics, key rotation).
- **Fixes found by tests:** the feed cursor contained `+`, which a URL turns into a space (now opaque and URL-safe); four timing races in the first version of the dashboard tests.

## 1.2.0 (2026-10-25)
- **Custom CSV export per customer** (column names and order, date format, decimal mark, delimiter, fixed columns such as account codes, Excel byte-order mark) so exports match a customer's accounting import file. Set by the customer (`PATCH /v1/settings`) or the operator (`set-export-template`); `template-check` previews with invented data. First use of the schema migration system: customer accounts upgrade in place (database version 1 to 2), covered by an upgrade test on a real version-1 database.
- **Billing drafts:** `invoiceguard billing` (exact decimal money, half-up rounding; drafts only, nothing is sent or charged).
- **Support tools:** `trace` (find a request by id), `show`, and `export-case`, which turns a reviewer-confirmed invoice into an accuracy test case and requires `--permission-confirmed`.
- **Skills:** added `accounting-export-mapper`, `support-triage`, `billing-run` (11 total).
- **Rehearsal** (`scripts/release_smoke.py`) now covers templates, tracing, case export, billing and lockout isolation: 33 steps against the real server process from a clean install. It found that a lockout blocks every client behind the same address for a minute (by design, and noted in `SECURITY.md`), which is why the proxy setting matters.
- **Fixes found by tests:** exports within the same second now keep arrival order (previously ordered by file hash); money rounds half-up exactly (previously `1234.5` with 0 decimals exported as `1234`).

## 1.1.0 (2026-10-25)
Release-readiness work.
- **Safety:** `backup` and `restore` (consistent online backups; restore never overwrites and refuses unsafe archives); `erase-tenant` (dry run by default); versioned database schema with refusal to open a newer database; `doctor` preflight.
- **Abuse and cost:** failed-key lockout per client address (also for the admin token); cap on simultaneous AI reads; overall deadline on provider retries.
- **Operations:** `/readyz`; one JSON log line per request with no content, keys or query strings; clean 500 responses with a request id; production deployment with automatic HTTPS (`docker-compose.prod.yml`, `deploy/Caddyfile`); runbook, architecture notes, launch plan.
- **Rehearsal:** `scripts/release_smoke.py` runs the whole customer journey against the real server process with a stand-in provider (23 steps, no key needed); CI runs it from a clean install.
- **Selling:** `eval` measures how often a wrong invoice passes every check; `onboard` drafts a welcome message (nothing is sent); `usage --csv` for billing; `economics` for pricing.
- **Legal:** templates for terms, privacy policy and data processing agreement (not legal advice; need a lawyer).
- **Skills:** added `accuracy-evaluation`, `customer-onboarding`, `release-checklist` (8 total).
- **Fixes found by tests:** the validator no longer reports a false "line items sum to 0.00" next to a missing total; Slack and email titles show vendor, number and total; evaluation no longer counts `True` as `1.0`.
- CI now builds the Docker image and checks that it starts and reports ready.

## 1.0.0 (2026-10-25)
- First release: AI reading of PDF and image invoices, deterministic checks, business rules, duplicate detection, review queue, reviewer dashboard, CSV export, per-customer isolation, usage metering, command line, Docker image.
