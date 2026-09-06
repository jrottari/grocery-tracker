"""
price_compare.py — Price comparison, analysis, and reporting.

Features:
  - Best current deals across all stores
  - Deal score (composite metric vs regular + regional + national)
  - Price history charts (terminal-friendly)
  - Cross-store price comparison for a specific product
  - Category deal summary
  - Export to CSV / JSON
"""

import csv
import json
import logging
from datetime import date
from typing import Optional
from io import StringIO

from grocery_tracker.db import get_current_deals, price_history, db_cursor
from grocery_tracker.utils import deal_score, fmt_price, fmt_pct

logger = logging.getLogger(__name__)


# ── Deal scoring / enrichment ──────────────────────────────────────────────────

def enrich_deals(deals: list[dict]) -> list[dict]:
    for d in deals:
        d["deal_score"] = deal_score(
            sale_price=d["sale_price"],
            original_price=d.get("original_price"),
            regional_avg=d.get("regional_avg"),
            national_avg=d.get("national_avg"),
        )
    return sorted(deals, key=lambda x: (
        x["deal_score"],
        x.get("pct_off_regular") or 0
    ), reverse=True)


# ── Top deals report ───────────────────────────────────────────────────────────

def top_deals(
    n: int = 200,
    min_score: float = 0.0,
    category: Optional[str] = None,
    store_chain: Optional[str] = None,
) -> list[dict]:
    """Return current deals sorted by store then product name."""
    deals = get_current_deals(category=category, store_chain=store_chain)
    deals = sorted(deals, key=lambda x: (x["store_name"], x["canonical_name"]))
    return deals[:n]


def print_top_deals(n: int = 25, **kwargs):
    """Pretty-print top deals to terminal."""
    deals = top_deals(n=n, **kwargs)
    if not deals:
        print("No deals found matching criteria.")
        return

    header = (
        f"{'Product':<40} {'Store':<20} {'Sale':>7}"
    )
    print(f"\n{'─' * len(header)}")
    print(header)
    print(f"{'─' * len(header)}")

    for d in deals:
        print(
            f"{(d.get('display_name') or d['canonical_name'])[:39]:<40} "
            f"{d['store_name'][:19]:<20} "
            f"{fmt_price(d['sale_price']):>7}"
        )
    print(f"{'─' * len(header)}")
    print(f"  {len(deals)} deals  |  valid through {date.today().strftime('%b %d, %Y')}\n")


# ── Cross-store comparison for one product ────────────────────────────────────

def compare_product(canonical_name: str) -> list[dict]:
    """
    Compare current prices for a product across all stores.
    Returns rows sorted by sale_price ascending.
    """
    with db_cursor() as cur:
        cur.execute("""
            SELECT
                p.price          AS sale_price,
                p.original_price,
                p.unit_price,
                p.unit_type,
                p.promo_label,
                p.is_sale,
                p.valid_from,
                p.valid_to,
                pr.display_name,
                s.name           AS store_name,
                s.chain,
                b_reg.avg_price  AS regional_avg,
                b_nat.avg_price  AS national_avg
            FROM prices p
            JOIN products pr ON pr.id = p.product_id
            JOIN stores   s  ON s.id  = p.store_id
            LEFT JOIN benchmark_prices b_reg
                ON b_reg.canonical_name = pr.canonical_name AND b_reg.scope = 'regional'
            LEFT JOIN benchmark_prices b_nat
                ON b_nat.canonical_name = pr.canonical_name AND b_nat.scope = 'national'
            WHERE pr.canonical_name LIKE ?
              AND date(p.valid_to) >= date('now')
            ORDER BY p.price ASC
        """, (f"%{canonical_name}%",))
        return [dict(r) for r in cur.fetchall()]


def print_product_comparison(canonical_name: str):
    """Pretty-print cross-store price comparison for a product."""
    rows = compare_product(canonical_name)
    if not rows:
        print(f"No current prices found for '{canonical_name}'")
        return

    reg_avg = rows[0].get("regional_avg")
    nat_avg = rows[0].get("national_avg")

    display = rows[0].get("display_name") or canonical_name
    print(f"\n── {display.upper()} ──")
    if reg_avg:
        print(f"  Regional avg (South): {fmt_price(reg_avg)}")
    if nat_avg:
        print(f"  National avg:         {fmt_price(nat_avg)}")
    print()

    header = f"  {'Store':<22} {'Price':>7} {'Regular':>8} {'vs Reg Avg':>11} {'Promo'}"
    print(header)
    print(f"  {'─' * (len(header) - 2)}")

    for r in rows:
        vs_reg = None
        if reg_avg and reg_avg > 0:
            vs_reg = (reg_avg - r["sale_price"]) / reg_avg * 100

        unit_str = f"${r['unit_price']:.2f}/{r['unit_type']}" if r.get("unit_price") else ""
        print(
            f"  {r['store_name'][:21]:<22} "
            f"{fmt_price(r['sale_price']):>7} "
            f"{fmt_price(r.get('original_price')):>8} "
            f"{fmt_pct(vs_reg, ''):>10} "
            f"{(r.get('promo_label') or '')[:30]}"
        )
    print()


# ── Price history (text sparkline) ───────────────────────────────────────────

def append_to_history(filepath: str):
    """Append this week's deals to running history CSV and dedupe."""
    import os
    from grocery_tracker.db import db_cursor

    with db_cursor() as cur:
        cur.execute("""
            SELECT
                COALESCE(p.display_name, p.name, p.canonical_name) AS product,
                s.name          AS store,
                pr.price        AS price,
                COALESCE(p.category, 'other') AS category,
                pr.valid_from   AS period_start,
                pr.valid_to     AS period_end
            FROM prices pr
            JOIN products p ON p.id = pr.product_id
            JOIN stores   s ON s.id = pr.store_id
            WHERE pr.is_sale = 1
            ORDER BY p.category, s.name, p.canonical_name
        """)
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        print("No current deals to append.")
        return

    fieldnames    = ["product", "store", "price", "category", "period_start", "period_end"]
    file_exists   = os.path.exists(filepath)
    rows_appended = len(rows)

    # Append new rows
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)

    # Read everything back and deduplicate
    with open(filepath, "r", newline="", encoding="utf-8") as f:
        reader     = csv.DictReader(f)
        all_rows   = list(reader)

    seen   = set()
    unique = []
    for row in all_rows:
        key = (row["product"], row["store"], row["price"],
               row["period_start"], row["period_end"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    removed = len(all_rows) - len(unique)

    # Rewrite the file with deduped rows
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(unique)

    print(f"✓ Appended {rows_appended} rows, removed {removed} duplicates → {filepath}")
    print(f"  Total rows in history: {len(unique)}")

def sparkline(values: list[float]) -> str:
    """Generate a Unicode sparkline from a list of floats."""
    if not values:
        return ""
    chars = "▁▂▃▄▅▆▇█"
    mn, mx = min(values), max(values)
    if mn == mx:
        return chars[3] * len(values)
    normalized = [(v - mn) / (mx - mn) for v in values]
    return "".join(chars[min(int(n * len(chars)), len(chars) - 1)] for n in normalized)


def print_price_history(canonical_name: str, days: int = 90):
    """Print a price history with sparkline for a product."""
    history = price_history(canonical_name, days=days)
    if not history:
        print(f"No price history found for '{canonical_name}'")
        return

    # Group by store
    by_store: dict[str, list] = {}
    for row in history:
        by_store.setdefault(row["store_name"], []).append(row)

    print(f"\n── PRICE HISTORY: {canonical_name.upper()} (last {days} days) ──\n")
    for store, rows in by_store.items():
        prices = [r["price"] for r in reversed(rows)]
        dates  = [r.get("valid_from", "?")[:10] for r in reversed(rows)]
        spark  = sparkline(prices)

        lo, hi, avg = min(prices), max(prices), sum(prices) / len(prices)
        sale_count = sum(1 for r in rows if r["is_sale"])

        print(f"  {store}")
        print(f"    {spark}  ({dates[0]} → {dates[-1]})")
        print(f"    low={fmt_price(lo)}  avg={fmt_price(avg):.2}  high={fmt_price(hi)}  "
              f"sales={sale_count}/{len(rows)}")
        print()


# ── Category summary ──────────────────────────────────────────────────────────

def category_deal_summary() -> dict[str, dict]:
    """Summarize deals by category."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT
                category,
                COUNT(*) AS total_deals
            FROM v_current_deals
            GROUP BY category
            ORDER BY total_deals DESC
        """)
        return {r["category"]: dict(r) for r in cur.fetchall()}


def print_category_summary():
    summary = category_deal_summary()
    print("\n── DEALS BY CATEGORY ──\n")
    print(f"  {'Category':<20} {'Deals':>6}")
    print(f"  {'─' * 28}")
    for cat, stats in summary.items():
        print(
            f"  {(cat or 'uncategorized')[:19]:<20} "
            f"{stats['total_deals']:>6}"
        )
    print()


# ── Exports ───────────────────────────────────────────────────────────────────

def export_deals_csv(filepath: str = "deals_export.csv", **kwargs):
    """Export current deals to CSV with Store, Price, Category, Period columns."""
    from grocery_tracker.db import db_cursor

    with db_cursor() as cur:
        cur.execute("""
            SELECT
                COALESCE(p.display_name, p.name, p.canonical_name) AS product,
                s.name          AS store,
                pr.price        AS price,
                COALESCE(p.category, 'other') AS category,
                pr.valid_from   AS period_start,
                pr.valid_to     AS period_end
            FROM prices pr
            JOIN products p ON p.id = pr.product_id
            JOIN stores   s ON s.id = pr.store_id
            WHERE pr.is_sale = 1
            ORDER BY p.category, s.name, p.canonical_name
        """)
        rows = cur.fetchall()

    if not rows:
        logger.warning("No deals to export")
        return

    fieldnames = ["product", "store", "price", "category", "period_start", "period_end"]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([dict(r) for r in rows])

    logger.info("Exported %d rows to %s", len(rows), filepath)
    print(f"✓ Exported {len(rows)} rows → {filepath}")


def export_deals_json(filepath: str = "deals_export.json", **kwargs):
    """Export current deals to JSON."""
    deals = top_deals(n=500, **kwargs)
    with open(filepath, "w") as f:
        json.dump(deals, f, indent=2, default=str)
    print(f"✓ Exported {len(deals)} deals → {filepath}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.WARNING)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "top"

    if cmd == "top":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 25
        print_top_deals(n=n)

    elif cmd == "product":
        if len(sys.argv) < 3:
            print("Usage: python price_compare.py product <name>")
        else:
            name = " ".join(sys.argv[2:])
            print_product_comparison(name)
            print_price_history(name)

    elif cmd == "categories":
        print_category_summary()

    elif cmd == "export":
        fmt = sys.argv[2] if len(sys.argv) > 2 else "csv"
        if fmt == "json":
            export_deals_json()
        else:
            export_deals_csv()

    else:
        print("Commands: top [N] | product <name> | categories | export [csv|json]")
