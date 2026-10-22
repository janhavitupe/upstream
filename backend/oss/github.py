"""Minimal GitHub client with caching and friendly rate-limit errors."""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import quote

import requests

from backend import config

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
CACHE_TTL = 600  # seconds

_session = requests.Session()
_cache: dict = {}
_lock = threading.Lock()


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class NotFound(GitHubError):
    pass


class Pending(GitHubError):
    """GitHub is still computing this (HTTP 202); ask again shortly."""


def _headers(accept: str = "application/vnd.github+json") -> dict:
    h = {"Accept": accept, "User-Agent": "upstream-hackathon",
         "X-GitHub-Api-Version": "2022-11-28"}
    if config.GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {config.GITHUB_TOKEN}"
    return h


def _cached(key):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return True, hit[1]
    return False, None


def _store(key, value):
    with _lock:
        _cache[key] = (time.time(), value)


def api_get(path: str, params: dict | None = None):
    key = ("api", path, tuple(sorted((params or {}).items())))
    found, value = _cached(key)
    if found:
        return value
    r = _session.get(API + path, params=params, headers=_headers(), timeout=20)
    if r.status_code == 404:
        raise NotFound(404, f"Not found on GitHub: {path}")
    if r.status_code in (403, 429) and r.headers.get("X-RateLimit-Remaining") == "0":
        raise GitHubError(r.status_code,
                          "GitHub rate limit reached. Add a free GITHUB_TOKEN to .env "
                          "(github.com/settings/tokens) and restart.")
    if r.status_code >= 400:
        raise GitHubError(r.status_code, f"GitHub {r.status_code} on {path}: {r.text[:200]}")
    if r.status_code == 202:  # never cache "still computing"
        raise Pending(202, f"GitHub is still computing {path}")
    value = r.json()
    _store(key, value)
    return value


def raw_file(owner: str, repo: str, ref: str, path: str) -> str | None:
    """Fetch a file's text from raw.githubusercontent.com (doesn't use the API rate limit)."""
    key = ("raw", owner, repo, ref, path)
    found, value = _cached(key)
    if found:
        return value
    url = f"{RAW}/{owner}/{repo}/{quote(ref)}/{quote(path)}"
    r = _session.get(url, headers={"User-Agent": "upstream-hackathon"}, timeout=20)
    value = r.text if r.status_code == 200 else None
    _store(key, value)
    return value


def get_file(owner: str, repo: str, path: str) -> str | None:
    return raw_file(owner, repo, "HEAD", path)


REPO_RE = re.compile(r"(?:github\.com[/:])?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


def parse_repo_ref(ref: str) -> tuple[str, str]:
    ref = ref.strip().rstrip("/")
    ref = re.sub(r"^https?://(www\.)?", "", ref)
    if ref.startswith("github.com/"):
        ref = ref[len("github.com/"):]
    parts = ref.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError("Enter a repository as owner/repo or a GitHub URL.")
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    if not REPO_RE.fullmatch(f"{owner}/{repo}"):
        raise ValueError(f"'{ref}' doesn't look like a GitHub repository.")
    return owner, repo


def repo_info(owner: str, repo: str) -> dict:
    return api_get(f"/repos/{owner}/{repo}")


def contributor_stats(owner: str, repo: str, attempts: int = 6) -> list[dict] | None:
    """Weekly additions/deletions/commits per author, or None if GitHub won't compute it."""
    for i in range(attempts):
        try:
            return api_get(f"/repos/{owner}/{repo}/stats/contributors") or []
        except Pending:
            time.sleep(min(2 * (i + 1), 6))
    return None


def get_tree(owner: str, repo: str, branch: str) -> list[str]:
    data = api_get(f"/repos/{owner}/{repo}/git/trees/{quote(branch)}", {"recursive": "1"})
    return [t["path"] for t in data.get("tree", []) if t.get("type") == "blob"]
