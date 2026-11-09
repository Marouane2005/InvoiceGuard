import sqlite3
from contextlib import closing
from datetime import date

import pytest

from invoiceguard import exports, store, tenants
from invoiceguard.policy import normalize_invoice
from conftest import GOOD_RAW

GOOD = {"columns": [{"header": "Supplier", "field": "vendor_name"}]}


@pytest.mark.parametrize("raw,needle", [
    ("nope", "must be an object"),
    ({"columns": [{"header": "A", "field": "vendor_name"}], "script": 1}, "unknown option"),
    ({}, "list of 1 to 30"),
    ({"columns": []}, "list of 1 to 30"),
    ({"columns": [{"header": "A", "field": "vendor_name"}] * 31}, "list of 1 to 30"),
    ({"columns": [{"field": "vendor_name"}]}, "header"),
    ({"columns": [{"header": "", "field": "vendor_name"}]}, "header"),
    ({"columns": [{"header": "A"}]}, "exactly one of"),
    ({"columns": [{"header": "A", "field": "vendor_name", "value": "x"}]}, "exactly one of"),
    ({"columns": [{"header": "A", "field": "password"}]}, "unknown field"),
    ({"columns": [{"header": "A", "field": "vendor_name", "extra": 1}]}, "use only these keys"),
    ({"columns": [{"header": "A", "field": "vendor_name", "date_format": "%Y"}]}, "only applies"),
    ({"columns": [{"header": "A", "field": "issue_date", "date_format": "dd/mm"}]}, "must look like"),
    ({"columns": [{"header": "A", "value": 5}]}, "'value' must be text"),
    ({**GOOD, "delimiter": "|"}, "delimiter"),
    ({**GOOD, "decimal": ";"}, "decimal"),
    ({**GOOD, "delimiter": ",", "decimal": ","}, "cannot be the same"),
    ({**GOOD, "decimals": 5}, "decimals"),
    ({**GOOD, "decimals": True}, "decimals"),
    ({**GOOD, "include_header": "yes"}, "true or false"),
])
def test_invalid_templates_say_what_to_fix(raw, needle):
    with pytest.raises(exports.TemplateError, match=needle):
        exports.validate_template(raw)


def test_valid_template_gets_defaults():
    t = exports.validate_template(GOOD)
    assert t["delimiter"] == "," and t["decimal"] == "." and t["decimals"] == 2 and t["include_header"] is True and t["utf8_bom"] is False


@pytest.fixture
def records(tmp_path):
    with closing(store.open_db(str(tmp_path / "t.db"))) as conn:
        def save(doc, vendor="Globex Corp", total=825.0, issue="2023-12-05"):
            raw = {**GOOD_RAW, "invoice_metadata": {**GOOD_RAW["invoice_metadata"], "vendor_name": vendor, "issue_date": issue},
                   "financial_summary": {**GOOD_RAW["financial_summary"], "grand_total": total}}
            store.save_invoice(conn, document_id=doc, filename=f"{doc}.pdf", media_type="application/pdf", status="clean",
                               invoice=normalize_invoice(raw), issues=[], model="m", input_tokens=1, output_tokens=1)
        save("a", total=1234.5)
        save("b", vendor="=HYPERLINK(\"x\")", total=-50.0, issue="not-a-date")
        yield list(reversed(store.list_invoices(conn, limit=10)))   # oldest first, as the API passes them


TEMPLATE = {"columns": [{"header": "Supplier", "field": "vendor_name"}, {"header": "Bill no", "field": "invoice_number"},
                        {"header": "Date", "field": "issue_date", "date_format": "%d/%m/%Y"}, {"header": "Account", "value": "4000"},
                        {"header": "Lines", "field": "line_items_count"}, {"header": "Amount", "field": "grand_total"}],
            "delimiter": ";", "decimal": ","}


def test_render_matches_an_accounting_import_layout(records):
    lines = exports.render(records, TEMPLATE).strip().splitlines()
    assert lines[0] == "Supplier;Bill no;Date;Account;Lines;Amount"
    assert lines[1] == "Globex Corp;9942;05/12/2023;4000;1;1234,50"


def test_render_is_safe_and_forgiving(records):
    import csv, io
    rows = list(csv.reader(io.StringIO(exports.render(records, TEMPLATE)), delimiter=";"))
    assert rows[2] == ["'=HYPERLINK(\"x\")", "9942", "not-a-date", "4000", "1", "-50,00"]   # formula neutralized, bad date kept as-is, minus kept


def test_render_options(records):
    plain = exports.render(records, {**TEMPLATE, "include_header": False, "utf8_bom": True, "decimals": 0, "delimiter": "\t", "decimal": "."})
    assert plain.startswith("\ufeff") and "Supplier" not in plain and plain.splitlines()[0].endswith("\t1235")


def test_empty_values_are_blank(tmp_path):
    with closing(store.open_db(str(tmp_path / "t.db"))) as conn:
        raw = {**GOOD_RAW, "invoice_metadata": {**GOOD_RAW["invoice_metadata"], "due_date": None}}
        store.save_invoice(conn, document_id="a", filename="a.pdf", media_type="application/pdf", status="clean",
                           invoice=normalize_invoice(raw), issues=[], model="m", input_tokens=1, output_tokens=1)
        out = exports.render(store.list_invoices(conn), {"columns": [{"header": "Number", "field": "invoice_number"}, {"header": "Due", "field": "due_date", "date_format": "%d/%m/%Y"}], "include_header": False})
    assert out.strip() == "9942,"


V1_TENANTS = """
CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100), api_key_hash TEXT NOT NULL UNIQUE,
  default_currency TEXT, monthly_limit INTEGER CHECK (monthly_limit IS NULL OR monthly_limit >= 0), slack_webhook_url TEXT,
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL REFERENCES tenants(id), document_id TEXT NOT NULL,
  month TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, created_at TEXT NOT NULL, UNIQUE (tenant_id, document_id));
"""


def test_an_existing_version_1_customer_database_upgrades_in_place(tmp_path):
    path = tmp_path / "control.db"
    with closing(sqlite3.connect(path)) as raw:
        raw.executescript(V1_TENANTS)
        raw.execute("INSERT INTO tenants (id, name, api_key_hash, default_currency, created_at) VALUES ('t_old','Old Customer',?, 'EUR','2026-01-01')",
                    (tenants._hash("ig_old_key"),))
        raw.execute("INSERT INTO usage (tenant_id, document_id, month, input_tokens, output_tokens, created_at) VALUES ('t_old','d','2026-01',5,6,'x')")
        raw.execute("PRAGMA user_version = 1")
        raw.commit()
    with closing(tenants.open_control(path)) as conn:
        assert [r[1] for r in conn.execute("PRAGMA table_info(tenants)")][-1] == "export_template"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        old = tenants.authenticate(conn, "ig_old_key")
        assert old["name"] == "Old Customer" and old["default_currency"] == "EUR" and old["export_template"] is None
        assert tenants.usage_summary(conn, "t_old", "2026-01")["documents"] == 1
        tenants.update_settings(conn, "t_old", export_template=GOOD)
        assert tenants.export_template(tenants.get_tenant(conn, "t_old"))["columns"][0]["field"] == "vendor_name"
        tenants.update_settings(conn, "t_old", export_template=None)
        assert tenants.export_template(tenants.get_tenant(conn, "t_old")) is None


def test_a_fresh_database_reaches_the_same_shape(tmp_path):
    with closing(tenants.open_control(tmp_path / "fresh.db")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == tenants.SCHEMA_VERSION == 2
        assert "export_template" in [r[1] for r in conn.execute("PRAGMA table_info(tenants)")]


@pytest.mark.parametrize("value,decimals,expected", [
    (1234.5, 0, "1235"), (0.125, 2, "0.13"), (2.675, 2, "2.68"), (-2.5, 0, "-3"), (-0.001, 2, "0.00"),
    (825.0, 2, "825.00"), (1e-07, 2, "0.00"), (1234567.891, 1, "1234567.9"),
])
def test_money_rounds_half_up_exactly(value, decimals, expected):
    assert exports._money(value, decimals) == expected


def test_export_order_is_arrival_order_even_within_one_second(tmp_path):
    with closing(store.open_db(str(tmp_path / "t.db"))) as conn:
        for doc in ("zzz", "aaa", "mmm"):   # deliberately not alphabetical
            store.save_invoice(conn, document_id=doc, filename=doc, media_type="application/pdf", status="clean",
                               invoice=normalize_invoice(GOOD_RAW), issues=[], model="m", input_tokens=1, output_tokens=1)
        conn.execute("UPDATE invoices SET created_at = '2026-10-01T00:00:00+00:00'")   # identical timestamps
        newest_first = [r["document_id"] for r in store.list_invoices(conn)]
        assert newest_first == ["mmm", "aaa", "zzz"]
        assert [r["document_id"] for r in reversed(store.list_invoices(conn))] == ["zzz", "aaa", "mmm"]
        lines = store.export_csv(conn).strip().splitlines()[1:]
        assert [l.split(",")[0] for l in lines] == ["zzz", "aaa", "mmm"]


def test_templates_can_use_the_new_amount_fields(records):
    t = {"columns": [{"header": "Discount", "field": "discount_amount"}, {"header": "Ship", "field": "shipping_amount"}, {"header": "Round", "field": "rounding_adjustment"}]}
    assert exports.validate_template(t)["columns"][0]["field"] == "discount_amount"
    assert exports.render(records, t).splitlines()[1] == ",,"
