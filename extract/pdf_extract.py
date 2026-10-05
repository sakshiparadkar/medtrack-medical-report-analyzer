"""
Report text extraction.

1. Digital (text-based) PDFs  -> pdfplumber (accurate, no OCR errors).
2. Scanned PDFs               -> each page is rendered to an image and read with OCR.
3. Photos (JPG / PNG)         -> read with OCR after a quick clean-up.

OCR needs the Tesseract program installed on the computer (see README).
Digital PDFs never touch OCR, so they work even without Tesseract.
"""

import os

import pdfplumber
from PIL import Image, ImageOps

# A page with fewer characters than this is treated as "scanned" and sent to OCR.
MIN_TEXT_CHARS = 40
# Safety limit so a huge scanned file cannot freeze the app.
MAX_OCR_PAGES = 10

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}

# --psm 6 = "one block of text", which keeps lab-report rows on a single line.
OCR_CONFIG = "--oem 3 --psm 6"


class OCRUnavailableError(Exception):
    """Raised when a file needs OCR but Tesseract is not installed."""


def _get_tesseract():
    """Import pytesseract and point it at the Tesseract program."""
    try:
        import pytesseract
    except ImportError as e:
        raise OCRUnavailableError("The pytesseract package is not installed.") from e

    custom_path = os.environ.get("TESSERACT_CMD")
    windows_default = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if custom_path:
        pytesseract.pytesseract.tesseract_cmd = custom_path
    elif os.name == "nt" and os.path.exists(windows_default):
        pytesseract.pytesseract.tesseract_cmd = windows_default

    try:
        pytesseract.get_tesseract_version()
    except Exception as e:
        raise OCRUnavailableError("Tesseract is not installed on this computer.") from e

    return pytesseract


def _prepare_image(img: Image.Image) -> Image.Image:
    """Fix rotation, remove colour and boost contrast so OCR reads better."""
    img = ImageOps.exif_transpose(img)          # phone photos are often rotated
    img = img.convert("L")                      # grayscale
    img = ImageOps.autocontrast(img)            # stronger black text on white

    # Tesseract works best when text is large enough; scale small photos up.
    if img.width < 1600:
        scale = 1600 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img


def _ocr_image(img: Image.Image) -> str:
    pytesseract = _get_tesseract()
    return pytesseract.image_to_string(_prepare_image(img), config=OCR_CONFIG)


def extract_text_from_image(filepath: str) -> str:
    """Read text from a JPG / PNG photo of a report."""
    with Image.open(filepath) as img:
        return _ocr_image(img)


def extract_text_from_pdf(filepath: str) -> str:
    """
    Extract raw text from a PDF, page by page.
    Pages with no readable text (scans) fall back to OCR.
    """
    full_text = []
    ocr_pages_used = 0
    ocr_missing = False

    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""

            if len(page_text.strip()) < MIN_TEXT_CHARS and ocr_pages_used < MAX_OCR_PAGES:
                try:
                    page_image = page.to_image(resolution=300).original
                    page_text = _ocr_image(page_image)
                    ocr_pages_used += 1
                except OCRUnavailableError:
                    # No Tesseract: keep whatever digital text this page had.
                    ocr_missing = True

            full_text.append(page_text)

    text = "\n".join(full_text)

    # Only complain if the PDF is a real scan (no digital text at all).
    if ocr_missing and not text.strip():
        raise OCRUnavailableError("This PDF is a scan and Tesseract is not installed.")

    return text


def extract_text(filepath: str) -> str:
    """Pick the right reader for the uploaded file."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return extract_text_from_image(filepath)
    return extract_text_from_pdf(filepath)