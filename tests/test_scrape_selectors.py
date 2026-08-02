"""The selector registry has to cover every name the codebase asks it for.

SCRAPE-01 rebuilt the search half of the registry. A rebuild is only safe if a
caller that still asks for an old name fails here rather than at runtime against
a live LinkedIn session, so this module scans the source for every selector name
anyone references and checks the registry answers all of them.
"""

import ast
import inspect
import re
from pathlib import Path

import pytest

from linkedin_mcp.browser.selectors import (
    SELECTORS,
    flatten_selector_names,
    selector_fallbacks,
    selector_payload,
    selector_union,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

LOOKUP_FUNCTIONS = frozenset(
    {
        "selector_fallbacks",
        "selector_union",
        "selector_payload",
        "flatten_selector_names",
    }
)

# `selectors.profile_name` inside an injected page.evaluate script. The payload
# handed to those scripts is built by selector_payload, so a name used in the JS
# and missing from the payload is a bug this catches too.
JS_REFERENCE = re.compile(r"\bselectors\.([a-z][a-z0-9_]*)\b")

SUBSCRIPT_REFERENCE = re.compile(r"SELECTORS\[\s*['\"]([a-z][a-z0-9_]*)['\"]\s*\]")

SKIPPED_DIRECTORIES = {".git", "__pycache__", ".venv", "venv", "node_modules"}


def python_sources() -> list[Path]:
    files = []
    for path in REPO_ROOT.rglob("*.py"):
        if SKIPPED_DIRECTORIES & set(path.parts):
            continue
        files.append(path)
    return sorted(files)


def literal_names(node: ast.AST) -> list[str]:
    """Return the string literals passed as arguments to one call."""
    names = []
    for argument in node.args:
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            names.append(argument.value)
        elif isinstance(argument, (ast.List, ast.Tuple, ast.Set)):
            for element in argument.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    names.append(element.value)
    return names


def called_name(node: ast.Call) -> str | None:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def referenced_selector_names() -> dict[str, set[Path]]:
    """Map every selector name the codebase references to the files using it."""
    found: dict[str, set[Path]] = {}

    def note(name: str, path: Path) -> None:
        found.setdefault(name, set()).add(path)

    for path in python_sources():
        source = path.read_text(encoding="utf-8")
        if path.name in {"selectors.py", "test_scrape_selectors.py"}:
            continue

        for match in SUBSCRIPT_REFERENCE.finditer(source):
            note(match.group(1), path)

        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and called_name(node) in LOOKUP_FUNCTIONS:
                for name in literal_names(node):
                    note(name, path)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for match in JS_REFERENCE.finditer(node.value):
                    note(match.group(1), path)

    return found


def test_every_selector_name_the_codebase_uses_exists_in_the_registry():
    missing = {
        name: sorted(str(path.relative_to(REPO_ROOT)) for path in paths)
        for name, paths in referenced_selector_names().items()
        if name not in SELECTORS
    }

    assert not missing, (
        "These selector names are referenced but missing from the registry. "
        "A registry rebuild must keep every name its callers already use: "
        f"{missing}"
    )


def test_the_scan_actually_finds_the_names_it_is_supposed_to_check():
    """Guard the guard. A scanner that finds nothing would pass silently."""
    found = referenced_selector_names()

    assert len(found) > 30
    assert "profile_name" in found
    assert "feed_post_container" in found
    assert "people_result_item" in found
    assert "post_result_item" in found
    assert "group_member_item" in found


def test_the_public_helper_signatures_are_unchanged():
    assert str(inspect.signature(selector_fallbacks)) == "(name: 'str') -> 'tuple[str, ...]'"
    assert str(inspect.signature(selector_union)) == "(name: 'str') -> 'str'"
    assert (
        str(inspect.signature(selector_payload))
        == "(*names: 'str') -> 'dict[str, list[str]]'"
    )
    assert (
        str(inspect.signature(flatten_selector_names))
        == "(names: 'Iterable[str]') -> 'list[str]'"
    )


@pytest.mark.parametrize("name", sorted(SELECTORS))
def test_every_group_is_a_non_empty_tuple_of_css_strings(name):
    fallbacks = SELECTORS[name]

    assert isinstance(fallbacks, tuple), f"{name} must be a tuple"
    assert fallbacks, f"{name} has no selectors"
    assert all(isinstance(entry, str) and entry.strip() for entry in fallbacks)
    assert len(set(fallbacks)) == len(fallbacks), f"{name} repeats a selector"


def test_a_missing_name_raises_rather_than_returning_nothing():
    with pytest.raises(KeyError):
        selector_fallbacks("this_selector_does_not_exist")


def test_the_union_joins_the_fallbacks_in_order():
    assert selector_union("login_username") == ", ".join(
        selector_fallbacks("login_username")
    )


def test_the_payload_is_json_friendly_lists():
    payload = selector_payload("login_username", "login_password")

    assert set(payload) == {"login_username", "login_password"}
    assert payload["login_username"] == list(selector_fallbacks("login_username"))
    assert all(isinstance(value, list) for value in payload.values())


def test_flattening_keeps_group_order():
    flattened = flatten_selector_names(["login_username", "login_password"])

    assert flattened == list(selector_fallbacks("login_username")) + list(
        selector_fallbacks("login_password")
    )


def test_the_rebuilt_search_surface_leads_with_attribute_hooks():
    """Class names churn. Attribute hooks are the durable half of the rebuild."""
    for name in ("people_result_item", "post_result_item"):
        assert "[data-" in selector_fallbacks(name)[0], name


def test_the_group_member_row_has_a_structural_fallback():
    """Group pages carry no result urn, so structure is the durable target."""
    assert any(
        '/in/' in fallback for fallback in selector_fallbacks("group_member_item")
    )


def test_the_rebuilt_groups_keep_a_fallback_chain():
    """One selector is a single point of failure. Every rebuilt group has spares."""
    rebuilt = [name for name in SELECTORS if name.startswith(("people_result_", "post_result_", "group_member_"))]

    assert len(rebuilt) >= 20
    thin = [name for name in rebuilt if len(SELECTORS[name]) < 2]
    assert not thin, f"these rebuilt groups have no fallback: {thin}"
