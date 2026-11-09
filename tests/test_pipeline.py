import dataclasses
from contextlib import closing

import pytest

from invoiceguard import pipeline, store, tenants
from invoiceguard.errors import CATALOG, PipelineError
from invoiceguard.extractor import ExtractionError, ExtractionNotConfigured
from conftest import BAD_RAW, GOOD_RAW, FakeExtractor, pdf


def run(settings, tenant, extractor, data, name="inv.pdf"):
    return pipeline.process_document(settings=settings, tenant=tenant, data=data, filename=name, extractor=extractor)


def usage(settings, tenant):
    with closing(tenants.open_control(pipeline.control_path(settings))) as c:
        return tenants.usage_summary(c, tenant["id"], tenants.current_month())


def test_clean_invoice_is_stored_metered_and_not_queued(settings, make_tenant):
    tenant, _ = make_tenant()
    res = run(settings, tenant, FakeExtractor(GOOD_RAW), pdf(1))
    assert res["status"] == "clean" and res["issues"] == [] and res["review_item_id"] is None and not res["cached"]
    assert usage(settings, tenant) == {"month": tenants.current_month(), "documents": 1, "input_tokens": 1200, "output_tokens": 300}
    files = list((pipeline.tenant_dir(settings, tenant["id"]) / "files").iterdir())
    assert len(files) == 1 and files[0].suffix == ".pdf"


def test_misread_invoice_goes_to_the_review_queue(settings, make_tenant):
    tenant, _ = make_tenant()
    res = run(settings, tenant, FakeExtractor(BAD_RAW), pdf(1))
    assert res["status"] == "needs_review" and res["review_item_id"] and any("5 x 150" in i for i in res["issues"])


def test_same_file_twice_is_free_and_does_not_call_the_model_again(settings, make_tenant):
    tenant, _ = make_tenant()
    ex = FakeExtractor(GOOD_RAW)
    first, second = run(settings, tenant, ex, pdf(1)), run(settings, tenant, ex, pdf(1))
    assert len(ex.calls) == 1 and second["cached"] and second["document_id"] == first["document_id"]
    assert usage(settings, tenant)["documents"] == 1


def test_file_checks(settings, make_tenant):
    tenant, _ = make_tenant()
    ex = FakeExtractor()
    for data, code in [(b"", "empty_file"), (b"just text", "unsupported_file"),
                       (b"%PDF-" + b"x" * settings.max_upload_bytes, "file_too_large")]:
        with pytest.raises(PipelineError) as err:
            run(settings, tenant, ex, data)
        assert err.value.code == code
    assert ex.calls == []


def test_monthly_quota_blocks_new_files_but_not_repeats(settings, make_tenant):
    tenant, _ = make_tenant(monthly_limit=1)
    ex = FakeExtractor(GOOD_RAW)
    run(settings, tenant, ex, pdf(1))
    with pytest.raises(PipelineError) as err:
        run(settings, tenant, ex, pdf(2))
    assert err.value.code == "quota_exceeded" and len(ex.calls) == 1
    assert run(settings, tenant, ex, pdf(1))["cached"]


def test_failed_reading_stores_nothing_and_counts_nothing(settings, make_tenant):
    tenant, _ = make_tenant()
    with pytest.raises(PipelineError) as err:
        run(settings, tenant, FakeExtractor(ExtractionError("the AI provider returned HTTP 503")), pdf(1))
    assert err.value.code == "extraction_failed" and "HTTP 503" in err.value.message
    assert usage(settings, tenant)["documents"] == 0
    with closing(pipeline.open_tenant_db(settings, tenant["id"])) as c:
        assert store.list_invoices(c) == []
    with pytest.raises(PipelineError) as err:
        run(settings, tenant, FakeExtractor(ExtractionNotConfigured("x")), pdf(1))
    assert err.value.code == "extraction_not_configured"


def test_same_vendor_and_number_in_a_different_file_is_flagged_as_duplicate(settings, make_tenant):
    tenant, _ = make_tenant()
    ex = FakeExtractor(GOOD_RAW)
    first = run(settings, tenant, ex, pdf(1))
    second = run(settings, tenant, ex, pdf(2))
    assert first["status"] == "clean"
    assert second["status"] == "needs_review" and any(i.startswith("possible duplicate of document") for i in second["issues"])


def test_default_currency_is_applied_and_missing_currency_is_flagged(settings, make_tenant):
    raw = {**GOOD_RAW, "invoice_metadata": {**GOOD_RAW["invoice_metadata"], "currency": None}}
    without, _ = make_tenant("NoDefault")
    assert "currency is missing" in run(settings, without, FakeExtractor(raw), pdf(1))["issues"]
    with_default, _ = make_tenant("WithDefault", default_currency="MAD")
    res = run(settings, with_default, FakeExtractor(raw), pdf(1))
    assert res["status"] == "clean" and res["invoice"]["invoice_metadata"]["currency"] == "MAD"


def test_tenants_never_share_documents_or_cache(settings, make_tenant):
    a, _ = make_tenant("A")
    b, _ = make_tenant("B")
    ex = FakeExtractor(GOOD_RAW)
    run(settings, a, ex, pdf(1))
    res = run(settings, b, ex, pdf(1))
    assert len(ex.calls) == 2 and not res["cached"]
    with closing(pipeline.open_tenant_db(settings, b["id"])) as conn:
        assert len(store.list_invoices(conn)) == 1


def flagged(settings, make_tenant, raw=BAD_RAW):
    tenant, _ = make_tenant()
    res = run(settings, tenant, FakeExtractor(raw), pdf(1))
    return tenant, res


def test_resolving_with_a_valid_correction_marks_invoice_reviewed(settings, make_tenant):
    tenant, res = flagged(settings, make_tenant)
    fixed = {**GOOD_RAW, "ocr_warnings": ["ignored"]}
    with closing(pipeline.open_tenant_db(settings, tenant["id"])) as conn:
        pipeline.resolve_review(conn, tenant, res["review_item_id"], "read 750 from the PDF", fixed)
        rec = store.get_invoice(conn, res["document_id"])
        assert rec["status"] == "reviewed" and rec["invoice"]["financial_summary"]["subtotal"] == 750.0
        assert rec["invoice"]["ocr_warnings"] == []


def test_a_bad_correction_is_rejected_and_the_item_stays_open(settings, make_tenant):
    tenant, res = flagged(settings, make_tenant)
    with closing(pipeline.open_tenant_db(settings, tenant["id"])) as conn:
        with pytest.raises(PipelineError) as err:
            pipeline.resolve_review(conn, tenant, res["review_item_id"], "tried", BAD_RAW)
        assert err.value.code == "invalid_correction"
        assert store.get_invoice(conn, res["document_id"])["status"] == "needs_review"


def test_dismiss_note_and_unknown_ids(settings, make_tenant):
    tenant, res = flagged(settings, make_tenant)
    with closing(pipeline.open_tenant_db(settings, tenant["id"])) as conn:
        with pytest.raises(PipelineError) as err:
            pipeline.dismiss_review(conn, res["review_item_id"], "  ")
        assert err.value.code == "invalid_request"
        pipeline.dismiss_review(conn, res["review_item_id"], "not our invoice")
        assert store.get_invoice(conn, res["document_id"])["status"] == "dismissed"
        with pytest.raises(PipelineError) as err:
            pipeline.dismiss_review(conn, 999, "x")
        assert err.value.code == "not_found"


def test_every_error_tells_the_customer_what_to_do_next():
    for code, (status, message, hint) in CATALOG.items():
        err = PipelineError(code, detail="d", limit_mb=1)
        assert err.message.strip() and err.hint.strip().endswith(".") and 400 <= status <= 599, code


def test_read_gate_rejects_extra_concurrent_reads_without_calling_the_model(settings, make_tenant):
    tenant, _ = make_tenant()
    gate = pipeline.ReadGate(1)
    ex = FakeExtractor(GOOD_RAW)
    with gate.slot():
        with pytest.raises(PipelineError) as err:
            pipeline.process_document(settings=settings, tenant=tenant, data=pdf(1), filename="a.pdf", extractor=ex, gate=gate)
    assert err.value.code == "server_busy" and ex.calls == []
    with closing(pipeline.open_tenant_db(settings, tenant["id"])) as conn:
        assert store.list_invoices(conn) == []
    res = pipeline.process_document(settings=settings, tenant=tenant, data=pdf(1), filename="a.pdf", extractor=ex, gate=gate)
    assert res["status"] == "clean"  # the slot was released
