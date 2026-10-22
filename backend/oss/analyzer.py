"""Read -> Reason pipeline for Module 1: repo -> dependencies -> signals -> fair split."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from backend import config
from backend.agent.explain import MAX_ADJUST_POINTS, review_allocations

from . import deps as deps_mod
from . import github, scoring, signals

MANIFESTS = [
    ("requirements.txt", "pypi", deps_mod.parse_requirements),
    ("pyproject.toml", "pypi", deps_mod.parse_pyproject),
    ("package.json", "npm", deps_mod.parse_package_json),
]
CODE_EXT = {"pypi": (".py",), "npm": (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")}
# Folders that aren't the project's own runtime code: vendored deps, builds, tests, docs, examples
SKIP_DIRS = {"node_modules", "venv", ".venv", "dist", "build", "site-packages", "vendor",
             "test", "tests", "testing", "__tests__", "spec", "specs", "fixtures", "docs", "doc",
             "docs_src", "examples", "example", "benchmarks", "benchmark", "bench", "scripts",
             ".github", "e2e"}
TEST_FILE = re.compile(r"(^test_|_test\.py$|\.(test|spec)\.[cm]?[jt]sx?$|^conftest\.py$)")
MAX_FILES = 150


def source_files(tree: list[str], ecosystem: str, repo_name: str) -> list[str]:
    """The repo's own code files, most likely runtime source first."""
    pkg_dirs = {repo_name.lower().replace("-", "_"), repo_name.lower(), "src", "lib"}
    files = []
    for path in tree:
        parts = path.split("/")
        if (path.endswith(CODE_EXT[ecosystem]) and not TEST_FILE.search(parts[-1])
                and not any(p.lower() in SKIP_DIRS for p in parts[:-1])):
            files.append(path)
    # Files inside the main package / src folder first, then shallow paths before deep ones
    files.sort(key=lambda p: (p.split("/")[0].lower() not in pkg_dirs, p.count("/"), p))
    return files[:MAX_FILES]


def _count_usage(owner, repo, branch, packages, ecosystem):
    files = source_files(github.get_tree(owner, repo, branch), ecosystem, repo)
    patterns = {p: deps_mod.usage_pattern(p, ecosystem) for p in packages}
    with ThreadPoolExecutor(8) as ex:
        texts = list(ex.map(lambda path: github.raw_file(owner, repo, branch, path) or "", files))
    counts = {p: 0 for p in packages}
    for text in texts:
        for p, pat in patterns.items():
            if pat.search(text):
                counts[p] += 1
    return counts, len(files)


def analyze_repo(repo_ref: str, budget: float, max_deps: int = 10) -> dict:
    owner, name = github.parse_repo_ref(repo_ref)
    info = github.repo_info(owner, name)
    owner, name = info["owner"]["login"], info["name"]
    branch = info.get("default_branch", "main")
    warnings: list[str] = []

    packages, ecosystem, manifest = [], None, None
    for path, eco, parser in MANIFESTS:
        text = github.raw_file(owner, name, branch, path)
        if not text:
            continue
        try:
            found = parser(text)
        except Exception as e:  # malformed manifest
            warnings.append(f"Couldn't parse {path}: {e}")
            continue
        if found:
            packages, ecosystem, manifest = found, eco, path
            break
    if not packages:
        raise ValueError(f"No dependencies found in {owner}/{name}. Looked for requirements.txt, "
                         "pyproject.toml and package.json at the repository root.")

    try:
        usage, files_scanned = _count_usage(owner, name, branch, packages, ecosystem)
    except github.GitHubError as e:
        usage, files_scanned = {p: 0 for p in packages}, 0
        warnings.append(f"Skipped import scan: {e}")

    if len(packages) > max_deps:
        warnings.append(f"Analysed the {max_deps} most-used of {len(packages)} dependencies.")
    packages = sorted(packages, key=lambda p: -usage.get(p, 0))[:max_deps]

    def gather(pkg: str) -> dict:
        row = {"package": pkg, "ecosystem": ecosystem, "import_count": usage.get(pkg, 0)}
        try:
            src = signals.find_source_repo(pkg, ecosystem)
            if not src:
                row["error"] = "Source repository not found on GitHub"
                return row
            row["source_repo"] = f"{src[0]}/{src[1]}"
            row["signals"] = signals.repo_signals(*src)
            row["source_repo"] = row["signals"]["html_url"].split("github.com/")[-1]
        except github.GitHubError as e:
            row["error"] = str(e)
        except Exception as e:  # keep one bad dependency from failing the whole run
            row["error"] = f"{type(e).__name__}: {e}"[:200]
        return row

    with ThreadPoolExecutor(5) as ex:
        rows = list(ex.map(gather, packages))

    if any("rate limit" in (r.get("error") or "") for r in rows):
        warnings.append("GitHub rate limit hit for some dependencies. Add GITHUB_TOKEN to .env.")
    if not config.GITHUB_TOKEN:
        warnings.append("No GITHUB_TOKEN set: security-issue counts are skipped and the "
                        "rate limit is 60 requests/hour.")

    rows = scoring.score_and_allocate(rows, budget)
    idle = [r["package"] for r in rows if r.get("signals") and not r.get("work_score")]
    if idle:
        warnings.append(f"No commits or releases in the last year, so not funded: "
                        f"{', '.join(idle)}. You rely on {'it' if len(idle) == 1 else 'them'}; "
                        f"consider whether {'it needs' if len(idle) == 1 else 'they need'} "
                        f"replacing.")

    review = review_allocations(rows, budget)
    if review:
        rows = scoring.apply_ai_adjustments(rows, budget, review, MAX_ADJUST_POINTS)
    for r in rows:
        r["explanation"] = ((review or {}).get(r["package"], {}).get("explanation")
                            or "; ".join(r.get("reasons", [])).capitalize()
                            or r.get("error", ""))

    # Who the money is for: the dependency's top human committer of the last year. In the
    # sandbox real developers can't be paid, so a demo account stands in for them.
    for r in rows:
        top = ((r.get("signals") or {}).get("top_committers") or [None])[0]
        r["intended_for"] = top
    demo = config.DEMO_MAINTAINER_EMAILS
    for i, r in enumerate([r for r in rows if r["amount"] > 0]):
        r["suggested_email"] = demo[i % len(demo)] if demo else ""

    return {
        "repo": f"{owner}/{name}",
        "manifest": manifest,
        "ecosystem": ecosystem,
        "files_scanned": files_scanned,
        "budget": budget,
        "explained_by": "llm" if review else "rules",
        "sandbox": "sandbox" in config.PAYPAL_BASE,
        "ai_adjust_limit_points": MAX_ADJUST_POINTS if review else 0,
        "dependencies": rows,
        "warnings": warnings,
    }
