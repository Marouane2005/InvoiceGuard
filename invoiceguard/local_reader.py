"""Read a text invoice without a cloud model.

This is a rule-based reader for digital PDFs (text already in the file) and, when the
``tesseract`` program is installed, for images. It copies numbers as printed. It does not
guess an ambiguous date, it does not turn a missing amount into 0, and it does not change
a total so the arithmetic works. ``validator.py`` still decides whether a person must look.
"""
from __future__ import annotations

import math
import re
import subprocess
import zlib
from datetime import date

from .extractor import ExtractionError, ExtractionResult

_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_ISO_CODES = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "SEK", "NOK", "DKK"}
_SYMBOLS = {"€": "EUR", "£": "GBP", "¥": "JPY"}
_AMBIGUOUS_CURRENCY = {"$", "US$", "kr", "KR"}

_LINE_PIPE = re.compile(
    r"^(?P<desc>.+?)\s*\|\s*(?P<qty>\d[\d.,]*)\s*\|\s*(?P<unit>\d[\d.,]*)\s*\|\s*(?P<total>\d[\d.,]*)\s*$"
)
_LINE_LABELED = re.compile(
    r"^(?P<desc>.+?)\s+Qty\s+(?P<qty>\d[\d.,]*)\s+Unit price\s+(?P<unit>\d[\d.,]*)\s+Line total\s+(?P<total>\d[\d.,]*)\s*$",
    re.IGNORECASE,
)
_FIELDS = (
    ("invoice_number", re.compile(r"^(?:invoice\s*(?:number|no\.?|#)|inv(?:oice)?\s*#)\s*[:.]?\s*(.+)$", re.I)),
    ("issue_date", re.compile(r"^(?:invoice\s*date|issue\s*date|date)\s*[:.]?\s*(.+)$", re.I)),
    ("due_date", re.compile(r"^(?:due\s*date|payment\s*due)\s*[:.]?\s*(.+)$", re.I)),
    ("currency", re.compile(r"^currency\s*[:.]?\s*(.*)$", re.I)),
    ("vendor_name", re.compile(r"^(?:vendor|from|supplier)\s*[:.]?\s*(.+)$", re.I)),
    ("subtotal", re.compile(r"^(?:subtotal|sub\s*total)\s*[:.]?\s*(.*)$", re.I)),
    ("tax_amount", re.compile(r"^(?:tax|vat|sales\s*tax)\s*[:.]?\s*(.*)$", re.I)),
    ("discount_amount", re.compile(r"^discount\s*[:.]?\s*(.*)$", re.I)),
    ("shipping_amount", re.compile(r"^(?:shipping|delivery)\s*[:.]?\s*(.*)$", re.I)),
    ("rounding_adjustment", re.compile(r"^rounding(?:\s*adjustment)?\s*[:.]?\s*(.*)$", re.I)),
    ("grand_total", re.compile(r"^(?:grand\s*total|total\s*due|amount\s*due|balance\s*due|total)\s*[:.]?\s*(.*)$", re.I)),
)


def _unescape(value: str) -> str:
    out = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "(": "(", ")": ")", "\\": "\\"}.get(nxt, nxt))
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def pdf_text(data: bytes) -> str:
    """Pull visible text out of a PDF. Compressed streams are inflated; no page is rendered."""
    pieces = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        raw = match.group(1)
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        text = raw.decode("latin-1", errors="replace")
        for lit in re.finditer(r"\((?:\\.|[^\\)])*\)\s*Tj", text):
            body = lit.group(0)
            body = body[: body.rfind(")")][1:]
            pieces.append(_unescape(body))
        for arr in re.finditer(r"\[(.*?)\]\s*TJ", text, re.S):
            parts = re.findall(r"\((?:\\.|[^\\)])*\)", arr.group(1))
            if parts:
                pieces.append("".join(_unescape(part[1:-1]) for part in parts))
    return "\n".join(piece.strip() for piece in pieces if piece.strip())


def _ocr_image(data: bytes) -> str:
    try:
        proc = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "6"],
            input=data, capture_output=True, timeout=30, check=False,
        )
    except FileNotFoundError:
        raise ExtractionError("image reading needs the tesseract program, or set INVOICEGUARD_READER=claude") from None
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise ExtractionError("tesseract could not read this image" + (f" ({detail[-1][:160]})" if detail else ""))
    return proc.stdout.decode("utf-8", errors="replace")


def parse_amount(raw: str):
    """Return a float copied from the text, or None when the text is blank or not a number.

    A thousands comma (1,234.50) and a decimal comma (21,00 or 1.234,50) are accepted.
    The digits are not rounded to make a later total match.
    """
    text = raw.strip()
    text = re.sub(r"(?:USD|EUR|GBP|JPY|CHF|CAD|AUD|SEK|NOK|DKK|€|£|¥|\$)$", "", text, flags=re.I).strip()
    text = text.replace(" ", "")
    if text in {"", "-", "–", "—"}:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        if re.fullmatch(r"\d{1,3}(,\d{3})+", text):
            text = text.replace(",", "")
        elif re.fullmatch(r"\d+,\d{2}", text):
            text = text.replace(",", ".")
        else:
            return None
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    value = float(text)
    if not math.isfinite(value):
        return None
    return value


def parse_date(raw: str, field: str, warnings: list):
    """ISO dates pass through. 12/05/23 stays null. A day or month above 12 can be resolved."""
    text = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            date.fromisoformat(text)
        except ValueError:
            warnings.append(f"{field}: {text!r} is not a real calendar date; set to null")
            return None
        return text
    match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})", text)
    if not match:
        warnings.append(f"{field}: {text!r} was not read as a date; set to null")
        return None
    first, second, year_raw = int(match.group(1)), int(match.group(2)), match.group(3)
    year = int(year_raw) + (2000 if len(year_raw) == 2 and int(year_raw) < 70 else 0)
    if len(year_raw) == 2 and int(year_raw) >= 70:
        year = 1900 + int(year_raw)
    if first > 12 and second <= 12:
        day, month = first, second
    elif second > 12 and first <= 12:
        month, day = first, second
    else:
        warnings.append(f"{field}: {text!r} could be day-first or month-first (for example 12/05/23); set to null")
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        warnings.append(f"{field}: {text!r} is not a real calendar date; set to null")
        return None


def parse_currency(raw: str, warnings: list):
    text = raw.strip()
    if not text:
        warnings.append("currency: blank; set to null")
        return None
    if text.upper() in _ISO_CODES:
        return text.upper()
    if text in _SYMBOLS:
        return _SYMBOLS[text]
    if text in _AMBIGUOUS_CURRENCY or text.upper() in _AMBIGUOUS_CURRENCY:
        warnings.append(f"currency: {text!r} is ambiguous (a bare $ or kr is not a currency code); set to null")
        return None
    warnings.append(f"currency: {text!r} is not a 3-letter currency code; set to null")
    return None


def _line_item(line: str):
    match = _LINE_PIPE.match(line) or _LINE_LABELED.match(line)
    if not match:
        return None
    return {
        "description": match.group("desc").strip(),
        "quantity": parse_amount(match.group("qty")),
        "unit_price": parse_amount(match.group("unit")),
        "total_amount": parse_amount(match.group("total")),
    }


def parse_invoice_text(text: str) -> dict:
    warnings: list = []
    meta = {"invoice_number": None, "vendor_name": None, "issue_date": None, "due_date": None, "currency": None}
    summary = {
        "subtotal": None, "discount_amount": None, "shipping_amount": None,
        "tax_amount": None, "rounding_adjustment": None, "grand_total": None,
    }
    items = []
    saw_tax_line = False
    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        item = _line_item(line)
        if item is not None:
            items.append(item)
            continue
        matched = False
        for name, pattern in _FIELDS:
            found = pattern.match(line)
            if not found:
                continue
            value = found.group(1).strip()
            if name in {"issue_date", "due_date"}:
                meta[name] = parse_date(value, name, warnings)
            elif name == "currency":
                meta["currency"] = parse_currency(value, warnings)
            elif name in meta:
                meta[name] = value or None
            elif name == "tax_amount":
                saw_tax_line = True
                summary["tax_amount"] = parse_amount(value)
                if summary["tax_amount"] is None:
                    warnings.append("tax_amount: the tax line was blank or unreadable; set to null")
            else:
                summary[name] = parse_amount(value)
                if summary[name] is None and value:
                    warnings.append(f"{name}: {value!r} is not a number; set to null")
            matched = True
            break
        if not matched and meta["vendor_name"] is None:
            meta["vendor_name"] = line
    if saw_tax_line is False:
        summary["tax_amount"] = None  # no tax line was printed; do not invent 0
    return {
        "invoice_metadata": meta,
        "line_items": items,
        "financial_summary": summary,
        "ocr_warnings": warnings,
    }


class LocalExtractor:
    """Drop-in reader used when INVOICEGUARD_READER=local. model is always ``local-rules``."""

    def extract(self, data: bytes, media_type: str) -> ExtractionResult:
        if media_type == "application/pdf":
            text = pdf_text(data)
            if not text.strip():
                raise ExtractionError(
                    "this PDF has no text layer. A scan needs tesseract on an image, or INVOICEGUARD_READER=claude"
                )
        elif media_type in _IMAGE_TYPES:
            text = _ocr_image(data)
            if not text.strip():
                raise ExtractionError("tesseract returned no text for this image")
        else:
            raise ExtractionError("unsupported file")
        return ExtractionResult(invoice=parse_invoice_text(text), input_tokens=0, output_tokens=0, model="local-rules")
