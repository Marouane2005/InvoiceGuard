import dataclasses
import pathlib
import sys
from contextlib import closing

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pytest
from fastapi.testclient import TestClient
from make_sample_pdf import make_pdf

from invoiceguard import tenants
from invoiceguard.api import create_app
from invoiceguard.config import Settings
from invoiceguard.extractor import ExtractionResult
from invoiceguard.pipeline import control_path

GOOD_RAW = {
    "invoice_metadata": {"invoice_number": "9942", "vendor_name": "Globex Corp", "issue_date": "2023-12-05",
                         "due_date": "2024-01-04", "currency": "USD"},
    "line_items": [{"description": "Services rendered", "quantity": 5, "unit_price": 150.0, "total_amount": 750.0}],
    "financial_summary": {"subtotal": 750.0, "tax_amount": 75.0, "grand_total": 825.0},
    "ocr_warnings": [],
}
BAD_RAW = {  # the 750 misread as 150
    **GOOD_RAW,
    "line_items": [{"description": "Services rendered", "quantity": 5, "unit_price": 150.0, "total_amount": 150.0}],
    "financial_summary": {"subtotal": 150.0, "tax_amount": 75.0, "grand_total": 825.0},
}


class FakeExtractor:
    """Stands in for the AI provider. Returns queued results in order (the last one repeats)."""

    def __init__(self, *results):
        self.results = list(results) or [GOOD_RAW]
        self.calls = []

    def extract(self, data, media_type):
        self.calls.append((len(data), media_type))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return ExtractionResult(invoice=result, input_tokens=1200, output_tokens=300, model="fake-model")


def pdf(n: int = 1) -> bytes:
    return make_pdf([f"Invoice sample {n}"])


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", admin_token="admin-token-123456", max_upload_bytes=1024 * 1024)


@pytest.fixture
def make_tenant(settings):
    def _make(name="Acme", **kw):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with closing(tenants.open_control(control_path(settings))) as conn:
            tenant, key = tenants.create_tenant(conn, name, **kw)
        return tenant, {"Authorization": f"Bearer {key}"}
    return _make


@pytest.fixture
def build_client(settings):
    def _build(extractor=None, **kw):
        s = dataclasses.replace(settings, **kw.pop("settings_overrides", {}))
        return TestClient(create_app(s, extractor=extractor or FakeExtractor(), **kw))
    return _build
