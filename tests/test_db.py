"""tests/test_db.py — Tests for database operations using a temp DB."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Provide a fresh temp database for each test."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))

    # Re-import db so DB_PATH picks up the env var
    import importlib
    import grocery_tracker.db as db_module
    importlib.reload(db_module)

    db_module.init_db(db_file)
    return db_module


class TestStoreOps:
    def test_upsert_store_returns_id(self, tmp_db):
        sid = tmp_db.upsert_store("harris_teeter", "Harris Teeter", "flipp")
        assert isinstance(sid, int)
        assert sid > 0

    def test_upsert_store_idempotent(self, tmp_db):
        id1 = tmp_db.upsert_store("food_lion", "Food Lion", "flipp")
        id2 = tmp_db.upsert_store("food_lion", "Food Lion", "flipp")
        assert id1 == id2

    def test_get_stores_active(self, tmp_db):
        tmp_db.upsert_store("walmart", "Walmart", "flipp")
        stores = tmp_db.get_stores(active_only=True)
        assert any(s["chain"] == "walmart" for s in stores)


class TestProductOps:
    def test_get_or_create_product(self, tmp_db):
        pid = tmp_db.get_or_create_product("chicken breast", "Chicken Breast", "meat")
        assert isinstance(pid, int)

    def test_get_or_create_idempotent(self, tmp_db):
        pid1 = tmp_db.get_or_create_product("eggs grade a", "Eggs Grade A", "dairy")
        pid2 = tmp_db.get_or_create_product("eggs grade a", "Eggs Grade A", "dairy")
        assert pid1 == pid2


class TestPriceOps:
    def test_insert_and_query_price(self, tmp_db):
        sid = tmp_db.upsert_store("publix", "Publix", "flipp")
        pid = tmp_db.get_or_create_product("bananas", "Bananas", "produce")

        tmp_db.insert_price(
            product_id=pid, store_id=sid, price=0.59,
            is_sale=True, valid_from="2025-01-01", valid_to="2099-12-31",
            original_price=0.79, source="flipp"
        )

        history = tmp_db.price_history("bananas", days=9999)
        assert len(history) >= 1
        assert any(abs(r["price"] - 0.59) < 0.01 for r in history)

    def test_bulk_insert_prices(self, tmp_db):
        sid = tmp_db.upsert_store("ht", "Harris Teeter", "flipp")
        pid = tmp_db.get_or_create_product("milk whole gallon", "Milk Whole", "dairy")

        rows = [
            {
                "product_id": pid, "store_id": sid, "price": 3.49,
                "unit_price": None, "unit_type": None, "quantity": 1,
                "is_sale": 1, "sale_type": "weekly_ad",
                "original_price": 4.29, "promo_label": "Member deal",
                "valid_from": "2025-01-01", "valid_to": "2099-12-31",
                "source": "flipp", "raw_data": None,
            }
        ]
        tmp_db.bulk_insert_prices(rows)
        history = tmp_db.price_history("milk whole gallon", days=9999)
        assert len(history) >= 1
