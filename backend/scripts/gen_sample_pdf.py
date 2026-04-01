"""
Generate a small multi-page digital PDF for Smart Find tests (embedded text + bboxes).
Run from repo root:  python backend/scripts/gen_sample_pdf.py
Output: backend/scripts/sample_digital_form.pdf
"""
from __future__ import annotations

import sys
from pathlib import Path

import fitz  # PyMuPDF

OUT = Path(__file__).resolve().parent / "sample_digital_form.pdf"


def main() -> None:
    doc = fitz.open()
    # Page 1 — header + policy + name
    p1 = doc.new_page(width=612, height=792)
    p1.insert_text((72, 80), "Insurance enrollment form", fontsize=14)
    p1.insert_text((72, 120), "Policy Number: POL-12345678", fontsize=11)
    p1.insert_text((72, 160), "Member Name: Alex R. Sample", fontsize=11)
    p1.insert_text((72, 200), "Plan: Gold PPO", fontsize=10)
    # Page 2 — DOB
    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((72, 100), "Additional information", fontsize=14)
    p2.insert_text((72, 160), "Date of Birth: 01/15/1990", fontsize=11)
    p2.insert_text((72, 220), "Signature: ___________________", fontsize=10)
    # Page 3 — alternate id in footer band
    p3 = doc.new_page(width=612, height=792)
    p3.insert_text((72, 80), "Subscriber copy", fontsize=12)
    p3.insert_text((72, 680), "Subscriber ID / Alt: SUB-887766", fontsize=10)

    doc.save(OUT)
    doc.close()
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
    sys.exit(0)
