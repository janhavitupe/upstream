"""Exact-to-the-cent splits, and the bounded AI adjustment applied on top of them."""
from __future__ import annotations


def split_cents(weights: list[float], total: float) -> list[float]:
    """Split total in proportion to weights, exact to the cent (largest remainder)."""
    cents = int(round(total * 100))
    total_w = sum(weights)
    if cents <= 0 or total_w <= 0:
        return [0.0] * len(weights)
    raw = [w / total_w * cents for w in weights]
    alloc = [int(x) for x in raw]
    order = sorted(range(len(raw)), key=lambda i: raw[i] - alloc[i], reverse=True)
    for i in order[:cents - sum(alloc)]:
        alloc[i] += 1
    return [c / 100 for c in alloc]


def apply_ai_adjustments(rows: list[dict], budget: float, adjustments: dict[str, dict],
                         max_points: float, key: str = "login") -> list[dict]:
    """Shift each rule-based share by at most +/- max_points percentage points.

    The clamp is enforced here, not trusted to the model. Shares are then
    renormalised so the total still equals the budget.
    """
    fundable = [r for r in rows if r.get("amount", 0) > 0]
    if not fundable:
        return rows
    shares = []
    for r in fundable:
        adj = adjustments.get(r[key]) or {}
        try:
            points = float(adj.get("adjust_points") or 0)
        except (TypeError, ValueError):
            points = 0.0
        points = max(-max_points, min(max_points, points))
        r["rule_amount"], r["rule_percent"] = r["amount"], r["percent"]
        r["ai_adjust_points"] = round(points, 1)
        r["ai_adjust_reason"] = (adj.get("adjust_reason") or "") if points else ""
        shares.append(max(0.0, r["percent"] + points))
    total_share = sum(shares)
    if total_share <= 0:  # adjustments wiped out every share: keep the rule-based split
        for r in fundable:
            r["ai_adjust_points"], r["ai_adjust_reason"] = 0.0, ""
        return rows
    for r, amount, share in zip(fundable, split_cents(shares, budget), shares):
        r["amount"] = amount
        r["percent"] = round(100 * share / total_share, 1)
    rows.sort(key=lambda r: (-r["amount"], r[key]))
    return rows
