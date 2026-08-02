"""Every RunConfig field must be reachable — settable *and* actually read.

This exists because the same defect appeared four times in one review: a field
defined in `config.py`, described in the documentation, and never wired to
anything. `gs_jobs` was documented as the bound on Ghostscript and never read;
`filename_pattern` was honoured by the render path but not by fetch. Pydantic
defaults make such a field behave perfectly normally, so nothing fails — the
option simply does not exist.

Unit tests cannot catch this by construction: they call the function directly
with the parameter, which proves the function *can* do it, not that anything
does. So the check has to look at the call graph instead.

The project already enforces the same thing for `Profile` (see
`test_bootstrap.py::test_example_yaml_mentions_every_model_field`); this extends
it to the runtime config.
"""

from __future__ import annotations

import ast
from pathlib import Path

from imaparc.config import RunConfig

# Fields deliberately exempt, each for a stated reason. Adding to this list is a
# decision, which is the point: it cannot happen by omission.
_EXEMPT = {
    # Resolved once per run from the environment, never passed in by a caller.
    "icc_profile",
    # Always supplied at construction; a RunConfig without them is meaningless,
    # so a missing wire-up would fail loudly rather than silently.
    "tools",
    "verbosity",
}

_SRC = Path(__file__).resolve().parent.parent / "src" / "imaparc"


def _modules() -> list[tuple[Path, ast.Module]]:
    """Every source module except the one that defines the config itself."""
    return [
        (path, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(_SRC.rglob("*.py"))
        if "__pycache__" not in path.parts and path.name != "config.py"
    ]


def _fields_set_by_callers() -> set[str]:
    """Field names passed as keywords to a ``RunConfig(...)`` call."""
    found: set[str] = set()
    for _path, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "RunConfig":
                continue
            found.update(kw.arg for kw in node.keywords if kw.arg)
    return found


def _fields_read() -> set[str]:
    """Attribute names read off anything called ``config``/``cfg``/``run_config``."""
    found: set[str] = set()
    for _path, tree in _modules():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in {"config", "cfg", "run_config"}
            ):
                found.add(node.attr)
    return found


def test_every_runconfig_field_can_be_set_by_a_caller() -> None:
    """A field nobody passes is not an option — it is a constant with a docstring."""
    unreachable = sorted(
        set(RunConfig.model_fields) - _EXEMPT - _fields_set_by_callers()
    )

    assert unreachable == [], (
        f"RunConfig fields nothing ever sets: {unreachable}. "
        "Wire them to a profile/CLI option, delete them, or add them to _EXEMPT "
        "with a reason."
    )


def test_every_runconfig_field_is_actually_read() -> None:
    """A field nobody reads changes nothing, however carefully it is documented."""
    unused = sorted(set(RunConfig.model_fields) - _EXEMPT - _fields_read())

    assert unused == [], (
        f"RunConfig fields nothing ever reads: {unused}. "
        "This is how gs_jobs shipped as a documented memory bound that did nothing."
    )


def test_the_exemptions_still_refer_to_real_fields() -> None:
    """A renamed field must not leave a stale exemption silently covering nothing."""
    stale = sorted(_EXEMPT - set(RunConfig.model_fields))

    assert stale == [], f"_EXEMPT names fields that no longer exist: {stale}"
