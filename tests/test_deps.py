"""Reading dependency files and finding which source files use each package."""
from backend.oss import analyzer, deps


def test_requirements_txt():
    text = "flask>=2.0\n# comment\nrequests[security]==2.31  # pinned\n-e .\npip\n\nPyYAML\n"
    names = deps.parse_requirements(text)
    assert "flask" in names and "requests" in names and "pyyaml" in names
    assert "pip" not in names


def test_package_json():
    assert deps.parse_package_json('{"dependencies": {"express": "^5", "@types/node": "1"}}') \
        == ["express", "@types/node"]


def test_import_names_cover_common_renames():
    assert "markdown_it" in deps.import_names("markdown-it-py")
    assert deps.import_names("pyyaml") == ["yaml"]
    assert "jose" in deps.import_names("python-jose")


def test_usage_pattern_matches_real_imports_only():
    py = deps.usage_pattern("markdown-it-py", "pypi")
    assert py.search("from markdown_it import MarkdownIt")
    assert not py.search("# markdown_it is great")
    js = deps.usage_pattern("debug", "npm")
    assert js.search("const debug = require('debug')('app')")
    assert js.search('import createDebug from "debug/src"')
    assert not js.search("// require('debugger')")


def test_source_files_skip_tests_docs_and_prefer_the_package():
    tree = ["docs/conf.py", "tests/test_core.py", "rich/console.py", "examples/demo.py",
            "rich/test_helpers.py", "setup.py", "rich/sub/deep.py", "conftest.py"]
    files = analyzer.source_files(tree, "pypi", "rich")
    assert files[0] == "rich/console.py"
    assert not any(f.startswith(("docs/", "tests/", "examples/")) for f in files)
    assert "rich/test_helpers.py" not in files and "conftest.py" not in files
