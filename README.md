# Automated Invoice Data Extraction

A fast, completely offline PDF parser that automatically extracts structured vendor data, line items, and totals from invoices into clean JSON.

✔ Eliminates hours of manual data entry for accounting and finance teams
✔ Protects sensitive financial data by processing everything 100% locally with zero API dependencies
✔ Scales instantly from processing a single file to thousands of invoices in bulk

## Use Cases
- **Accounting Automation:** Automatically parse incoming vendor invoices and sync them directly into your accounting software.
- **Expense Management:** Extract and verify line items from employee receipts and PDF bills instantly.
- **Data Migration:** Digitize thousands of historical PDF invoices into a structured database format.

## Project Structure

```
pdf-invoice-parser/
├── parser.py           # Core parsing logic
├── requirements.txt
└── .env.example
```

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Parse a single invoice
python parser.py invoice.pdf --pretty

# Parse all PDFs in a directory
python parser.py ./invoices/ --pretty

# Save output to file
python parser.py invoice.pdf --output result.json --pretty
```

## Example Output

```json
[
  {
    "source_file": "invoice_2024_001.pdf",
    "vendor_name": "Acme Corp Ltd",
    "invoice_number": "INV-2024-001",
    "invoice_date": "2024-05-01",
    "due_date": "2024-05-31",
    "currency": "USD",
    "subtotal": "1200.00",
    "tax_amount": "216.00",
    "total_amount": "1416.00",
    "iban": "GB29NWBK60161331926819",
    "line_items": [
      {
        "description": "Web Development Service",
        "quantity": "1",
        "unit_price": "1200.00",
        "total": "1200.00"
      }
    ],
    "raw_text": "..."
  }
]
```

## Extending

Add custom regex patterns to the `PATTERNS` dict in `parser.py` to extract additional fields specific to your invoice format.

## Tech Stack

`pdfplumber` · `python-dotenv`

## Screenshot

![Preview](screenshots/preview.png)

