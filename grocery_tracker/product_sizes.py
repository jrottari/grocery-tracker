"""
product_sizes.py — Enrich products with package sizes from external APIs.

Sources:
  1. USDA FoodData Central (primary)
     - Free API key: https://fdc.nal.usda.gov/api-key-signup
     - Returns packageWeight like "16 oz/1 lbs", "12.5 oz/354 g", "1 gal"
     - Add FDC_API_KEY to your .env

  2. Open Food Facts (fallback)
     - No key required
     - Returns quantity like "3 lb", "454 g", "1 gallon"
     - Slower and less reliable for US products

Results are cached in the product_sizes table so each product is
looked up at most once. Once a size is known, unit_price is computed
and stored back on the prices table.
"""

import os
import re
import time
import logging
from typing import Optional

import requests

from grocery_tracker.db import db_cursor, get_connection

logger = logging.getLogger(__name__)

FDC_BASE = "https://api.nal.usda.gov/fdc/v1/foods/search"
OFF_BASE = "https://world.openfoodfacts.org/cgi/search.pl"

HEADERS = {"User-Agent": "GroceryTracker/1.0 (personal-project)"}


# ── Schema: product_sizes table ───────────────────────────────────────────────

SIZES_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_sizes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name  TEXT NOT NULL UNIQUE,
    size_oz         REAL,            -- package size in oz (solids)
    size_fl_oz      REAL,            -- package size in fl oz (liquids)
    size_count      INTEGER,         -- count (eggs, etc.)
    size_raw        TEXT,            -- raw string from API e.g. "16 oz/1 lbs"
    source          TEXT,            -- 'fdc' | 'off' | 'manual'
    looked_up_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sizes_canonical ON product_sizes(canonical_name);
"""

# Categories where per-unit pricing doesn't make sense
NO_UNIT_PRICE_CATEGORIES = {
    "alcohol",   # price per pack/bottle, not per oz
    "health",    # pills, tablets — count matters but $/oz is meaningless  
    "other",     # non-food items
}

# For these categories, only compute unit price if it's a liquid volume
VOLUME_ONLY_CATEGORIES = {
    "beverages",  # $/fl_oz makes sense
}

def init_sizes_table():
    conn = get_connection()
    conn.executescript(SIZES_SCHEMA)
    conn.close()


# ── Package weight parser ─────────────────────────────────────────────────────
# Handles formats seen in FDC:
#   "16 oz/1 lbs"       → 16 oz solid
#   "12.5 oz/354 g"     → 12.5 oz solid
#   "1 gal"             → 128 fl oz
#   "0.5 gal"           → 64 fl oz
#   "1 lbs/453 g"       → 16 oz solid
#   "240 MLT"           → 8.11 fl oz  (MLT = mL)
#   "3 lb"              → 48 oz solid
#   "454 g"             → 16.01 oz solid

# Conversion constants
OZ_PER_LB  = 16.0
OZ_PER_KG  = 35.274
FLOZ_PER_GAL = 128.0
FLOZ_PER_L   = 33.814
FLOZ_PER_ML  = 0.033814
FLOZ_PER_QT  = 32.0
FLOZ_PER_PT  = 16.0
FLOZ_PER_CUP = 8.0
OZ_PER_G   = 0.035274

WEIGHT_RE = [
    # oz first (most common in FDC packageWeight)
    (re.compile(r"([\d.]+)\s*oz", re.I),           "oz"),
    (re.compile(r"([\d.]+)\s*lbs?", re.I),         "lb"),
    (re.compile(r"([\d.]+)\s*kg", re.I),            "kg"),
    (re.compile(r"([\d.]+)\s*g\b", re.I),           "g"),
]

VOLUME_RE = [
    (re.compile(r"([\d.]+)\s*gal(?:lon)?", re.I),  "gal"),
    (re.compile(r"([\d.]+)\s*(?:fl\.?\s*oz|floz)", re.I), "floz"),
    (re.compile(r"([\d.]+)\s*(?:MLT|ml)", re.I),   "ml"),
    (re.compile(r"([\d.]+)\s*l(?:iter|itre)?(?:\b|$)", re.I), "l"),
    (re.compile(r"([\d.]+)\s*qt", re.I),            "qt"),
    (re.compile(r"([\d.]+)\s*pt", re.I),            "pt"),
    (re.compile(r"([\d.]+)\s*cup", re.I),           "cup"),
]

COUNT_RE = re.compile(
    r"(\d+)\s*(?:count|ct|pack|pk|eggs?|pieces?|slices?|rolls?)", re.I
)


def parse_package_weight(raw: str) -> dict:
    """
    Parse a raw packageWeight string into standardized size fields.
    Returns dict with keys: size_oz, size_fl_oz, size_count (any may be None).

    Examples:
      "16 oz/1 lbs"     → {size_oz: 16.0}
      "12.5 oz/354 g"   → {size_oz: 12.5}
      "1 gal"           → {size_fl_oz: 128.0}
      "0.5 gal"         → {size_fl_oz: 64.0}
      "1 lbs/453 g"     → {size_oz: 16.0}
      "454 g"           → {size_oz: 16.01}
      "12 count"        → {size_count: 12}
    """
    result = {"size_oz": None, "size_fl_oz": None, "size_count": None}
    if not raw:
        return result

    text = raw.strip()

    # Try weight units (oz takes priority since FDC lists oz first)
    for pattern, unit in WEIGHT_RE:
        m = pattern.search(text)
        if m:
            try:
                val = float(m.group(1))
            except (ValueError, TypeError):
                continue
            if unit == "oz":
                result["size_oz"] = round(val, 3)
            elif unit == "lb":
                result["size_oz"] = round(val * OZ_PER_LB, 3)
            elif unit == "kg":
                result["size_oz"] = round(val * OZ_PER_KG, 3)
            elif unit == "g":
                result["size_oz"] = round(val * OZ_PER_G, 3)
            return result

    # Try volume units
    for pattern, unit in VOLUME_RE:
        m = pattern.search(text)
        if m:
            try:
                val = float(m.group(1))
            except (ValueError, TypeError):
                continue
            if unit == "gal":
                result["size_fl_oz"] = round(val * FLOZ_PER_GAL, 3)
            elif unit == "floz":
                result["size_fl_oz"] = round(val, 3)
            elif unit == "ml":
                result["size_fl_oz"] = round(val * FLOZ_PER_ML, 3)
            elif unit == "l":
                result["size_fl_oz"] = round(val * FLOZ_PER_L, 3)
            elif unit == "qt":
                result["size_fl_oz"] = round(val * FLOZ_PER_QT, 3)
            elif unit == "pt":
                result["size_fl_oz"] = round(val * FLOZ_PER_PT, 3)
            elif unit == "cup":
                result["size_fl_oz"] = round(val * FLOZ_PER_CUP, 3)
            return result

    # Try count
    m = COUNT_RE.search(text)
    if m:
        result["size_count"] = int(m.group(1))

    return result


# ── FDC lookup ────────────────────────────────────────────────────────────────

def fdc_lookup(query: str, fdc_key: str, max_results: int = 10) -> Optional[dict]:
    """
    Search FDC for a product and return the best packageWeight match.
    Returns parsed size dict or None if not found.
    """
    try:
        resp = requests.get(
            FDC_BASE,
            params={
                "query":    query,
                "api_key":  fdc_key,
                "dataType": "Branded",
                "pageSize": max_results,
            },
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        foods = resp.json().get("foods", [])
    except Exception as e:
        logger.warning("FDC lookup failed for %r: %s", query, e)
        return None

    # Pick the first result that has a packageWeight
    for food in foods:
        pw = food.get("packageWeight")
        if pw:
            parsed = parse_package_weight(pw)
            if any(v is not None for v in parsed.values()):
                logger.debug("FDC match: %r → packageWeight=%r → %s",
                             query, pw, parsed)
                return {**parsed, "size_raw": pw, "source": "fdc"}

    # Fallback: try parsing size from description text
    for food in foods:
        desc = food.get("description", "")
        parsed = parse_package_weight(desc)
        if any(v is not None for v in parsed.values()):
            logger.debug("FDC desc parse: %r → %r → %s", query, desc, parsed)
            return {**parsed, "size_raw": desc, "source": "fdc_desc"}

    return None


# ── Open Food Facts lookup ────────────────────────────────────────────────────

def off_lookup(query: str) -> Optional[dict]:
    """
    Search Open Food Facts for a product and return size info.
    Used as fallback when FDC has no packageWeight.
    """
    try:
        resp = requests.get(
            OFF_BASE,
            params={
                "search_terms":  query,
                "search_simple": 1,
                "action":        "process",
                "json":          1,
                "fields":        "product_name,quantity,serving_size,serving_quantity",
                "page_size":     5,
                "cc":            "us",
                "lc":            "en",
            },
            headers=HEADERS,
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        products = resp.json().get("products", [])
    except Exception as e:
        logger.warning("OFF lookup failed for %r: %s", query, e)
        return None

    for product in products:
        qty = product.get("quantity", "")
        if qty:
            parsed = parse_package_weight(qty)
            if any(v is not None for v in parsed.values()):
                logger.debug("OFF match: %r → quantity=%r → %s", query, qty, parsed)
                return {**parsed, "size_raw": qty, "source": "off"}

    return None


# ── Cache layer ───────────────────────────────────────────────────────────────

def get_cached_size(canonical_name: str) -> Optional[dict]:
    """Return cached size for a product, or None if not yet looked up."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM product_sizes WHERE canonical_name = ?",
            (canonical_name,)
        )
        row = cur.fetchone()
        return dict(row) if row else None


def cache_size(canonical_name: str, size_data: dict):
    """Store a size lookup result in the cache."""
    with db_cursor() as cur:
        cur.execute("""
            INSERT OR REPLACE INTO product_sizes
                (canonical_name, size_oz, size_fl_oz, size_count, size_raw, source)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            canonical_name,
            size_data.get("size_oz"),
            size_data.get("size_fl_oz"),
            size_data.get("size_count"),
            size_data.get("size_raw"),
            size_data.get("source", "unknown"),
        ))


def lookup_size(canonical_name: str, fdc_key: str,
                delay: float = 0.3) -> Optional[dict]:
    """
    Look up package size for a product, using cache first.
    Falls back from FDC → OFF → None.
    """
    # Check cache
    cached = get_cached_size(canonical_name)
    if cached is not None:
        return cached

    # Try FDC
    result = fdc_lookup(canonical_name, fdc_key)
    time.sleep(delay)

    # Try OFF as fallback
    if result is None:
        result = off_lookup(canonical_name)
        time.sleep(delay)

    # Cache result (even if None, to avoid re-querying)
    if result:
        cache_size(canonical_name, result)
        logger.info("Cached size for %r: %s", canonical_name, result.get("size_raw"))
    else:
        # Cache a null entry so we don't keep retrying
        cache_size(canonical_name, {
            "size_oz": None, "size_fl_oz": None,
            "size_count": None, "size_raw": None, "source": "not_found"
        })
        logger.debug("No size found for %r", canonical_name)

    return result


# ── Unit price updater ────────────────────────────────────────────────────────

def compute_unit_price_from_size(price: float, size: dict) -> tuple[Optional[float], Optional[str]]:
    """
    Given a sale price and package size dict, compute unit price.
    Returns (unit_price, unit_type).
    """
    if size.get("size_oz"):
        lbs = size["size_oz"] / OZ_PER_LB
        if lbs > 0:
            return round(price / lbs, 3), "lb"

    if size.get("size_fl_oz"):
        fl_oz = size["size_fl_oz"]
        if fl_oz > 0:
            return round(price / fl_oz, 3), "fl_oz"

    if size.get("size_count"):
        count = size["size_count"]
        if count > 0:
            return round(price / count, 3), "each"

    return None, None


def update_unit_prices(fdc_key: str, limit: int = 500, delay: float = 0.3):
    init_sizes_table()

    with db_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT p.canonical_name, p.category
            FROM products p
            JOIN prices pr ON pr.product_id = p.id
            WHERE pr.unit_price IS NULL
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()

    logger.info("Looking up sizes for %d products", len(rows))
    updated = 0

    for row in rows:
        name     = row["canonical_name"]
        category = row["category"] or "other"

        # Skip categories where unit pricing is meaningless
        if category in NO_UNIT_PRICE_CATEGORIES:
            continue

        size = lookup_size(name, fdc_key, delay=delay)
        if not size:
            continue

        # For beverages only use fl_oz, not weight
        if category in VOLUME_ONLY_CATEGORIES:
            if not size.get("size_fl_oz"):
                continue

        unit_price, unit_type = compute_unit_price_from_size(0, size)
        if unit_price is None:
            continue

        with db_cursor() as cur:
            cur.execute("""
                SELECT pr.id, pr.price
                FROM prices pr
                JOIN products p ON p.id = pr.product_id
                WHERE p.canonical_name = ?
                  AND pr.unit_price IS NULL
            """, (name,))
            price_rows = cur.fetchall()

            for pr in price_rows:
                up, ut = compute_unit_price_from_size(pr["price"], size)
                if up:
                    cur.execute("""
                        UPDATE prices SET unit_price = ?, unit_type = ?
                        WHERE id = ?
                    """, (up, ut, pr["id"]))
                    updated += 1

    logger.info("Updated unit prices for %d price rows", updated)
    return updated


# ── Batch enrichment for all unmatched products ───────────────────────────────

def enrich_all_products(fdc_key: str = None, delay: float = 0.5):
    """
    Main entry point. Look up sizes for all products missing unit_price
    and update the prices table.
    """
    if fdc_key is None:
        fdc_key = os.environ.get("FDC_API_KEY", "")
    if not fdc_key:
        logger.error("FDC_API_KEY not set. Add it to your .env file.")
        logger.error("Get a free key at: https://fdc.nal.usda.gov/api-key-signup")
        return 0

    init_sizes_table()
    n = update_unit_prices(fdc_key, delay=delay)
    print(f"✓ Unit prices updated for {n} price rows")
    return n


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s — %(message)s")

    from dotenv import load_dotenv
    load_dotenv()

    fdc_key = os.environ.get("FDC_API_KEY", "")
    if not fdc_key:
        print("Set FDC_API_KEY in your .env file")
        print("Free key: https://fdc.nal.usda.gov/api-key-signup")
        sys.exit(1)

    if len(sys.argv) > 1:
        # Test a specific product name
        query = " ".join(sys.argv[1:])
        print(f"Looking up: {query!r}")
        result = fdc_lookup(query, fdc_key)
        if result:
            print(f"FDC result: {result}")
            up, ut = compute_unit_price_from_size(5.99, result)
            print(f"Unit price at $5.99: ${up}/{ut}")
        else:
            result = off_lookup(query)
            if result:
                print(f"OFF result: {result}")
            else:
                print("Not found in either source")
    else:
        enrich_all_products(fdc_key)
