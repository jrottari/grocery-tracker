"""
db.py — SQLite database layer for the grocery price tracker.
Uses sqlite3 directly (no ORM overhead) with a thin connection wrapper.
"""

import os
import sqlite3
import json
import logging
from pathlib import Path
from contextlib import contextmanager
from typing import Optional, Any

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DB_PATH", "data/grocery_tracker.db"))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row          # access columns by name
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_cursor(db_path: Path = DB_PATH):
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH, schema_path: Path = SCHEMA_PATH):
    """Create tables from schema.sql if they don't exist."""
    schema = schema_path.read_text()
    conn = get_connection(db_path)
    conn.executescript(schema)
    conn.close()
    logger.info("Database initialized at %s", db_path)


# ─── Store helpers ────────────────────────────────────────────────────────────

def upsert_store(chain: str, name: str, scrape_method: str, **kwargs) -> int:
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO stores (chain, name, scrape_method,
                                flipp_merchant, address, zip_code, lat, lon, flyer_url)
            VALUES (:chain,:name,:scrape_method,
                    :flipp_merchant,:address,:zip_code,:lat,:lon,:flyer_url)
            ON CONFLICT(chain) DO UPDATE SET updated_at=datetime('now')
        """, {"chain": chain, "name": name, "scrape_method": scrape_method,
              "flipp_merchant": kwargs.get("flipp_merchant"),
              "address": kwargs.get("address"),
              "zip_code": kwargs.get("zip_code"),
              "lat": kwargs.get("lat"),
              "lon": kwargs.get("lon"),
              "flyer_url": kwargs.get("flyer_url")})
        cur.execute("SELECT id FROM stores WHERE chain=?", (chain,))
        return cur.fetchone()["id"]


def get_stores(active_only: bool = True) -> list[dict]:
    with db_cursor() as cur:
        q = "SELECT * FROM stores" + (" WHERE active=1" if active_only else "")
        cur.execute(q)
        return [dict(r) for r in cur.fetchall()]


# ─── Product helpers ──────────────────────────────────────────────────────────

def get_or_create_product(canonical_name: str, name: str,
                           display_name: str = None,
                           category: str = None, **kwargs) -> int:
    with db_cursor() as cur:
        cur.execute("SELECT id FROM products WHERE canonical_name=?", (canonical_name,))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur.execute("""
            INSERT INTO products (name, display_name, canonical_name, brand,
                                  category, subcategory, unit_type, upc)
            VALUES (?,?,?,?,?,?,?,?)
        """, (name, display_name or name, canonical_name,
              kwargs.get("brand"), category,
              kwargs.get("subcategory"), kwargs.get("unit_type"),
              kwargs.get("upc")))
        return cur.lastrowid


# ─── Price helpers ────────────────────────────────────────────────────────────

def insert_price(product_id: int, store_id: int, price: float,
                 is_sale: bool = False, **kwargs):
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO prices
                (product_id, store_id, price, unit_price, unit_type, quantity,
                 is_sale, sale_type, original_price, promo_label,
                 valid_from, valid_to, source, raw_data)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (product_id, store_id, price,
              kwargs.get("unit_price"), kwargs.get("unit_type"),
              kwargs.get("quantity", 1),
              1 if is_sale else 0,
              kwargs.get("sale_type"), kwargs.get("original_price"),
              kwargs.get("promo_label"),
              kwargs.get("valid_from"), kwargs.get("valid_to"),
              kwargs.get("source", "unknown"),
              json.dumps(kwargs.get("raw_data")) if kwargs.get("raw_data") else None))


def bulk_insert_prices(rows: list[dict]):
    """Efficiently insert many price rows at once, ignoring duplicates."""
    with db_cursor() as cur:
        cur.executemany("""
            INSERT OR IGNORE INTO prices
                (product_id, store_id, price, unit_price, unit_type, quantity,
                 is_sale, sale_type, original_price, promo_label,
                 valid_from, valid_to, source, raw_data)
            VALUES (:product_id,:store_id,:price,:unit_price,:unit_type,:quantity,
                    :is_sale,:sale_type,:original_price,:promo_label,
                    :valid_from,:valid_to,:source,:raw_data)
        """, rows)
        logger.info("Bulk inserted %d price rows", cur.rowcount)


def insert_benchmark(canonical_name: str, scope: str, avg_price: float,
                     source: str, **kwargs):
    with db_cursor() as cur:
        cur.execute("""
            INSERT OR REPLACE INTO benchmark_prices
                (canonical_name, scope, region, avg_price, unit_type, source, period)
            VALUES (?,?,?,?,?,?,?)
        """, (canonical_name, scope,
              kwargs.get("region"), avg_price,
              kwargs.get("unit_type"), source,
              kwargs.get("period")))


# ─── Query helpers ────────────────────────────────────────────────────────────

def get_current_deals(min_pct_off: float = 0,
                      category: Optional[str] = None,
                      store_chain: Optional[str] = None,
                      limit: int = 200) -> list[dict]:
    """Return current sales with benchmark comparisons from the view."""
    clauses = []
    params: list[Any] = []
    if min_pct_off:
        clauses.append("(pct_off_regional_avg >= ? OR pct_off_national_avg >= ?)")
        params += [min_pct_off, min_pct_off]
    if category:
        clauses.append("category = ?")
        params.append(category)
    if store_chain:
        clauses.append("chain = ?")
        params.append(store_chain)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with db_cursor() as cur:
        cur.execute(f"""
            SELECT v.*, p.display_name
            FROM v_current_deals v
            JOIN products p ON p.canonical_name = v.canonical_name
            {where}
            ORDER BY pct_off_regional_avg DESC NULLS LAST
            LIMIT ?
        """, params + [limit])
        return [dict(r) for r in cur.fetchall()]


def price_history(canonical_name: str, store_chain: Optional[str] = None,
                  days: int = 90) -> list[dict]:
    """Historical prices for a product across stores."""
    params: list[Any] = [canonical_name, days]
    store_filter = ""
    if store_chain:
        store_filter = "AND s.chain = ?"
        params.append(store_chain)

    with db_cursor() as cur:
        cur.execute(f"""
            SELECT p.price, p.unit_price, p.is_sale, p.valid_from, p.valid_to,
                   p.promo_label, p.original_price, s.name AS store_name, s.chain
            FROM prices p
            JOIN products pr ON pr.id = p.product_id
            JOIN stores   s  ON s.id  = p.store_id
            WHERE pr.canonical_name = ?
              AND p.scraped_at >= datetime('now', ? || ' days')
              {store_filter}
            ORDER BY p.scraped_at DESC
        """, [canonical_name, f"-{days}"] + ([store_chain] if store_chain else []))
        return [dict(r) for r in cur.fetchall()]
