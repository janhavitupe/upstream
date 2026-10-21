"""Deterministic, explainable scoring: weight = criticality x (0.5 + health risk)."""
from __future__ import annotations

from datetime import datetime, timezone


def months_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((datetime.now(timezone.utc) - then).days / 30.4))


def health_risk(sig: dict) -> tuple[float, list[str]]:
    risk, reasons = 0.0, []

    m = sig.get("active_maintainers")
    if m is not None:
        if m == 0:
            risk += 0.4
            reasons.append("no commits in the last year")
        elif m == 1:
            risk += 0.4
            reasons.append("solo maintainer (1 active committer in the last year)")
        elif m <= 3:
            risk += 0.2
            reasons.append(f"small team ({m} active committers in the last year)")
        else:
            label = f"{m}+" if sig.get("commits_capped") else str(m)
            reasons.append(f"{label} active committers")

    rel_m = months_since(sig.get("last_release"))
    if rel_m is not None:
        if rel_m >= 12:
            risk += 0.25
            reasons.append(f"last release {rel_m} months ago")
        elif rel_m >= 6:
            risk += 0.1
            reasons.append(f"last release {rel_m} months ago")
    else:
        push_m = months_since(sig.get("pushed_at"))
        if push_m is not None and push_m >= 12:
            risk += 0.2
            reasons.append(f"no releases published; last push {push_m} months ago")

    sec = sig.get("security_issues")
    if sec:
        risk += min(0.2, 0.05 * sec)
        reasons.append(f"{sec} open security-labelled issue{'s' if sec != 1 else ''}")

    if sig.get("archived"):
        risk += 0.15
        reasons.append("repository is archived")

    if not sig.get("has_funding"):
        risk += 0.05
        reasons.append("no funding channel (FUNDING.yml) set up")

    return min(risk, 1.0), reasons


def criticality(import_count: int, max_count: int) -> float:
    if max_count <= 0:
        return 1.0
    return 0.3 + 0.7 * (import_count / max_count)


def score_and_allocate(rows: list[dict], budget: float) -> list[dict]:
    fundable = [r for r in rows if r.get("signals")]
    max_imp = max((r.get("import_count", 0) for r in fundable), default=0)

    for r in fundable:
        risk, reasons = health_risk(r["signals"])
        crit = criticality(r.get("import_count", 0), max_imp)
        n = r.get("import_count", 0)
        usage = (f"imported in {n} of your files" if n
                 else "direct dependency (no imports found in the scanned files)")
        r.update(risk=round(risk, 2), criticality=round(crit, 2),
                 reasons=[usage] + reasons, weight=crit * (0.5 + risk))

    total_w = sum(r["weight"] for r in fundable)
    if fundable and total_w > 0:
        for r, amount in zip(fundable, split_cents([r["weight"] for r in fundable], budget)):
            r["amount"] = amount
            r["percent"] = round(100 * r["weight"] / total_w, 1)

    for r in rows:
        r.setdefault("amount", 0.0)
        r.setdefault("percent", 0.0)
        r.setdefault("reasons", [])
        r.pop("weight", None)

    rows.sort(key=lambda r: (-r["amount"], r["package"]))
    return rows


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
                         max_points: float) -> list[dict]:
    """Shift each rule-based share by at most +/- max_points percentage points.

    The clamp is enforced here, not trusted to the model. Shares are then
    renormalised so the total still equals the budget.
    """
    fundable = [r for r in rows if r.get("amount", 0) > 0]
    if not fundable:
        return rows
    shares = []
    for r in fundable:
        adj = adjustments.get(r["package"]) or {}
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
    rows.sort(key=lambda r: (-r["amount"], r["package"]))
    return rows
