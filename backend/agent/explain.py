"""LLM review of each allocation: a bounded adjustment plus a plain-language justification.

The base numbers come from deterministic scoring (backend/oss/scoring.py). The LLM may
nudge each share by a few percentage points (scoring.apply_ai_adjustments enforces the
limit) and explains each one, and is told never to invent facts. Without an LLM_API_KEY
the app keeps the rule-based split and reasons.
"""
from __future__ import annotations

import json

from backend import config

from .llm import complete

MAX_ADJUST_POINTS = 5.0

SYSTEM = (
    "You review how an open-source funding budget was split between a project's dependencies. "
    "Use ONLY the facts provided. Never invent numbers, maintainers or events. "
    "Keep each reason's exact meaning: 'no commits in the last year' is not 'no maintainers', "
    "and a committer count is not the same as the number of people who maintain the project."
)


def review_allocations(rows: list[dict], budget: float) -> dict[str, dict] | None:
    """Returns {package: {"explanation", "adjust_points", "adjust_reason"}}, or None."""
    if not config.llm_configured():
        return None
    facts = [
        {
            "package": r["package"],
            "source_repo": r.get("source_repo"),
            "rule_share_percent": r["percent"],
            "files_importing_it": r.get("import_count", 0),
            # "committers" rather than "maintainers": the data counts commit authors
            "signals": {"committers_last_year": r["signals"].get("active_maintainers"),
                        **{k: r["signals"].get(k) for k in
                           ("human_commits_last_year", "releases_last_year", "code_kb",
                            "open_issues", "last_release", "archived", "security_issues",
                            "has_funding", "funding_platforms", "stars")}},
            "recent_commit_subjects": r["signals"].get("recent_commit_subjects", []),
            "rule_reasons": r.get("reasons", []),
        }
        for r in rows if r.get("amount", 0) > 0 and r.get("signals")
    ]
    if not facts:
        return None
    prompt = (
        f"Monthly budget: ${budget:.2f}. The rule pays each dependency for the work done in "
        "the last year (human commits plus releases) times the complexity of the job (code size "
        "in KB), with diminishing returns. Dependencies with no work in the last year get "
        f"nothing and are not listed. Shares:\n{json.dumps(facts, indent=1)}\n\n"
        f"You may adjust each share by between -{MAX_ADJUST_POINTS:g} and "
        f"+{MAX_ADJUST_POINTS:g} percentage points, judging ONLY how difficult the work was, "
        "from recent_commit_subjects (the newest commits of the last year). The counts and code "
        "size are already in the rule: do not adjust for them again. Harder work: security "
        "fixes, bug fixes in core logic, new features, performance work, major refactors, "
        "breaking-change releases. Easier work: typos, docs, formatting, CI or tooling tweaks, "
        "version bumps, dependency updates, merge commits. Never raise a share because of low "
        "activity or open issues: this budget pays for work done. Use 0 when the work is "
        "ordinary or the subjects are too few to judge. Adjustments should roughly cancel out."
        "\n\n"
        "Return ONLY a JSON object mapping each package name to an object with:\n"
        '  "explanation": one sentence under 30 words, plain English, no hype, describing the '
        "work done and the size of the job. Do not mention dollar amounts or percentages.\n"
        '  "adjust_points": a number (percentage points, 0 if no change)\n'
        '  "adjust_reason": one short sentence naming the kind of work seen in the commits '
        '(e.g. "Recent commits include a security fix") when adjust_points is not 0, else ""'
    )
    try:
        text = complete(prompt, system=SYSTEM, max_tokens=3000, json_mode=True)
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception as e:  # never let the reviewer break the analysis
        print(f"[explain] LLM review skipped: {e}")
        return None
    out = {}
    for pkg, v in data.items():
        if isinstance(v, str):  # model returned just an explanation
            v = {"explanation": v}
        if isinstance(v, dict):
            out[str(pkg)] = {"explanation": str(v.get("explanation") or ""),
                             "adjust_points": v.get("adjust_points", 0),
                             "adjust_reason": str(v.get("adjust_reason") or "")}
    return out or None
