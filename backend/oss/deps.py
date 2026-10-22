"""Parse dependency manifests and build import-detection patterns."""
from __future__ import annotations

import json
import re

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

# PyPI package name -> import name, for packages where they differ
IMPORT_ALIASES = {
    "opencv-python": "cv2", "opencv-contrib-python": "cv2", "opencv-python-headless": "cv2",
    "scikit-learn": "sklearn", "scikit-image": "skimage", "pillow": "PIL",
    "beautifulsoup4": "bs4", "python-dotenv": "dotenv", "pyyaml": "yaml",
    "python-dateutil": "dateutil", "protobuf": "google.protobuf", "pymupdf": "fitz",
    "python-multipart": "multipart", "attrs": "attr", "pyjwt": "jwt",
    "typing-extensions": "typing_extensions", "msgpack-python": "msgpack",
    "google-generativeai": "google.generativeai", "uvicorn": "uvicorn",
}

# Packaging/tooling we don't treat as runtime dependencies to fund
SKIP = {"pip", "setuptools", "wheel", "python"}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dedupe(names) -> list[str]:
    out, seen = [], set()
    for n in names:
        n = normalize(n)
        if n and n not in seen and n not in SKIP:
            seen.add(n)
            out.append(n)
    return out


def parse_requirements(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-") or "://" in line:
            continue
        m = NAME_RE.match(line)
        if m:
            names.append(m.group(1))
    return _dedupe(names)


def parse_pyproject(text: str) -> list[str]:
    data = tomllib.loads(text)
    names = []
    for dep in data.get("project", {}).get("dependencies", []) or []:
        m = NAME_RE.match(dep)
        if m:
            names.append(m.group(1))
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
    names += [k for k in poetry if k.lower() != "python"]
    return _dedupe(names)


def parse_package_json(text: str) -> list[str]:
    data = json.loads(text)
    out, seen = [], set()
    for name in (data.get("dependencies") or {}):
        if name not in seen:
            seen.add(name)
            out.append(name)  # npm names are case-sensitive and may be scoped; keep as-is
    return out


def import_names(pkg: str) -> list[str]:
    """Likely module names for a PyPI package: markdown-it-py -> markdown_it, python-jose -> jose."""
    if pkg in IMPORT_ALIASES:
        return [IMPORT_ALIASES[pkg]]
    base = pkg.lower()
    names = [base.replace("-", "_")]
    for prefix in ("python-", "py-"):
        if base.startswith(prefix):
            names.append(base[len(prefix):].replace("-", "_"))
    for suffix in ("-py", "-python", "-python3"):
        if base.endswith(suffix):
            names.append(base[: -len(suffix)].replace("-", "_"))
    return list(dict.fromkeys(names))


def usage_pattern(pkg: str, ecosystem: str) -> re.Pattern:
    if ecosystem == "pypi":
        mods = "|".join(re.escape(n) for n in import_names(pkg))
        return re.compile(rf"^\s*(?:from|import)\s+(?:{mods})\b", re.MULTILINE | re.IGNORECASE)
    name = re.escape(pkg)
    return re.compile(rf"""(?:require\(\s*|from\s+|import\s+)['"]{name}(?:/[^'"]*)?['"]""")
