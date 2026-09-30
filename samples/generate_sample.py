"""Generate a sample invoice PDF for trying the parser: python samples/generate_sample.py"""

from pathlib import Path

from fpdf import FPDF

ROWS = [
    ("Web Development Service", "1", "1200.00", "1200.00"),
    ("Hosting (12 months)", "12", "15.00", "180.00"),
    ("SSL Certificate", "1", "20.00", "20.00"),
]


def build(path: Path) -> Path:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Acme Digital Ltd", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    for line in [
        "221B Baker Street, London",
        "",
        "Invoice No: INV-2024-001",
        "Invoice Date: 01/05/2024",
        "Due Date: 31/05/2024",
        "Bill To: Globex Corporation",
        "",
    ]:
        pdf.cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")

    widths = (90, 25, 35, 35)
    pdf.set_font("Helvetica", "B", 10)
    for w, h in zip(widths, ("Description", "Qty", "Unit Price", "Total")):
        pdf.cell(w, 8, h, border=1)
    pdf.ln()
    pdf.set_font("Helvetica", size=10)
    for row in ROWS:
        for w, value in zip(widths, row):
            pdf.cell(w, 8, value, border=1)
        pdf.ln()
    pdf.ln(4)
    for label, value in [("Subtotal:", "GBP 1,400.00"), ("VAT (20%):", "GBP 280.00"), ("Grand Total:", "GBP 1,680.00")]:
        pdf.cell(150, 7, label, align="R")
        pdf.cell(35, 7, value, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.cell(0, 6, "IBAN: GB29 NWBK 6016 1331 9268 19", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, "VAT No: GB123456789", new_x="LMARGIN", new_y="NEXT")
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))
    return path


if __name__ == "__main__":
    out = build(Path(__file__).resolve().parent / "sample_invoice.pdf")
    print(f"Wrote {out}")
