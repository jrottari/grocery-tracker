"""
benchmarks.py — Fetch regional and national food price averages.

Sources:
  1. BLS (Bureau of Labor Statistics) — Average Retail Food Prices
     API: https://api.bls.gov/publicAPI/v2/timeseries/data/
     Free tier: 500 queries/day, 10 series/request, 20 years of data
     Register for free API key at: https://data.bls.gov/registrationEngine/
     Series IDs: https://www.bls.gov/cpi/factsheets/average-prices.htm

  2. USDA ERS (Economic Research Service) — Food Price Outlook
     https://www.ers.usda.gov/data-products/food-price-outlook/

  3. USDA Agricultural Marketing Service — local/wholesale prices
     https://www.ams.usda.gov/market-news/terminal-markets

BLS series cover ~70 common grocery items nationally and by region.
The South region covers Raleigh, NC.
"""

import os
import json
import logging
import time
from datetime import datetime, date
from typing import Optional

import requests

from grocery_tracker.db import insert_benchmark, DB_PATH, init_db

logger = logging.getLogger(__name__)

BLS_BASE    = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

# ── BLS Series IDs for average retail food prices ─────────────────────────────
# Format: APU0000XXXXX
# Region prefixes:
#   APU0000 = National
#   APU0100 = Northeast
#   APU0200 = Midwest
#   APU0300 = South (includes NC — use this!)
#   APU0400 = West
#
# Full list: https://download.bls.gov/pub/time.series/ap/ap.series
# Selected items relevant to grocery shopping:

BLS_SERIES = {
    # ── Produce ──────────────────────────────────────────────────────────────
    "bananas":              {"national": "APU0000711211", "south": "APU0300711211"},
    "apples_red_delicious": {"national": "APU0000711311", "south": "APU0300711311"},
    "oranges_navel":        {"national": "APU0000711412", "south": "APU0300711412"},
    "tomatoes":             {"national": "APU0000712311", "south": "APU0300712311"},
    "potatoes_white":       {"national": "APU0000712112", "south": "APU0300712112"},
    "lettuce_iceberg":      {"national": "APU0000712211", "south": "APU0300712211"},
    "broccoli":             {"national": "APU0000712412", "south": "APU0300712412"},
    "celery":               {"national": "APU0000712311", "south": "APU0300712311"},

    # ── Meat & Seafood ────────────────────────────────────────────────────────
    "ground_beef_regular":  {"national": "APU0000703112", "south": "APU0300703112"},
    "ground_beef_lean":     {"national": "APU0000703511", "south": "APU0300703511"},
    "beef_chuck_roast":     {"national": "APU0000703213", "south": "APU0300703213"},
    "beef_sirloin_steak":   {"national": "APU0000703411", "south": "APU0300703411"},
    "chicken_whole":        {"national": "APU0000706111", "south": "APU0300706111"},
    "chicken_breast":       {"national": "APU0000706211", "south": "APU0300706211"},
    "chicken_legs":         {"national": "APU0000706311", "south": "APU0300706311"},
    "pork_chops_bone_in":   {"national": "APU0000704111", "south": "APU0300704111"},
    "bacon":                {"national": "APU0000704311", "south": "APU0300704311"},
    "ham_boneless":         {"national": "APU0000704412", "south": "APU0300704412"},
    "tuna_canned":          {"national": "APU0000717311", "south": "APU0300717311"},

    # ── Dairy & Eggs ─────────────────────────────────────────────────────────
    "milk_whole_gallon":    {"national": "APU0000709112", "south": "APU0300709112"},
    "milk_reduced_fat":     {"national": "APU0000709113", "south": "APU0300709113"},
    "eggs_grade_a_dozen":   {"national": "APU0000708111", "south": "APU0300708111"},
    "butter":               {"national": "APU0000710111", "south": "APU0300710111"},
    "cheese_american":      {"national": "APU0000710211", "south": "APU0300710211"},
    "yogurt":               {"national": "APU0000710411", "south": "APU0300710411"},

    # ── Bread & Grains ────────────────────────────────────────────────────────
    "white_bread":          {"national": "APU0000702111", "south": "APU0300702111"},
    "wheat_bread":          {"national": "APU0000702212", "south": "APU0300702212"},
    "flour_white":          {"national": "APU0000701111", "south": "APU0300701111"},
    "rice_white":           {"national": "APU0000701312", "south": "APU0300701312"},
    "spaghetti_macaroni":   {"national": "APU0000701322", "south": "APU0300701322"},
    "corn_flakes_cereal":   {"national": "APU0000702421", "south": "APU0300702421"},

    # ── Oils, Condiments, Other ───────────────────────────────────────────────
    "vegetable_oil":        {"national": "APU0000714231", "south": "APU0300714231"},
    "margarine_stick":      {"national": "APU0000714312", "south": "APU0300714312"},
    "orange_juice":         {"national": "APU0000711411", "south": "APU0300711411"},
    "coffee_ground":        {"national": "APU0000717311", "south": "APU0300717311"},
    "sugar_white":          {"national": "APU0000715211", "south": "APU0300715211"},
    "potatoes_frozen":      {"national": "APU0000712112", "south": "APU0300712112"},
}

# Unit types per canonical name (for unit_price calculations)
BLS_UNITS = {
    "bananas":              ("lb", 1.0),
    "apples_red_delicious": ("lb", 1.0),
    "ground_beef_regular":  ("lb", 1.0),
    "chicken_breast":       ("lb", 1.0),
    "eggs_grade_a_dozen":   ("dozen", 12),
    "milk_whole_gallon":    ("gallon", 1.0),
    "butter":               ("lb", 1.0),
}


def match_products_to_benchmarks():
    """
    Link products to BLS benchmarks by overwriting canonical_name
    on products whose names contain known food keywords.
    """
    from grocery_tracker.db import db_cursor

    KEYWORD_MAP = [
        (["ground beef", "lean beef", "beef 80", "beef 85", "beef 90", "beef 93",
          "lean ground"],                              "ground beef regular"),
        (["chuck roast", "beef chuck"],                "beef chuck roast"),
        (["sirloin", "strip steak", "ribeye", "t-bone", "beef steak",
          "york strip", "new york strip"],             "beef chuck roast"),
        (["chicken breast", "boneless breast",
          "chicken thigh", "chicken drumstick",
          "chicken leg", "chicken tender",
          "chicken wing"],                             "chicken whole"),
        (["whole chicken", "rotisserie"],              "chicken whole"),
        (["pork chop", "pork loin", "pork tenderloin"],"pork chops bone in"),
        (["bacon"],                                    "bacon"),
        (["ham", "prosciutto"],                        "ham boneless"),
        (["flounder", "tilapia", "cod fillet", "salmon fillet",
          "fish fillet", "catfish", "mahi", "halibut", "grouper",
          "canned tuna", "tuna can"],                  "tuna canned"),
        (["shrimp"],                                   "tuna canned"),
        (["egg", "eggs"],                              "eggs grade a dozen"),
        (["whole milk", "2% milk", "skim milk", "reduced fat milk",
          "lactaid", "oat milk", "almond milk", "milk "],
                                                       "milk whole gallon"),
        (["butter", "margarine"],                      "butter"),
        (["cheddar", "mozzarella", "string cheese",
          "parmesan", "swiss cheese", "provolone",
          "pepper jack", "colby", "american cheese",
          "shredded cheese", "sliced cheese"],         "cheese american"),
        (["yogurt", "greek yogurt"],                   "yogurt"),
        (["white bread", "sandwich bread", "sourdough",
          "brioche", "potato bread", "italian bread"],  "white bread"),
        (["wheat bread", "whole grain bread",
          "multigrain bread", "whole wheat"],           "wheat bread"),
        (["bagel", "english muffin", "roll", "bun",
          "croissant", "pita"],                        "white bread"),
        (["banana"],                                   "bananas"),
        (["apple "],                                   "apples red delicious"),
        (["orange ", "mandarin", "clementine", "tangerine"],
                                                       "oranges navel"),
        (["tomato"],                                   "tomatoes"),
        (["russet potato", "yukon potato", "red potato",
          "sweet potato", "potato "],                  "potatoes white"),
        (["frozen potato", "frozen fries", "tater tot",
          "hash brown"],                               "potatoes frozen"),
        (["romaine", "iceberg lettuce", "head lettuce",
          "lettuce "],                                  "lettuce iceberg"),
        (["broccoli"],                                 "broccoli"),
        (["celery"],                                   "celery"),
        (["orange juice", " oj "],                     "orange juice"),
        (["coffee", "k-cup", "ground coffee", "espresso",
          "cold brew"],                                "coffee ground"),
        (["sugar"],                                    "sugar white"),
        (["flour"],                                    "flour white"),
        (["white rice", "jasmine rice", "basmati rice",
          "brown rice", "rice "],                      "rice white"),
        (["pasta", "spaghetti", "penne", "linguine",
          "fettuccine", "macaroni", "noodle", "rotini",
          "rigatoni", "farfalle"],                     "spaghetti macaroni"),
        (["cereal", "corn flakes", "granola", "oatmeal",
          "oats"],                                     "corn flakes cereal"),
        (["vegetable oil", "canola oil", "olive oil",
          "cooking oil", "avocado oil"],               "vegetable oil"),
    ]

    with db_cursor() as cur:
        cur.execute("SELECT id, canonical_name FROM products")
        products = cur.fetchall()

        cur.execute("SELECT DISTINCT canonical_name FROM benchmark_prices")
        valid_benchmarks = {r[0] for r in cur.fetchall()}

        matches = 0
        for product in products:
            pid  = product["id"]
            name = product["canonical_name"].lower()

            for keywords, benchmark_name in KEYWORD_MAP:
                if benchmark_name not in valid_benchmarks:
                    continue
                if any(kw in name for kw in keywords):
                    cur.execute("""
                        UPDATE products SET canonical_name = ?
                        WHERE id = ? AND canonical_name != ?
                    """, (benchmark_name, pid, benchmark_name))
                    if cur.rowcount:
                        matches += 1
                    break

    logger.info("Fuzzy-matched %d products to BLS benchmarks", matches)
    return matches

def fetch_bls_series(series_ids: list[str],
                     start_year: int = None,
                     end_year: int = None) -> dict:
    """
    Query BLS API for one or more series (max 50 per request with API key,
    10 without).
    Returns {series_id: [{year, period, value}, ...]}
    """
    BLS_API_KEY = os.environ.get("BLS_API_KEY", "")  # read at call time, not import time
    if not series_ids:
        return {}

    current_year = date.today().year
    start_year = start_year or current_year - 1
    end_year   = end_year   or current_year

    payload = {
        "seriesid":  series_ids[:50],
        "startyear": str(start_year),
        "endyear":   str(end_year),
        "catalog":   False,
        "calculations": False,
        "annualaverage": False,
    }
    if BLS_API_KEY:
        payload["registrationkey"] = BLS_API_KEY

    resp = requests.post(BLS_BASE, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "REQUEST_SUCCEEDED":
        logger.error("BLS API error: %s", data.get("message", "unknown"))
        return {}

    results = {}
    for series in data.get("Results", {}).get("series", []):
        sid  = series["seriesID"]
        rows = series.get("data", [])
        results[sid] = [
            {
                "year":   r["year"],
                "period": r["period"],
                "value":  float(r["value"]) if r["value"] != "-" else None,
            }
            for r in rows
        ]
    return results


def get_latest_value(series_data: list[dict]) -> Optional[float]:
    """Return the most recent non-null value from a BLS series."""
    # BLS returns newest-first; periods are M01–M12 or S01–S02 (semiannual)
    for row in series_data:
        if row["value"] is not None:
            return row["value"]
    return None


def fetch_all_benchmarks(delay: float = 0.5):
    """
    Fetch national and South-region benchmarks for all tracked items
    and store them in the DB.
    """
    BLS_API_KEY = os.environ.get("BLS_API_KEY", "")  # add this as first line
    total = 0

    # Gather series IDs in batches of 10 (unauthenticated limit)
    batch_size = 50 if BLS_API_KEY else 10
    all_lookups: list[tuple[str, str, str]] = []  # (canonical_name, scope, series_id)

    for canonical, scopes in BLS_SERIES.items():
        all_lookups.append((canonical, "national", scopes["national"]))
        all_lookups.append((canonical, "regional", scopes["south"]))

    # Deduplicate series IDs
    series_map: dict[str, list[tuple[str, str]]] = {}
    for canonical, scope, sid in all_lookups:
        series_map.setdefault(sid, []).append((canonical, scope))

    unique_ids = list(series_map.keys())

    for batch_start in range(0, len(unique_ids), batch_size):
        batch = unique_ids[batch_start:batch_start + batch_size]
        logger.info("Fetching BLS batch %d–%d of %d",
                    batch_start + 1, batch_start + len(batch), len(unique_ids))

        results = fetch_bls_series(batch)

        for sid, series_data in results.items():
            latest = get_latest_value(series_data)
            if latest is None:
                continue

            # Determine period label from latest row
            row = next(r for r in series_data if r["value"] is not None)
            period = f"{row['year']}-{row['period']}"

            for canonical, scope in series_map.get(sid, []):
                region = "South" if scope == "regional" else None
                insert_benchmark(
                    canonical_name=canonical.replace("_", " "),
                    scope=scope,
                    avg_price=latest,
                    source="bls",
                    region=region,
                    period=period,
                    unit_type=BLS_UNITS.get(canonical, (None, None))[0],
                )
                total += 1
                logger.debug("  %s [%s] = $%.2f (%s)", canonical, scope, latest, period)

        time.sleep(delay)

    logger.info("Stored %d benchmark prices from BLS", total)
    return total


# ── USDA AMS — spot/wholesale prices for local context ────────────────────────
# AMS Market News API: https://mymarketnews.ams.usda.gov/mars/public
# Free, no key needed. Returns current wholesale prices at terminal markets.

AMS_BASE = "https://marsapi.ams.usda.gov/services/v1.2"

AMS_REPORTS = {
    # Mid-Atlantic terminal market (closest to Raleigh)
    "produce_mid_atlantic": "2634",
}


def fetch_ams_report(report_id: str, commodity: str = None) -> list[dict]:
    """
    Fetch the latest AMS market news report.
    Returns a list of price rows: {commodity, grade, price, unit, market}
    """
    url = f"{AMS_BASE}/reports/{report_id}"
    params = {}
    if commodity:
        params["q"] = commodity

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        logger.warning("AMS fetch failed for report %s: %s", report_id, e)
        return []


def ingest_ams_produce():
    """Store AMS produce prices as 'local' benchmarks."""
    rows = fetch_ams_report(AMS_REPORTS["produce_mid_atlantic"])
    count = 0
    for r in rows:
        commodity = r.get("commodity_name", "").lower().replace(" ", "_")
        price_str = r.get("low_price") or r.get("high_price")
        if not commodity or not price_str:
            continue
        try:
            price = float(str(price_str).replace(",", ""))
        except ValueError:
            continue

        insert_benchmark(
            canonical_name=commodity,
            scope="local",
            avg_price=price,
            source="usda_ams",
            region="Mid-Atlantic",
            unit_type=r.get("unit_of_sale", "").lower() or None,
            period=str(date.today()),
        )
        count += 1
    logger.info("Stored %d AMS produce benchmarks", count)
    return count


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s — %(message)s")

    if not BLS_API_KEY:
        logger.warning(
            "BLS_API_KEY not set — limited to 10 series/request (slower). "
            "Register free at https://data.bls.gov/registrationEngine/"
        )

    init_db()
    print("Fetching BLS national and regional averages...")
    n = fetch_all_benchmarks()
    print(f"  ✓ {n} benchmark prices stored")

    print("Fetching USDA AMS local wholesale prices...")
    n2 = ingest_ams_produce()
    print(f"  ✓ {n2} AMS prices stored")
