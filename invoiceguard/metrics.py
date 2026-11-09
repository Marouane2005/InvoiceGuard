"""Minimal Prometheus metrics (text format). No dependency, no customer ids, no paths: labels stay low-cardinality and private."""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Callable, Optional

HELP = {
    "invoiceguard_http_requests_total": ("counter", "HTTP requests by method and status class."),
    "invoiceguard_errors_total": ("counter", "Customer-facing errors by error code."),
    "invoiceguard_invoices_total": ("counter", "Uploads by outcome: clean, needs_review, or cached (already processed)."),
    "invoiceguard_upload_seconds": ("summary", "Time to process an upload, including the AI reading."),
    "invoiceguard_reads_in_flight": ("gauge", "Invoices being read by the AI provider right now."),
    "invoiceguard_info": ("gauge", "Build information."),
}


def _labels(labels: Optional[dict]) -> str:
    if not labels:
        return ""
    esc = lambda v: str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in sorted(labels.items())) + "}"


class Metrics:
    def __init__(self, version: str = "", gauges: Optional[dict] = None):
        self._lock = threading.Lock()
        self._counters: dict = defaultdict(float)
        self._sum: dict = defaultdict(float)
        self._count: dict = defaultdict(int)
        self._gauges: dict = gauges or {}
        self._version = version

    def inc(self, name: str, labels: Optional[dict] = None, value: float = 1.0) -> None:
        with self._lock:
            self._counters[(name, _labels(labels))] += value

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            self._sum[name] += seconds
            self._count[name] += 1

    def render(self) -> str:
        with self._lock:
            counters, sums, counts = dict(self._counters), dict(self._sum), dict(self._count)
        out = []
        for name, (kind, text) in HELP.items():
            lines = []
            if kind == "counter":
                lines = [f"{n}{lab} {v:g}" for (n, lab), v in sorted(counters.items()) if n == name]
            elif kind == "summary" and name in counts:
                lines = [f"{name}_sum {sums[name]:.6f}", f"{name}_count {counts[name]}"]
            elif kind == "gauge" and name in self._gauges:
                lines = [f"{name} {self._gauges[name]()}"]
            elif name == "invoiceguard_info":
                lines = [f'{name}{{version="{self._version}"}} 1']
            if lines or kind == "counter":
                out += [f"# HELP {name} {text}", f"# TYPE {name} {kind}"] + lines
        return "\n".join(out) + "\n"
