# tests/test_smoke.py
import ast
from pathlib import Path

from tests.conftest import write_json

REPO = Path(__file__).resolve().parent.parent
ADMIN = REPO / "quota_keeper_admin.py"


def test_modules_load(qk, load_admin):
    adm = load_admin()
    assert callable(qk.qk_record_usage)
    assert callable(adm.qk_fetch_pricing)


def _page_source() -> str:
    """The QK_PAGE literal as Python evaluates it (escape processing applied)."""
    tree = ast.parse(ADMIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "QK_PAGE":
            return node.value.value
    raise AssertionError("QK_PAGE assignment not found")


def test_scrollable_tables_have_sticky_headers():
    """The 7 `.scroll` tables keep their column headers on screen while scrolling.
    Pin the CSS so a future edit doesn't silently drop it (the rule is the only
    thing standing between a usable and a header-less long table)."""
    page = _page_source()
    assert ".scroll thead th" in page, "no sticky rule targets .scroll thead th"
    # grab the rule block and check the key declarations
    rule = page.split(".scroll thead th", 1)[1]
    rule = rule.split("}", 1)[0]
    assert "position:sticky" in rule
    assert "top:0" in rule
    assert "background:var(--card)" in rule  # opaque: covers rows scrolling under
    assert "z-index" in rule


def test_alias_concept_hints_present():
    """The issue #2/#3 clarifications must live in the page, not only the README.

    Both misunderstandings came from the UI: the naming map was taken for a
    pricing setting, and the ledger-merge button for a pricing config. Pin the
    hints so a future edit can't drop them silently.
    """
    page = _page_source()
    # model_aliases label: a naming map for stats -- it does NOT set prices
    assert "not</b> set prices" in page
    # rename/merge: a repair tool for historical ledger buckets
    assert "REPAIR tool" in page
