"""tests/test_price_compare.py — Tests for deal scoring and comparison logic."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from grocery_tracker.utils import deal_score
from grocery_tracker.price_compare import sparkline, enrich_deals


class TestSparkline:
    def test_empty(self):
        assert sparkline([]) == ""

    def test_uniform(self):
        s = sparkline([5.0, 5.0, 5.0])
        assert len(s) == 3
        assert len(set(s)) == 1   # all same char

    def test_increasing(self):
        s = sparkline([1.0, 2.0, 3.0, 4.0])
        assert s[0] < s[-1]       # first char < last char (lower bar)

    def test_length_matches_input(self):
        vals = [1.0, 2.0, 1.5, 3.0, 2.5]
        assert len(sparkline(vals)) == len(vals)


class TestEnrichDeals:
    def _make_deal(self, sale, original=None, reg_avg=None, nat_avg=None):
        return {
            "canonical_name": "test product",
            "store_name": "Test Store",
            "chain": "test",
            "category": "produce",
            "sale_price": sale,
            "original_price": original,
            "regional_avg": reg_avg,
            "national_avg": nat_avg,
            "pct_off_regional_avg": None,
            "pct_off_national_avg": None,
            "valid_from": "2025-01-01",
            "valid_to": "2099-12-31",
            "promo_label": None,
            "unit_price": None,
            "unit_type": None,
        }

    def test_score_added(self):
        deals = [self._make_deal(1.99, original_price=2.99)]
        enriched = enrich_deals(deals)
        assert "deal_score" in enriched[0]
        assert enriched[0]["deal_score"] > 0

    def test_sorted_descending(self):
        deals = [
            self._make_deal(2.00, original_price=2.10),  # tiny discount
            self._make_deal(1.00, original_price=5.00),  # big discount
        ]
        enriched = enrich_deals(deals)
        assert enriched[0]["deal_score"] >= enriched[1]["deal_score"]

    def test_no_data_score_zero(self):
        deals = [self._make_deal(2.99)]
        enriched = enrich_deals(deals)
        assert enriched[0]["deal_score"] == 0.0
