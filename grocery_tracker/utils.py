"""
utils.py — Shared utility functions for the grocery price tracker.
"""

import re
import math
from typing import Optional

# ── Product name normalization ─────────────────────────────────────────────────

# Words to strip for canonical matching
STOP_WORDS = {
    "fresh", "frozen", "organic", "natural", "premium", "select", "choice",
    "grade", "large", "medium", "small", "extra", "super", "jumbo",
    "new", "improved", "original", "classic", "traditional",
    "brand", "store", "value", "economy", "family",
    "the", "a", "an", "of", "with", "and", "&",
}

# Common abbreviations to expand
EXPANSIONS = {
    r"\blb\b":    "pound",
    r"\boz\b":    "ounce",
    r"\bfl\.?\s*oz\b": "fluid ounce",
    r"\bct\b":    "count",
    r"\bpk\b":    "pack",
    r"\bpkg\b":   "package",
    r"\bgal\b":   "gallon",
    r"\bqt\b":    "quart",
    r"\bpt\b":    "pint",
    r"\bdoz\b":   "dozen",
    r"\bslcd\b":  "sliced",
    r"\bboneless\b": "boneless",
    r"\bskinless\b": "skinless",
    r"\bwhl\b":   "whole",
    r"\bchkn\b":  "chicken",
    r"\bgrnd\b":  "ground",
}


def normalize_name(name: str) -> str:
    """
    Produce a canonical product name for cross-store matching.
    e.g. "CHICKEN BREAST BONELESS 4lb" → "chicken breast boneless"
    """
    text = name.lower()

    # Remove package sizes like "32 oz", "4 lb", "2.5 kg"
    text = re.sub(r"\b[\d]+\.?[\d]*\s*(?:lb|oz|kg|g|ml|l|ct|pk|gal|qt|pt|fl\.?\s*oz)\b",
                  "", text, flags=re.I)

    # Remove price artifacts ($2.99, 2/$5)
    text = re.sub(r"\$[\d.]+", "", text)
    text = re.sub(r"\d+\s*/\s*\$?\d+", "", text)

    # Remove special chars except spaces and hyphens
    text = re.sub(r"[^a-z0-9\s\-]", " ", text)

    # Remove stop words
    tokens = [t for t in text.split() if t not in STOP_WORDS and len(t) > 1]

    # Collapse whitespace
    canonical = " ".join(tokens).strip()

    # Truncate to a reasonable length
    return canonical[:80]


# ── Unit price calculation ─────────────────────────────────────────────────────

# Regex patterns to extract quantity from product name/description
WEIGHT_PATTERNS = [
    (re.compile(r"([\d.]+)\s*lb", re.I),        "lb",  1.0),
    (re.compile(r"([\d.]+)\s*oz", re.I),         "oz",  1/16),    # oz → lb
    (re.compile(r"([\d.]+)\s*kg", re.I),         "kg",  2.20462), # kg → lb
    (re.compile(r"([\d.]+)\s*fl\s*oz", re.I),    "fl_oz", 1.0),
    (re.compile(r"([\d.]+)\s*ml", re.I),          "ml",  1/29.574),  # ml → fl_oz
    (re.compile(r"([\d.]+)\s*l(?:iter)?", re.I), "l",   33.814),  # L → fl_oz
    (re.compile(r"([\d.]+)\s*gal", re.I),         "gal", 128),     # gal → fl_oz
    (re.compile(r"([\d.]+)\s*qt", re.I),          "qt",  32),      # qt → fl_oz
    (re.compile(r"([\d.]+)\s*ct|count", re.I),    "ct",  1.0),
]


def compute_unit_price(price: float, description: str) -> tuple[Optional[float], Optional[str]]:
    """
    Attempt to extract a per-unit price from a product description.
    Returns (unit_price, unit_type) or (None, None) if unparseable.

    Examples:
      "Ground Beef 80/20 3 lb"          → (price/3, "lb")
      "Chicken Breast 32 oz"             → (price/2, "lb")   [32oz = 2lb]
      "Tropicana OJ 52 fl oz"            → (price/52, "fl_oz")
    """
    for pattern, unit, conversion in WEIGHT_PATTERNS:
        m = pattern.search(description)
        if m:
            try:
                qty_str = next((g for g in m.groups() if g is not None), None)
                if qty_str is None:
                    continue
                qty = float(qty_str)
                if qty <= 0:
                    continue
                standard_qty = qty * conversion
                std_unit = "lb" if unit in ("lb", "oz", "kg") else "fl_oz" if unit in ("fl_oz", "ml", "l", "gal", "qt") else unit
                unit_price = round(price / standard_qty, 3)
                return unit_price, std_unit
            except (ValueError, ZeroDivisionError):
                continue
    return None, None


# ── Price label parsing ────────────────────────────────────────────────────────

def parse_price_label(label: str) -> tuple[Optional[float], Optional[str]]:
    """
    Parse promo labels like "2 for $5", "Buy 2 Get 1 Free", "Save $1.00".
    Returns (effective_unit_price, normalized_label).
    """
    label = label.strip()

    # "N for $X.XX"
    m = re.match(r"(\d+)\s*(?:for|\/)\s*\$?\s*([\d.]+)", label, re.I)
    if m:
        qty, total = int(m.group(1)), float(m.group(2))
        return round(total / qty, 2), f"{qty} for ${total:.2f}"

    # "Buy N Get M Free" → effective price = price * N / (N+M)
    m = re.match(r"buy\s+(\d+)\s+get\s+(\d+)\s+free", label, re.I)
    if m:
        buy, free = int(m.group(1)), int(m.group(2))
        return None, f"Buy {buy} Get {free} Free"

    # "Save $X.XX" — just a label, no standalone price
    m = re.match(r"save\s+\$?([\d.]+)", label, re.I)
    if m:
        return None, f"Save ${float(m.group(1)):.2f}"

    return None, label


# ── Deal scoring ───────────────────────────────────────────────────────────────

def deal_score(sale_price: float,
               original_price: Optional[float],
               regional_avg: Optional[float],
               national_avg: Optional[float]) -> float:
    """
    Composite deal quality score from 0 (no deal) to 100 (exceptional).
    Weights: 40% vs regular, 35% vs regional, 25% vs national.
    """
    scores = []
    weights = []

    if original_price and original_price > 0:
        pct = (original_price - sale_price) / original_price * 100
        scores.append(min(pct, 60))  # cap at 60% off to avoid outliers
        weights.append(0.40)

    if regional_avg and regional_avg > 0:
        pct = (regional_avg - sale_price) / regional_avg * 100
        scores.append(min(pct, 60))
        weights.append(0.35)

    if national_avg and national_avg > 0:
        pct = (national_avg - sale_price) / national_avg * 100
        scores.append(min(pct, 60))
        weights.append(0.25)

    if not scores:
        return 0.0

    # Normalize weights
    total_w = sum(weights)
    score = sum(s * w / total_w for s, w in zip(scores, weights))

    # Map to 0–100 scale (60% off = score of 100)
    return max(0.0, min(100.0, score / 60 * 100))


# ── Formatting helpers ─────────────────────────────────────────────────────────

def fmt_price(price: Optional[float]) -> str:
    if price is None:
        return "N/A"
    return f"${price:.2f}"


def fmt_pct(pct: Optional[float], prefix: str = "") -> str:
    if pct is None:
        return "N/A"
    sign = "+" if pct > 0 else ""
    return f"{prefix}{sign}{pct:.1f}%"
