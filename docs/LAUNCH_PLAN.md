# Launch plan

Order matters. Each step removes a risk before the next one costs you money or reputation.

1. **Prove the reader.** `python scripts/live_smoke_test.py` with your key. One document of cost.
2. **Measure accuracy on invoices like your customers'.** Collect 30 to 50 real invoices you have permission to use, label them (`eval_cases/README.md`), run `invoiceguard eval --dir eval_cases --out accuracy.md --yes`. Read the "Passed, something wrong" number first. Decide your own bar before you look (a sensible start for money fields is zero). If it is not met, tighten the checks or the prompt, rerun, and repeat. Re-run after every prompt or model change with `--fail-on-false-clean`.
3. **Price it.** `invoiceguard economics` with the provider's current prices and your measured token usage. Add hosting, support time, payment fees, and your tax. Choose a price you are comfortable with at the cost you measured.
4. **Deploy** following `docs/RUNBOOK.md` on your domain. Run the restore drill before the first customer.
5. **Legal.** Fill in and have a lawyer review `legal/`. If you operate from Morocco, ask about the data-protection law (09-08) and any registration with the CNDP, and about rules for customers in other countries.
6. **Words before customers.** Draft the landing page and outreach from measured results only. Use numbers that appear in the accuracy report. Do not promise perfect accuracy, a certification you do not hold, or a customer count you have not measured.
6b. **Design partners.** Start with 3 to 5 friendly companies at a reduced price, in exchange for feedback and a reference. Onboard each with `invoiceguard onboard` (and load their backlog with `process-folder`), and ask each one's accountant for their import template so you can set a matching export layout (`invoiceguard template-check`, then `set-export-template`). Bill manually from `usage --csv` at first.
7. **Watch the first month.** Record how many invoices reviewers correct, which fields, and how long reviews take. Add the failures to `eval_cases` so they never return.
8. **Only then build** what customers actually ask for (their accounting export format, individual users, automatic billing).

## Not built, on purpose
Online payment, self-service sign-up, single sign-on, per-user accounts, a multi-server database. None of them can be verified without your accounts and customers; each is a good next step once a real customer asks.
