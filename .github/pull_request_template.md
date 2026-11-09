## What changes and why

## Checklist
- [ ] `make test` and `make ui-test` pass
- [ ] If the API changed on purpose: `python scripts/export_openapi.py` was run and the change is in `CHANGELOG.md`
- [ ] If the error list changed: the docs that quote those errors were updated
- [ ] If the database schema changed: a new entry was added to `MIGRATIONS` (old entries untouched) with an upgrade test
- [ ] No customer data, keys, or `.env` files in this change
- [ ] Docs updated (README, runbook, or the relevant guide)
