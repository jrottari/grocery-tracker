"""
pdf_scraper.py — Parse PDF flyers for stores not well-covered by Flipp.

Targets: BJ's Wholesale, H Mart, Lidl (Costco has a PDF flyer too).
Strategy:
  1. Try pdfplumber text extraction (fast, works for digital PDFs)
  2. If text is sparse/garbled, fall back to pytesseract OCR on rasterized pages
  3. Use a regex + LLM-assisted parser to extract (product, price) pairs

Dependencies:
  pip install pdfplumber pytesseract pillow pdf2image requests
  System: poppler-utils, tesseract-ocr
"""

import re
import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from datetime import date, timedelta

import requests
import pdfplumber
from PIL import Image

logger = logging.getLogger(__name__)

# ── Store-specific PDF sources ────────────────────────────────────────────────
# Update URLs weekly or use a scheduler to pull the latest.

PDF_STORES = {
    "bjs": {
        "name":           "BJ's Wholesale Club",
        "chain":          "bjs",
        "flyer_url":      "https://www.bjs.com/content/dam/bjs-website/circular",
        "zip":            "27513",  # Cary, NC location
        "scrape_method":  "pdf",
    },
    "lidl": {
        "name":           "Lidl",
        "chain":          "lidl",
        "flyer_url":      "https://www.lidl.com/en_US/specials.htm",
        "zip":            "27606",
        "scrape_method":  "pdf",
    },
    "hmart": {
        "name":           "H Mart",
        "chain":          "hmart",
        "flyer_url":      "https://www.hmart.com/weekly-sale",
        "zip":            "27519",
        "scrape_method":  "pdf",
    },
}

# ── Price regex patterns ───────────────────────────────────────────────────────
# Handles: $2.99  •  2/$5  •  $1.99/lb  •  Buy 2 Get 1  •  .99
PRICE_PATTERNS = [
    # "2 for $5.00" or "2/$5"
    re.compile(r"(\d+)\s*(?:for|\/)\s*\$?\s*([\d]+\.?\d*)"),
    # "$2.99/lb" or "$2.99 /lb"
    re.compile(r"\$\s*([\d]+\.?\d*)\s*/\s*(lb|oz|kg|each|ea|ct|count)", re.I),
    # Standard "$X.XX"
    re.compile(r"\$\s*([\d]+\.[\d]{2})"),
    # Leading dot: ".99"
    re.compile(r"(?<!\d)\.([\d]{2})(?!\d)"),
]


@dataclass
class ParsedItem:
    name: str
    price: float
    original_price: Optional[float] = None
    promo_label: Optional[str] = None
    unit_type: Optional[str] = None
    unit_price: Optional[float] = None
    quantity: int = 1
    page: int = 0
    raw_text: str = ""


# ── PDF download ──────────────────────────────────────────────────────────────

def download_pdf(url: str, dest_dir: Path = Path("flyer_cache")) -> Optional[Path]:
    """Download a PDF flyer and cache it locally."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = url.split("/")[-1].split("?")[0]
    if not filename.endswith(".pdf"):
        filename += ".pdf"
    dest = dest_dir / filename

    if dest.exists():
        logger.info("Using cached %s", dest)
        return dest

    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (compatible; GroceryTracker/1.0)"
        })
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        logger.info("Downloaded %s → %s (%d KB)", url, dest, len(resp.content) // 1024)
        return dest
    except requests.RequestException as e:
        logger.error("Failed to download %s: %s", url, e)
        return None


# ── Text extraction ───────────────────────────────────────────────────────────

def extract_text_pdfplumber(pdf_path: Path) -> list[str]:
    """Extract text per page using pdfplumber (best for digital PDFs)."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            pages.append(text)
    return pages


def extract_text_ocr(pdf_path: Path, dpi: int = 200) -> list[str]:
    """
    OCR fallback: rasterize pages with pdf2image then run Tesseract.
    Use when pdfplumber yields < 50 chars per page (scanned/image PDF).
    """
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError:
        logger.error("pdf2image or pytesseract not installed. Run: "
                     "pip install pdf2image pytesseract && "
                     "brew install tesseract poppler  (or apt-get)")
        return []

    pages = []
    images = convert_from_path(str(pdf_path), dpi=dpi)
    for i, img in enumerate(images):
        text = pytesseract.image_to_string(img, config="--psm 6")
        pages.append(text)
        logger.debug("OCR page %d: %d chars", i + 1, len(text))
    return pages


def get_page_texts(pdf_path: Path, ocr_threshold: int = 50) -> list[str]:
    """
    Smart dispatch: use pdfplumber first; fall back to OCR if text is sparse.
    """
    pages = extract_text_pdfplumber(pdf_path)
    avg_chars = sum(len(p) for p in pages) / max(len(pages), 1)
    if avg_chars < ocr_threshold:
        logger.info("Low text yield (%.0f chars/page), switching to OCR", avg_chars)
        pages = extract_text_ocr(pdf_path)
    return pages


# ── Price parsing ─────────────────────────────────────────────────────────────

def extract_price(text: str) -> Optional[tuple[float, str]]:
    """
    Find the first recognizable price in a text snippet.
    Returns (price_float, raw_match_string) or None.
    """
    for pat in PRICE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                groups = m.groups()
                if len(groups) == 2 and groups[0].isdigit():
                    # "N for $X" pattern
                    qty = int(groups[0])
                    total = float(groups[1])
                    return round(total / qty, 2), m.group(0)
                else:
                    price_str = groups[0] if groups else m.group(1)
                    return float(price_str), m.group(0)
            except (ValueError, IndexError):
                continue
    return None


def parse_page_text(text: str, page_num: int) -> list[ParsedItem]:
    """
    Heuristic parser: splits page text into candidate product blocks
    and extracts name + price from each.

    Heuristic: prices usually appear on their own line or after a product
    description. We look for lines with a price and attribute the nearest
    non-price text above it as the product name.
    """
    items: list[ParsedItem] = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]
        price_result = extract_price(line)

        if price_result:
            price, raw_price = price_result
            # Grab the name from the line above (if available)
            name_line = lines[i - 1] if i > 0 else ""
            # Clean up price artifacts from name
            name = re.sub(r"\$[\d.]+", "", name_line).strip()
            name = re.sub(r"\s+", " ", name).strip()

            # Look for "was $X" / "reg $X" pattern nearby
            context = " ".join(lines[max(0, i-2):i+2])
            orig_match = re.search(
                r"(?:was|reg\.?|regular|orig\.?)\s*\$?\s*([\d]+\.[\d]{2})",
                context, re.I
            )
            original_price = float(orig_match.group(1)) if orig_match else None

            # Promo label: "Save $X", "BOGO", "Buy 2 Get 1", etc.
            promo_match = re.search(
                r"(save\s+\$?[\d.]+|bogo|buy\s+\d+\s+get\s+\d+|[\d]+\s*for\s+\$[\d.]+)",
                context, re.I
            )
            promo_label = promo_match.group(0) if promo_match else None

            if name and len(name) > 2 and price > 0:
                items.append(ParsedItem(
                    name=name,
                    price=price,
                    original_price=original_price,
                    promo_label=promo_label,
                    page=page_num,
                    raw_text=context[:200],
                ))
        i += 1

    return items


# ── Main orchestration ────────────────────────────────────────────────────────

def scrape_pdf_flyer(
    pdf_path: Path,
    store_id: int,
    valid_from: str,
    valid_to: str,
) -> list[dict]:
    """
    Parse a single PDF flyer and return normalized price row dicts
    ready for bulk_insert_prices().
    """
    from utils import normalize_name, compute_unit_price
    from db import get_or_create_product

    pages = get_page_texts(pdf_path)
    all_items: list[ParsedItem] = []

    for page_num, text in enumerate(pages, start=1):
        page_items = parse_page_text(text, page_num)
        all_items.extend(page_items)

    logger.info("PDF %s: extracted %d items across %d pages",
                pdf_path.name, len(all_items), len(pages))

    rows = []
    for item in all_items:
        canonical = normalize_name(item.name)
        unit_price, unit_type = compute_unit_price(item.price, item.name)
        product_id = get_or_create_product(canonical, item.name)

        rows.append({
            "product_id":     product_id,
            "store_id":       store_id,
            "price":          item.price,
            "unit_price":     unit_price,
            "unit_type":      unit_type,
            "quantity":       item.quantity,
            "is_sale":        1,
            "sale_type":      "weekly_ad",
            "original_price": item.original_price,
            "promo_label":    item.promo_label,
            "valid_from":     valid_from,
            "valid_to":       valid_to,
            "source":         "pdf_parse",
            "raw_data":       json.dumps({
                "page": item.page,
                "raw_text": item.raw_text,
            }),
        })

    return rows


# ── Lidl-specific scraper (they post PDFs on their website) ──────────────────

def scrape_lidl(zip_code: str = "27606") -> list[ParsedItem]:
    """
    Lidl posts a weekly PDF ad. This grabs the current week's PDF URL.
    Falls back to manual URL if auto-discovery fails.
    """
    # Lidl's weekly ad page — we look for a link to a PDF
    try:
        resp = requests.get("https://www.lidl.com/en_US/specials.htm",
                            timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        pdf_links = re.findall(r'href="([^"]+\.pdf)"', resp.text, re.I)
        if pdf_links:
            pdf_url = "https://www.lidl.com" + pdf_links[0] if pdf_links[0].startswith("/") else pdf_links[0]
            logger.info("Found Lidl PDF: %s", pdf_url)
            pdf_path = download_pdf(pdf_url)
            if pdf_path:
                pages = get_page_texts(pdf_path)
                items = []
                for i, text in enumerate(pages, 1):
                    items.extend(parse_page_text(text, i))
                return items
    except Exception as e:
        logger.warning("Lidl auto-discovery failed: %s", e)

    logger.warning("Lidl: could not auto-discover PDF. Set manual URL in PDF_STORES.")
    return []


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python pdf_scraper.py path/to/flyer.pdf [store_id] [YYYY-MM-DD] [YYYY-MM-DD]")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    store_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    today = date.today()
    valid_from = sys.argv[3] if len(sys.argv) > 3 else str(today)
    valid_to   = sys.argv[4] if len(sys.argv) > 4 else str(today + timedelta(days=6))

    rows = scrape_pdf_flyer(pdf_path, store_id, valid_from, valid_to)
    print(f"\nParsed {len(rows)} items from {pdf_path.name}")
    for r in rows[:10]:
        from db import get_stores
        print(f"  ${r['price']:.2f}  {r['promo_label'] or ''}  (product_id={r['product_id']})")
