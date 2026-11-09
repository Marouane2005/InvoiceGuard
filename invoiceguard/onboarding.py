"""Creating a customer and drafting the welcome message. Drafts only: nothing is ever sent from here."""
from __future__ import annotations

from typing import Optional

from . import tenants
from .config import Settings


def welcome_message(name: str, base_url: str, currency: Optional[str], monthly_limit: Optional[int]) -> str:
    plan = f"up to {monthly_limit} invoices per month" if monthly_limit is not None else "no monthly invoice limit"
    cur = f"Default currency: {currency}." if currency else "No default currency is set, so invoices without a clear currency will be sent to review."
    return (f"Subject: Your InvoiceGuard account is ready\n\nHello {name} team,\n\n"
            "Your InvoiceGuard account is ready.\n\n"
            f"1. Open {base_url}/ and sign in with your access key. We send the key in a separate message.\n"
            "2. Upload an invoice. Anything doubtful appears under \"Needs review\" with the reason.\n"
            "3. Check the numbers against the original, correct anything wrong, and choose \"Save corrections and resolve\".\n"
            "4. Download checked invoices as a CSV whenever you need them.\n\n"
            f"Your plan: {plan}. {cur}\n\n"
            "Nothing is paid or sent on your behalf. A person confirms every doubtful invoice.\n"
            "Anyone with the access key can see your invoices, so please keep it in a password manager.\n")


def onboard(conn, settings: Settings, name: str, currency: Optional[str], monthly_limit: Optional[int]) -> dict:
    if not settings.public_base_url:
        raise ValueError("set INVOICEGUARD_PUBLIC_URL first so the welcome message has the right address")
    tenant, key = tenants.create_tenant(conn, name, currency, monthly_limit)
    return {"tenant": tenants.public(tenant), "api_key": key,
            "welcome_message": welcome_message(tenant["name"], settings.public_base_url, tenant["default_currency"], tenant["monthly_limit"]),
            "next_step": "Send the welcome message and the key in two separate messages. Draft only: nothing was sent."}
