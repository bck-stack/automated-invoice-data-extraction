"""
PDF Invoice Parser
Extracts structured fields (vendor, invoice number, dates, amounts, IBAN,
line items) from PDF invoices and returns clean, validated JSON/CSV output.
"""

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

AMOUNT_TOLERANCE = 0.02  # rounding tolerance when cross-checking totals


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class LineItem:
    description: str
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    total: Optional[float] = None


@dataclass
class Invoice:
    source_file: str
    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None     # ISO 8601 (YYYY-MM-DD) when parseable
    due_date: Optional[str] = None
    currency: Optional[str] = None
    subtotal: Optional[float] = None
    tax_amount: Optional[float] = None
    total_amount: Optional[float] = None
    iban: Optional[str] = None
    iban_valid: Optional[bool] = None
    line_items: list[LineItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_text: str = ""


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

CURRENCY_SYMBOLS = {"£": "GBP", "$": "USD", "€": "EUR", "₺": "TRY", "₹": "INR", "¥": "JPY"}
CURRENCY_CODES = ["USD", "EUR", "GBP", "TRY", "INR", "CAD", "AUD", "CHF", "JPY", "SEK", "NOK", "DKK", "PLN", "TL"]
AMOUNT_RE = r"(?:(?:USD|EUR|GBP|TRY|TL|CHF|INR)\s*)?[£$€₺₹¥]?\s*-?\d[\d.,' ]*\d(?:\s*(?:TL|TRY|USD|EUR|GBP))?|[£$€₺₹¥]?\s*\d"
DATE_RE = (
    r"(\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}"
    r"|\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"
    r"|\d{1,2}\s+[A-Za-zçğıöşüÇĞİÖŞÜ]{3,}\.?\s+\d{4}"
    r"|[A-Za-z]{3,}\.?\s+\d{1,2},?\s+\d{4})"
)
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "oca": 1, "şub": 2, "sub": 2, "nis": 4, "mayıs": 5, "haz": 6, "tem": 7, "ağu": 8, "agu": 8,
    "eyl": 9, "eki": 10, "kas": 11, "ara": 12,
}


def parse_amount(raw: Optional[str]) -> Optional[float]:
    """Locale-independent amount parsing: 1,234.56 / 1.234,56 / 1 234,56 / 1234 / 12,5."""
    if not raw:
        return None
    m = re.search(r"-?\d[\d.,'  ]*", raw)
    if not m:
        return None
    s = re.sub(r"[\s ']", "", m.group(0)).rstrip(".,")
    negative = s.startswith("-")
    s = s.lstrip("-")
    if "." in s and "," in s:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        s = s.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif "." in s or "," in s:
        sep = "." if "." in s else ","
        parts = s.split(sep)
        s = s.replace(sep, "") if len(parts) > 2 or len(parts[-1]) == 3 else "".join(parts[:-1]) + "." + parts[-1]
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


def normalize_date(raw: Optional[str], day_first: bool = True) -> Optional[str]:
    """Convert common invoice date formats to ISO YYYY-MM-DD. Returns the raw string if ambiguous/unknown."""
    if not raw:
        return None
    text = raw.strip().rstrip(".")
    try:
        m = re.fullmatch(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", text)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", text)
        if m:
            a, b, y = int(m[1]), int(m[2]), int(m[3])
            y += 2000 if y < 100 else 0
            if a > 12:
                d, mo = a, b
            elif b > 12:
                mo, d = a, b
            else:
                d, mo = (a, b) if day_first else (b, a)
            return date(y, mo, d).isoformat()
        m = re.fullmatch(r"(\d{1,2})\s+([^\W\d_]+)\.?\s+(\d{4})", text)
        if m:
            mo = MONTHS.get(m[2].lower()[:5]) or MONTHS.get(m[2].lower()[:3])
            if mo:
                return date(int(m[3]), mo, int(m[1])).isoformat()
        m = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", text)
        if m:
            mo = MONTHS.get(m[1].lower()[:3])
            if mo:
                return date(int(m[3]), mo, int(m[2])).isoformat()
    except ValueError:
        pass
    return raw.strip()


def iban_is_valid(iban: str) -> bool:
    """ISO 13616 mod-97 checksum."""
    s = re.sub(r"\s", "", iban).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def detect_currency(text: str) -> Optional[str]:
    for code in CURRENCY_CODES:
        if re.search(rf"(?<![A-Z]){code}(?![A-Z])", text):
            return "TRY" if code == "TL" else code
    counts = {code: text.count(sym) for sym, code in CURRENCY_SYMBOLS.items() if sym in text}
    return max(counts, key=counts.get) if counts else None


# ---------------------------------------------------------------------------
# Field patterns
# ---------------------------------------------------------------------------

LABELS = {
    "invoice_number": r"invoice\s*(?:no\.?|number|num|#)|inv\s*(?:no\.?|#)|fatura\s*(?:no|numarası)|faktura\s*(?:no|nr)|rechnungs?(?:nummer|-?nr\.?)",
    "invoice_date": r"invoice\s*date|date\s*of\s*issue|issue\s*date|date\s*issued|fatura\s*tarihi|düzenleme\s*tarihi|rechnungsdatum|(?<![a-z])date|(?<![a-z])tarih",
    "due_date": r"due\s*date|payment\s*due|pay\s*by|son\s*ödeme(?:\s*tarihi)?|vade(?:\s*tarihi)?|fällig(?:keitsdatum)?",
    "subtotal": r"sub\s*-?\s*total|net\s*(?:amount|total)|total\s*excl\.?\s*(?:vat|tax)|ara\s*toplam|mal\s*hizmet\s*toplam|zwischensumme|nettobetrag",
    "tax_amount": r"(?:vat|tax|kdv|gst|mwst|ust)(?:\s*\(?\s*(?:%\s*\d{1,2}(?:[.,]\d+)?|\d{1,2}(?:[.,]\d+)?\s*%)\s*\)?)?(?:\s*amount|\s*total|\s*tutarı)?|hesaplanan\s*kdv|sales\s*tax",
    "total_amount": r"grand\s*total|total\s*(?:amount\s*)?due|amount\s*due|balance\s*due|total\s*incl\.?\s*(?:vat|tax)|invoice\s*total|genel\s*toplam|ödenecek\s*tutar|vergiler\s*dahil\s*toplam|gesamtbetrag|(?<![a-z])total",
}
SEP = r"\s*[:#]?\s*"


def _find_labeled(text: str, label: str, value: str, last: bool = False) -> Optional[str]:
    pattern = re.compile(rf"(?im)(?:{label}){SEP}({value})")
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    return (matches[-1] if last else matches[0]).group(1).strip()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class InvoiceParser:
    """Extract structured invoice data from PDF files using pdfplumber + heuristics."""

    def __init__(self, day_first: bool = True) -> None:
        self.day_first = day_first

    def parse(self, pdf_path: str) -> Invoice:
        """Parse a single PDF and return an Invoice dataclass."""
        path = Path(pdf_path)
        if not path.is_file():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        logger.info("Parsing: %s", path.name)
        text, tables = self._extract(path)
        invoice = self.parse_text(text, source_file=path.name, tables=tables)
        if not text.strip():
            invoice.warnings.append("No text layer found — the PDF is probably scanned and needs OCR.")
        return invoice

    def parse_text(self, text: str, source_file: str = "", tables: Optional[list[list[list[str]]]] = None) -> Invoice:
        """Parse already-extracted invoice text (also used by the tests)."""
        invoice = Invoice(source_file=source_file, raw_text=text)
        self._extract_fields(text, invoice)
        invoice.line_items = self._items_from_tables(tables or []) or self._extract_line_items(text)
        self._validate(invoice)
        return invoice

    @staticmethod
    def _extract(path: Path) -> tuple[str, list]:
        """Extract layout-preserving text and any ruled tables from every page."""
        import pdfplumber

        pages: list[str] = []
        tables: list = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text(layout=True) or page.extract_text() or "")
                try:
                    tables.extend(page.extract_tables())
                except Exception:  # malformed table geometry should not kill the parse
                    pass
        return "\n".join(pages), tables

    def _extract_fields(self, text: str, invoice: Invoice) -> None:
        invoice.invoice_number = self._invoice_number(text)
        invoice.invoice_date = normalize_date(_find_labeled(text, LABELS["invoice_date"], DATE_RE), self.day_first)
        invoice.due_date = normalize_date(_find_labeled(text, LABELS["due_date"], DATE_RE), self.day_first)
        if invoice.invoice_date and invoice.invoice_date == invoice.due_date:
            # the generic "date" label may have matched the due-date line
            invoice.invoice_date = normalize_date(self._first_date_excluding_due(text), self.day_first)

        invoice.subtotal = parse_amount(_find_labeled(text, LABELS["subtotal"], AMOUNT_RE))
        invoice.tax_amount = self._tax(text)
        invoice.total_amount = self._total(text)
        invoice.currency = detect_currency(text)

        iban_match = re.search(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?)\b", text)
        if iban_match:
            invoice.iban = re.sub(r"\s", "", iban_match.group(1))
            invoice.iban_valid = iban_is_valid(invoice.iban)

        invoice.vendor_name = self._vendor(text)

    @staticmethod
    def _invoice_number(text: str) -> Optional[str]:
        m = re.search(rf"(?im)(?:{LABELS['invoice_number']}){SEP}([A-Z0-9][A-Z0-9\-/_.]{{1,30}})", text)
        if not m:
            return None
        value = m.group(1).rstrip(".")
        return value if re.search(r"\d", value) else None

    @staticmethod
    def _first_date_excluding_due(text: str) -> Optional[str]:
        for line in text.splitlines():
            if re.search(LABELS["due_date"], line, re.I):
                continue
            m = re.search(DATE_RE, line)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _tax(text: str) -> Optional[float]:
        for line in text.splitlines():
            if re.search(r"(?i)\b(?:tax|vat|kdv|gst|mwst|ust)\s*(?:id|no\.?|number|reg|registration|office|dairesi|numarası)\b", line):
                continue
            m = re.search(rf"(?i)(?:{LABELS['tax_amount']}){SEP}({AMOUNT_RE})\s*$", line.strip())
            if m:
                return parse_amount(m.group(1))
        return None

    @staticmethod
    def _total(text: str) -> Optional[float]:
        """Prefer explicit grand-total labels; never match 'Subtotal'; take the last plain 'Total'."""
        lines = text.splitlines()
        strong = re.compile(rf"(?i)(?:grand\s*total|total\s*(?:amount\s*)?due|amount\s*due|balance\s*due|total\s*incl\.?\s*(?:vat|tax)|invoice\s*total|genel\s*toplam|ödenecek\s*tutar|vergiler\s*dahil\s*toplam|gesamtbetrag){SEP}({AMOUNT_RE})")
        plain = re.compile(rf"(?i)(?<![a-z])(?<!sub)(?<!sub\s)(?<!sub-)total(?!\s*excl){SEP}({AMOUNT_RE})")
        for line in lines:
            m = strong.search(line)
            if m:
                return parse_amount(m.group(1))
        candidates = [m.group(1) for line in lines if not re.search(r"(?i)sub\s*-?\s*total|ara\s*toplam", line)
                      for m in [plain.search(line)] if m]
        return parse_amount(candidates[-1]) if candidates else None

    @staticmethod
    def _vendor(text: str) -> Optional[str]:
        suffix = r"(?:Ltd\.?|Limited|LLC|Inc\.?|GmbH|AG|S\.?A\.?|B\.?V\.?|A\.Ş\.|A\.S\.|Ltd\.\s*Şti\.|Co\.?|Corp\.?|PLC|SRL|SAS)"
        m = re.search(r"(?im)^\s*(?:from|billed\s*by|seller|supplier|vendor|satıcı|company)\s*[:\-]\s*(.+?)\s*$", text)
        if m:
            return re.split(r"\s{2,}", m.group(1))[0].strip()
        m = re.search(rf"(?m)^\s*([A-ZÇĞİÖŞÜ][\w&.,'\- ]{{1,60}}?\s{suffix})(?=\s{{2,}}|\s*$)", text)
        if m:
            return " ".join(m.group(1).split())
        for line in text.splitlines()[:5]:  # first meaningful line heuristic
            cleaned = re.split(r"\s{2,}", line.strip())[0]
            if cleaned and not re.search(r"(?i)invoice|fatura|rechnung|\d{3,}", cleaned) and len(cleaned) > 2:
                return cleaned
        return None

    @staticmethod
    def _items_from_tables(tables: list[list[list[Optional[str]]]]) -> list[LineItem]:
        """Use pdfplumber's ruled tables when present: find a header row with description/qty/price/total."""
        items: list[LineItem] = []
        for table in tables:
            if not table or len(table) < 2:
                continue
            header = [(c or "").strip().lower() for c in table[0]]

            def col(*names: str) -> Optional[int]:
                for i, h in enumerate(header):
                    if any(n in h for n in names):
                        return i
                return None

            d = col("description", "item", "product", "service", "açıklama", "ürün", "hizmet", "beschreibung")
            q = col("qty", "quantity", "miktar", "adet", "menge")
            u = col("unit price", "price", "rate", "birim", "fiyat", "einzelpreis")
            t = col("total", "amount", "tutar", "toplam", "betrag")
            if d is None or t is None or t == u:
                continue
            for row in table[1:]:
                cells = [(c or "").strip() for c in row]
                if len(cells) <= max(i for i in (d, q, u, t) if i is not None):
                    continue
                desc = cells[d]
                total = parse_amount(cells[t])
                if not desc or total is None or re.search(r"(?i)sub\s*total|total|toplam|tax|vat|kdv", desc):
                    continue
                items.append(LineItem(
                    description=desc,
                    quantity=parse_amount(cells[q]) if q is not None else None,
                    unit_price=parse_amount(cells[u]) if u is not None else None,
                    total=total,
                ))
        return items

    @staticmethod
    def _extract_line_items(text: str) -> list[LineItem]:
        """
        Parse table-like rows from layout text.
        Primary pattern: description  qty  unit_price  total (columns separated by 2+ spaces).
        """
        items: list[LineItem] = []
        money = r"[£$€₺₹]?\s*-?\d[\d.,]*(?:\s*(?:TL|TRY|USD|EUR|GBP))?"
        row_pattern = re.compile(rf"^\s*(.+?)\s{{2,}}(\d+(?:[.,]\d+)?)\s{{2,}}({money})\s{{2,}}({money})\s*$", re.MULTILINE)
        skip = re.compile(r"(?i)sub\s*total|total|toplam|tax|vat|kdv|balance|amount\s*due|iban|date|tarih")
        for m in row_pattern.finditer(text):
            desc = m.group(1).strip()
            if skip.search(desc) or not re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]{2,}", desc):
                continue
            items.append(LineItem(
                description=desc,
                quantity=parse_amount(m.group(2)),
                unit_price=parse_amount(m.group(3)),
                total=parse_amount(m.group(4)),
            ))
        if items:
            return items

        # Fallback: description + amount rows, only inside the block that follows an item-table header.
        header = re.search(r"(?im)^.*\b(description|item|product|service|açıklama|hizmet)\b.*\b(amount|total|tutar)\b.*$", text)
        if not header:
            return items
        block = text[header.end():]
        fallback = re.compile(rf"^\s*([A-Za-zÇĞİÖŞÜçğıöşü][^\n]{{2,80}}?)\s{{3,}}({money})\s*$", re.MULTILINE)
        for m in fallback.finditer(block):
            desc = m.group(1).strip()
            if skip.search(desc):
                break  # reached the totals section
            amount = parse_amount(m.group(2))
            if amount is not None:
                items.append(LineItem(description=desc, total=amount))
        return items

    @staticmethod
    def _validate(inv: Invoice) -> None:
        """Cross-check numbers and flag anything suspicious instead of silently trusting regexes."""
        def close(a: float, b: float) -> bool:
            return abs(a - b) <= max(AMOUNT_TOLERANCE, abs(b) * 0.0005)

        if inv.total_amount is None:
            inv.warnings.append("Total amount not found.")
        if inv.subtotal is not None and inv.tax_amount is not None and inv.total_amount is not None:
            if not close(inv.subtotal + inv.tax_amount, inv.total_amount):
                inv.warnings.append(
                    f"subtotal + tax ({inv.subtotal + inv.tax_amount:.2f}) != total ({inv.total_amount:.2f})"
                )
        items_with_total = [i.total for i in inv.line_items if i.total is not None]
        if items_with_total and inv.subtotal is not None and not close(sum(items_with_total), inv.subtotal):
            inv.warnings.append(f"line items sum ({sum(items_with_total):.2f}) != subtotal ({inv.subtotal:.2f})")
        for item in inv.line_items:
            if None not in (item.quantity, item.unit_price, item.total) and not close(item.quantity * item.unit_price, item.total):
                inv.warnings.append(f"line '{item.description[:40]}': qty x unit price != line total")
        if inv.iban and inv.iban_valid is False:
            inv.warnings.append(f"IBAN {inv.iban} fails checksum validation.")
        if inv.invoice_date and inv.due_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", inv.invoice_date) \
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", inv.due_date) and inv.due_date < inv.invoice_date:
            inv.warnings.append("Due date is before invoice date.")


# ---------------------------------------------------------------------------
# Batch processing & export
# ---------------------------------------------------------------------------

def parse_directory(directory: str, recursive: bool = False, day_first: bool = True) -> tuple[list[Invoice], list[tuple[str, str]]]:
    """Parse all PDFs in a directory. Returns (invoices, failures[(file, error)])."""
    parser = InvoiceParser(day_first=day_first)
    pattern = "**/*.pdf" if recursive else "*.pdf"
    invoices: list[Invoice] = []
    failures: list[tuple[str, str]] = []
    files = sorted(p for p in Path(directory).glob(pattern) if p.suffix.lower() == ".pdf")
    files += sorted(p for p in Path(directory).glob(pattern.replace(".pdf", ".PDF")) if p not in files)
    for pdf_path in files:
        try:
            invoices.append(parser.parse(str(pdf_path)))
        except Exception as exc:
            logger.error("Failed to parse %s: %s", pdf_path.name, exc)
            failures.append((str(pdf_path), str(exc)))
    return invoices, failures


def to_dicts(invoices: list[Invoice], include_raw: bool) -> list[dict]:
    out = []
    for inv in invoices:
        d = asdict(inv)
        if not include_raw:
            d.pop("raw_text", None)
        out.append(d)
    return out


def write_csv(invoices: list[Invoice], path: Path) -> None:
    """One row per invoice (line items summarised) — easy to open in Excel."""
    cols = ["source_file", "vendor_name", "invoice_number", "invoice_date", "due_date", "currency",
            "subtotal", "tax_amount", "total_amount", "iban", "iban_valid", "line_item_count", "warnings"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for inv in invoices:
            row = {c: getattr(inv, c, None) for c in cols}
            row["line_item_count"] = len(inv.line_items)
            row["warnings"] = " | ".join(inv.warnings)
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="PDF Invoice Parser")
    parser.add_argument("input", help="PDF file or directory containing PDFs")
    parser.add_argument("--output", default="", help="Save results to this file (.json or .csv)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON to stdout")
    parser.add_argument("--recursive", action="store_true", help="Include PDFs in sub-directories")
    parser.add_argument("--include-raw", action="store_true", help="Include the extracted raw text in JSON output")
    parser.add_argument("--month-first", action="store_true", help="Read ambiguous dates as MM/DD/YYYY (US)")
    parser.add_argument("--strict", action="store_true", help="Exit with code 2 if any invoice has warnings")
    args = parser.parse_args()

    input_path = Path(args.input)
    failures: list[tuple[str, str]] = []
    if input_path.is_dir():
        invoices, failures = parse_directory(str(input_path), args.recursive, day_first=not args.month_first)
    elif input_path.suffix.lower() == ".pdf":
        try:
            invoices = [InvoiceParser(day_first=not args.month_first).parse(str(input_path))]
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1)
    else:
        print(f"Error: {input_path} is not a PDF or directory.", file=sys.stderr)
        raise SystemExit(1)

    for inv in invoices:
        for w in inv.warnings:
            logger.warning("%s: %s", inv.source_file, w)

    data = to_dicts(invoices, include_raw=args.include_raw)
    if args.output:
        out = Path(args.output)
        if out.suffix.lower() == ".csv":
            write_csv(invoices, out)
        else:
            out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Results saved to %s", out)

    print(json.dumps(data, indent=2 if args.pretty else None, ensure_ascii=False))

    if failures:
        logger.error("%d file(s) could not be parsed.", len(failures))
        raise SystemExit(1)
    if args.strict and any(inv.warnings for inv in invoices):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
