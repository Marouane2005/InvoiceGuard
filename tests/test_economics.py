import json

import pytest

from invoiceguard import cli, economics


def test_cost_and_price_math():
    cost = economics.cost_per_document(1_200_000, 300_000, 1000, 3.0, 15.0)
    assert cost == pytest.approx(0.0081)
    assert economics.price_for_margin(cost, 0.8) == pytest.approx(0.0405)
    assert economics.cost_per_document(10, 10, 0, 3.0, 15.0) is None


@pytest.mark.parametrize("margin", [-0.1, 1.0, 2])
def test_margin_must_be_below_one(margin):
    with pytest.raises(ValueError):
        economics.price_for_margin(0.01, margin)


def test_report_handles_customers_without_usage():
    rows = [{"name": "A", "documents": 2, "input_tokens": 2_000_000, "output_tokens": 0},
            {"name": "B", "documents": 0, "input_tokens": 0, "output_tokens": 0}]
    out = economics.report(rows, 3.0, 15.0, 0.5)
    assert out[0]["cost_per_document"] == pytest.approx(3.0) and out[0]["price_per_document_at_margin"] == pytest.approx(6.0)
    assert out[1]["cost_per_document"] is None


def test_cli_economics(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("INVOICEGUARD_DATA_DIR", str(tmp_path / "d"))
    assert cli.main(["create-tenant", "Acme"]) == 0
    capsys.readouterr()
    assert cli.main(["economics", "--input-price-per-mtok", "3", "--output-price-per-mtok", "15"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["customers"][0]["customer"] == "Acme" and "hosting" in out["note"]
    assert cli.main(["economics", "--input-price-per-mtok", "3", "--output-price-per-mtok", "15", "--target-margin", "1"]) == 2


def test_negative_prices_are_rejected():
    with pytest.raises(ValueError):
        economics.report([], -1.0, 15.0, 0.5)
