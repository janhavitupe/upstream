"""Minimal client for any OpenAI-compatible chat API (Groq by default).

Switching provider (Gemini, GitHub Models, Ollama, ...) only needs LLM_BASE_URL,
LLM_API_KEY and LLM_MODEL in .env.
"""
from __future__ import annotations

import requests

from backend import config


class LLMError(RuntimeError):
    pass


def complete(prompt: str, *, system: str | None = None, max_tokens: int = 1024,
             json_mode: bool = False, temperature: float = 0.2) -> str:
    if not config.llm_configured():
        raise LLMError("LLM_API_KEY is not set in .env")
    messages = ([{"role": "system", "content": system}] if system else []) + \
        [{"role": "user", "content": prompt}]
    body: dict = {"model": config.LLM_MODEL, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(f"{config.LLM_BASE_URL}/chat/completions", json=body, timeout=60,
                      headers={"Authorization": f"Bearer {config.LLM_API_KEY}"})
    if r.status_code >= 400:
        raise LLMError(f"HTTP {r.status_code} {r.text[:300]}")
    return r.json()["choices"][0]["message"].get("content") or ""


def list_models() -> list[str]:
    r = requests.get(f"{config.LLM_BASE_URL}/models", timeout=30,
                     headers={"Authorization": f"Bearer {config.LLM_API_KEY}"})
    if r.status_code >= 400:
        raise LLMError(f"HTTP {r.status_code} {r.text[:300]}")
    return sorted(m["id"] for m in r.json().get("data", []))
