-- Grocery Price Tracker Database Schema
-- SQLite (easily portable to PostgreSQL)

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ─── Stores ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    chain           TEXT NOT NULL,           -- e.g. "harris_teeter"
    flipp_merchant  TEXT,                    -- Flipp merchant_id or slug
    address         TEXT,
    city            TEXT DEFAULT 'Raleigh',
    state           TEXT DEFAULT 'NC',
    zip_code        TEXT,
    lat             REAL,
    lon             REAL,
    scrape_method   TEXT NOT NULL,           -- 'flipp' | 'pdf' | 'api' | 'manual'
    flyer_url       TEXT,                    -- direct PDF/page URL if not Flipp
    active          INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- ─── Product Catalog ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    display_name    TEXT,                    -- original name from Flipp
    brand           TEXT,
    category        TEXT,                    -- 'produce','dairy','meat','pantry',etc.
    subcategory     TEXT,
    unit_type       TEXT,                    -- 'lb','oz','each','fl_oz','count'
    upc             TEXT UNIQUE,
    canonical_name  TEXT,                    -- normalized name for cross-store matching
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_products_canonical ON products(canonical_name);
CREATE INDEX IF NOT EXISTS idx_products_category  ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_upc       ON products(upc);
CREATE UNIQUE INDEX IF NOT EXISTS idx_stores_chain ON stores(chain);
CREATE UNIQUE INDEX IF NOT EXISTS idx_prices_dedup ON prices(product_id, store_id, price, valid_from, valid_to);

-- ─── Price Records ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    price           REAL NOT NULL,           -- raw price in USD
    unit_price      REAL,                    -- price per standard unit (e.g. per lb/oz)
    unit_type       TEXT,                    -- unit for unit_price
    quantity        REAL DEFAULT 1,          -- package size
    is_sale         INTEGER DEFAULT 0,       -- 1 = on sale this week
    sale_type       TEXT,                    -- 'weekly_ad','bogo','digital_coupon','member'
    original_price  REAL,                    -- regular shelf price if known
    promo_label     TEXT,                    -- raw label: "2 for $5", "Buy 2 Get 1"
    valid_from      TEXT,                    -- sale start date (ISO 8601)
    valid_to        TEXT,                    -- sale end date
    source          TEXT,                    -- 'flipp' | 'pdf_parse' | 'manual' | 'bls'
    raw_data        TEXT,                    -- JSON blob of original scraped data
    scraped_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_prices_product   ON prices(product_id);
CREATE INDEX IF NOT EXISTS idx_prices_store     ON prices(store_id);
CREATE INDEX IF NOT EXISTS idx_prices_valid     ON prices(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_prices_sale      ON prices(is_sale);
CREATE INDEX IF NOT EXISTS idx_prices_scraped   ON prices(scraped_at);

-- ─── Regional / National Benchmark Prices ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS benchmark_prices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER REFERENCES products(id),
    canonical_name  TEXT NOT NULL,           -- fallback if no product_id match
    scope           TEXT NOT NULL,           -- 'local' | 'regional' | 'national'
    region          TEXT,                    -- BLS region: 'South','Northeast',etc.
    avg_price       REAL NOT NULL,
    unit_type       TEXT,
    source          TEXT NOT NULL,           -- 'bls' | 'usda_ers' | 'manual'
    period          TEXT,                    -- e.g. '2025-Q1' or '2025-04'
    fetched_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_bench_canonical ON benchmark_prices(canonical_name);
CREATE INDEX IF NOT EXISTS idx_bench_scope     ON benchmark_prices(scope, region);

CREATE TABLE IF NOT EXISTS product_sizes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name  TEXT NOT NULL UNIQUE,
    size_oz         REAL,
    size_fl_oz      REAL,
    size_count      INTEGER,
    size_raw        TEXT,
    source          TEXT,
    looked_up_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sizes_canonical ON product_sizes(canonical_name);

-- ─── Flyers / Ad Cycles ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS flyers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    flipp_flyer_id  TEXT,
    valid_from      TEXT NOT NULL,
    valid_to        TEXT NOT NULL,
    pdf_path        TEXT,                    -- local cache path if PDF was downloaded
    page_count      INTEGER,
    scraped_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(store_id, valid_from)
);

-- ─── Deal Alerts (for future notification system) ─────────────────────────────
CREATE TABLE IF NOT EXISTS deal_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER REFERENCES products(id),
    canonical_name  TEXT,
    threshold_pct   REAL DEFAULT 20.0,       -- alert if sale > X% below avg
    active          INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now'))
);

-- ─── Price Comparisons (materialized for speed) ───────────────────────────────
CREATE VIEW IF NOT EXISTS v_current_deals AS
SELECT
    p.id            AS price_id,
    pr.canonical_name,
    pr.category,
    s.name          AS store_name,
    s.chain,
    p.price         AS sale_price,
    p.original_price,
    p.unit_price,
    p.unit_type,
    p.promo_label,
    p.valid_from,
    p.valid_to,
    b_reg.avg_price AS regional_avg,
    b_nat.avg_price AS national_avg,
    ROUND(
        CASE WHEN p.original_price > 0
             THEN (p.original_price - p.price) / p.original_price * 100.0
             ELSE NULL END, 1
    )               AS pct_off_regular,
    ROUND(
        CASE WHEN b_reg.avg_price > 0
             THEN (b_reg.avg_price - p.price) / b_reg.avg_price * 100.0
             ELSE NULL END, 1
    )               AS pct_off_regional_avg,
    ROUND(
        CASE WHEN b_nat.avg_price > 0
             THEN (b_nat.avg_price - p.price) / b_nat.avg_price * 100.0
             ELSE NULL END, 1
    )               AS pct_off_national_avg
FROM prices p
JOIN stores   s  ON s.id  = p.store_id
JOIN products pr ON pr.id = p.product_id
LEFT JOIN (
    SELECT canonical_name, avg_price
    FROM benchmark_prices
    WHERE scope = 'regional'
    GROUP BY canonical_name
    HAVING fetched_at = MAX(fetched_at)
) b_reg ON b_reg.canonical_name = pr.canonical_name
LEFT JOIN (
    SELECT canonical_name, avg_price
    FROM benchmark_prices
    WHERE scope = 'national'
    GROUP BY canonical_name
    HAVING fetched_at = MAX(fetched_at)
) b_nat ON b_nat.canonical_name = pr.canonical_name
WHERE p.is_sale = 1
  AND date(p.valid_to) >= date('now');
