.PHONY: proxy-check security install test ui-test run bench smoke rehearsal openapi constraints
install:
	pip install -e ".[dev]"
test:
	python scripts/export_openapi.py --check
	python -m pytest -q
run:
	invoiceguard serve
bench:
	python bench/bench_queue.py FULL
smoke:
	python scripts/live_smoke_test.py
rehearsal:
	python scripts/release_smoke.py
ui-test:
	cd tests/ui && npm ci
	node --test tests/ui/dashboard.test.js
openapi:
	python scripts/export_openapi.py
constraints:
	rm -rf /tmp/ig-constraints && python -m venv /tmp/ig-constraints
	/tmp/ig-constraints/bin/pip install -q ".[dev]"
	{ echo "# Exact versions the test suite passed against (Python 3.12). Used by the Docker build and CI so builds are reproducible."; \
	  echo "# Update with: make constraints; run make test ui-test; commit. Run pip-audit -r constraints.txt before releasing."; \
	  /tmp/ig-constraints/bin/pip freeze --exclude-editable | grep -vi "^invoiceguard" | sort; } > constraints.txt
security:
	python -m pip install -q pip-audit
	python scripts/security_review.py --audit
proxy-check:
	python scripts/proxy_check.py
