"""Turns measured token usage into provider cost per invoice and a price that hits a target margin.

Prices are inputs (dollars per million tokens, from the provider's current price list). Nothing is assumed.
This covers provider cost only: hosting, support, payment fees and taxes are not included.
"""
from __future__ import annotations

from typing import Optional


def cost_per_document(input_tokens: int, output_tokens: int, documents: int,
                      input_price_per_mtok: float, output_price_per_mtok: float) -> Optional[float]:
    if documents <= 0:
        return None
    total = input_tokens / 1_000_000 * input_price_per_mtok + output_tokens / 1_000_000 * output_price_per_mtok
    return total / documents


def price_for_margin(cost: float, margin: float) -> float:
    if not 0 <= margin < 1:
        raise ValueError("margin must be at least 0 and less than 1 (for example 0.8 for 80%)")
    return cost / (1 - margin)


def report(rows: list, input_price_per_mtok: float, output_price_per_mtok: float, margin: float) -> list:
    price_for_margin(0.0, margin)  # reject a bad margin even when there is no usage to price yet
    if input_price_per_mtok < 0 or output_price_per_mtok < 0:
        raise ValueError("prices must not be negative")
    out = []
    for row in rows:
        cost = cost_per_document(row["input_tokens"], row["output_tokens"], row["documents"],
                                 input_price_per_mtok, output_price_per_mtok)
        out.append({
            "customer": row["name"], "documents": row["documents"],
            "provider_cost": None if cost is None else round(cost * row["documents"], 4),
            "cost_per_document": None if cost is None else round(cost, 6),
            "price_per_document_at_margin": None if cost is None else round(price_for_margin(cost, margin), 6),
        })
    return out
