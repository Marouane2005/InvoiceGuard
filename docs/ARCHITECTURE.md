# Architecture

## The path of one invoice
`POST /v1/invoices` -> `api.py` (auth, limits) -> `pipeline.process_document`:
file check by content -> hash = document id -> already seen? return stored result -> quota -> `extractor` (Claude reads it; at most N at once, overall deadline) -> `policy.normalize_invoice` / `apply_policy` (clean-up, currency, dates) -> `validator.validate_invoice` (math) -> duplicate check -> store -> `review_queue.enqueue` if anything was found -> usage metering.

## Modules
| Module | Responsibility | Depends on |
|---|---|---|
| `api.py` | HTTP, auth, limits, logging | pipeline, tenants, store, review_queue |
| `pipeline.py` | The whole job for one upload; review actions; erase | extractor, policy, validator, store, tenants |
| `extractor.py` | Provider call, retries, deadline, the reading prompt | schema |
| `policy.py` | Clean model output; business rules | nothing |
| `validator.py` | Invoice arithmetic. Pure function | nothing |
| `review_queue.py` | Review items, states, Slack/email formatting | nothing (stdlib) |
| `store.py` | Processed invoices, duplicates, CSV, purge | review_queue, migrate |
| `tenants.py` | Customers, key hashes, settings, usage | migrate |
| `backup.py`, `doctor.py`, `evaluation.py`, `onboarding.py`, `economics.py` | Operations | the above |
| `cli.py` | Commands | everything |

## Decisions
| Decision | Why | Revisit when |
|---|---|---|
| One SQLite file per customer | Isolation by construction: no query can cross customers. Trivial backup, trivial erase | More than one server is needed, or ~hundreds of active customers |
| AI reads, code checks | The model is good at reading, unreliable at arithmetic; checks must be deterministic and auditable | Never for the arithmetic |
| `null` + warning instead of a best guess | A wrong confident value passes silently; a `null` always reaches a person | Evaluation shows a safe rule for a specific field |
| Document id = SHA-256 of the file | Re-uploads cost nothing; duplicates are detectable; no file names on disk | Customers need versioned re-reads of the same file |
| Nothing sent or deleted automatically | Trust and legal safety. Slack only on request; purge, erase and eval need an explicit flag | Customers ask for automation (make it opt-in per customer) |
| Keys stored as hashes, one key per customer | Simple and safe to leak the database | Customers need individual users, roles, SSO |
| Versioned schema (`PRAGMA user_version`) | Safe upgrades; a newer database is refused | Always |

## Data layout
```
data/
  control.db                 customers, key hashes, settings, usage
  tenants/<id>/tenant.db     invoices, review queue
  tenants/<id>/files/<sha256>.<ext>
```

## Failure behavior
- Provider down or slow: upload fails with `extraction_failed`; nothing stored, nothing charged.
- Too many simultaneous reads: `server_busy`; nothing stored.
- Crash mid-upload: the file may exist without a database row; harmless, it is re-processed on the next upload.
- Slack down: items stay `open` and un-notified; the next dispatch retries.
