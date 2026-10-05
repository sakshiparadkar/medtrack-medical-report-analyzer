"""
Generates 6 sample lab report PDFs simulating one patient's test history
over time, so the dashboard has enough data points to show real trends.
Run: python generate_samples.py
"""

import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

# (report_date, hemoglobin, wbc, platelets, glucose, cholesterol, creatinine, tsh, vitd)
REPORTS = [
    ("2025-09-05", 14.2, 6.8, 260, 94,  185, 0.9, 2.1, 28),
    ("2025-11-12", 13.6, 7.1, 245, 98,  192, 0.9, 2.4, 24),
    ("2026-01-18", 12.9, 7.4, 230, 103, 205, 1.0, 2.9, 19),
    ("2026-03-22", 12.1, 8.0, 210, 111, 214, 1.1, 3.6, 16),
    ("2026-05-30", 11.4, 8.6, 195, 121, 228, 1.2, 4.8, 14),
    ("2026-07-15", 11.0, 9.1, 180, 128, 236, 1.3, 5.6, 12),
]


def make_report(path, date, hb, wbc, plt, glu, chol, crea, tsh, vitd):
    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4
    y = height - 30 * mm

    def line(text, size=10, bold=False, gap=6.5 * mm):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(25 * mm, y, text)
        y -= gap

    line("MedTrack Diagnostics Laboratory", 14, bold=True, gap=8 * mm)
    line("123 Health Avenue, Karachi", 9, gap=6 * mm)
    line(f"Patient: Demo Patient   |   Patient ID: DP-1001", 10)
    line(f"Report Date: {date}", 10, gap=10 * mm)

    line("COMPLETE BLOOD COUNT (CBC)", 11, bold=True, gap=7 * mm)
    line(f"Hemoglobin: {hb} g/dL (Normal: 12.0-16.0)")
    line(f"WBC Count: {wbc} K/uL (Normal: 4.0-11.0)")
    line(f"Platelet Count: {plt} K/uL (Normal: 150-450)", gap=10 * mm)

    line("BIOCHEMISTRY", 11, bold=True, gap=7 * mm)
    line(f"Glucose (Fasting): {glu} mg/dL (Normal: 70-100)")
    line(f"Total Cholesterol: {chol} mg/dL (Normal: 0-200)")
    line(f"Creatinine: {crea} mg/dL (Normal: 0.6-1.3)", gap=10 * mm)

    line("HORMONES", 11, bold=True, gap=7 * mm)
    line(f"TSH: {tsh} uIU/mL (Normal: 0.4-4.0)")
    line(f"Vitamin D: {vitd} ng/mL (Normal: 30-100)", gap=10 * mm)

    line("-- End of Report --", 9, gap=6 * mm)
    line("This is a system-generated sample report for FYP demo purposes.", 8)

    c.save()


if __name__ == "__main__":
    for i, row in enumerate(REPORTS, start=1):
        date = row[0]
        fname = os.path.join(OUT_DIR, f"sample_report_{i}_{date}.pdf")
        make_report(fname, *row)
        print(f"Created {fname}")

    # One deliberately messy report to demo the "needs_review" flag
    messy_path = os.path.join(OUT_DIR, "sample_report_edgecase.pdf")
    c = canvas.Canvas(messy_path, pagesize=A4)
    width, height = A4
    y = height - 30 * mm
    c.setFont("Helvetica-Bold", 14)
    c.drawString(25 * mm, y, "MedTrack Diagnostics Laboratory")
    y -= 12 * mm
    c.setFont("Helvetica", 10)
    c.drawString(25 * mm, y, "Patient: Demo Patient | Report Date: 2026-08-10")
    y -= 10 * mm
    c.drawString(25 * mm, y, "Hemoglobin 10.2")  # no unit -> needs_review
    y -= 8 * mm
    c.drawString(25 * mm, y, "Glucose (Fasting): 250 mg/dL (Normal: 70-100)")  # abnormal
    y -= 8 * mm
    c.drawString(25 * mm, y, "TSH: 1.8 uIU/mL (Normal: 0.4-4.0)")  # normal
    c.save()
    print(f"Created {messy_path}")
