# MedTrack — Medical Report Digitization & Health Trend Analytics

Final Year Project. Upload PDF lab reports, automatically extract structured
test values using rule-based NLP (regex + a reference list), validate them
against normal ranges, and visualize trends over time with interactive
Plotly charts.

## Features
- Upload PDF lab reports and get automatic structured extraction
- Data-quality flags: `normal` / `abnormal` / `needs_review`
- **Percentage change indicator** — each test value shows % change vs. the previous report
- **Trending alerts** — dashboard flags any test moving consistently up/down over the last 3 reports
- **Trend charts & comparison bar chart** — interactive Plotly visualizations
- **Compare two reports side-by-side** — pick any two reports and see every test's change
- **PDF health summary export** — one-click downloadable summary (latest values + trend table)
- Filter all reports by test name

## Tech Stack
- **Backend + Frontend**: Flask + Jinja2 templates (Bootstrap 5 + custom CSS)
- **Database**: SQLite (via Flask-SQLAlchemy)
- **Auth**: Flask-Login
- **PDF text extraction**: pdfplumber
- **Structured extraction**: regex + reference-list matching (`extract/nlp_extract.py`)
- **Charts**: Plotly

## Project Structure
```
medtrack/
├── app.py                      # routes, auth, dashboard, chart building
├── models.py                   # SQLAlchemy models (User, Report, TestResult)
├── extract/
│   ├── pdf_extract.py          # pdfplumber text extraction
│   └── nlp_extract.py          # regex + reference-list structured extraction
├── data/
│   └── test_reference.json     # known test names, aliases, units, normal ranges
├── templates/                  # Jinja2 HTML templates
├── static/css/style.css        # design system
├── sample_reports/
│   └── generate_samples.py     # generates demo PDF lab reports
├── instance/                   # SQLite db + uploaded files (auto-created)
└── requirements.txt
```

## Setup — Step by Step

### 1. Create a virtual environment
```bash
cd medtrack
python3 -m venv venv

# Activate it
source venv/bin/activate        # macOS/Linux
venv\Scripts\activate           # Windows
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Generate sample lab report PDFs (for testing/demo)
```bash
cd sample_reports
python generate_samples.py
cd ..
```
This creates 6 dated sample reports + 1 "edge case" report in `sample_reports/`.

### 4. Run the app
```bash
python app.py
```
The app will create `instance/medtrack.db` automatically on first run.

### 5. Open in browser
Go to **http://127.0.0.1:5000**

### 6. Try it out
1. Sign up for an account.
2. Go to **Upload**, pick a report date, and upload one of the PDFs from
   `sample_reports/` (upload them in date order for the best trend demo).
3. After uploading 2+ reports, go to **Dashboard** to see trend charts.
4. Upload `sample_report_edgecase.pdf` to see the "needs review" flag
   (missing unit) and an "abnormal" flag (high glucose) in action.

## Why this approach (for viva)
- **pdfplumber over OCR**: sample/demo reports are digital PDFs, so direct
  text extraction is far more accurate than OCR — no recognition errors.
- **Regex + reference list over a trained ML model**: lab reports are
  semi-structured (`test name: value unit (range)`), so a predictable
  rule-based pipeline gets high, explainable accuracy without needing a
  large labeled training dataset.
- **Confidence/validation flags**: every extracted value is checked against
  its reference range (`normal` / `abnormal`) or flagged `needs_review` when
  the unit couldn't be confidently parsed — this is a basic data-quality
  layer, acknowledging that extraction isn't always perfect.

## Known Limitations / Future Scope
- Only digital text-based PDFs are supported (scanned image reports would
  need Tesseract OCR + OpenCV preprocessing — noted as future scope).
- Single-user-per-report model (no admin role) — the `User` foreign key on
  `Report` already supports extending to multi-user/admin views.
- Test reference list currently covers ~25 common tests; easily extendable
  by adding entries to `data/test_reference.json`.
