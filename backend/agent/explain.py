"""LLM-written justifications for each allocation.

The numbers come from deterministic scoring (backend/oss/scoring.py). The LLM only
explains them in plain language, and is told never to invent facts. Without an
ANTHROPIC_API_KEY the app falls back to the rule-based reasons.
"""
from __future__ import annotations

import json

import requests

from backend import config

SYSTEM = (
    "You write short, factual justifications for how an open-source funding budget was split. "
    "Use ONLY the facts provided. Never invent numbers, maintainers or events. "
    "Each justification is one sentence, under 30 words, plain English, no hype."
)


def explain_allocations(rows: list[dict], budget: float) -> dict[str, str] | None:
    if not config.ANTHROPIC_API_KEY:
        return None
    facts = [
        {
            "package": r["package"],
            "source_repo": r.get("source_repo"),
            "share_percent": r["percent"],
            "amount_usd": r["amount"],
            "files_importing_it": r.get("import_count", 0),
            "signals": {k: r["signals"].get(k) for k in
                        ("active_maintainers", "last_release", "pushed_at", "archived",
                         "security_issues", "has_funding", "stars")},
            "rule_reasons": r.get("reasons", []),
        }
        for r in rows if r.get("amount", 0) > 0 and r.get("signals")
    ]
    if not facts:
        return None
    prompt = (
        f"Monthly budget: ${budget:.2f}. Here is each funded dependency with the facts "
        f"that produced its share:\n{json.dumps(facts, indent=1)}\n\n"
        "Return ONLY a JSON object mapping each package name to its one-sentence justification. "
        "Lead with the most important reason (e.g. solo maintainer, stale releases, heavy usage)."
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": config.ANTHROPIC_MODEL, "max_tokens": 1500, "system": SYSTEM,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", [])
                       if b.get("type") == "text")
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        return {str(k): str(v) for k, v in data.items()}
    except Exception as e:  # never let the explainer break the analysis
        print(f"[explain] LLM explanation skipped: {e}")
        return None
