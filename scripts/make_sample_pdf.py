"""Builds a small, valid, text-only PDF. Used by tests and the live smoke test (no extra libraries)."""
from __future__ import annotations

import sys


def make_pdf(lines) -> bytes:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    stream = "BT /F1 11 Tf 50 780 Td 14 TL\n" + "".join(f"({esc(line)}) Tj T*\n" for line in lines) + "ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii") + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    return bytes(out)


SAMPLE_INVOICE_LINES = [
    "ACME Supplies Ltd", "Invoice No: SMK-1001", "Invoice date: 2025-03-01", "Due date: 2025-03-31", "Currency: USD",
    "Widget   Qty 4   Unit price 25.00   Line total 100.00",
    "Gadget   Qty 2   Unit price 10.00   Line total 20.00",
    "Subtotal: 120.00", "Tax: 12.00", "Total due: 132.00 USD",
]

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "sample_invoice.pdf"
    with open(path, "wb") as fh:
        fh.write(make_pdf(SAMPLE_INVOICE_LINES))
    print(f"wrote {path}")
