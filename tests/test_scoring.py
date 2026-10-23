"""The split: exact to the cent, pays for work, and the AI can only nudge it."""
import pytest

from backend.oss import scoring


@pytest.mark.parametrize("weights,total", [([1, 1, 1], 100), ([3, 2, 1], 10), ([0.1, 7, 2.5], 33.33),
                                           ([1], 5)])
def test_split_cents_adds_up_exactly(weights, total):
    parts = scoring.split_cents(weights, total)
    assert round(sum(parts), 2) == round(total, 2)
    assert all(p >= 0 for p in parts)


def test_split_cents_handles_nothing_to_split():
    assert scoring.split_cents([0, 0], 10) == [0.0, 0.0]
    assert scoring.split_cents([1, 2], 0) == [0.0, 0.0]


def test_work_score_counts_humans_and_releases():
    assert scoring.work_score({"human_commits_last_year": 10, "releases_last_year": 2}) == 20
    assert scoring.work_score({}) == 0


def _row(pkg, commits, releases, kb, imports=1):
    return {"package": pkg, "import_count": imports,
            "signals": {"human_commits_last_year": commits, "releases_last_year": releases,
                        "code_kb": kb, "active_maintainers": 3, "has_funding": True}}


def test_idle_projects_get_nothing_and_busy_ones_get_most():
    rows = scoring.score_and_allocate(
        [_row("busy", 80, 6, 120), _row("small", 5, 0, 15), _row("idle", 0, 0, 50)], 100)
    by = {r["package"]: r for r in rows}
    assert by["idle"]["amount"] == 0
    assert "not funded" in by["idle"]["reasons"][0]
    assert by["busy"]["amount"] > by["small"]["amount"] > 0
    assert round(sum(r["amount"] for r in rows), 2) == 100


def test_ai_adjustment_is_clamped_and_budget_is_kept():
    rows = [{"package": "a", "amount": 50.0, "percent": 50.0},
            {"package": "b", "amount": 50.0, "percent": 50.0}]
    scoring.apply_ai_adjustments(rows, 100, {"a": {"adjust_points": 40, "adjust_reason": "x"}}, 5)
    by = {r["package"]: r for r in rows}
    assert by["a"]["ai_adjust_points"] == 5          # asked for +40, allowed +5
    assert round(by["a"]["amount"] + by["b"]["amount"], 2) == 100
    assert by["a"]["amount"] > by["b"]["amount"]


def test_ai_adjustment_ignores_garbage():
    rows = [{"package": "a", "amount": 60.0, "percent": 60.0},
            {"package": "b", "amount": 40.0, "percent": 40.0}]
    scoring.apply_ai_adjustments(rows, 100, {"a": {"adjust_points": "lots"}}, 5)
    assert [r["amount"] for r in rows] == [60.0, 40.0]


def test_ai_adjustments_that_zero_everything_fall_back_to_the_rule():
    rows = [{"package": "a", "amount": 3.0, "percent": 3.0},
            {"package": "b", "amount": 2.0, "percent": 2.0}]
    scoring.apply_ai_adjustments(rows, 5, {"a": {"adjust_points": -5}, "b": {"adjust_points": -5}}, 5)
    assert [r["amount"] for r in rows] == [3.0, 2.0]
