"""tests/test_utils.py — Unit tests for normalization and price utilities."""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from grocery_tracker.utils import (
    normalize_name,
    compute_unit_price,
    parse_price_label,
    deal_score,
    fmt_price,
)


class TestNormalizeName:
    def test_strips_package_size(self):
        assert "chicken breast" in normalize_name("Chicken Breast 3 lb")

    def test_lowercase(self):
        result = normalize_name("GROUND BEEF 80/20")
        assert result == result.lower()

    def test_strips_price_artifacts(self):
        result = normalize_name("Eggs Grade A $2.99/dozen")
        assert "$" not in result

    def test_strips_stop_words(self):
        result = normalize_name("Fresh Organic Large Eggs")
        assert "fresh" not in result
        assert "organic" not in result
        assert "large" not in result
        assert "eggs" in result

    def test_empty_string(self):
        assert normalize_name("") == ""

    def test_max_length(self):
        long_name = "a" * 200
        assert len(normalize_name(long_name)) <= 80


class TestComputeUnitPrice:
    def test_pounds(self):
        price, unit = compute_unit_price(5.97, "Ground Beef 3 lb")
        assert unit == "lb"
        assert abs(price - 1.99) < 0.01

    def test_ounces_to_lb(self):
        price, unit = compute_unit_price(3.99, "Chicken Breast 32 oz")
        assert unit == "lb"
        assert abs(price - 1.995) < 0.01  # 32oz = 2lb → $1.995/lb

    def test_fluid_oz(self):
        price, unit = compute_unit_price(4.49, "Orange Juice 52 fl oz")
        assert unit == "fl_oz"
        assert price == pytest.approx(4.49 / 52, rel=0.01)

    def test_no_size_returns_none(self):
        price, unit = compute_unit_price(2.99, "Bananas")
        assert price is None
        assert unit is None


class TestParsePriceLabel:
    def test_n_for_x(self):
        price, label = parse_price_label("2 for $5.00")
        assert price == 2.50
        assert "2 for" in label

    def test_n_slash_x(self):
        price, label = parse_price_label("3/$9")
        assert price == 3.00

    def test_bogo(self):
        price, label = parse_price_label("Buy 2 Get 1 Free")
        assert price is None
        assert "Buy 2" in label

    def test_save(self):
        price, label = parse_price_label("Save $1.50")
        assert price is None
        assert "Save" in label


class TestDealScore:
    def test_no_data_returns_zero(self):
        assert deal_score(2.99, None, None, None) == 0.0

    def test_big_discount_high_score(self):
        score = deal_score(1.00, 2.00, None, None)  # 50% off regular
        assert score > 50

    def test_above_avg_negative_discount(self):
        score = deal_score(3.00, None, 2.00, None)  # above regional avg
        assert score == 0.0

    def test_score_bounded_0_100(self):
        score = deal_score(0.01, 10.00, 10.00, 10.00)
        assert 0 <= score <= 100


class TestFmtPrice:
    def test_none(self):
        assert fmt_price(None) == "N/A"

    def test_normal(self):
        assert fmt_price(2.99) == "$2.99"

    def test_zero(self):
        assert fmt_price(0.0) == "$0.00"
