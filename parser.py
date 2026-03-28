"""
PDF Invoice Parser
Extracts structured fields (vendor, date, total, IBAN, line items)
from PDF invoices and returns clean JSON output.
"""

import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import pdfplumber
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class LineItem:
    description: str
    quantity: Optional[str] = None
    unit_price: Optional[str] = None
    total: Optional[str] = None


@dataclass
class Invoice:
    source_file: str
    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    due_date: Optional[str] = None
    currency: Optional[str] = None
    subtotal: Optional[str] = None
    tax_amount: Optional[str] = None
    total_amount: Optional[str] = None
    iban: Optional[str] = None
    line_items: list[LineItem] = field(default_factory=list)
    raw_text: str = ""


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

PATTERNS: dict[str, str] = {
    "invoice_number": r"(?i)(?:invoice\s*(?:no|number|#)|faktura\s*no)[:\s#]*([A-Z0-9\-\/]+)",
    "invoice_date": (
        r"(?i)(?:invoice\s*date|date\s*of\s*issue|tarih)[:\s]*"
        r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{2}[\/\-\.]\d{2})"
    ),
    "due_date": (
        r"(?i)(?:due\s*date|payment\s*due|son\s*ödeme)[:\s]*"
        r"(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\d{4}[\/\-\.]\d{2}[\/\-\.]\d{2})"
    ),
    "total_amount": r"(?i)(?:total|grand\s*total|genel\s*toplam|amount\s*due)[:\s]*([£$€₺₹]?\s*[\d,\.]+)",
    "subtotal": r"(?i)(?:subtotal|sub\s*total|ara\s*toplam)[:\s]*([£$€₺₹]?\s*[\d,\.]+)",
    "tax_amount": r"(?i)(?:tax|vat|kdv|gst)[:\s]*([£$€₺₹]?\s*[\d,\.]+)",
    "iban": r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}([A-Z0-9]?){0,16}\b",
    "currency": r"(?:USD|EUR|GBP|TRY|INR|CAD|AUD|CHF|JPY|[£$€₺₹])",
}

VENDOR_KEYWORDS = [
    r"(?i)(?:from|billed\s*by|company|vendor|satıcı)[:\s]+([A-Z][A-Za-z\s&,\.]+(?:Ltd|LLC|Inc|GmbH|A\.Ş\.|A\.S\.)?)",
    r"^([A-Z][A-Za-z\s&]+(?:Ltd|LLC|Inc|GmbH|A\.Ş\.))",  # First line heuristic
]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class InvoiceParser:
    """Extract structured invoice data from PDF files using pdfplumber + regex."""

    def parse(self, pdf_path: str) -> Invoice:
        """Parse a single PDF and return an Invoice dataclass."""
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        logger.info("Parsing: %s", path.name)
        text = self._extract_text(path)
        invoice = Invoice(source_file=path.name, raw_text=text)

        self._extract_fields(text, invoice)
        invoice.line_items = self._extract_line_items(text)

        return invoice

    def _extract_text(self, path: Path) -> str:
        """Extract all text from a PDF, joining pages with newlines."""
        pages: list[str] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                pages.append(page_text)
        return "\n".join(pages)

    def _extract_fields(self, text: str, invoice: Invoice) -> None:
        """Apply regex patterns to populate Invoice fields."""
        for field_name, pattern in PATTERNS.items():
            match = re.search(pattern, text)
            if match:
                value = match.group(1).strip() if match.lastindex else match.group(0).strip()
                setattr(invoice, field_name, value)

        # Vendor name — try multiple heuristics
        for pattern in VENDOR_KEYWORDS:
            match = re.search(pattern, text, re.MULTILINE)
            if match:
                invoice.vendor_name = match.group(1).strip()
                break

    def _extract_line_items(self, text: str) -> list[LineItem]:
        """
        Attempt to parse a table-like line-item section.
        Pattern: description  qty  unit_price  total
        """
        items: list[LineItem] = []
        # Look for rows that end with a monetary amount
        row_pattern = re.compile(
            r"^(.+?)\s{2,}(\d+(?:[,.]\d+)?)\s{2,}([£$€₺₹]?\s*[\d,\.]+)\s{2,}([£$€₺₹]?\s*[\d,\.]+)\s*$",
            re.MULTILINE,
        )
        for m in row_pattern.finditer(text):
            items.append(
                LineItem(
                    description=m.group(1).strip(),
                    quantity=m.group(2).strip(),
                    unit_price=m.group(3).strip(),
                    total=m.group(4).strip(),
                )
            )

        # Fallback: simple two-column (description + total)
        if not items:
            fallback = re.compile(
                r"^([A-Za-z][A-Za-z0-9 \-\/]{3,})\s{3,}([£$€₺₹]?\s*[\d,\.]{3,})\s*$",
                re.MULTILINE,
            )
            for m in fallback.finditer(text):
                items.append(LineItem(description=m.group(1).strip(), total=m.group(2).strip()))

        return items


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

def parse_directory(directory: str, output_file: str = "") -> list[dict]:
    """Parse all PDFs in a directory and return a list of invoice dicts."""
    parser = InvoiceParser()
    results: list[dict] = []

    for pdf_path in Path(directory).glob("*.pdf"):
        try:
            invoice = parser.parse(str(pdf_path))
            results.append(asdict(invoice))
        except Exception as exc:
            logger.error("Failed to parse %s: %s", pdf_path.name, exc)

    if output_file:
        out_path = Path(output_file)
        out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Results saved to %s", out_path)

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="PDF Invoice Parser")
    parser.add_argument("input", help="PDF file or directory containing PDFs")
    parser.add_argument("--output", default="", help="Save JSON output to this file")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON to stdout")
    args = parser.parse_args()

    input_path = Path(args.input)
    invoice_parser = InvoiceParser()

    if input_path.is_dir():
        results = parse_directory(str(input_path), output_file=args.output)
    elif input_path.suffix.lower() == ".pdf":
        invoice = invoice_parser.parse(str(input_path))
        results = [asdict(invoice)]
        if args.output:
            Path(args.output).write_text(
                json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
            )
    else:
        print(f"Error: {input_path} is not a PDF or directory.", file=sys.stderr)
        raise SystemExit(1)

    output = json.dumps(results, indent=2 if args.pretty else None, ensure_ascii=False)
    print(output)


if __name__ == "__main__":
    main()
