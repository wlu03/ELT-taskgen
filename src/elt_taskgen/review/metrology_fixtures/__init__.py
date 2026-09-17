"""Expose lazily loaded metrology fixture families.

Each family contributes specimens and a canary to the pool digest. Lazy construction
avoids an import cycle with `review.metrology`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Iterator

from elt_taskgen.review.metrology_fixtures.families import (
    ANCHOR_KINDS,
    FAMILY_SEPARATOR,
    KIND_ROLES,
    SPECIMEN_KINDS,
    FixtureFamily,
    InjectorAnchor,
    build_prose,
    family_of,
    resolve_anchor,
    specimen_name,
)

__all__ = [
    "ANCHOR_KINDS",
    "FAMILIES",
    "FAMILY_NAMES",
    "FAMILY_SEPARATOR",
    "KIND_ROLES",
    "SPECIMEN_KINDS",
    "FixtureFamily",
    "InjectorAnchor",
    "build_prose",
    "family_of",
    "resolve_anchor",
    "specimen_name",
]

#: Family names in pool order (the demo first, then by name).
FAMILY_NAMES: tuple[str, ...] = ("demo", "clinic_visits", "stock_ledger")


def _build(name: str) -> FixtureFamily:
    if name == "demo":
        from elt_taskgen.review.metrology_fixtures import demo

        return demo.FAMILY
    if name == "clinic_visits":
        from elt_taskgen.review.metrology_fixtures import clinic_visits

        return clinic_visits.family()
    if name == "stock_ledger":
        from elt_taskgen.review.metrology_fixtures import stock_ledger

        return stock_ledger.family()
    raise KeyError(name)


class _Families(Mapping[str, FixtureFamily]):
    """`Mapping[str, FixtureFamily]`, built per family on first access.

    Read-only and ordered as `FAMILY_NAMES`; building a family reads
    vocabulary constants from metrology, hence the laziness (see the module
    docstring). Every access after the first returns the same object.
    """

    def __init__(self) -> None:
        self._built: dict[str, FixtureFamily] = {}

    def __getitem__(self, name: str) -> FixtureFamily:
        if name not in FAMILY_NAMES:
            raise KeyError(name)
        family = self._built.get(name)
        if family is None:
            family = self._built[name] = _build(name)
        return family

    def __iter__(self) -> Iterator[str]:
        return iter(FAMILY_NAMES)

    def __len__(self) -> int:
        return len(FAMILY_NAMES)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"FAMILIES({', '.join(FAMILY_NAMES)})"


FAMILIES: Mapping[str, FixtureFamily] = _Families()
