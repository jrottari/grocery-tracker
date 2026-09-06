"""
main.py — Grocery Price Tracker: main orchestrator.

Usage:
  python main.py init          # Initialize the database
  python main.py update        # Run all scrapers (Flipp + PDF + benchmarks)
  python main.py flipp         # Flipp only
  python main.py benchmarks    # BLS/USDA benchmarks only
  python main.py deals         # Print top deals
  python main.py product <q>   # Compare prices for a product
  python main.py export        # Export deals to CSV
  python main.py schedule      # Run update on a weekly cron schedule

Dependencies:
  pip install requests pdfplumber pdf2image pytesseract pillow schedule
  System: tesseract-ocr poppler-utils
"""

import sys
import logging
from pathlib import Path
from datetime import date

# ── Load .env BEFORE any grocery_tracker imports ──────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


def cmd_init():
    """Initialize the SQLite database and seed stores."""
    import os
    from grocery_tracker.db import init_db, upsert_store
    init_db()

    zip_code = os.environ.get("POSTAL_CODE", "27601")

    stores = [
        ("harris_teeter", "Harris Teeter",  "flipp",  {"zip_code": zip_code}),
        ("food_lion",     "Food Lion",       "flipp",  {"zip_code": zip_code}),
        ("publix",        "Publix",          "flipp",  {"zip_code": zip_code}),
        ("lowes_foods",   "Lowes Foods",     "flipp",  {"zip_code": zip_code}),
        ("wegmans",       "Wegmans",         "flipp",  {"zip_code": zip_code}),
        ("aldi",          "ALDI",            "flipp",  {"zip_code": zip_code}),
        ("bjs",           "BJ's Wholesale",  "pdf",    {"zip_code": zip_code}),
        ("lidl",          "Lidl",            "pdf",    {"zip_code": zip_code}),
        ("hmart",         "H Mart",          "pdf",    {"zip_code": zip_code}),
    ]

    for chain, name, method, kwargs in stores:
        store_id = upsert_store(chain, name, method, **kwargs)
        logger.info("Store ready: %s (id=%s)", name, store_id)

    print("✓ Database initialized")


def cmd_update_flipp(postal_code: str = "27601"):
    """Scrape all Flipp-based stores."""
    from grocery_tracker.flipp_scraper import scrape_all_flyers
    summary = scrape_all_flyers(postal_code)

    total = sum(summary.values())
    print(f"\n✓ Flipp: {total} items from {len(summary)} stores")
    for store, count in sorted(summary.items(), key=lambda x: -x[1]):
        print(f"    {store:<30} {count:>5}")


def cmd_update_benchmarks():
    from grocery_tracker.benchmarks import fetch_all_benchmarks, ingest_ams_produce, match_products_to_benchmarks
    n_bls = fetch_all_benchmarks()
    n_ams = ingest_ams_produce()
    n_matched = match_products_to_benchmarks()
    print(f"✓ Benchmarks: {n_bls} BLS prices, {n_ams} AMS prices, {n_matched} products matched")

def cmd_update_sizes():
    """Look up package sizes from USDA FDC and update unit prices."""
    from grocery_tracker.product_sizes import enrich_all_products
    enrich_all_products()

def cmd_update_pdf_stores():
    """
    Scrape PDF-based stores (BJ's, Lidl, H Mart).
    These stores require you to provide the PDF URL or file path.
    The system will attempt to auto-discover from known URLs.
    """
    from grocery_tracker.pdf_scraper import scrape_pdf_flyer, download_pdf, scrape_lidl
    from grocery_tracker.db import get_stores, upsert_store, bulk_insert_prices

    today = date.today()
    # Weekly ads typically run Wed → Tue
    weekday = today.weekday()
    days_since_wed = (weekday - 2) % 7
    valid_from = str(today - __import__("datetime").timedelta(days=days_since_wed))
    valid_to   = str(today + __import__("datetime").timedelta(days=6 - days_since_wed))

    # ── Lidl (auto-discover PDF) ──
    try:
        from grocery_tracker.pdf_scraper import scrape_lidl, PDF_STORES
        from grocery_tracker.db import get_or_create_product
        import json

        lidl_items = scrape_lidl()
        if lidl_items:
            from grocery_tracker.db import upsert_store, bulk_insert_prices, get_or_create_product
            from grocery_tracker.utils import normalize_name, compute_unit_price
            lidl_store_id = upsert_store("lidl", "Lidl", "pdf", zip_code="27606")
            rows = []
            for item in lidl_items:
                canonical = normalize_name(item.name)
                unit_price, unit_type = compute_unit_price(item.price, item.name)
                product_id = get_or_create_product(canonical, item.name)
                rows.append({
                    "product_id": product_id, "store_id": lidl_store_id,
                    "price": item.price, "unit_price": unit_price,
                    "unit_type": unit_type, "quantity": 1,
                    "is_sale": 1, "sale_type": "weekly_ad",
                    "original_price": item.original_price,
                    "promo_label": item.promo_label,
                    "valid_from": valid_from, "valid_to": valid_to,
                    "source": "pdf_parse",
                    "raw_data": json.dumps({"page": item.page}),
                })
            if rows:
                bulk_insert_prices(rows)
                print(f"✓ Lidl: {len(rows)} items")
    except Exception as e:
        logger.warning("Lidl PDF scrape failed: %s", e)

    # ── BJ's / H Mart: require manual PDF path ──
    # To add: download the weekly PDF manually and run:
    #   python main.py pdf_import bjs /path/to/bjs_flyer.pdf
    #   python main.py pdf_import hmart /path/to/hmart_flyer.pdf
    print("  Note: BJ's and H Mart require manual PDF import.")
    print("  Run: python main.py pdf_import bjs /path/to/bjs.pdf")

def cmd_pdf_import(chain: str, pdf_path_str: str):
    """Import a manually-downloaded PDF flyer."""
    from grocery_tracker.pdf_scraper import scrape_pdf_flyer
    from grocery_tracker.db import upsert_store, bulk_insert_prices
    import datetime

    pdf_path = Path(pdf_path_str)
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}")
        sys.exit(1)

    chain_names = {
        "bjs": "BJ's Wholesale", "hmart": "H Mart",
        "lidl": "Lidl", "costco": "Costco",
    }
    store_name = chain_names.get(chain.lower(), chain.title())
    store_id = upsert_store(chain.lower(), store_name, "pdf")

    today = date.today()
    valid_from = str(today)
    valid_to   = str(today + datetime.timedelta(days=6))

    rows = scrape_pdf_flyer(pdf_path, store_id, valid_from, valid_to)
    if rows:
        bulk_insert_prices(rows)
        print(f"✓ {store_name}: {len(rows)} items imported from {pdf_path.name}")
    else:
        print(f"No items extracted from {pdf_path.name}. "
              "Try checking the PDF format (scanned vs digital).")


def cmd_deals(n: int = 30):
    """Print top current deals."""
    from grocery_tracker.price_compare import print_top_deals, print_category_summary
    print_top_deals(n=n)
    print_category_summary()


def cmd_product(query: str):
    """Compare a specific product across stores."""
    from grocery_tracker.price_compare import print_product_comparison, print_price_history
    print_product_comparison(query)
    print_price_history(query)


def cmd_export(fmt: str = "csv", export_dir: str = None):
    """Export all current deals to a directory."""
    import os
    from grocery_tracker.price_compare import export_deals_csv, export_deals_json

    if export_dir is None:
        export_dir = r"C:\Users\user\iCloudDrive\grocery_deals"

    os.makedirs(export_dir, exist_ok=True)
    filepath = os.path.join(export_dir, f"deals_{date.today()}.{fmt}")

    if fmt == "json":
        export_deals_json(filepath)
    else:
        export_deals_csv(filepath)

def cmd_append_history(export_dir: str = None):
    """Append this week's deals to the running history CSV."""
    import os
    from grocery_tracker.price_compare import append_to_history

    if export_dir is None:
        export_dir = r"C:\Users\user\iCloudDrive\grocery_deals"

    os.makedirs(export_dir, exist_ok=True)
    filepath = os.path.join(export_dir, "deals_history.csv")
    append_to_history(filepath)

def cmd_schedule(interval_hours: int = 168):
    """
    Run a weekly update automatically using the `schedule` library.
    Default: every 168 hours (7 days), timed for Wednesday morning (new flyer cycle).
    """
    try:
        import schedule
        import time as time_module
    except ImportError:
        print("pip install schedule")
        sys.exit(1)

    def run_update():
        logger.info("─── Scheduled weekly update ───")
        try:
            cmd_update_flipp()
            cmd_update_pdf_stores()
            cmd_update_benchmarks()
        except Exception as e:
            logger.error("Update failed: %s", e)

    # Run immediately, then every Wednesday at 08:00
    run_update()
    schedule.every().wednesday.at("08:00").do(run_update)

    logger.info("Scheduler running. Press Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time_module.sleep(60)

def cmd_categorize(force: bool = False):
    """Classify all products by category."""
    from grocery_tracker.categorizer import classify_all_products, category_counts
    n = classify_all_products(force=force)
    print(f"✓ Categorized {n} products")
    counts = category_counts()
    for cat, count in counts.items():
        print(f"    {cat:<15} {count:>4}")


def cmd_digest(categories: list = None):
    """Print weekly sales digest grouped by category."""
    from grocery_tracker.digest import print_digest
    print_digest(categories=categories)


def cmd_history(query: str):
    """Print sale history for a product."""
    from grocery_tracker.digest import print_history
    print_history(query)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if not args or args[0] == "help":
        print(__doc__)
        return

    cmd = args[0].lower()

    if cmd == "init":
        cmd_init()

    elif cmd == "update":
        cmd_init()
        cmd_update_flipp()
        cmd_update_pdf_stores()
        cmd_update_benchmarks()
        cmd_update_sizes()
        cmd_categorize()

    elif cmd == "flipp":
        postal = args[1] if len(args) > 1 else "27601"
        cmd_update_flipp(postal)

    elif cmd == "benchmarks":
        cmd_update_benchmarks()

    elif cmd == "pdf_stores":
        cmd_update_pdf_stores()

    elif cmd == "pdf_import":
        if len(args) < 3:
            print("Usage: python main.py pdf_import <chain> <path/to/flyer.pdf>")
            sys.exit(1)
        cmd_pdf_import(args[1], args[2])

    elif cmd == "deals":
        n = int(args[1]) if len(args) > 1 else 30
        cmd_deals(n)

    elif cmd == "product":
        if len(args) < 2:
            print("Usage: python main.py product <product name>")
            sys.exit(1)
        cmd_product(" ".join(args[1:]))

    elif cmd == "export":
        fmt       = args[1] if len(args) > 1 else "csv"
        directory = args[2] if len(args) > 2 else None
        cmd_export(fmt, directory)

    elif cmd == "schedule":
        cmd_schedule()
    
    elif cmd == "sizes":
        cmd_update_sizes()
    
    elif cmd == "categorize":
        cmd_categorize(force="--force" in args)
    
    elif cmd == "digest":
        cats = [a for a in args[1:] if not a.startswith("--")] or None
        cmd_digest(cats)
    
    elif cmd == "history":
        if len(args) < 2:
            print("Usage: python main.py history <product name>")
            sys.exit(1)
        cmd_history(" ".join(args[1:]))
    
    elif cmd == "history_append":
        directory = args[1] if len(args) > 1 else None
        cmd_append_history(directory)

    else:
        print(f"Unknown command: {cmd}")
        print("Run 'python main.py help' for usage")
        sys.exit(1)


if __name__ == "__main__":
    main()
