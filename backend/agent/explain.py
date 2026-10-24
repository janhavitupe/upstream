"""LLM review of each team member's pay: a bounded adjustment plus a plain-language reason.

The base numbers come from a deterministic split (backend/payroll.py). The LLM may
nudge each share by a few percentage points (split.apply_ai_adjustments enforces the
limit) and explains each one, and is told never to invent facts. Without an LLM_API_KEY
the app keeps the rule-based split and reasons.
"""
from __future__ import annotations

import json

from backend import config

from .llm import complete

MAX_ADJUST_POINTS = 5.0

SYSTEM = (
    "You review how a monthly team payroll was split between a repository's contributors. "
    "Use ONLY the facts provided. Never invent numbers, people or events."
)


def review_contributors(rows: list[dict], total: float) -> dict[str, dict] | None:
    """Returns {login: {"explanation", "adjust_points", "adjust_reason"}}, or None."""
    if not config.llm_configured():
        return None
    facts = [{"login": r["login"], "rule_share_percent": r["percent"], "commits": r["commits"],
              "lines_changed": r.get("lines_changed"), "commit_subjects": r.get("subjects", [])}
             for r in rows if r.get("amount", 0) > 0]
    if not facts:
        return None
    prompt = (
        f"Monthly team payroll: ${total:.2f} for one repository. The rule pays each contributor "
        "for their work this period (commits) times its size (lines changed), with diminishing "
        f"returns. Shares:\n{json.dumps(facts, indent=1)}\n\n"
        f"You may adjust each share by between -{MAX_ADJUST_POINTS:g} and "
        f"+{MAX_ADJUST_POINTS:g} percentage points, judging ONLY how difficult the work was, "
        "from commit_subjects. Counts and line totals are already in the rule: do not adjust "
        "for them again. Harder work: security fixes, core bug fixes, new features, "
        "performance work, major refactors. Easier work: typos, docs, formatting, CI or "
        "tooling tweaks, version bumps, dependency updates, generated or vendored files "
        "(which inflate lines changed). Use 0 when the work is ordinary or there is too little "
        "to judge. Adjustments should roughly cancel out.\n\n"
        "Return ONLY a JSON object mapping each login to an object with:\n"
        '  "explanation": one sentence under 30 words describing what they worked on this '
        "period. Do not mention dollar amounts or percentages.\n"
        '  "adjust_points": a number (percentage points, 0 if no change)\n'
        '  "adjust_reason": one short sentence naming the kind of work when adjust_points is '
        'not 0, else ""'
    )
    try:
        text = complete(prompt, system=SYSTEM, max_tokens=3000, json_mode=True)
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception as e:
        print(f"[explain] payroll review skipped: {e}")
        return None
    return {str(k): {"explanation": str(v.get("explanation") or ""),
                     "adjust_points": v.get("adjust_points", 0),
                     "adjust_reason": str(v.get("adjust_reason") or "")}
            for k, v in data.items() if isinstance(v, dict)} or None
