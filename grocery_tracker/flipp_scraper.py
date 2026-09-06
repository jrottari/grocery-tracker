"""
flipp_scraper.py — Scrapes weekly flyer data from the Flipp platform.

Working endpoints (confirmed 2025):
  GET https://flyers-ng.flippback.com/api/flipp/data?locale=en&postal_code=XXXXX&sid=XXXXXXXXXXXXXXXX
  GET https://flyers-ng.flippback.com/api/flipp/flyers/{flyer_id}/flyer_items?locale=en&sid=XXXXXXXXXXXXXXXX

The sid is a randomly generated 16-digit numeric string, generated fresh each request.
US zip codes work fine (e.g. 27601).
"""

import time
import logging
import json
import random
import os
from datetime import date
from typing import Optional

import requests

from grocery_tracker.db import (
    get_or_create_product, bulk_insert_prices,
    upsert_store, init_db
)
from grocery_tracker.utils import normalize_name, compute_unit_price


# Keywords that indicate non-grocery items — used to filter mixed flyers
import re

# Matches Flipp internal ad/banner items that aren't real products
BANNER_PATTERN = re.compile(
    r'^[A-Z0-9]{4,}-[A-Z0-9]|^\d{7,}-\d{3}-|^[A-Z]{2,}\d{2}-',
    re.IGNORECASE
)

NON_GROCERY_NAMES = {
    # Electronics
    "bluetooth", "boombox", "speaker", "projector", "streamer", "streaming",
    "roku", "vizio", "television", " tv ", "monitor", "laptop", "chromebook",
    "tablet", "headphone", "earbuds", "earbud", "keyboard", "mouse", "gaming",
    "razer", "logitech", "skullcandy", "jbl", "airpods", "apple pencil",
    "airtag", "ring camera", "smartwatch", "pixel watch", "oura ring",
    "canon ivy", "nespresso", "blink video",
    # Appliances & kitchen equipment
    "air fryer", "coffee maker", "coffee machine", "toaster oven", "microwave",
    "blender", "stand mixer", "dutch oven", "griddle", "indoor grill",
    "slushi machine", "ice cream maker", "spin scrubber", "electric grill",
    "smart thermometer", "meat thermometer",
    # Cookware & bakeware (non-food)
    "cookware", "bakeware", "skillet", "frying pan", "dinnerware", "stoneware",
    "sheet pan", "cookie sheet", "pizza pan", "nonstick", "cast iron",
    # Cleaning & household
    "vacuum", "bissell", "dyson", "shark navigator", "air purifier",
    "carpet cleaner", "steam cleaner", "spin mop", "power mop", "mop bucket",
    "swiffer", "glass cleaner", "disinfecting wipes", "multi-surface cleaner",
    "laundry detergent", "fabric softener", "dryer sheet", "dishwasher detergent",
    "scent booster", "oxiclean", "tide pods", "purex", "gain ", "dawn ",
    "nellie laundry", "laundry soda",
    # Personal care & beauty (non-food)
    "shampoo", "conditioner", "body wash", "soap bar", "antiperspirant",
    "deodorant", "toothpaste", "mouthwash", "lip balm", "cosmetics", "makeup",
    "nail set", "hair spray", "skin cream", "face mask", "eye drops",
    "nicotine gum", "facial towel", "collagen cream",
    # Baby non-food
    "diaper", "wipes", "diaper pail", "car seat", "stroller", "baby monitor",
    "infant formula", "baby toiletries",
    # Clothing & apparel
    "clothing", "apparel", "shirt", "pants", "dress", "shorts", "jeans",
    "swimsuit", "linen pant", "beach pant", "bath towel", "towel set",
    # Luggage & bags
    "luggage", "suitcase", "carry-on", "hardside", "softside", "duffel",
    "under the seat bag", "weekender",
    # Furniture & home decor
    "furniture", "patio", "chaise lounge", "adirondack chair", "platform bed",
    "kids bed", "vanity", "picture frame", "planter pot", "ceramic planter",
    # Garden & outdoor
    "mulch", "fertilizer", "lawn mower", "leaf blower", "string trimmer",
    "pellet smoker", "pellet grill", "charcoal briquette", "kingsford",
    "rose shrub", "live shrub",
    # Books, media, toys
    "lego", "vinyl record", "vinyl ", "book", "novel", "grad book",
    "audiobook", "smartwatch",
    # Automotive & tires
    "tire ", "suv crossover tire", "all season r",
    # Misc non-food
    "picture frame", "tumbler", "insulated stainless steel",
    "nitrile gloves", "scour pad", "plastic fork", "plastic cup",
    "trash bag", "storage bag", "ziploc", "snack bag",
    "toilet paper", "paper towel", "facial tissue", "kleenex", "puffs ",
    "charmin", "bounty ", "hefty ", "up & up",
}

GROCERY_FLYER_ONLY = {"Groceries"}  # flyers with exactly this are pure grocery


def is_grocery_item(name: str, flyer_categories: list) -> bool:
    """Return False if this item is clearly non-grocery."""
    # Block internal Flipp ad/banner items
    if BANNER_PATTERN.match(name):
        return False

    # Pure grocery flyers — trust everything in them
    if flyer_categories and all(c in ("All Flyers", "Groceries") for c in flyer_categories):
        return True

    # Mixed flyer — filter by name keywords
    name_lower = name.lower()
    return not any(kw in name_lower for kw in NON_GROCERY_NAMES)
logger = logging.getLogger(__name__)

FLIPPBACK_BASE = "https://flyers-ng.flippback.com/api/flipp"
FLYERS_URL     = FLIPPBACK_BASE + "/data"
ITEMS_URL      = FLIPPBACK_BASE + "/flyers/{flyer_id}/flyer_items"

POSTAL_CODE = os.environ.get("POSTAL_CODE", "27601")

TARGET_CHAINS = [
    "harris teeter",
    "food lion",
    "publix",
    "lowes foods",
    "wegmans",
    "aldi",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def generate_sid() -> str:
    return "".join(str(random.randint(0, 9)) for _ in range(16))


def fetch_flyers(postal_code: str = POSTAL_CODE) -> list[dict]:
    sid = generate_sid()
    params = {"locale": "en", "postal_code": postal_code, "sid": sid}
    resp = SESSION.get(FLYERS_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    flyers = data.get("flyers", [])
    logger.info("Found %d total flyers for %s", len(flyers), postal_code)
    return flyers


def filter_target_flyers(flyers: list[dict]) -> list[dict]:
    """Keep only flyers from target chains that have a Groceries category."""
    results = []
    for flyer in flyers:
        merchant = (flyer.get("merchant") or flyer.get("name") or "").lower()
        if not any(chain in merchant for chain in TARGET_CHAINS):
            continue
        categories = flyer.get("categories") or []
        # Skip flyers with no grocery content at all (e.g. Costco auto/pharmacy flyer)
        if categories and "Groceries" not in categories:
            logger.info("Skipping %s flyer %s — categories: %s",
                        merchant, flyer.get("id"), categories)
            continue
        results.append(flyer)
    logger.info("Filtered to %d target flyers", len(results))
    return results


def fetch_flyer_items(flyer_id) -> list[dict]:
    sid = generate_sid()
    url = ITEMS_URL.format(flyer_id=flyer_id)
    params = {"locale": "en", "sid": sid}
    resp = SESSION.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def parse_item(item: dict, store_id: int,
               valid_from: str, valid_to: str, flyer_categories: list = None) -> Optional[dict]:
    name = (item.get("name") or item.get("description") or "").strip()
    if not name:
        return None

    # Filter non-grocery items from mixed flyers
    if not is_grocery_item(name, flyer_categories or []):
        return None

    raw = item.get("current_price") or item.get("price")
    if raw is None:
        return None
    try:
        price_val = float(raw)
        price = price_val / 100.0 if (price_val > 200 and isinstance(raw, int)) else price_val
    except (ValueError, TypeError):
        return None

    if price <= 0:
        return None

    orig_raw = item.get("original_price") or item.get("was_price")
    original_price = None
    if orig_raw is not None:
        try:
            ov = float(orig_raw)
            original_price = ov / 100.0 if (ov > 200 and isinstance(orig_raw, int)) else ov
        except (ValueError, TypeError):
            pass

    promo_label = (
        item.get("sale_story") or item.get("description2") or
        item.get("pre_price_text") or ""
    ).strip() or None

    category  = (item.get("category") or item.get("category_name") or "").lower()
    canonical = normalize_name(name)
    unit_price, unit_type = compute_unit_price(price, name)

    product_id = get_or_create_product(
        canonical_name=canonical,
        display_name=name,
        name=name,
        category=category,
        brand=item.get("brand"),
    )

    return {
        "product_id":     product_id,
        "store_id":       store_id,
        "price":          price,
        "unit_price":     unit_price,
        "unit_type":      unit_type,
        "quantity":       1,
        "is_sale":        1,
        "sale_type":      "weekly_ad",
        "original_price": original_price,
        "promo_label":    promo_label,
        "valid_from":     valid_from,
        "valid_to":       valid_to,
        "source":         "flipp",
        "raw_data":       json.dumps({
            "flipp_flyer_id": item.get("flyer_id"),
            "flipp_item_id":  item.get("id"),
        }),
    }


def scrape_all_flyers(postal_code: str = POSTAL_CODE,
                      delay: float = 1.0) -> dict[str, int]:
    postal_code = os.environ.get("POSTAL_CODE", postal_code)
    summary: dict[str, int] = {}

    all_flyers    = fetch_flyers(postal_code)
    target_flyers = filter_target_flyers(all_flyers)

    if not target_flyers:
        logger.warning(
            "No target flyers found for %s. "
            "Check TARGET_CHAINS list or try a different zip code.", postal_code
        )
        return summary

    for flyer in target_flyers:
        merchant          = (flyer.get("merchant") or flyer.get("name") or "unknown").strip()
        flyer_id          = flyer.get("id")
        valid_from        = (flyer.get("valid_from") or str(date.today()))[:10]
        valid_to          = (flyer.get("valid_to")   or str(date.today()))[:10]
        flyer_categories  = flyer.get("categories", [])   # <-- added

        if not flyer_id:
            continue

        store_id = upsert_store(
            chain=merchant.lower().replace(" ", "_"),
            name=merchant,
            scrape_method="flipp",
            flipp_merchant=str(flyer.get("merchant_id", "")),
            zip_code=postal_code,
        )

        logger.info("Scraping %s (flyer %s, %s to %s)", merchant, flyer_id, valid_from, valid_to)
        try:
            items = fetch_flyer_items(flyer_id)
        except requests.HTTPError as e:
            logger.error("Failed to fetch items for %s: %s", merchant, e)
            continue

        rows = [
            r for item in items
            if (r := parse_item(item, store_id, valid_from, valid_to, flyer_categories))  # <-- added flyer_categories
        ]

        if rows:
            bulk_insert_prices(rows)
            summary[merchant] = len(rows)
            logger.info("  v %d items saved for %s", len(rows), merchant)

        time.sleep(delay)

    return summary


def diagnose(postal_code: str = POSTAL_CODE):
    """Print raw API info to help debug. Run: python -m grocery_tracker.flipp_scraper diagnose"""
    postal_code = os.environ.get("POSTAL_CODE", postal_code)
    sid = generate_sid()
    params = {"locale": "en", "postal_code": postal_code, "sid": sid}

    print(f"URL: {FLYERS_URL}")
    print(f"Params: {params}\n")

    resp = SESSION.get(FLYERS_URL, params=params, timeout=20)
    print(f"Status:       {resp.status_code}")
    print(f"Content-Type: {resp.headers.get('content-type')}")

    try:
        data = resp.json()
        flyers = data.get("flyers", [])
        print(f"Flyers found: {len(flyers)}")
        if flyers:
            print("\nAll merchants near your zip:")
            for f in flyers:
                print(f"  {f.get('merchant', '?'):<30}  id={f.get('id')}")
        else:
            print("Response keys:", list(data.keys()))
            print("Body preview:", resp.text[:300])
    except Exception as e:
        print(f"JSON parse failed: {e}")
        print("Body preview:", resp.text[:500])


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s -- %(message)s")

    cmd    = sys.argv[1] if len(sys.argv) > 1 else "scrape"
    postal = sys.argv[2] if len(sys.argv) > 2 else POSTAL_CODE

    if cmd == "diagnose":
        diagnose(postal)
    else:
        init_db()
        summary = scrape_all_flyers(postal)
        total = sum(summary.values())
        print(f"\n-- Scrape complete: {total} items --")
        for store, count in sorted(summary.items(), key=lambda x: -x[1]):
            print(f"  {store:<30} {count:>5}")
