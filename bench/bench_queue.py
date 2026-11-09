"""Measures enqueue latency/throughput, list_active, and dispatch on a file-backed SQLite DB."""
import json
import pathlib
import statistics
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from invoiceguard import review_queue as rq  # noqa: E402
from invoiceguard.validator import validate_invoice  # noqa: E402

N = 5000


def invoice(i):
    return {"line_items": [{"quantity": 5, "unit_price": 150.0, "total_amount": i + 0.5}],
            "financial_summary": {"subtotal": i + 0.5, "tax_amount": 75.0, "grand_total": 825.0},
            "ocr_warnings": []}


with tempfile.TemporaryDirectory() as d:
    conn = rq.connect(str(pathlib.Path(d) / "bench.db"))
    SYNC = sys.argv[1] if len(sys.argv) > 1 else "NORMAL"
    conn.execute(f"PRAGMA synchronous={SYNC}")
    lat = []
    t0 = time.perf_counter()
    for i in range(N):
        inv = invoice(i)
        s = time.perf_counter()
        v = validate_invoice(inv)
        rq.enqueue(conn, f"doc-{i}", inv, v, document_url=f"https://files.example/doc-{i}.pdf")
        lat.append((time.perf_counter() - s) * 1000)
    total = time.perf_counter() - t0

    s = time.perf_counter()
    rows = rq.list_active(conn, 50)
    list_ms = (time.perf_counter() - s) * 1000

    s = time.perf_counter()
    res = rq.dispatch_pending(conn, lambda p: None, limit=N)
    dispatch_s = time.perf_counter() - s

    lat.sort()
    print(json.dumps({
        "synchronous": SYNC,
        "invoices": N,
        "validate_plus_enqueue_per_sec": round(N / total),
        "latency_ms_p50": round(statistics.median(lat), 3),
        "latency_ms_p95": round(lat[int(N * 0.95)], 3),
        "latency_ms_p99": round(lat[int(N * 0.99)], 3),
        "list_active_50_ms": round(list_ms, 2),
        "dispatch_5000_noop_sends_s": round(dispatch_s, 2),
        "dispatched": res["sent"],
        "rows_returned_by_list_active": len(rows),
    }, indent=2))
