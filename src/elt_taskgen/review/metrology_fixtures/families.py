"""Define metrology fixture families and injector anchors.

A family supplies a frozen task factory, per-role anchors, specimen builders, and a
canary. Injectors remain implemented in `review.metrology` and are imported lazily to
avoid a cycle. Non-demo families use distinct schemas and generator paths.
"""

from __future__ import annotations

import importlib
import inspect
import re
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from elt_taskgen.models import CouncilRole, MartOpKind, PopulationName, TaskIR

if TYPE_CHECKING:  # pragma: no cover - typing only (metrology imports us)
    from elt_taskgen.review.metrology import Specimen

__all__ = [
    "ANCHOR_KINDS",
    "FAMILY_SEPARATOR",
    "KIND_ROLES",
    "SPECIMEN_KINDS",
    "AmbiguityVariant",
    "FamilyDefinition",
    "FeasibilityVariant",
    "FixtureFamily",
    "InjectorAnchor",
    "PopulationVariant",
    "ShortcutVariant",
    "build_family",
    "build_prose",
    "family_of",
    "resolve_anchor",
    "specimen_name",
]

#: The specimen kinds every family builds, in pool order: the clean pool plus
#: one tampered pool per critic class. Keys of `FixtureFamily.specimen_builders`.
SPECIMEN_KINDS: tuple[str, ...] = (
    "clean",
    "ambiguity",
    "population",
    "shortcut",
    "feasibility",
)

#: Tampered specimen kind -> the critic seat it targets.
KIND_ROLES: Mapping[str, CouncilRole] = {
    "ambiguity": CouncilRole.AMBIGUITY_CRITIC,
    "population": CouncilRole.POPULATION_ADVERSARY,
    "shortcut": CouncilRole.SHORTCUT_ATTACKER,
    "feasibility": CouncilRole.FEASIBILITY_REVIEWER,
}

#: Supported fixture mutations; each injector validates its target format.
ANCHOR_KINDS: tuple[str, ...] = (
    "omit_op",
    "contradict_column",
    "replace_conditions",
    "drop_population",
    "describe_column",
    "keep_populations",
    "drop_column",
    "drop_table",
    "prose_reference",
)

#: Separator between a specimen's own name and its family name, appended as a
#: SUFFIX (`feasibility-missing-units@clinic_visits`) so every pool name keeps
#: its CLASS PREFIX — existing pool consumers key on `name.startswith(
#: "feasibility-")` — while `family_of` parses the family back. Demo names carry
#: no suffix, so the demo pool's names, and every test keyed on them, stay put.
FAMILY_SEPARATOR = "@"

_POPULATION_PREFIX = "population:"


def _metrology() -> ModuleType:
    """The metrology module, imported at CALL time (see the module docstring)."""
    return importlib.import_module("elt_taskgen.review.metrology")


def _build_specimen(family: str, name: str, **kwargs):
    """`metrology._specimen` for one family, stamping `family=` when the
    harness-6 `Specimen` carries the field (metrology re-stamps otherwise)."""
    m = _metrology()
    if "family" in inspect.signature(m._specimen).parameters:
        kwargs["family"] = family
    return m._specimen(specimen_name(family, name), **kwargs)


# Public API objects

@dataclass(frozen=True)
class InjectorAnchor:
    """Where one of metrology's injectors is applied inside one family.

    `role` is the critic seat the resulting specimen targets, `kind` is one of
    `ANCHOR_KINDS`, `target` names the PUBLIC identifier (a mart op, a mart
    column, a population, a source column or table) in that kind's grammar and
    `note` says what the tamper plants. Pure data: the builder that consumes
    it lives in this module, so `metrology.py` applies its injectors per
    family without editing them.
    """

    role: str
    kind: str
    target: str
    note: str

    def __post_init__(self) -> None:
        if self.kind not in ANCHOR_KINDS:
            raise ValueError(f"unknown anchor kind {self.kind!r}")
        if self.role not in {r.value for r in CouncilRole}:
            raise ValueError(f"unknown critic role {self.role!r}")
        if not self.target:
            raise ValueError("anchor target must name a public identifier")


@dataclass(frozen=True)
class FixtureFamily:
    """One frozen fixture family (see the module docstring)."""

    name: str
    #: A FRESH `TaskIR` per call, so no two specimens share mutable state.
    task: Callable[[], TaskIR]
    #: critic role value -> the anchors that role's specimens are planted on.
    anchors: Mapping[str, Sequence[InjectorAnchor]]
    #: specimen kind (`SPECIMEN_KINDS`) -> builder of that kind's specimens.
    #: Each builder returns the kind's whole pool, exactly as
    #: `metrology.clean_specimens()` and its siblings do.
    specimen_builders: Mapping[str, Callable[..., tuple["Specimen", ...]]]
    #: Contamination canary: lives in this family's SOURCE, is folded into
    #: `pool_sha256`, and never enters any rendered view.
    canary_guid: str

    def __post_init__(self) -> None:
        if set(self.specimen_builders) != set(SPECIMEN_KINDS):
            raise ValueError(
                f"family {self.name!r}: specimen_builders must be keyed by "
                f"{SPECIMEN_KINDS}, got {sorted(self.specimen_builders)}"
            )
        unknown = set(self.anchors) - {r.value for r in CouncilRole}
        if unknown:
            raise ValueError(f"family {self.name!r}: anchors keyed by non-roles {sorted(unknown)}")

    def specimens(self) -> tuple["Specimen", ...]:
        """Every SCORED specimen of this family, kinds in `SPECIMEN_KINDS` order."""
        out: list[Specimen] = []
        for kind in SPECIMEN_KINDS:
            out.extend(self.specimen_builders[kind]())
        return tuple(out)

    def anchors_for(self, role: CouncilRole | str) -> tuple[InjectorAnchor, ...]:
        key = role.value if isinstance(role, CouncilRole) else role
        return tuple(self.anchors.get(key, ()))


def specimen_name(family: str, name: str) -> str:
    """The pool-wide name of specimen `name` of `family` (class prefix kept)."""
    return f"{name}{FAMILY_SEPARATOR}{family}"


def family_of(name: str, *, default: str = "demo") -> str:
    """The family a pool specimen name belongs to (demo names carry no suffix)."""
    _, sep, tail = name.rpartition(FAMILY_SEPARATOR)
    return tail if sep else default


def _population(value: str) -> PopulationName:
    try:
        return PopulationName(value)
    except ValueError as exc:
        raise ValueError(f"anchor names unknown population {value!r}") from exc


def _populations_of(target: str) -> tuple[PopulationName, ...]:
    if not target.startswith(_POPULATION_PREFIX):
        raise ValueError(f"population anchor target must start with {_POPULATION_PREFIX!r}: {target!r}")
    return tuple(_population(v) for v in target[len(_POPULATION_PREFIX):].split(",") if v)


def resolve_anchor(task: TaskIR, anchor: InjectorAnchor) -> None:
    """Fail loud unless `anchor.target` names something the CLEAN task publishes.

    A tamper anchored on nothing plants nothing; checking the anchor against
    the untampered task is what makes the anchors auditable metadata rather
    than free text.
    """
    kind, target = anchor.kind, anchor.target
    if kind in ("replace_conditions", "drop_population", "keep_populations"):
        declared = {p.name for p in task.populations}
        missing = [p.value for p in _populations_of(target) if p not in declared]
        if missing:
            raise ValueError(f"anchor {target!r}: populations {missing} not in the clean task")
        return
    if kind == "drop_table":
        if target not in {t.name for t in task.tables}:
            raise ValueError(f"anchor {target!r}: no such source table")
        return
    if kind == "drop_column":
        table, _, column = target.partition(".")
        if column not in {c.name for c in task.table(table).columns}:
            raise ValueError(f"anchor {target!r}: no such source column")
        return
    mart_name, _, rest = target.partition(".")
    mart = task.mart(mart_name)
    if kind == "omit_op":
        if not (rest.startswith("ops[") and rest.endswith("]")):
            raise ValueError(f"anchor {target!r}: omit_op targets '<mart>.ops[<index>]'")
        index = int(rest[4:-1])
        if not 0 <= index < len(mart.plan.ops):
            raise ValueError(f"anchor {target!r}: op index out of range")
        return
    if rest not in {c.name for c in mart.columns}:
        raise ValueError(f"anchor {target!r}: no such mart column")


# Solver-visible prose, count-neutral

#: SURFACE realizations of the project overview, count-parametrised on the
#: number of published source tables (`metrology._OVERVIEW_VARIANTS` hard-codes
#: "three", the demo's count). NONE states or hints at any mart RULE.
_OVERVIEW_VARIANTS = (
    "This project builds an analytics warehouse from {n} operational "
    "sources and publishes the mart described below.",
    "You are asked to extract {n} operational sources, load them into the "
    "warehouse, and publish the mart described below.",
    "{N} operational source systems feed a warehouse; your job is to "
    "extract them, load them, and publish the mart specified below.",
    "The work is an ELT pipeline: {n} operational sources are extracted and "
    "loaded into a warehouse, on top of which the mart below is published.",
    "Below is a warehouse build over {n} operational source systems, "
    "together with the specification of the mart it must publish.",
    "This specification covers {n} operational sources, the warehouse they "
    "are loaded into, and the mart that is published from them.",
)

#: SURFACE realizations of the closing deliverables sentence (family-neutral).
_DELIVERABLE_VARIANTS = (
    "Deliverables: extract every source backend, load the tables into "
    "the warehouse, and build each mart exactly as specified.",
    "What to deliver: an extraction of every source backend, a load of those "
    "tables into the warehouse, and each mart built exactly as specified.",
    "To deliver: every source backend extracted, every table loaded into the "
    "warehouse, and each mart built exactly to the specification above.",
)

_NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def build_prose(
    task: TaskIR,
    *,
    omit_op_kinds: frozenset[MartOpKind] = frozenset(),
    omit_op_indices: frozenset[int] = frozenset(),
    variant: int = 0,
) -> str:
    """Deterministic solver-visible prose for a (possibly tampered) task.

    Line for line the shape of `metrology.build_prose` (schema block, numbered
    rules, deliverables), with the overview's source count read off the task
    instead of fixed at the demo's three. Derived ONLY from public fields.
    """
    count = len(task.tables)
    word = _NUMBER_WORDS.get(count, str(count))
    overview = _OVERVIEW_VARIANTS[variant % len(_OVERVIEW_VARIANTS)].format(
        n=word, N=word.capitalize()
    )
    lines: list[str] = [
        f"Project: {task.title or task.task_id}.",
        overview,
        "",
        "Public source schema:",
    ]
    for table in task.tables:
        backend = task.backend_for(table.name).backend.value
        lines.append(f"- {table.name} ({backend}): {table.description}")
        for col in table.columns:
            bits = [col.type.value]
            if col.nullable:
                bits.append("nullable")
            if col.enum_values:
                bits.append("one of " + ", ".join(col.enum_values))
            lines.append(f"  - {col.name} ({'; '.join(bits)}): {col.description}")
    enriched_author_contract = task.task_id == "demo__customer_summary"
    public_source_tables = {table.name for table in task.tables}
    public_columns = {
        *(column.name for table in task.tables for column in table.columns),
        *(column.name for mart in task.marts for column in mart.columns),
    }
    public_detail_keys = {
        "boundary", "domain", "mode", "no_activity", "null_result",
        "rounding", "tie_break", "units",
    }
    if enriched_author_contract and task.relationships:
        lines.extend(("", "Source relationships:"))
        for relationship in task.relationships:
            requirement = "required" if relationship.required else "optional"
            lines.append(
                f"- The relationship has child {relationship.child_table} "
                f"{', '.join(relationship.child_columns)} and parent "
                f"{relationship.parent_table} "
                f"{', '.join(relationship.parent_columns)} is {requirement}."
            )
    lines.append("")
    for mart in task.marts:
        lines.append(f"Mart {mart.name}: {mart.description}")
        lines.append(f"Grain: {mart.grain}")
        if enriched_author_contract:
            lines.append(f"Key columns: {', '.join(mart.key_columns)}")
            lines.append("Output columns:")
            for column in mart.columns:
                lines.append(
                    f"  - {column.name} ({column.type.value}): {column.description}"
                )
        lines.append("Rules:")
        n = 0
        for index, op in enumerate(mart.plan.ops):
            if op.kind in omit_op_kinds or index in omit_op_indices:
                continue
            n += 1
            description_tokens = set(
                re.findall(r"[a-z0-9_]+", op.description.lower())
            )
            required_tokens: dict[str, None] = {}
            if enriched_author_contract:
                for token in (
                    *(table for table in op.tables if table in public_source_tables),
                    *(column for column in op.columns if column in public_columns),
                ):
                    if token.lower() not in description_tokens:
                        required_tokens.setdefault(token.lower(), None)
                detail_values = (
                    value for key, value in op.details.items()
                    if key in public_detail_keys
                )
                for candidate in (op.predicate, *detail_values):
                    for token in re.findall(r"[a-z0-9_]+", candidate.lower()):
                        if (
                            ("_" in token or token.isdigit())
                            and token not in description_tokens
                        ):
                            required_tokens.setdefault(token, None)
            structured = list(required_tokens)
            suffix = (
                " Structured requirements: " + ", ".join(structured) + "."
                if structured
                else ""
            )
            lines.append(f"  {n}. {op.description}{suffix}")
        lines.append("")
    lines.append(_DELIVERABLE_VARIANTS[variant % len(_DELIVERABLE_VARIANTS)])
    return "\n".join(lines)


# Declarative family definition (what a family module writes down)

@dataclass(frozen=True)
class AmbiguityVariant:
    """One ambiguity specimen: an OMISSION (`omit` op indices, with the
    `rewrites` that scrub every public restatement) or a CONTRADICTION
    (`omit` empty; `rewrites` rewrites one column description to disagree
    with a rule that survives verbatim)."""

    name: str
    omit: frozenset[int]
    rewrites: dict[str, Any]
    description: str
    detection_terms: tuple[str, ...]
    anchor_terms: tuple[str, ...]


@dataclass(frozen=True)
class PopulationVariant:
    """One population specimen: the conditions of the named populations are
    replaced (and the counterfactual's literal rows, when given); `drop`
    removes populations outright."""

    name: str
    conditions: dict[PopulationName, tuple[str, ...]]
    description: str
    detection_terms: tuple[str, ...]
    anchor_terms: tuple[str, ...]
    literal_rows: dict[str, tuple[dict, ...]] | None = None
    drop: tuple[PopulationName, ...] = ()


@dataclass(frozen=True)
class ShortcutVariant:
    """One shortcut specimen: `suffix` appended to the named mart columns'
    descriptions (None = every non-key column), or only `keep` populations
    retained."""

    name: str
    description: str
    detection_terms: tuple[str, ...]
    anchor_terms: tuple[str, ...]
    columns: tuple[str, ...] | None = None
    suffix: str = ""
    keep: tuple[PopulationName, ...] = ()


@dataclass(frozen=True)
class FeasibilityVariant:
    """One feasibility specimen: a DELETION (`table` plus `column`, or the
    whole `table`) or a PROSE REFERENCE (`mart_column` described as computed
    from `suffix`'s unpublished input)."""

    name: str
    description: str
    anchor_terms: tuple[str, ...]
    table: str = ""
    column: str | None = None
    mart_column: str = ""
    suffix: str = ""


@dataclass(frozen=True)
class FamilyDefinition:
    """Everything a non-demo family declares; `build_family` does the rest."""

    name: str
    task: Callable[[], TaskIR]
    canary_guid: str
    #: Honest decoy notes for the adversarial clean specimens (rotated): each
    #: TRUE of the untampered task while carrying a defect class's vocabulary.
    decoy_notes: tuple[str, ...]
    #: The counterfactual conditions REWORDED without weakening them.
    reworded_counterfactual_conditions: tuple[str, ...]
    ambiguity: tuple[AmbiguityVariant, ...]
    population: tuple[PopulationVariant, ...]
    shortcut: tuple[ShortcutVariant, ...]
    feasibility: tuple[FeasibilityVariant, ...]


# The kit: metrology's injectors applied at a family's anchors

def _with_prose(task: TaskIR, *, variant: int, omit: frozenset[int] = frozenset(), notes: str = "") -> TaskIR:
    prose = build_prose(task, omit_op_indices=omit, variant=variant)
    if notes:
        prose = prose + "\n\n" + notes
    return _metrology()._with_prose(task, prose)


def _reworded_counterfactual(task: TaskIR, conditions: tuple[str, ...]) -> TaskIR:
    return task.model_copy(
        update={
            "populations": tuple(
                pop.model_copy(update={"conditions": conditions})
                if pop.name is PopulationName.COUNTERFACTUAL
                else pop
                for pop in task.populations
            )
        }
    )


def _clean_builder(defn: FamilyDefinition) -> Callable[[], tuple["Specimen", ...]]:
    def build() -> tuple[Specimen, ...]:
        out: list[Specimen] = [
            _build_specimen(
                defn.name, "clean-plain",
                kind="clean",
                target_role=None,
                description=f"untampered {defn.name} task with complete public prose",
                make_task=lambda v: _with_prose(defn.task(), variant=v),
            )
        ]
        for i, suffix in enumerate(("a", "b", "c", "d")):
            notes = defn.decoy_notes[i:] + defn.decoy_notes[:i]

            def _decoy(v: int, notes: tuple[str, ...] = notes) -> TaskIR:
                return _with_prose(defn.task(), variant=v, notes="\n".join(notes))

            out.append(
                _build_specimen(
                    defn.name, f"clean-decoy-{suffix}",
                    kind="clean",
                    target_role=None,
                    description="untampered task whose prose foregrounds the "
                    "vocabulary of every defect class while stating the rules "
                    "correctly",
                    make_task=_decoy,
                )
            )

        def _reworded(v: int) -> TaskIR:
            return _with_prose(
                _reworded_counterfactual(defn.task(), defn.reworded_counterfactual_conditions),
                variant=v,
            )

        def _reworded_decoy(v: int) -> TaskIR:
            return _with_prose(
                _reworded_counterfactual(defn.task(), defn.reworded_counterfactual_conditions),
                variant=v,
                notes="\n".join(defn.decoy_notes),
            )

        out.append(
            _build_specimen(
                defn.name, "clean-reworded-counterfactual",
                kind="clean",
                target_role=None,
                description="untampered task; counterfactual conditions reworded "
                "without weakening what they distinguish",
                make_task=_reworded,
            )
        )
        out.append(
            _build_specimen(
                defn.name, "clean-reworded-decoy",
                kind="clean",
                target_role=None,
                description="untampered task carrying BOTH adversarial surfaces: "
                "reworded counterfactual conditions and the decoy notes",
                make_task=_reworded_decoy,
            )
        )
        return tuple(out)

    return build


def _ambiguity_builder(defn: FamilyDefinition) -> Callable[[], tuple["Specimen", ...]]:
    def build() -> tuple[Specimen, ...]:
        m = _metrology()
        out: list[Specimen] = []
        for variant in defn.ambiguity:

            def _make(v: int, variant: AmbiguityVariant = variant) -> TaskIR:
                task = m._rewrite_mart(defn.task(), **variant.rewrites)
                return _with_prose(task, variant=v, omit=variant.omit)

            out.append(
                _build_specimen(
                    defn.name, variant.name,
                    kind="tampered",
                    target_role=CouncilRole.AMBIGUITY_CRITIC,
                    description=variant.description,
                    detection_terms=variant.detection_terms,
                    anchor_terms=variant.anchor_terms,
                    make_task=_make,
                )
            )
        return tuple(out)

    return build


def _population_builder(defn: FamilyDefinition) -> Callable[[], tuple["Specimen", ...]]:
    def build() -> tuple[Specimen, ...]:
        m = _metrology()
        out: list[Specimen] = []
        for variant in defn.population:

            def _make(v: int, variant: PopulationVariant = variant) -> TaskIR:
                task = m._replace_conditions(
                    defn.task(), variant.conditions, literal_rows=variant.literal_rows
                )
                if variant.drop:
                    task = task.model_copy(
                        update={
                            "populations": tuple(
                                p for p in task.populations if p.name not in variant.drop
                            )
                        }
                    )
                return _with_prose(task, variant=v)

            out.append(
                _build_specimen(
                    defn.name, variant.name,
                    kind="tampered",
                    target_role=CouncilRole.POPULATION_ADVERSARY,
                    description=variant.description,
                    detection_terms=variant.detection_terms,
                    anchor_terms=variant.anchor_terms,
                    make_task=_make,
                )
            )
        return tuple(out)

    return build


def _non_key_columns(task: TaskIR) -> tuple[str, ...]:
    mart = task.marts[0]
    return tuple(c.name for c in mart.columns if c.name not in mart.key_columns)


def _shortcut_builder(defn: FamilyDefinition) -> Callable[[], tuple["Specimen", ...]]:
    def build() -> tuple[Specimen, ...]:
        m = _metrology()
        out: list[Specimen] = []
        for variant in defn.shortcut:

            def _make(v: int, variant: ShortcutVariant = variant) -> TaskIR:
                task = defn.task()
                if variant.keep:
                    task = m._keep_populations(task, variant.keep)
                else:
                    columns = variant.columns or _non_key_columns(task)
                    for column in columns:
                        task = m._describe_one_mart_column(task, column, variant.suffix)
                return _with_prose(task, variant=v)

            out.append(
                _build_specimen(
                    defn.name, variant.name,
                    kind="tampered",
                    target_role=CouncilRole.SHORTCUT_ATTACKER,
                    description=variant.description,
                    detection_terms=variant.detection_terms,
                    anchor_terms=variant.anchor_terms,
                    make_task=_make,
                )
            )
        return tuple(out)

    return build


def _feasibility_builder(defn: FamilyDefinition) -> Callable[[], tuple["Specimen", ...]]:
    def build() -> tuple[Specimen, ...]:
        m = _metrology()
        out: list[Specimen] = []
        for variant in defn.feasibility:

            def _make(v: int, variant: FeasibilityVariant = variant) -> TaskIR:
                base = defn.task()
                if variant.mart_column:
                    task = m._revalidated(
                        m._describe_one_mart_column(base, variant.mart_column, variant.suffix)
                    )
                elif variant.column is None:
                    task = m._drop_table(base, variant.table)
                else:
                    task = m._drop_column(base, variant.table, variant.column)
                return _with_prose(task, variant=v)

            out.append(
                _build_specimen(
                    defn.name, variant.name,
                    kind="tampered",
                    target_role=CouncilRole.FEASIBILITY_REVIEWER,
                    description=variant.description,
                    detection_terms=m._MISSING_TERMS,
                    anchor_terms=variant.anchor_terms,
                    make_task=_make,
                )
            )
        return tuple(out)

    return build


def _derive_anchors(defn: FamilyDefinition) -> dict[str, tuple[InjectorAnchor, ...]]:
    """The anchors ARE the variants: derived, never declared twice."""
    task = defn.task()
    mart = task.marts[0].name
    amb, pop, shc, fea = (
        CouncilRole.AMBIGUITY_CRITIC.value,
        CouncilRole.POPULATION_ADVERSARY.value,
        CouncilRole.SHORTCUT_ATTACKER.value,
        CouncilRole.FEASIBILITY_REVIEWER.value,
    )
    out: dict[str, list[InjectorAnchor]] = {amb: [], pop: [], shc: [], fea: []}
    for variant in defn.ambiguity:
        for index in sorted(variant.omit):
            out[amb].append(
                InjectorAnchor(amb, "omit_op", f"{mart}.ops[{index}]", variant.description)
            )
        if not variant.omit:  # a CONTRADICTION anchors on the rewritten column
            for column, _, _ in variant.rewrites.get("columns", ()):
                out[amb].append(
                    InjectorAnchor(amb, "contradict_column", f"{mart}.{column}", variant.description)
                )
    for variant in defn.population:
        if variant.conditions:
            names = ",".join(p.value for p in variant.conditions)
            out[pop].append(
                InjectorAnchor(
                    pop, "replace_conditions", f"{_POPULATION_PREFIX}{names}", variant.description
                )
            )
        for dropped in variant.drop:
            out[pop].append(
                InjectorAnchor(
                    pop, "drop_population", f"{_POPULATION_PREFIX}{dropped.value}", variant.description
                )
            )
    for variant in defn.shortcut:
        if variant.keep:
            names = ",".join(p.value for p in variant.keep)
            out[shc].append(
                InjectorAnchor(
                    shc, "keep_populations", f"{_POPULATION_PREFIX}{names}", variant.description
                )
            )
        else:
            for column in variant.columns or _non_key_columns(task):
                out[shc].append(
                    InjectorAnchor(shc, "describe_column", f"{mart}.{column}", variant.description)
                )
    for variant in defn.feasibility:
        if variant.mart_column:
            out[fea].append(
                InjectorAnchor(
                    fea, "prose_reference", f"{mart}.{variant.mart_column}", variant.description
                )
            )
        elif variant.column is None:
            out[fea].append(InjectorAnchor(fea, "drop_table", variant.table, variant.description))
        else:
            out[fea].append(
                InjectorAnchor(
                    fea, "drop_column", f"{variant.table}.{variant.column}", variant.description
                )
            )
    anchors = {role: tuple(items) for role, items in out.items()}
    for items in anchors.values():
        for anchor in items:
            resolve_anchor(task, anchor)
    return anchors


def build_family(defn: FamilyDefinition) -> FixtureFamily:
    """A `FixtureFamily` from its declaration: anchors derived from the
    variants and checked against the clean task, builders closed over the
    task factory so every variant builds from a fresh `TaskIR`."""
    return FixtureFamily(
        name=defn.name,
        task=defn.task,
        anchors=_derive_anchors(defn),
        specimen_builders={
            "clean": _clean_builder(defn),
            "ambiguity": _ambiguity_builder(defn),
            "population": _population_builder(defn),
            "shortcut": _shortcut_builder(defn),
            "feasibility": _feasibility_builder(defn),
        },
        canary_guid=defn.canary_guid,
    )
