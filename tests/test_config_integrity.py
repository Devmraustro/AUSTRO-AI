"""AUSTRO AI - configuration integrity regressions.

CONFIG-1 (maintainability / silent-default trap)
    `Settings` declared 49 of its 75 fields twice. Python keeps the *last*
    default and the *first* position, so:

      * 31 settings documented as "required (no defaults)" silently had
        defaults - a missing value could never fail at startup;
      * editing the first declaration of a duplicated field had no effect at
        all, because the later declaration won. A typo'd default (e.g. a wrong
        AI timeout) would look applied in the diff and never take effect.

    Each field is now declared exactly once. `Settings.__init__` is unchanged
    (same order, same defaults), so this is a pure de-duplication.

CONFIG-2 (silently ignored configuration)
    A field added to `Settings` but not populated by `load_settings()` would
    keep its default forever while the documented environment variable was
    ignored. The second test derives both sides from the source and fails if
    they drift.
"""

import ast
import pathlib

SETTINGS_PATH = pathlib.Path(__file__).resolve().parents[1] / "app" / "config" / "settings.py"


def _class_node(tree, name):
    return next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name
    )


def _settings_tree():
    return ast.parse(SETTINGS_PATH.read_text(encoding="utf-8"))


def test_no_settings_field_is_declared_twice():
    cls = _class_node(_settings_tree(), "Settings")
    declared = [
        node.target.id
        for node in cls.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    duplicates = sorted({name for name in declared if declared.count(name) > 1})
    assert not duplicates, (
        "a field declared twice keeps only the last default (a silent trap): "
        f"{duplicates}"
    )


def test_every_setting_is_populated_by_load_settings():
    """No field may exist that `load_settings()` never fills in."""
    tree = _settings_tree()
    cls = _class_node(tree, "Settings")
    fields = {
        node.target.id
        for node in cls.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }

    loader = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "load_settings"
    )
    supplied = set()
    for node in ast.walk(loader):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Settings":
            supplied = {kw.arg for kw in node.keywords if kw.arg}
            break
    assert supplied, "could not find the Settings(...) construction in load_settings()"

    missing = sorted(fields - supplied)
    unknown = sorted(supplied - fields)
    assert not unknown, f"load_settings() passes fields Settings does not define: {unknown}"
    assert not missing, (
        "these Settings fields are never populated from the environment, so "
        f"their documented variables would be ignored: {missing}"
    )


def test_settings_object_exposes_every_declared_field():
    from app.config.settings import Settings, settings

    assert isinstance(settings, Settings)
    for name in Settings.__dataclass_fields__:
        assert hasattr(settings, name), name
