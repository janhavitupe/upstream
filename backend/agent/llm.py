"""Minimal client for any OpenAI-compatible chat API (Groq by default).

Switching provider (Gemini, GitHub Models, Ollama, ...) only needs LLM_BASE_URL,
LLM_API_KEY and LLM_MODEL in .env.
"""
from __future__ import annotations

import re
import time

import requests

from backend import config

MAX_RETRY_WAIT = 20  # seconds; longer waits fail instead of hanging the request


class LLMError(RuntimeError):
    pass


def _post(body: dict) -> dict:
    """POST to /chat/completions, waiting and retrying when the provider rate-limits us."""
    if not config.llm_configured():
        raise LLMError("LLM_API_KEY is not set in .env")
    for attempt in range(3):
        r = requests.post(f"{config.LLM_BASE_URL}/chat/completions", json=body, timeout=90,
                          headers={"Authorization": f"Bearer {config.LLM_API_KEY}"})
        if r.status_code in (500, 502, 503, 504) and attempt < 2:
            time.sleep(2 + 3 * attempt)  # the provider had a hiccup: try again shortly
            continue
        if r.status_code != 429 or attempt == 2:
            break
        # Free tiers limit tokens per minute and say how long to wait ("try again in 8.01s")
        m = re.search(r"try again in ([\d.]+)(ms|s)", r.text)
        wait = (float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)) if m else 5.0
        wait = max(wait, float(r.headers.get("retry-after") or 0)) + 0.5
        if wait > MAX_RETRY_WAIT:
            break
        time.sleep(wait)
    if r.status_code == 429:
        raise LLMError("The AI provider's rate limit was reached; try again in a minute.")
    if r.status_code >= 400:
        raise LLMError(f"HTTP {r.status_code} {r.text[:300]}")
    return r.json()["choices"][0]["message"]


def complete(prompt: str, *, system: str | None = None, max_tokens: int = 1024,
             json_mode: bool = False, temperature: float = 0.2) -> str:
    messages = ([{"role": "system", "content": system}] if system else []) + \
        [{"role": "user", "content": prompt}]
    body: dict = {"model": config.LLM_MODEL, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return _post(body).get("content") or ""


def chat(messages: list[dict], *, tools: list[dict] | None = None, max_tokens: int = 2000,
         temperature: float = 0.2) -> dict:
    """One chat turn with optional tools. Returns the assistant message (content, tool_calls)."""
    body: dict = {"model": config.LLM_MODEL, "messages": messages, "max_tokens": max_tokens,
                  "temperature": temperature}
    if tools:
        body["tools"] = tools
    return _post(body)


def vision(prompt: str, image: bytes, mime: str, *, max_tokens: int = 1500,
           json_mode: bool = False) -> str:
    """Ask the vision model about an image (e.g. a photographed bill)."""
    import base64
    body: dict = {"model": config.LLM_VISION_MODEL, "max_tokens": max_tokens, "temperature": 0,
                  "messages": [{"role": "user", "content": [
                      {"type": "text", "text": prompt},
                      {"type": "image_url", "image_url": {
                          "url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}}]}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return _post(body).get("content") or ""


def list_models() -> list[str]:
    r = requests.get(f"{config.LLM_BASE_URL}/models", timeout=30,
                     headers={"Authorization": f"Bearer {config.LLM_API_KEY}"})
    if r.status_code >= 400:
        raise LLMError(f"HTTP {r.status_code} {r.text[:300]}")
    return sorted(m["id"] for m in r.json().get("data", []))
