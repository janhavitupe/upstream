"""The pay split: exact to the cent, and the AI can only nudge it."""
import pytest

from backend import split


@pytest.mark.parametrize("weights,total", [([1, 1, 1], 100), ([3, 2, 1], 10), ([0.1, 7, 2.5], 33.33),
                                           ([1], 5)])
def test_split_cents_adds_up_exactly(weights, total):
    parts = split.split_cents(weights, total)
    assert round(sum(parts), 2) == round(total, 2)
    assert all(p >= 0 for p in parts)


def test_split_cents_handles_nothing_to_split():
    assert split.split_cents([0, 0], 10) == [0.0, 0.0]
    assert split.split_cents([1, 2], 0) == [0.0, 0.0]


def test_ai_adjustment_is_clamped_and_budget_is_kept():
    rows = [{"login": "a", "amount": 50.0, "percent": 50.0},
            {"login": "b", "amount": 50.0, "percent": 50.0}]
    split.apply_ai_adjustments(rows, 100, {"a": {"adjust_points": 40, "adjust_reason": "x"}}, 5)
    by = {r["login"]: r for r in rows}
    assert by["a"]["ai_adjust_points"] == 5          # asked for +40, allowed +5
    assert round(by["a"]["amount"] + by["b"]["amount"], 2) == 100
    assert by["a"]["amount"] > by["b"]["amount"]


def test_ai_adjustment_ignores_garbage():
    rows = [{"login": "a", "amount": 60.0, "percent": 60.0},
            {"login": "b", "amount": 40.0, "percent": 40.0}]
    split.apply_ai_adjustments(rows, 100, {"a": {"adjust_points": "lots"}}, 5)
    assert [r["amount"] for r in rows] == [60.0, 40.0]


def test_ai_adjustments_that_zero_everything_fall_back_to_the_rule():
    rows = [{"login": "a", "amount": 3.0, "percent": 3.0},
            {"login": "b", "amount": 2.0, "percent": 2.0}]
    split.apply_ai_adjustments(rows, 5, {"a": {"adjust_points": -5}, "b": {"adjust_points": -5}}, 5)
    assert [r["amount"] for r in rows] == [3.0, 2.0]
