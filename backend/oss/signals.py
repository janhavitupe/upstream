"""Find each dependency's source repo and collect health signals from GitHub."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

from backend import config

from . import github

GH_URL_RE = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.IGNORECASE)
NOT_REPO_OWNERS = {"sponsors", "orgs", "users", "features", "marketplace", "topics"}
SOURCE_HINTS = ("source", "repository", "code", "github", "homepage", "home")
COMMIT_PAGES = 3  # look at up to 300 commits from the last year when counting committers
RECENT_SUBJECTS = 20  # commit subjects kept for the AI's judgement of how hard the work was

_source_cache: dict = {}


def _repo_from_url(url: str | None) -> tuple[str, str] | None:
    m = GH_URL_RE.search(url or "")
    if not m:
        return None
    owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
    if owner.lower() in NOT_REPO_OWNERS:
        return None
    return owner, repo


def find_source_repo(pkg: str, ecosystem: str) -> tuple[str, str] | None:
    key = (pkg, ecosystem)
    if key in _source_cache:
        return _source_cache[key]
    found = None
    try:
        if ecosystem == "pypi":
            r = requests.get(f"https://pypi.org/pypi/{quote(pkg)}/json", timeout=20)
            if r.status_code == 200:
                info = r.json().get("info", {})
                urls = list((info.get("project_urls") or {}).items())
                urls.sort(key=lambda kv: 0 if any(h in kv[0].lower() for h in SOURCE_HINTS) else 1)
                candidates = [u for _, u in urls] + [info.get("home_page")]
                found = next((rr for rr in map(_repo_from_url, candidates) if rr), None)
        else:
            r = requests.get(f"https://registry.npmjs.org/{quote(pkg, safe='@')}", timeout=20)
            if r.status_code == 200:
                data = r.json()
                repo = data.get("repository")
                url = repo.get("url") if isinstance(repo, dict) else repo
                bugs = data.get("bugs")
                candidates = [url, data.get("homepage"),
                              bugs.get("url") if isinstance(bugs, dict) else bugs]
                found = next((rr for rr in map(_repo_from_url, candidates) if rr), None)
    except requests.RequestException:
        found = None
    _source_cache[key] = found
    return found


def repo_signals(owner: str, repo: str) -> dict:
    info = github.repo_info(owner, repo)
    # The API follows renames; use the canonical name for follow-up calls
    owner = info.get("owner", {}).get("login", owner)
    repo = info.get("name", repo)

    year_ago = datetime.now(timezone.utc) - timedelta(days=365)
    since = year_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
    authors = set()
    by_login: dict[str, int] = {}  # GitHub login -> human commits, to show who did the work
    subjects: list[str] = []  # newest human commit subjects, for the AI to judge the work
    commits_last_year = human_commits = 0
    try:
        for page in range(1, COMMIT_PAGES + 1):
            commits = github.api_get(f"/repos/{owner}/{repo}/commits",
                                     {"since": since, "per_page": 100, "page": page})
            commits_last_year += len(commits)
            for c in commits:
                login = (c.get("author") or {}).get("login")
                name = (c.get("commit", {}).get("author") or {}).get("name") or ""
                ident = login or (c.get("commit", {}).get("author") or {}).get("email")
                if _is_bot(login, name):
                    continue  # automated dependency bumps aren't maintainer work
                human_commits += 1
                subject = (c.get("commit", {}).get("message") or "").strip().splitlines()[0:1]
                subject = subject[0] if subject else ""
                if subject and len(subjects) < RECENT_SUBJECTS:
                    subjects.append(subject[:120])
                if ident:
                    authors.add(ident.lower())
                if login:
                    by_login[login] = by_login.get(login, 0) + 1
            if len(commits) < 100:
                break
    except github.GitHubError:
        pass

    last_release, releases_last_year = None, 0
    try:
        for rel in github.api_get(f"/repos/{owner}/{repo}/releases", {"per_page": 100}):
            published = rel.get("published_at")
            if rel.get("draft") or not published:
                continue
            if not rel.get("prerelease") and (last_release is None or published > last_release):
                last_release = published
            if datetime.fromisoformat(published.replace("Z", "+00:00")) > year_ago:
                releases_last_year += 1
    except github.GitHubError:
        pass

    try:  # bytes of code per language: the size of what the maintainers keep working
        code_bytes = sum(github.api_get(f"/repos/{owner}/{repo}/languages").values())
    except github.GitHubError:
        code_bytes = None

    security_issues = None
    if config.GITHUB_TOKEN:  # search API is tightly rate-limited without a token
        try:
            q = f"repo:{owner}/{repo} is:issue is:open label:security"
            security_issues = github.api_get("/search/issues", {"q": q, "per_page": 1}).get("total_count")
        except github.GitHubError:
            security_issues = None

    funding, funding_source = find_funding(owner, repo)

    return {
        "html_url": info.get("html_url"),
        "stars": info.get("stargazers_count"),
        "archived": bool(info.get("archived")),
        "open_issues": info.get("open_issues_count"),
        "pushed_at": info.get("pushed_at"),
        "last_release": last_release,
        "active_maintainers": len(authors) if commits_last_year else 0,
        "commits_last_year": commits_last_year,
        "human_commits_last_year": human_commits,
        "commits_capped": commits_last_year >= 100 * COMMIT_PAGES,
        "releases_last_year": releases_last_year,
        "recent_commit_subjects": subjects,
        "top_committers": [{"login": l, "commits": n} for l, n in
                           sorted(by_login.items(), key=lambda kv: -kv[1])[:3]],
        "code_kb": None if code_bytes is None else round(code_bytes / 1024, 1),
        "security_issues": security_issues,
        "has_funding": funding is not None,
        "funding_source": funding_source,
        "funding_platforms": funding_platforms(funding),
    }


def _is_bot(login: str | None, name: str) -> bool:
    who = f"{login or ''} {name}".lower()
    return "[bot]" in who or any(b in who for b in ("dependabot", "renovate", "greenkeeper"))


def find_funding(owner: str, repo: str) -> tuple[str | None, str | None]:
    """FUNDING.yml in the repo, else the org/user-wide default in {owner}/.github."""
    for where, r in (("repo", repo), ("org", ".github")):
        for path in (".github/FUNDING.yml", "FUNDING.yml"):
            text = github.get_file(owner, r, path)
            if text is not None:
                return text, where
    return None, None


def funding_platforms(text: str | None) -> list[str]:
    """Platform names with a value set, e.g. ["github", "open_collective"]."""
    platforms = []
    for line in (text or "").splitlines():
        m = re.match(r"^([a-z_]+)\s*:\s*([^#\s][^#]*?)\s*(#.*)?$", line.strip())
        if m and m.group(2) not in ("", "[]", "null", "~", "''", '""'):
            platforms.append(m.group(1))
    return platforms
