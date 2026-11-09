import dataclasses
from contextlib import closing

import pytest

from invoiceguard import onboarding, pipeline, store, tenants
from invoiceguard.errors import PipelineError
from conftest import GOOD_RAW, FakeExtractor, pdf


@pytest.fixture
def control(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with closing(tenants.open_control(pipeline.control_path(settings))) as c:
        yield c


def test_onboarding_drafts_a_message_and_keeps_the_key_out_of_it(settings, control):
    s = dataclasses.replace(settings, public_base_url="https://invoices.example.com")
    out = onboarding.onboard(control, s, "Acme Ltd", "usd", 250)
    msg = out["welcome_message"]
    assert out["api_key"].startswith("ig_") and out["api_key"] not in msg
    assert "https://invoices.example.com/" in msg and "up to 250 invoices per month" in msg and "Default currency: USD" in msg
    assert "separate message" in msg and "Draft only" in out["next_step"]
    assert tenants.authenticate(control, out["api_key"])["name"] == "Acme Ltd"


def test_onboarding_needs_a_public_address(settings, control):
    with pytest.raises(ValueError, match="INVOICEGUARD_PUBLIC_URL"):
        onboarding.onboard(control, settings, "Acme", None, None)
    assert control.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 0


def test_message_without_limit_or_currency_says_so():
    text = onboarding.welcome_message("Acme", "https://x.example", None, None)
    assert "no monthly invoice limit" in text and "sent to review" in text


def processed(settings, make_tenant):
    tenant, headers = make_tenant("Erase Me")
    pipeline.process_document(settings=settings, tenant=tenant, data=pdf(1), filename="a.pdf", extractor=FakeExtractor(GOOD_RAW))
    return tenant, headers["Authorization"].split()[1]


def test_erase_is_a_dry_run_unless_confirmed(settings, control, make_tenant):
    tenant, _ = processed(settings, make_tenant)
    out = pipeline.erase_tenant(settings, control, tenant["id"])
    assert out == {"tenant": tenant["id"], "documents": 1, "files": 1, "dry_run": True}
    assert pipeline.tenant_dir(settings, tenant["id"]).exists() and tenants.get_tenant(control, tenant["id"])["active"] == 1


def test_confirmed_erase_removes_content_switches_the_account_off_and_keeps_usage_counts(settings, control, make_tenant):
    tenant, key = processed(settings, make_tenant)
    out = pipeline.erase_tenant(settings, control, tenant["id"], confirm=True)
    assert out["dry_run"] is False and out["documents"] == 1 and out["account_active"] is False
    assert not pipeline.tenant_dir(settings, tenant["id"]).exists()
    assert tenants.authenticate(control, key) is None
    assert tenants.usage_summary(control, tenant["id"], tenants.current_month())["documents"] == 1
    again = pipeline.erase_tenant(settings, control, tenant["id"], confirm=True)   # safe to repeat
    assert again["documents"] == 0


def test_erase_unknown_customer(settings, control):
    with pytest.raises(PipelineError) as err:
        pipeline.erase_tenant(settings, control, "t_missing", confirm=True)
    assert err.value.code == "tenant_not_found"
