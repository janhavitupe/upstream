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
    cents = int(round(budget * 100))
    if fundable and total_w > 0:
        raw = [r["weight"] / total_w * cents for r in fundable]
        alloc = [int(x) for x in raw]
        remainder = cents - sum(alloc)
        order = sorted(range(len(raw)), key=lambda i: raw[i] - alloc[i], reverse=True)
        for i in order[:remainder]:
            alloc[i] += 1
        for r, c in zip(fundable, alloc):
            r["amount"] = c / 100
            r["percent"] = round(100 * r["weight"] / total_w, 1)

    for r in rows:
        r.setdefault("amount", 0.0)
        r.setdefault("percent", 0.0)
        r.setdefault("reasons", [])
        r.pop("weight", None)

    rows.sort(key=lambda r: (-r["amount"], r["package"]))
    return rows
