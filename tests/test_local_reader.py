"""The local reader must keep nulls, refuse ambiguous dates, and not repair bad totals."""
import json
import re
import shutil
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from invoiceguard.api import create_app
from invoiceguard.config import Settings
from invoiceguard.local_reader import LocalExtractor, parse_amount, parse_date, parse_invoice_text, pdf_text
from invoiceguard.pipeline import control_path
from invoiceguard.validator import validate_invoice
import make_demo_invoices as demo
from make_sample_pdf import make_pdf
from invoiceguard import tenants


def test_amounts_keep_decimal_commas_and_blank_is_null():
    assert parse_amount("1,234.50") == 1234.5
    assert parse_amount("1.234,50") == 1234.5
    assert parse_amount("21,00 EUR") == 21.0
    assert parse_amount("") is None
    assert parse_amount("-") is None


def test_ambiguous_numeric_date_stays_null_and_a_clear_day_does_not():
    warnings = []
    assert parse_date("12/05/23", "issue_date", warnings) is None
    assert any("12/05/23" in item for item in warnings)
    assert parse_date("31/01/24", "issue_date", []) == "2024-01-31"
    assert parse_date("05/31/23", "issue_date", []) == "2023-05-31"
    assert parse_date("2024-07-01", "issue_date", []) == "2024-07-01"


def test_each_demo_layout_round_trips_through_a_pdf():
    for name, lines in demo.LAYOUTS.items():
        assert pdf_text(make_pdf(lines)).splitlines() == lines, name


def _read(name):
    return parse_invoice_text("\n".join(demo.LAYOUTS[name]))


def test_clean_usd_is_accepted_without_changing_numbers():
    invoice = _read("clean_usd")
    assert invoice["ocr_warnings"] == []
    assert invoice["financial_summary"]["tax_amount"] == 12.0
    assert invoice["financial_summary"]["grand_total"] == 132.0
    assert validate_invoice(invoice)["requires_review"] is False


def test_ambiguous_date_is_null_and_sent_to_review_with_the_printed_total():
    invoice = _read("ambiguous_date")
    assert invoice["invoice_metadata"]["issue_date"] is None
    assert invoice["invoice_metadata"]["currency"] == "GBP"
    assert invoice["financial_summary"]["grand_total"] == 36.0
    result = validate_invoice(invoice)
    assert result["requires_review"] is True
    assert any("12/05/23" in item for item in result["issues"])
    assert invoice["financial_summary"]["grand_total"] == 36.0


def test_a_total_that_does_not_add_up_is_flagged_and_not_rewritten():
    invoice = _read("mismatch")
    assert invoice["financial_summary"]["grand_total"] == 99.0
    assert invoice["financial_summary"]["subtotal"] == 20.0
    issues = validate_invoice(invoice)["issues"]
    assert any("expected total" in item for item in issues)
    assert invoice["financial_summary"]["grand_total"] == 99.0


def test_a_missing_tax_line_stays_null_instead_of_becoming_zero():
    invoice = _read("missing_tax")
    assert invoice["financial_summary"]["tax_amount"] is None
    assert invoice["financial_summary"]["discount_amount"] is None
    assert "null" in json.dumps(invoice["financial_summary"])
    assert validate_invoice(invoice)["requires_review"] is False


def test_a_blank_tax_line_stays_null_and_is_not_treated_as_a_printed_zero():
    text = "\n".join(demo.LAYOUTS["missing_tax"]).replace("Total: 50.00", "Tax:\nTotal: 50.00")
    invoice = parse_invoice_text(text)
    assert invoice["financial_summary"]["tax_amount"] is None
    assert any("tax_amount" in item for item in invoice["ocr_warnings"])
    assert validate_invoice(invoice)["requires_review"] is True


def test_bare_dollar_is_not_guessed_as_usd():
    invoice = _read("bare_dollar")
    assert invoice["invoice_metadata"]["currency"] is None
    assert any("$" in item for item in invoice["ocr_warnings"])
    assert invoice["financial_summary"]["tax_amount"] == 0.0


def test_european_decimals_are_read_and_still_checked():
    invoice = _read("eur_comma")
    assert invoice["financial_summary"] == {
        "subtotal": 21.0, "discount_amount": None, "shipping_amount": None,
        "tax_amount": 3.99, "rounding_adjustment": None, "grand_total": 24.99,
    }
    assert invoice["line_items"][0]["unit_price"] == 10.5
    assert validate_invoice(invoice)["requires_review"] is False


def test_day_first_date_with_a_day_over_twelve_is_kept():
    invoice = _read("unambiguous_day")
    assert invoice["invoice_metadata"]["issue_date"] == "2024-01-31"
    assert validate_invoice(invoice)["requires_review"] is False


def test_api_returns_valid_json_and_does_not_replace_null_with_zero(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", reader="local", admin_token="admin-token-123456")
    settings.data_dir.mkdir()
    with closing(tenants.open_control(control_path(settings))) as conn:
        _, key = tenants.create_tenant(conn, "Demo")
    client = TestClient(create_app(settings))
    headers = {"Authorization": f"Bearer {key}"}
    pdfs = demo.write_pdfs(tmp_path / "pdfs")

    missing = client.post("/v1/invoices", headers=headers, files={"file": ("missing_tax.pdf", pdfs["missing_tax"].read_bytes(), "application/pdf")})
    assert missing.status_code == 201
    body = json.loads(missing.text)
    assert body["status"] == "clean"
    assert body["invoice"]["financial_summary"]["tax_amount"] is None
    assert re.search(r'"tax_amount"\s*:\s*null', missing.text)

    mismatch = client.post("/v1/invoices", headers=headers, files={"file": ("mismatch.pdf", pdfs["mismatch"].read_bytes(), "application/pdf")})
    parsed = json.loads(mismatch.text)
    assert parsed["status"] == "needs_review"
    assert parsed["invoice"]["financial_summary"]["grand_total"] == 99.0
    assert any("expected total" in item for item in parsed["issues"])

    ambiguous = client.post("/v1/invoices", headers=headers, files={"file": ("ambiguous_date.pdf", pdfs["ambiguous_date"].read_bytes(), "application/pdf")})
    amb = json.loads(ambiguous.text)
    assert amb["invoice"]["invoice_metadata"]["issue_date"] is None
    assert amb["status"] == "needs_review"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_image_ocr_does_not_invent_a_zero_tax(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont

    lines = demo.LAYOUTS["missing_tax"]
    image = Image.new("RGB", (900, 80 + 36 * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    y = 20
    for line in lines:
        draw.text((20, y), line, fill="black", font=font)
        y += 36
    path = tmp_path / "missing_tax.png"
    image.save(path)
    result = LocalExtractor().extract(path.read_bytes(), "image/png")
    summary = result.invoice["financial_summary"]
    text = "\n".join(lines)
    if "Tax:" not in text:
        assert summary["tax_amount"] is None
    assert summary["grand_total"] in (None, 50.0)
    json.dumps(result.invoice)
