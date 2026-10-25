"""
Make sample hospital bills (text PDFs) for demoing Verified causes.

    python scripts/make_demo_bills.py

Writes to data/demo-bills/ (not committed):
  real-bill.pdf   matches the campaign below: every check passes
  fake-bill.pdf   someone else's, smaller bill: the patient and amount checks fail

Each run uses a new bill number, so a fresh file never trips "bill not used before".
Campaign to type in the form:
  Patient: Sam Lee · Hospital: City General Hospital · Amount needed: 5000
"""
from __future__ import annotations

import secrets
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "demo-bills"


def pdf(lines: list[tuple[int, str]]) -> bytes:
    """A one-page PDF with real text (readable without a vision model). lines: (size, text)."""
    ops, y = ["BT"], 790
    for size, text in lines:
        y -= size + 8
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"/F1 {size} Tf 1 0 0 1 56 {y} Tm ({safe}) Tj")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def bill(hospital: str, patient: str, items: list[tuple[str, float]]) -> bytes:
    total = sum(a for _, a in items)
    today = date.today()
    lines = [(18, hospital.upper()), (10, "1200 Health Way, Springfield  |  (555) 010-2000"),
             (14, "PATIENT BILLING STATEMENT"),
             (11, f"Statement no: CGH-{today:%Y}-{secrets.randbelow(900000) + 100000}"),
             (11, f"Statement date: {today:%B} {today.day}, {today.year}"),
             (11, f"Patient: {patient}"), (11, "Account: 0048213"), (11, ""),
             (12, "Services")]
    lines += [(11, f"{name}    ${amount:,.2f}") for name, amount in items]
    lines += [(11, ""), (13, f"TOTAL AMOUNT DUE: ${total:,.2f}"),
              (10, "Please pay by the due date shown on your account. Thank you.")]
    return pdf(lines)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "real-bill.pdf").write_bytes(bill("City General Hospital", "Sam Lee", [
        ("Emergency room visit", 850.00), ("Appendectomy (surgery)", 2400.00),
        ("Anesthesia", 600.00), ("Hospital stay, 2 nights", 1100.00), ("Medication", 150.00)]))
    (OUT / "fake-bill.pdf").write_bytes(bill("City General Hospital", "Jordan Park", [
        ("Outpatient consultation", 300.00), ("Blood tests", 180.00), ("X-ray", 220.00)]))
    print(f"Wrote {OUT / 'real-bill.pdf'} and {OUT / 'fake-bill.pdf'}")
    print("Form: patient 'Sam Lee', hospital 'City General Hospital', amount needed 5000")


if __name__ == "__main__":
    main()
