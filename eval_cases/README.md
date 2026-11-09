# Labelled invoices for accuracy evaluation

Put your own real invoices here (the folder is ignored by git except this README and the sample):
for each `name.pdf` (or png, jpg, gif, webp) add `name.expected.json` with the correct values.
Only fields you list are scored. Use `null` where the document truly does not say (for example an unclear date).
Never commit customer invoices to a public repository.

```bash
invoiceguard eval --dir eval_cases --out accuracy.md --yes --fail-on-false-clean
```

The number to watch is **"Passed, something wrong"**: invoices that passed every check yet contain a wrong value.
