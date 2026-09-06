"""
digest.py — Weekly sales digest grouped by category with sale history.

Usage:
  python main.py digest              # Full digest, all categories
  python main.py digest meat         # Single category
  python main.py digest meat dairy   # Multiple categories
  python main.py history "chicken breast"  # Sale history for one item
"""

import logging
from datetime import date, datetime
from typing import Optional

from grocery_tracker.db import db_cursor
from grocery_tracker.utils import fmt_price

logger = logging.getLogger(__name__)

# Categories shown in digest, in display order
DIGEST_CATEGORIES = [
    "meat",
    "seafood",
    "dairy",
    "produce",
    "frozen",
    "bakery",
    "snacks",
    "beverages",
    "alcohol",
    "pantry",
    "prepared",
    "deli",
    "health",
    "other",
]

CATEGORY_EMOJI = {
    "meat":      "🥩",
    "seafood":   "🐟",
    "dairy":     "🥛",
    "produce":   "🥦",
    "frozen":    "🧊",
    "bakery":    "🍞",
    "snacks":    "🍿",
    "beverages": "🥤",
    "alcohol":   "🍺",
    "pantry":    "🥫",
    "prepared":  "🍱",
    "deli":      "🥪",
    "health":    "💊",
    "other":     "📦",
}


# ── Data fetchers ─────────────────────────────────────────────────────────────

def get_current_sales(categories: list[str] = None) -> dict[str, list[dict]]:
    """
    Fetch all current sales grouped by category.
    Returns {category: [sale_row, ...]}
    """
    cat_filter = ""
    params = []
    if categories:
        placeholders = ",".join("?" * len(categories))
        cat_filter = f"AND COALESCE(p.category, 'other') IN ({placeholders})"
        params = list(categories)

    with db_cursor() as cur:
        cur.execute(f"""
            SELECT
                p.id                                    AS product_id,
                p.canonical_name,
                COALESCE(p.display_name, p.name, p.canonical_name) AS display_name,
                p.category,
                s.name                                  AS store_name,
                s.chain,
                pr.price                                AS sale_price,
                pr.original_price,
                pr.unit_price,
                pr.unit_type,
                pr.promo_label,
                pr.valid_from,
                pr.valid_to,
                ps.size_raw
            FROM prices pr
            JOIN products p  ON p.id  = pr.product_id
            JOIN stores   s  ON s.id  = pr.store_id
            LEFT JOIN product_sizes ps ON ps.canonical_name = p.canonical_name
            WHERE pr.is_sale = 1
              AND date(pr.valid_to) >= date('now')
              {cat_filter}
            ORDER BY
                COALESCE(p.category, 'other'),
                s.name,
                p.canonical_name
        """, params)

        rows = cur.fetchall()

    # Group by category, dedup by (product, store, price)
    seen = set()
    grouped: dict[str, list[dict]] = {}

    for row in rows:
        d = dict(row)
        cat = d.get("category") or "other"
        key = (d["canonical_name"], d["chain"], d["sale_price"])
        if key in seen:
            continue
        seen.add(key)
        grouped.setdefault(cat, []).append(d)

    return grouped


def get_sale_history(canonical_name: str, limit: int = 10) -> list[dict]:
    """
    Return past sale prices for a product across all stores,
    ordered most recent first.
    """
    with db_cursor() as cur:
        cur.execute("""
            SELECT
                p.canonical_name,
                p.display_name,
                s.name          AS store_name,
                s.chain,
                pr.price        AS sale_price,
                pr.unit_price,
                pr.unit_type,
                pr.promo_label,
                pr.valid_from,
                pr.valid_to,
                pr.scraped_at
            FROM prices pr
            JOIN products p ON p.id  = pr.product_id
            JOIN stores   s ON s.id  = pr.store_id
            WHERE p.canonical_name LIKE ?
              AND pr.is_sale = 1
            ORDER BY pr.valid_from DESC, pr.scraped_at DESC
            LIMIT ?
        """, (f"%{canonical_name}%", limit))
        return [dict(r) for r in cur.fetchall()]


def get_previous_sale(canonical_name: str,
                      chain: str,
                      current_valid_from: str) -> Optional[dict]:
    """
    Return the most recent previous sale for a specific product at a
    specific store, before the current sale period.
    """
    with db_cursor() as cur:
        cur.execute("""
            SELECT
                pr.price        AS sale_price,
                pr.unit_price,
                pr.unit_type,
                pr.valid_from,
                pr.valid_to
            FROM prices pr
            JOIN products p ON p.id = pr.product_id
            JOIN stores   s ON s.id = pr.store_id
            WHERE p.canonical_name = ?
              AND s.chain = ?
              AND pr.is_sale = 1
              AND pr.valid_from < ?
            ORDER BY pr.valid_from DESC
            LIMIT 1
        """, (canonical_name, chain, current_valid_from))
        row = cur.fetchone()
        return dict(row) if row else None


# ── Formatting helpers ─────────────────────────────────────────────────────────

def fmt_unit(unit_price: Optional[float], unit_type: Optional[str],
             size_raw: Optional[str]) -> str:
    """Format unit price display."""
    if unit_price and unit_type:
        return f"${unit_price:.2f}/{unit_type}"
    if size_raw:
        return f"({size_raw})"
    return ""


def fmt_sale_change(current_price: float,
                    prev: Optional[dict]) -> str:
    """Show price change vs last sale."""
    if not prev or not prev.get("sale_price"):
        return ""
    diff = current_price - prev["sale_price"]
    pct  = diff / prev["sale_price"] * 100
    prev_date = (prev.get("valid_from") or "")[:10]
    if abs(diff) < 0.01:
        return f"  ↔ same as last ({prev_date})"
    elif diff < 0:
        return f"  ↓ ${abs(diff):.2f} cheaper than last ({prev_date}, was ${prev['sale_price']:.2f})"
    else:
        return f"  ↑ ${diff:.2f} more than last ({prev_date}, was ${prev['sale_price']:.2f})"


def weeks_ago(date_str: str) -> str:
    """Return human-readable time since a date."""
    if not date_str:
        return ""
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        days = (date.today() - d).days
        if days < 7:
            return "this week"
        elif days < 14:
            return "1 week ago"
        else:
            return f"{days // 7} weeks ago"
    except ValueError:
        return date_str[:10]


# ── Main digest printer ───────────────────────────────────────────────────────

def print_digest(categories: list[str] = None, show_history: bool = True):
    """
    Print the weekly sales digest grouped by category.
    """
    if categories:
        # Normalize and validate
        cats_to_show = [c.lower() for c in categories]
    else:
        cats_to_show = DIGEST_CATEGORIES

    grouped = get_current_sales(cats_to_show if categories else None)

    today = date.today().strftime("%B %d, %Y")
    print(f"\n{'═' * 72}")
    print(f"  WEEKLY GROCERY DEALS  —  {today}")
    print(f"{'═' * 72}")

    total_deals = sum(len(v) for v in grouped.values())
    if total_deals == 0:
        print("  No current deals found. Run 'python main.py update' first.")
        return

    for cat in cats_to_show:
        items = grouped.get(cat, [])
        if not items:
            continue

        emoji = CATEGORY_EMOJI.get(cat, "•")
        print(f"\n  {emoji}  {cat.upper()}  ({len(items)} items)")
        print(f"  {'─' * 68}")

        for item in items:
            name       = item["display_name"][:38]
            store      = item["store_name"][:16]
            price      = item["sale_price"]
            unit_str   = ""
            promo      = (item.get("promo_label") or "")[:25]
            valid_to   = (item.get("valid_to") or "")[:10]

            # Main line
            print(f"  {name:<38}  {store:<16}  ${price:<7.2f}  {promo}")

            # History line
            if show_history:
                prev = get_previous_sale(
                    item["canonical_name"],
                    item["chain"],
                    item.get("valid_from", str(date.today()))
                )
                change = fmt_sale_change(price, prev)
                if change:
                    print(f"  {'':38}  {'':16}  {change}")

    print(f"\n{'═' * 72}")
    print(f"  {total_deals} deals across {len(grouped)} categories")
    print(f"  Valid through {(items or [{}])[-1].get('valid_to', '?')[:10] if grouped else '?'}")
    print(f"{'═' * 72}\n")


def print_history(query: str):
    """Print sale history grouped by individual product."""
    history = get_sale_history(query, limit=100)

    if not history:
        print(f"\nNo sale history found for '{query}'")
        return

    # Group by canonical_name so each distinct product gets its own section
    from collections import defaultdict
    by_product: dict[str, list] = defaultdict(list)
    for r in history:
        by_product[r["canonical_name"]].append(r)

    print(f"\nSALE HISTORY — search: '{query}'\n")

    for canonical, rows in by_product.items():
        display = rows[0].get("display_name") or canonical
        print(f"{'─' * 64}")
        print(f"  {display.upper()}")
        print(f"{'─' * 64}")
        print(f"  {'Store':<18}  {'Price':>7}  {'Period':<22}  {'When'}")
        print(f"  {'─' * 58}")

        seen = set()
        for r in rows:
            key = (r["chain"], r["sale_price"], r["valid_from"])
            if key in seen:
                continue
            seen.add(key)

            period = f"{(r.get('valid_from') or '')[:10]} → {(r.get('valid_to') or '')[:10]}"
            when   = weeks_ago(r.get("valid_from", ""))
            print(f"  {r['store_name']:<18}  ${r['sale_price']:<6.2f}  {period}  {when}")
        print()

def print_category_summary():
    """Print a one-line summary of deals per category."""
    from grocery_tracker.db import db_cursor

    with db_cursor() as cur:
        cur.execute("""
            SELECT
                COALESCE(p.category, 'other') AS category,
                COUNT(DISTINCT p.id)           AS products,
                MIN(pr.price)                  AS min_price,
                MAX(pr.price)                  AS max_price
            FROM prices pr
            JOIN products p ON p.id = pr.product_id
            WHERE pr.is_sale = 1
              AND date(pr.valid_to) >= date('now')
            GROUP BY category
            ORDER BY products DESC
        """)
        rows = cur.fetchall()

    if not rows:
        print("No current deals.")
        return

    print(f"\n  {'Category':<12}  {'Items':>5}  {'Price range'}")
    print(f"  {'─' * 40}")
    for r in rows:
        cat   = (r["category"] or "other")
        emoji = CATEGORY_EMOJI.get(cat, " ")
        rng   = f"${r['min_price']:.2f} – ${r['max_price']:.2f}"
        print(f"  {emoji} {cat:<10}  {r['products']:>5}  {rng}")
    print()
