from pathlib import Path

import pytest

from parser import InvoiceParser, iban_is_valid, normalize_date, parse_amount

US_INVOICE = """
Globex Corporation Inc.                         INVOICE
Tax ID: 12-3456789
Invoice #: 2024-0042
Date: May 3, 2024
Due Date: 06/02/2024

Description                      Qty      Unit Price       Amount
Consulting hours                 10       $150.00          $1,500.00
Travel expenses                  1        $320.50          $320.50

Subtotal:                                                  $1,820.50
Sales Tax (8.875%):                                        $161.57
Total:                                                     $1,982.07
"""

TR_INVOICE = """
Örnek Yazılım A.Ş.
Vergi Dairesi: Kadıköy   Vergi No: 1234567890
Fatura No: ABC2024000000123
Fatura Tarihi: 15.03.2024
Son Ödeme Tarihi: 14.04.2024

Açıklama                         Miktar    Birim Fiyat     Tutar
Yazılım lisansı                  2         1.250,00 TL     2.500,00 TL
Kurulum hizmeti                  1         500,00 TL       500,00 TL

Ara Toplam:                                                3.000,00 TL
Hesaplanan KDV (%20):                                      600,00 TL
Genel Toplam:                                              3.600,00 TL
IBAN: TR33 0006 1005 1978 6457 8413 26
"""


@pytest.mark.parametrize(
    "raw,expected",
    [("$1,982.07", 1982.07), ("3.600,00 TL", 3600.0), ("GBP 1,400.00", 1400.0), ("1 234,50 €", 1234.5), ("-12.50", -12.5), ("", None)],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize(
    "raw,day_first,expected",
    [
        ("15.03.2024", True, "2024-03-15"),
        ("06/02/2024", False, "2024-06-02"),
        ("06/02/2024", True, "2024-02-06"),
        ("2024-05-01", True, "2024-05-01"),
        ("May 3, 2024", True, "2024-05-03"),
        ("3 May 2024", True, "2024-05-03"),
        ("15 Mart 2024", True, "2024-03-15"),
        ("31/02/2024", True, "31/02/2024"),  # impossible date is kept verbatim
    ],
)
def test_normalize_date(raw, day_first, expected):
    assert normalize_date(raw, day_first) == expected


def test_iban_checksum():
    assert iban_is_valid("GB29 NWBK 6016 1331 9268 19")
    assert iban_is_valid("TR330006100519786457841326")
    assert not iban_is_valid("GB29NWBK60161331926818")


def test_us_invoice():
    inv = InvoiceParser(day_first=False).parse_text(US_INVOICE, "us.txt")
    assert inv.vendor_name == "Globex Corporation Inc."
    assert inv.invoice_number == "2024-0042"
    assert inv.invoice_date == "2024-05-03"
    assert inv.due_date == "2024-06-02"
    assert inv.currency == "USD"
    assert (inv.subtotal, inv.tax_amount, inv.total_amount) == (1820.50, 161.57, 1982.07)
    assert [i.description for i in inv.line_items] == ["Consulting hours", "Travel expenses"]
    assert inv.line_items[0].quantity == 10 and inv.line_items[0].total == 1500.0
    assert inv.warnings == []


def test_turkish_invoice():
    inv = InvoiceParser().parse_text(TR_INVOICE, "tr.txt")
    assert inv.vendor_name == "Örnek Yazılım A.Ş."
    assert inv.invoice_number == "ABC2024000000123"
    assert inv.invoice_date == "2024-03-15"
    assert inv.due_date == "2024-04-14"
    assert inv.currency == "TRY"
    assert (inv.subtotal, inv.tax_amount, inv.total_amount) == (3000.0, 600.0, 3600.0)
    assert inv.iban == "TR330006100519786457841326" and inv.iban_valid
    assert len(inv.line_items) == 2 and inv.line_items[0].unit_price == 1250.0
    assert inv.warnings == []


def test_subtotal_is_never_taken_as_total():
    inv = InvoiceParser().parse_text("Subtotal: 100.00\nVAT: 20.00\nTotal: 120.00\n")
    assert inv.total_amount == 120.0 and inv.subtotal == 100.0


def test_validation_flags_mismatch():
    inv = InvoiceParser().parse_text("Subtotal: 100.00\nVAT: 20.00\nTotal: 150.00\n")
    assert any("!= total" in w for w in inv.warnings)


def test_missing_total_is_reported():
    inv = InvoiceParser().parse_text("Hello world")
    assert "Total amount not found." in inv.warnings


def test_table_extraction():
    tables = [[["Description", "Qty", "Unit Price", "Total"], ["Widget", "2", "5.00", "10.00"], ["Subtotal", "", "", "10.00"]]]
    inv = InvoiceParser().parse_text("Subtotal: 10.00\nTotal: 10.00", tables=tables)
    assert len(inv.line_items) == 1 and inv.line_items[0].total == 10.0


def test_real_pdf_roundtrip(tmp_path):
    pytest.importorskip("fpdf")
    pytest.importorskip("pdfplumber")
    sys_path = Path(__file__).resolve().parent.parent / "samples"
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen", sys_path / "generate_sample.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    pdf = gen.build(tmp_path / "inv.pdf")

    inv = InvoiceParser().parse(str(pdf))
    assert inv.vendor_name == "Acme Digital Ltd"
    assert inv.invoice_number == "INV-2024-001"
    assert (inv.subtotal, inv.tax_amount, inv.total_amount) == (1400.0, 280.0, 1680.0)
    assert inv.iban_valid and len(inv.line_items) == 3
    assert inv.warnings == []
