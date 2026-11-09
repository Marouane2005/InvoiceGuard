"""Write invented invoices for the local reader. Nothing in here is a customer document."""
from __future__ import annotations

import sys
from pathlib import Path

LAYOUTS = {
    "clean_usd": [
        "Vendor: ACME Supplies Ltd",
        "Invoice number: SMK-1001",
        "Invoice date: 2025-03-01",
        "Due date: 2025-03-31",
        "Currency: USD",
        "Widget | 4 | 25.00 | 100.00",
        "Gadget | 2 | 10.00 | 20.00",
        "Subtotal: 120.00",
        "Tax: 12.00",
        "Total: 132.00",
    ],
    "ambiguous_date": [
        "Vendor: Harbour Metals",
        "Invoice number: HM-12",
        "Invoice date: 12/05/23",
        "Due date: 2023-06-01",
        "Currency: GBP",
        "Copper pipe | 2 | 15.00 | 30.00",
        "Subtotal: 30.00",
        "Tax: 6.00",
        "Total: 36.00",
    ],
    "mismatch": [
        "Vendor: Bright Office",
        "Invoice number: BO-9",
        "Invoice date: 2024-01-15",
        "Due date: 2024-02-15",
        "Currency: USD",
        "Paper | 5 | 4.00 | 20.00",
        "Subtotal: 20.00",
        "Tax: 2.00",
        "Total: 99.00",
    ],
    "missing_tax": [
        "Vendor: Plain Goods",
        "Invoice number: PG-1",
        "Invoice date: 2024-07-01",
        "Due date: 2024-07-31",
        "Currency: EUR",
        "Crate | 1 | 50.00 | 50.00",
        "Subtotal: 50.00",
        "Total: 50.00",
    ],
    "eur_comma": [
        "Vendor: Mueller GmbH",
        "Invoice number: DE-44",
        "Invoice date: 2024-11-08",
        "Due date: 2024-12-08",
        "Currency: EUR",
        "Schrauben | 2 | 10,50 | 21,00",
        "Subtotal: 21,00",
        "Tax: 3,99",
        "Total: 24,99",
    ],
    "bare_dollar": [
        "Vendor: Corner Shop",
        "Invoice number: CS-3",
        "Invoice date: 2024-04-04",
        "Due date: 2024-05-04",
        "Currency: $",
        "Coffee | 1 | 3.50 | 3.50",
        "Subtotal: 3.50",
        "Tax: 0.00",
        "Total: 3.50",
    ],
    "unambiguous_day": [
        "Vendor: Day First Ltd",
        "Invoice number: DF-31",
        "Invoice date: 31/01/24",
        "Due date: 2024-02-28",
        "Currency: EUR",
        "Bolt | 1 | 8.00 | 8.00",
        "Subtotal: 8.00",
        "Tax: 0.00",
        "Total: 8.00",
    ],
}


def write_pdfs(dest: Path) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from make_sample_pdf import make_pdf

    dest.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, lines in LAYOUTS.items():
        path = dest / f"{name}.pdf"
        path.write_bytes(make_pdf(lines))
        written[name] = path
    return written


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dest = Path(args[0] if args else "samples")
    written = write_pdfs(dest)
    for path in written.values():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
