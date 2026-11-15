"""Closed structural certificates for the three advanced recipe graphs.

Reconstruct every operation (including joins, grains, predicates and rounding)
from the recorded binding and compare the whole live graph. This deliberately
certifies only these recipes, not arbitrary SQL containing LEAD or division.
Schema binding and executable counterexamples are checked separately on TaskIR.
"""
from dataclasses import fields
import json
from types import SimpleNamespace

from elt_taskgen.generation.advanced_evidence import AdvancedEvidence, ADVANCED_RECIPES


def read_binding(notes):
    raw = json.loads(notes)
    if set(raw) != {"evidence", "source_tables"} or set(raw["evidence"]) != {f.name for f in fields(AdvancedEvidence)}:
        raise ValueError("Invalid advanced recipe binding")
    if not isinstance(raw["source_tables"], list) or not all(isinstance(t, str) for t in raw["source_tables"]):
        raise ValueError("Invalid source tables")
    for field in ("keys", "links", "row_keys", "history_links", "history_keys"):
        raw["evidence"][field] = tuple(raw["evidence"][field])
    return AdvancedEvidence(**raw["evidence"]), tuple(raw["source_tables"])


def certifies_advanced(plan, recipe):
    if recipe not in ADVANCED_RECIPES or plan.template_id != recipe:
        return False
    from elt_taskgen.generation.advanced_plan import build_advanced_ops
    try:
        evidence, sources = read_binding(plan.notes)
        expected = build_advanced_ops(SimpleNamespace(tables=tuple(SimpleNamespace(name=n) for n in sources)), evidence, recipe)
        return plan.ops == expected
    except (TypeError, ValueError, KeyError, AttributeError):
        return False
