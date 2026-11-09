# Security notes

What the service does to protect customer data, and what it does not do yet.

## In place
- **API keys** are random (256 bits), shown once, and stored only as SHA-256 hashes. Keys can be rotated and accounts deactivated.
- **Customer isolation.** Each customer has a separate database file and file folder. There is no shared table that holds invoice data, so a missing filter cannot leak another customer's data. This is tested (`tests/test_api.py`).
- **Uploads** are identified by content, not file name, limited in size, and stored under a hash name, so file names cannot reach the file system.
- **No content in logs.** The service does not log invoice contents or API keys. Error messages from the AI provider are shortened and never include the key.
- **Slack webhooks** are stored server-side, must be `https://hooks.slack.com/...`, and are masked in every response and error.
- **Abuse limits.** Wrong API keys or admin tokens lock the sending address out for a minute after a set number of failures (default 20 per minute). The number of invoices being read by the AI at once is capped, and provider retries stop after an overall deadline, so one client cannot run up your provider bill or tie up the server.
- **Logs.** One JSON line per request: time, method, path, status, duration, request id, customer id. No query strings, headers, keys, or invoice contents. Unexpected errors log only the error type and request id.
- **Backups** are single files with owner-only permissions, restore never overwrites, and archives with unsafe paths or links are refused. Databases are integrity-checked on backup and restore.
- **Erasure.** `erase-tenant` removes a customer's invoices, files and review items and switches the account off (dry run by default). Usage counts without invoice content are kept for billing.
- **Safe upgrades.** Database schemas are versioned; a database written by a newer version is refused instead of being altered.
- **Proxy behaviour was tested with the real Caddy**, not assumed: the admin rule returns 404 to other addresses even with the right token, uploads over the limit are refused, and a spoofed `X-Forwarded-For` is overwritten so it cannot be used to dodge the lockout (`scripts/proxy_check.py`).
- **Admin surface closed by default.** In the production setup `/admin/*` (customer accounts, metrics) answers 404 to the internet unless you list your own address in `ADMIN_ALLOW`; the admin token still applies on top.
- **Dependencies pinned and scanned.** Builds use the exact versions the tests passed against (`constraints.txt`); CI runs `pip-audit` on every push, and a scan on 2026-10-27 found no known vulnerabilities in the 23 pinned packages. That says nothing about problems not yet in the public database.
- **Dashboard output is escaped by construction:** invoice text is only ever inserted as text. A test uploads an invoice whose vendor name is an HTML image tag with a script handler and checks it is shown as plain text and never runs; the page also runs under a policy that forbids inline scripts.
- **Automated security review.** `make security` checks that no keys, databases or `.env` files are tracked, the container runs as a non-root user, only the proxy publishes ports, the admin area is closed at the proxy, dependencies are pinned and have no known vulnerabilities, defaults are safe, security headers are present, and errors reveal nothing. It cannot find design flaws: a person must still read each change.
- **Export templates are plain data.** Only a fixed list of invoice fields, short text and standard separators are accepted; there is no code, formula or file access in a template, and text cells that start with `=`, `+`, `-` or `@` are neutralized.
- **Support tools handle customer data deliberately.** `show` prints it with a warning, and `export-case` refuses to run without `--permission-confirmed` and refuses invoices a reviewer has not confirmed.
- **Nothing is sent or deleted automatically.** Slack messages go out only when a person calls dispatch. Deleting old documents requires `--confirm`; the default is a dry run.
- **Dashboard** is served with a strict content-security policy (no inline scripts), and renders data as text only.
- **CSV export** neutralizes spreadsheet formulas (`=`, `+`, `-`, `@` at the start of text).
- **Prompt injection.** The reading step is told to treat text inside invoices as data, and its output is checked by code. A hostile invoice can at worst produce wrong numbers that the checks or a reviewer catch. It cannot trigger an action, because the reader has no tools beyond recording the invoice.

## Not in place (decide before selling to larger customers)
- No single sign-on, per-user accounts, or role permissions. Each customer has one API key.
- No encryption of stored files, databases, or backup files beyond what the disk or hosting provider offers. Use an encrypted volume and encrypt backups before copying them off the server.
- Rate limits and lockouts are in memory and per process. Run one process. Behind a proxy, set `INVOICEGUARD_TRUST_PROXY=1` or all customers share one address.
- A lockout applies to an address, so several staff behind one office address share it.
- SQLite is a single-machine database. Running several replicas against one data folder is not supported.
- No audit log of who viewed what. Review actions are recorded with the reviewer's name and note.
- No independent security review or penetration test has been done. Invoice images and PDFs are sent to the AI provider; customers must be told this in your terms and privacy policy.

Report a vulnerability by contacting the maintainer privately rather than opening a public issue.
