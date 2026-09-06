# Grocery Price Tracker

A Python system that aggregates weekly grocery flyers and sale data into a local
SQLite database, enriches them with regional/national average prices from the BLS
and USDA, and lets you quickly identify the best deals.

## Architecture

```
grocery_tracker/
├── main.py           # CLI orchestrator
├── db.py             # SQLite schema + query helpers
├── schema.sql        # Full DDL (tables, indexes, views)
├── flipp_scraper.py  # Flipp API scraper (most stores)
├── pdf_scraper.py    # PDF flyer parser (BJ's, Lidl, H Mart)
├── benchmarks.py     # BLS + USDA price fetchers
├── price_compare.py  # Deal scoring, comparison, reporting
├── utils.py          # Shared helpers (normalize, unit price, deal score)
└── requirements.txt
```

## Data sources

| Source | What it covers | Auth required |
|--------|---------------|---------------|
| **Flipp API** | Harris Teeter, Food Lion, Walmart, Publix, Lowes Foods, Wegmans, Costco | None (public) |
| **BLS Avg Retail Prices** | National + South region averages for ~70 items | Free API key recommended |
| **USDA AMS Market News** | Mid-Atlantic wholesale produce spot prices | None |
| **PDF scraper** | BJ's, Lidl, H Mart (manual download or auto-discover) | None |

## Quick start

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install system dependencies
# macOS:
brew install tesseract poppler
# Ubuntu/Debian:
apt-get install tesseract-ocr poppler-utils

# 3. (Optional but recommended) Get a free BLS API key
# Register at: https://data.bls.gov/registrationEngine/
# Then: export BLS_API_KEY=your_key_here

# 4. Initialize the database
python main.py init

# 5. Run the full update (Flipp + benchmarks)
python main.py update

# 6. View top deals
python main.py deals

# 7. Compare a specific product
python main.py product chicken breast

# 8. Export to CSV
python main.py export csv
```

## Weekly automation

The pipeline runs automatically every **Wednesday at 9:00 AM America/New_York**
via **GitHub Actions** (`.github/workflows/weekly-update.yml`), so it no longer
depends on a local machine being powered on. The workflow:

1. Checks out the repo (which includes `data/grocery_tracker.db` — tracked in
   git specifically so price history survives across stateless CI runs).
2. Runs `update` → `categorize` → `export csv` → `history_append`, writing
   exports into `exports/` (via the `EXPORT_DIR` env var — see below) instead
   of the local iCloud Drive path.
3. Commits and pushes the updated `data/grocery_tracker.db` and `exports/*`
   back to `main`.

GitHub Actions cron is UTC-only and doesn't shift for US daylight saving, so
the workflow schedules two triggers (13:00 and 14:00 UTC) and a `check-time`
job that skips whichever one doesn't actually land at 9am Eastern that day.
It can also be run manually anytime from the **Actions** tab (`workflow_dispatch`).

Required GitHub repo secrets (`gh secret set <NAME>`, values from `.env`):
`BLS_API_KEY`, `FDC_API_KEY`, `POSTAL_CODE`.

### Getting the results into iCloud Drive

GitHub Actions has no access to iCloud, so a second, lightweight piece runs
locally: a Windows Scheduled Task, **"Grocery Tracker iCloud Sync"**, that
just pulls what GitHub already produced — no scraping, so timing isn't
critical:

```powershell
# scripts/sync_to_icloud.ps1
git fetch origin main && git merge --ff-only origin/main
# copy exports/* into iCloudDrive\grocery_deals
```

It's registered to run **at every logon** and **daily at 9:15 AM** (whichever
comes first), so `iCloudDrive\grocery_deals\deals_history.csv` and the latest
dated export stay current without needing the laptop on at 9am Wednesday —
only on at *some* point afterward. Set it up once with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_icloud_sync_task.ps1
```

This also disables the old "Grocery Tracker Weekly Update" task, which ran
the full scrape locally and only worked when the PC happened to be on.

`main.py schedule` (running the full pipeline on a loop via the `schedule`
library) still works for local/manual use, but is no longer how the weekly
run actually happens.

## Manual PDF import (BJ's, H Mart)

Some stores post PDFs that require manual download:
```bash
# Download the PDF from the store website, then:
python main.py pdf_import bjs ~/Downloads/bjs_weekly.pdf
python main.py pdf_import hmart ~/Downloads/hmart_sale.pdf
```

## Database inspection

```bash
# Install sqlite-utils for handy CLI browsing
pip install sqlite-utils

sqlite-utils tables grocery_tracker.db
sqlite-utils rows grocery_tracker.db v_current_deals --limit 10
sqlite-utils query grocery_tracker.db "SELECT * FROM v_current_deals WHERE pct_off_regional_avg > 20 ORDER BY deal_score DESC"
```

Or open directly in DB Browser for SQLite (https://sqlitebrowser.org/).

## Price comparison logic

Deal quality is scored 0–100 using a weighted composite:

| Comparison | Weight | Meaning |
|-----------|--------|---------|
| Sale vs. regular shelf price | 40% | Store's own markdown |
| Sale vs. South region average (BLS) | 35% | Local market context |
| Sale vs. national average (BLS) | 25% | National benchmark |

BLS data is updated monthly. Regional data uses BLS South region (covers NC).

## Extending for more stores

1. **If the store is on Flipp**: Add its name to `TARGET_CHAINS` in `flipp_scraper.py`.
2. **If it has a PDF flyer**: Use `pdf_import` or add auto-discovery logic to `pdf_scraper.py`.
3. **If it has its own API** (rare): Create a new scraper module following the pattern in `flipp_scraper.py` and wire it into `main.py`.

## Next steps (roadmap)

- [ ] Meal planning integration: match cookbook ingredients against current deals
- [ ] Inventory tracking: know what you already have before shopping
- [ ] Push notifications: alert when a watchlisted item hits a price threshold
- [ ] Web UI: Flask/FastAPI dashboard with charts (price history, deal heatmap)
- [ ] ML price forecasting: predict when items will go on sale using historical data
- [ ] Receipt scanning: auto-update inventory by photographing receipts
