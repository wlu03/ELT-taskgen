"""Define the `clinic_visits` metrology fixture family.

Most strings are hashed task data; changing them rekeys the family. The schema exercises
required and optional joins, scoped distinct and filtered counts, fan-out sums,
wrong-grain fees, and a count-based case ladder.
"""

from __future__ import annotations

from elt_taskgen.generation.populations import el_attack_cases
from elt_taskgen.models import (
    AttackCase,
    AttackKind,
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    JoinType,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    Origin,
    PopulationName,
    PopulationSpec,
    ReferenceSolution,
    Relationship,
    Row,
    TableSpec,
    TaskIR,
    derive_seed,
)
from elt_taskgen.review.metrology_fixtures.families import (
    AmbiguityVariant,
    FamilyDefinition,
    FeasibilityVariant,
    PopulationVariant,
    ShortcutVariant,
    _metrology,
    build_family,
)

FAMILY_NAME = "clinic_visits"
TASK_ID = "metrology__clinic_visits"
FAMILY_ID = "metrology__clinic_visits"
CLUSTER_ID = "metrology__clinic_visits"
MART_NAME = "practitioner_activity"

#: Contamination canary GUID (pool source only; never rendered).
CANARY_GUID = "elt-taskgen-metrology-family-canary:clinic_visits:0f4c2e7b-9a6d-4b31-8e5f-7c2a1d9b6e44"

P = PopulationName

# Counterfactual literal rows: four practitioners at two clinics.
COUNTERFACTUAL_LITERAL_ROWS: dict[str, tuple[Row, ...]] = {
    "clinics": (
        {"clinic_id": 901, "clinic_name": "Northgate"},
        {"clinic_id": 902, "clinic_name": "Riverside"},
    ),
    "practitioners": (
        {"practitioner_id": 9011, "clinic_id": 901, "specialty": "general"},
        {"practitioner_id": 9012, "clinic_id": 901, "specialty": "dental"},
        {"practitioner_id": 9021, "clinic_id": 902, "specialty": "physio"},
        {"practitioner_id": 9022, "clinic_id": 902, "specialty": "general"},
    ),
    "visits": (
        {"visit_id": 90121, "practitioner_id": 9012, "status": "attended",
         "booking": "scheduled", "fee": 40.0},
        {"visit_id": 90121, "practitioner_id": 9012, "status": "attended",
         "booking": "scheduled", "fee": 40.0},  # exact duplicate row
        {"visit_id": 90122, "practitioner_id": 9012, "status": "attended",
         "booking": "walk_in", "fee": 25.0},
        {"visit_id": 90123, "practitioner_id": 9012, "status": "cancelled",
         "booking": "scheduled", "fee": 60.0},
        {"visit_id": 90211, "practitioner_id": 9021, "status": "attended",
         "booking": "scheduled", "fee": 10.0},
        {"visit_id": 90212, "practitioner_id": 9021, "status": "attended",
         "booking": "scheduled", "fee": 10.0},
        {"visit_id": 90213, "practitioner_id": 9021, "status": "attended",
         "booking": "walk_in", "fee": 10.0},
        {"visit_id": 90221, "practitioner_id": 9022, "status": "cancelled",
         "booking": "walk_in", "fee": 15.0},
        {"visit_id": 90901, "practitioner_id": None, "status": "attended",
         "booking": "scheduled", "fee": 99.0},
    ),
    "procedures": (
        {"visit_id": 90122, "procedure_code": "xray", "units": 3},
        {"visit_id": 90122, "procedure_code": "consult", "units": 4},
        {"visit_id": 90123, "procedure_code": "cleaning", "units": 9},
        {"visit_id": 90211, "procedure_code": "therapy", "units": 1},
        {"visit_id": 90212, "procedure_code": "therapy", "units": 1},
        {"visit_id": 90213, "procedure_code": "consult", "units": 1},
        {"visit_id": 90901, "procedure_code": "xray", "units": 2},
    ),
}

#: Expected counterfactual mart, sorted by practitioner_id. Reference
#: execution must reproduce these EXACTLY (pinned by test).
COUNTERFACTUAL_EXPECTED_MART: tuple[Row, ...] = (
    {"practitioner_id": 9011, "clinic_name": "Northgate", "specialty": "general",
     "attended_visit_count": 0, "walk_in_visit_count": 0, "procedure_units": 0,
     "fee_total": 0.0, "caseload_band": "idle"},
    {"practitioner_id": 9012, "clinic_name": "Northgate", "specialty": "dental",
     "attended_visit_count": 2, "walk_in_visit_count": 1, "procedure_units": 7,
     "fee_total": 65.0, "caseload_band": "light"},
    {"practitioner_id": 9021, "clinic_name": "Riverside", "specialty": "physio",
     "attended_visit_count": 3, "walk_in_visit_count": 1, "procedure_units": 3,
     "fee_total": 30.0, "caseload_band": "busy"},
    {"practitioner_id": 9022, "clinic_name": "Riverside", "specialty": "general",
     "attended_visit_count": 0, "walk_in_visit_count": 0, "procedure_units": 0,
     "fee_total": 0.0, "caseload_band": "idle"},
)

# Trusted reference SQL (DuckDB) and the hand-authored transform mutants.
REFERENCE_SQL = """\
WITH attended AS (
    SELECT DISTINCT visit_id, practitioner_id, booking, fee
    FROM visits
    WHERE status = 'attended'
),
visit_units AS (
    SELECT visit_id, SUM(units) AS units
    FROM procedures
    GROUP BY visit_id
),
per_practitioner AS (
    SELECT
        a.practitioner_id,
        COUNT(DISTINCT a.visit_id) AS attended_visit_count,
        COUNT(DISTINCT CASE WHEN a.booking = 'walk_in' THEN a.visit_id END) AS walk_in_visit_count,
        SUM(COALESCE(u.units, 0)) AS procedure_units,
        SUM(a.fee) AS fee_total
    FROM attended AS a
    LEFT JOIN visit_units AS u ON u.visit_id = a.visit_id
    GROUP BY a.practitioner_id
)
SELECT
    p.practitioner_id AS practitioner_id,
    c.clinic_name AS clinic_name,
    p.specialty AS specialty,
    COALESCE(v.attended_visit_count, 0) AS attended_visit_count,
    COALESCE(v.walk_in_visit_count, 0) AS walk_in_visit_count,
    COALESCE(v.procedure_units, 0) AS procedure_units,
    COALESCE(v.fee_total, 0) AS fee_total,
    CASE
        WHEN COALESCE(v.attended_visit_count, 0) >= 3 THEN 'busy'
        WHEN COALESCE(v.attended_visit_count, 0) >= 1 THEN 'light'
        ELSE 'idle'
    END AS caseload_band
FROM practitioners AS p
INNER JOIN clinics AS c ON c.clinic_id = p.clinic_id
LEFT JOIN per_practitioner AS v ON v.practitioner_id = p.practitioner_id
ORDER BY p.practitioner_id
"""

# Practitioners without in-scope visits are dropped instead of reported as 0.
_INNER_JOIN_SQL = REFERENCE_SQL.replace(
    "LEFT JOIN per_practitioner AS v", "INNER JOIN per_practitioner AS v"
)

# Cancelled visits are counted like attended ones.
_NO_FILTER_SQL = REFERENCE_SQL.replace("    WHERE status = 'attended'\n", "")

# The duplicate visit row is summed twice (counts survive through DISTINCT).
_NO_DEDUP_SQL = REFERENCE_SQL.replace(
    "SELECT DISTINCT visit_id, practitioner_id, booking, fee",
    "SELECT visit_id, practitioner_id, booking, fee",
)

# fee summed at the PROCEDURE grain: a visit with k procedure rows pays k times.
_WRONG_GRAIN_FEE_SQL = """\
WITH attended AS (
    SELECT DISTINCT visit_id, practitioner_id, booking, fee
    FROM visits
    WHERE status = 'attended'
),
visit_rows AS (
    SELECT a.practitioner_id, a.visit_id, a.booking, a.fee, COALESCE(pr.units, 0) AS units
    FROM attended AS a
    LEFT JOIN procedures AS pr ON pr.visit_id = a.visit_id
),
per_practitioner AS (
    SELECT
        practitioner_id,
        COUNT(DISTINCT visit_id) AS attended_visit_count,
        COUNT(DISTINCT CASE WHEN booking = 'walk_in' THEN visit_id END) AS walk_in_visit_count,
        SUM(units) AS procedure_units,
        SUM(fee) AS fee_total
    FROM visit_rows
    GROUP BY practitioner_id
)
SELECT
    p.practitioner_id AS practitioner_id,
    c.clinic_name AS clinic_name,
    p.specialty AS specialty,
    COALESCE(v.attended_visit_count, 0) AS attended_visit_count,
    COALESCE(v.walk_in_visit_count, 0) AS walk_in_visit_count,
    COALESCE(v.procedure_units, 0) AS procedure_units,
    COALESCE(v.fee_total, 0) AS fee_total,
    CASE
        WHEN COALESCE(v.attended_visit_count, 0) >= 3 THEN 'busy'
        WHEN COALESCE(v.attended_visit_count, 0) >= 1 THEN 'light'
        ELSE 'idle'
    END AS caseload_band
FROM practitioners AS p
INNER JOIN clinics AS c ON c.clinic_id = p.clinic_id
LEFT JOIN per_practitioner AS v ON v.practitioner_id = p.practitioner_id
ORDER BY p.practitioner_id
"""

# NULL instead of 0 for practitioners without in-scope visits.
_NO_COALESCE_SQL = (
    REFERENCE_SQL.replace(
        "    COALESCE(v.attended_visit_count, 0) AS attended_visit_count,\n"
        "    COALESCE(v.walk_in_visit_count, 0) AS walk_in_visit_count,\n"
        "    COALESCE(v.procedure_units, 0) AS procedure_units,\n"
        "    COALESCE(v.fee_total, 0) AS fee_total,\n",
        "    v.attended_visit_count AS attended_visit_count,\n"
        "    v.walk_in_visit_count AS walk_in_visit_count,\n"
        "    v.procedure_units AS procedure_units,\n"
        "    v.fee_total AS fee_total,\n",
    )
)

# The ladder's thresholds shifted down by one: 'busy' from two visits.
_BAND_OFF_BY_ONE_SQL = REFERENCE_SQL.replace(
    "WHEN COALESCE(v.attended_visit_count, 0) >= 3 THEN 'busy'",
    "WHEN COALESCE(v.attended_visit_count, 0) >= 2 THEN 'busy'",
)

HARDCODE_PRIMARY_DIRECTIVE = "directive:hardcode-population-outputs:primary"


def mart_plan() -> MartPlan:
    """Declarative record of the intended relational ops for the mart."""
    return MartPlan(
        mart=MART_NAME,
        ops=(
            MartOp(
                kind=MartOpKind.FILTER,
                # SOLE DEFINITION OF SCOPE: no other op names a status, or the
                # ambiguity-no-filter specimen (which strips this op) is
                # silently repaired.
                description=(
                    "Keep only visits with status = 'attended'; these are the "
                    "visits in scope for every rule below."
                ),
                tables=("visits",),
                columns=("status",),
                predicate="status = 'attended'",
            ),
            MartOp(
                kind=MartOpKind.DEDUPE,
                description=(
                    "Deduplicate exact-duplicate visit rows in scope: DISTINCT "
                    "(visit_id, practitioner_id, booking, fee)."
                ),
                tables=("visits",),
                columns=("visit_id", "practitioner_id", "booking", "fee"),
            ),
            # The procedure-row repetition POLICY lives here (see the demo's
            # item-total op for why): no "dedup*" token, no denominator hint.
            MartOp(
                kind=MartOpKind.AGGREGATE,
                description=(
                    "Per-visit procedure units: SUM(units) grouped by visit_id "
                    "over every procedure row of that visit; two procedure rows "
                    "carrying identical values are two procedures, and both are "
                    "counted."
                ),
                tables=("procedures",),
                columns=("visit_id", "units"),
                details={"function": "sum", "expression": "units"},
            ),
            MartOp(
                kind=MartOpKind.JOIN,
                description=(
                    "INNER JOIN each practitioner onto its clinic on clinic_id to "
                    "carry clinic_name; every practitioner belongs to exactly one "
                    "clinic."
                ),
                tables=("practitioners", "clinics"),
                columns=("clinic_id", "clinic_name"),
                join_type=JoinType.INNER,
                predicate="practitioners.clinic_id = clinics.clinic_id",
            ),
            MartOp(
                kind=MartOpKind.JOIN,
                description=(
                    "LEFT JOIN the visits in scope onto practitioners so "
                    "practitioners with no such visits are retained. Visits whose "
                    "practitioner_id is NULL belong to no practitioner: they are "
                    "excluded from every measure, and the mart never emits a row "
                    "whose practitioner_id is NULL."
                ),
                tables=("practitioners", "visits"),
                columns=("practitioner_id",),
                join_type=JoinType.LEFT,
                predicate="visits.practitioner_id = practitioners.practitioner_id",
            ),
            MartOp(
                kind=MartOpKind.JOIN,
                description=(
                    "LEFT JOIN per-visit procedure units onto the visits in scope; "
                    "a visit with no procedure rows contributes 0 units."
                ),
                tables=("visits", "procedures"),
                columns=("visit_id",),
                join_type=JoinType.LEFT,
                predicate="procedures.visit_id = visits.visit_id",
            ),
            MartOp(
                kind=MartOpKind.AGGREGATE,
                description=(
                    # EVERY CLAUSE NAMES THE SET; none binds anaphorically to
                    # another (a back-reference would close a specimen).
                    "Per practitioner: attended_visit_count = COUNT(DISTINCT "
                    "visit_id) over the visits in scope; walk_in_visit_count = "
                    "COUNT(DISTINCT visit_id) over the visits in scope whose "
                    "booking = 'walk_in'; procedure_units = SUM of per-visit "
                    "procedure units of the visits in scope; fee_total = SUM of "
                    "fee over the DISTINCT visits in scope, at the visit grain and "
                    "never at the procedure-row grain."
                ),
                tables=("practitioners",),
                columns=(
                    "practitioner_id", "specialty", "attended_visit_count",
                    "walk_in_visit_count", "procedure_units", "fee_total",
                ),
                details={
                    "group_by": "practitioner_id",
                    "attended_visit_count": "COUNT(DISTINCT visit_id)",
                    "walk_in_visit_count": (
                        "COUNT(DISTINCT CASE WHEN booking = 'walk_in' THEN visit_id END)"
                    ),
                    "procedure_units": "SUM(units)",
                    "fee_total": "SUM(fee)",
                },
            ),
            MartOp(
                kind=MartOpKind.DERIVE,
                description=(
                    "COALESCE the four measures to 0 for practitioners without "
                    "in-scope visits (never NULL)."
                ),
                columns=(
                    "attended_visit_count", "walk_in_visit_count",
                    "procedure_units", "fee_total",
                ),
                predicate="COALESCE(measure, 0); every measure is 0 when no visits are in scope",
            ),
            MartOp(
                kind=MartOpKind.CONDITIONAL,
                description=(
                    "caseload_band = 'busy' when attended_visit_count >= 3, "
                    "'light' when it is 1 or 2, else 'idle'."
                ),
                columns=("caseload_band", "attended_visit_count"),
                predicate=(
                    "CASE WHEN attended_visit_count >= 3 THEN 'busy' "
                    "WHEN attended_visit_count >= 1 THEN 'light' ELSE 'idle' END"
                ),
            ),
            MartOp(
                kind=MartOpKind.TIE_BREAK,
                description="Deterministic output order: sort by practitioner_id.",
                columns=("practitioner_id",),
            ),
        ),
        notes=(
            "Include practitioners with no visits; count DISTINCT attended "
            "visits; fee once per visit; COALESCE to 0; band on the count."
        ),
    )


def _populations() -> tuple[PopulationSpec, ...]:
    return (
        PopulationSpec(
            name=P.DEVELOPMENT,
            seed=derive_seed(TASK_ID, P.DEVELOPMENT.value),
            scale={"clinics": 2, "practitioners": 3, "visits": 12, "procedures": 20},
            conditions=(
                "Tiny debug data: exactly two clinics and three practitioners.",
                "Every practitioner has at least one attended visit "
                "(INNER JOIN is indistinguishable here by design).",
                "Some visits carry two procedure rows.",
                "No NULL practitioner_id, no duplicate rows.",
            ),
        ),
        PopulationSpec(
            name=P.PRIMARY,
            seed=derive_seed(TASK_ID, P.PRIMARY.value),
            scale={"clinics": 40, "practitioners": 120, "visits": 900, "procedures": 1500},
            conditions=(
                "Approximately 120 practitioners across about 40 clinics.",
                "Cancelled visits are present.",
                "Some practitioners have no visits at all.",
                "Some practitioners have visits but no attended visits.",
                # NO 'dangling' TOKEN: it is source_data's _DANGLING_FRAC lever.
                "Some ATTENDED visits have NULL practitioner_id and carry "
                "procedure rows; they belong to no practitioner and contribute "
                "to no output row.",
                "Attended visits may carry several procedure rows (fee_total "
                "counts a fee once per visit, never once per procedure row).",
            ),
        ),
        PopulationSpec(
            name=P.RESAMPLED,
            seed=derive_seed(TASK_ID, P.RESAMPLED.value),
            scale={"clinics": 40, "practitioners": 120, "visits": 900, "procedures": 1500},
            conditions=(
                "Same generator and conditions as primary; new seed and new id "
                "ranges (memorization check).",
            ),
        ),
        PopulationSpec(
            name=P.COUNTERFACTUAL,
            seed=derive_seed(TASK_ID, P.COUNTERFACTUAL.value),
            conditions=(
                "Literal constructed rows only — four practitioners at two clinics.",
                "9011: a practitioner with no visits -> every measure 0, "
                "caseload_band 'idle'.",
                "9012: two attended visits, one of them recorded twice as an "
                "exact duplicate row, plus one cancelled visit; the walk-in "
                "carries two procedure rows (3 + 4 units) and a fee of 25 -> "
                "attended_visit_count 2 (row-grain counting gives 3), "
                "walk_in_visit_count 1, procedure_units 7, fee_total 40 + 25 = "
                "65, caseload_band 'light'.",
                "9021: three attended visits of one procedure each, one a "
                "walk-in -> attended_visit_count 3, caseload_band 'busy'.",
                "9022: only a cancelled visit -> every measure 0, caseload_band "
                "'idle'.",
                "One attended visit with NULL practitioner_id carries a fee of 99 "
                "and a procedure row; it contributes to no output row.",
            ),
            literal_rows=COUNTERFACTUAL_LITERAL_ROWS,
        ),
        PopulationSpec(
            name=P.STRESS,
            seed=derive_seed(TASK_ID, P.STRESS.value),
            scale={"clinics": 10, "practitioners": 30, "visits": 6000, "procedures": 12000},
            conditions=(
                "A few hot practitioners hold a large share of all visits.",
                "Exact-duplicate visit rows are present (correct logic must dedupe).",
                "Exact-duplicate procedure rows are also present; procedure rows "
                "are not deduplicated.",
                "Ties: distinct practitioners with identical totals.",
                "Every practitioner has at least one attended visit "
                "(INNER JOIN is indistinguishable here by design).",
            ),
        ),
    )


def _attack_cases() -> tuple[AttackCase, ...]:
    return (
        AttackCase(
            name="inner_join",
            kind=AttackKind.INNER_JOIN,
            description=(
                "The practitioner-level LEFT JOIN replaced with INNER JOIN: "
                "practitioners without attended visits are dropped instead of "
                "reported as zeros and 'idle'."
            ),
            mutation=_INNER_JOIN_SQL,
            expected_pass={
                P.DEVELOPMENT: True,
                P.STRESS: True,
                P.PRIMARY: False,
                P.RESAMPLED: False,
                P.COUNTERFACTUAL: False,
            },
        ),
        AttackCase(
            name="hardcoded_primary_outputs",
            kind=AttackKind.CONSTANTS,
            description=(
                "Emits the frozen primary-population gold outputs verbatim, "
                "computing nothing. Must score full reward ONLY on primary and "
                "fail every other population."
            ),
            mutation=HARDCODE_PRIMARY_DIRECTIVE,
            expected_pass={
                P.PRIMARY: True,
                P.DEVELOPMENT: False,
                P.RESAMPLED: False,
                P.COUNTERFACTUAL: False,
                P.STRESS: False,
            },
        ),
        AttackCase(
            name="no_status_filter",
            kind=AttackKind.DROPPED_FILTER,
            description="Cancelled visits are counted and summed like attended ones.",
            mutation=_NO_FILTER_SQL,
            expected_pass={
                P.DEVELOPMENT: False,
                P.PRIMARY: False,
                P.RESAMPLED: False,
                P.STRESS: False,
                P.COUNTERFACTUAL: False,
            },
        ),
        AttackCase(
            name="no_dedup",
            kind=AttackKind.NO_DEDUP,
            description=(
                "The DISTINCT over visit rows is dropped: a visit recorded twice "
                "pays its fee and its units twice."
            ),
            mutation=_NO_DEDUP_SQL,
            expected_pass={P.DEVELOPMENT: True, P.STRESS: False, P.COUNTERFACTUAL: False},
        ),
        AttackCase(
            name="wrong_grain_fee",
            kind=AttackKind.WRONG_GRAIN,
            description=(
                "fee summed at the procedure-row grain: a visit with two "
                "procedure rows contributes its fee twice."
            ),
            mutation=_WRONG_GRAIN_FEE_SQL,
            expected_pass={
                P.DEVELOPMENT: False,
                P.PRIMARY: False,
                P.RESAMPLED: False,
                P.STRESS: False,
                P.COUNTERFACTUAL: False,
            },
        ),
        AttackCase(
            name="no_coalesce",
            kind=AttackKind.NO_NULL_DEFAULT,
            description=(
                "Drops COALESCE: practitioners without attended visits get NULL "
                "measures instead of 0."
            ),
            mutation=_NO_COALESCE_SQL,
            expected_pass={
                P.DEVELOPMENT: True,
                P.STRESS: True,
                P.PRIMARY: False,
                P.RESAMPLED: False,
                P.COUNTERFACTUAL: False,
            },
        ),
        AttackCase(
            name="band_off_by_one",
            kind=AttackKind.CUSTOM,
            description=(
                "The caseload ladder's 'busy' threshold shifted from three "
                "attended visits to two."
            ),
            mutation=_BAND_OFF_BY_ONE_SQL,
            expected_pass={P.COUNTERFACTUAL: False},
        ),
    )


def task() -> TaskIR:
    """The complete clinic_visits candidate as a validated TaskIR."""
    tables = (
        TableSpec(
            name="clinics",
            description="One row per clinic.",
            columns=(
                ColumnSpec(name="clinic_id", type=ColumnType.INTEGER,
                           description="Unique clinic identifier."),
                ColumnSpec(name="clinic_name", type=ColumnType.TEXT,
                           description="Display name of the clinic."),
            ),
            primary_key=("clinic_id",),
        ),
        TableSpec(
            name="practitioners",
            description="One row per practitioner; each belongs to exactly one clinic.",
            columns=(
                ColumnSpec(name="practitioner_id", type=ColumnType.INTEGER,
                           description="Unique practitioner identifier."),
                ColumnSpec(name="clinic_id", type=ColumnType.INTEGER,
                           description="Clinic the practitioner belongs to."),
                ColumnSpec(name="specialty", type=ColumnType.TEXT,
                           enum_values=("general", "dental", "physio"),
                           description="Clinical specialty."),
            ),
            primary_key=("practitioner_id",),
        ),
        TableSpec(
            name="visits",
            description="One row per visit (duplicates possible under stress).",
            columns=(
                ColumnSpec(name="visit_id", type=ColumnType.INTEGER,
                           description="Unique visit identifier."),
                ColumnSpec(name="practitioner_id", type=ColumnType.INTEGER, nullable=True,
                           description="Practitioner who saw the patient; may be NULL."),
                ColumnSpec(name="status", type=ColumnType.TEXT,
                           enum_values=("attended", "cancelled"),
                           description="Visit status."),
                ColumnSpec(name="booking", type=ColumnType.TEXT,
                           enum_values=("scheduled", "walk_in"),
                           description="How the visit was booked."),
                ColumnSpec(name="fee", type=ColumnType.DECIMAL,
                           description="Fee charged for the visit."),
            ),
            primary_key=(),  # duplicates of full rows allowed under stress
            business_key=("visit_id",),
        ),
        TableSpec(
            name="procedures",
            description="One row per procedure performed during a visit.",
            columns=(
                ColumnSpec(name="visit_id", type=ColumnType.INTEGER,
                           description="Visit this procedure belongs to."),
                ColumnSpec(name="procedure_code", type=ColumnType.TEXT,
                           enum_values=("consult", "xray", "cleaning", "therapy"),
                           description="Procedure performed."),
                ColumnSpec(name="units", type=ColumnType.INTEGER,
                           description="Units of the procedure performed."),
            ),
        ),
    )
    backends = (
        BackendAssignment(table="clinics", backend=Backend.POSTGRES),
        BackendAssignment(table="practitioners", backend=Backend.REST),
        BackendAssignment(table="visits", backend=Backend.MONGODB),
        BackendAssignment(table="procedures", backend=Backend.FILES,
                          options={"format": "csv"}),
    )
    populations = _populations()
    return TaskIR(
        task_id=TASK_ID,
        family_id=FAMILY_ID,
        cluster_id=CLUSTER_ID,
        origin=Origin.SYNTHETIC,
        license="CC0-1.0",
        attribution="elt-taskgen metrology fixture family clinic_visits",
        title="Practitioner visit activity",
        tables=tables,
        relationships=(
            Relationship(
                child_table="practitioners",
                child_columns=("clinic_id",),
                parent_table="clinics",
                parent_columns=("clinic_id",),
                required=True,
            ),
            Relationship(
                child_table="visits",
                child_columns=("practitioner_id",),
                parent_table="practitioners",
                parent_columns=("practitioner_id",),
                required=False,  # NULL practitioner_id allowed (primary population)
            ),
            Relationship(
                child_table="procedures",
                child_columns=("visit_id",),
                parent_table="visits",
                parent_columns=("visit_id",),
                required=True,
            ),
        ),
        backends=backends,
        marts=(
            MartSpec(
                name=MART_NAME,
                # SCOPE-NEUTRAL: survives every tamper.
                description="Per-practitioner visit activity summary.",
                grain="One row per practitioner, including practitioners with no visits.",
                key_columns=("practitioner_id",),
                columns=(
                    MartColumn(name="practitioner_id", type=ColumnType.INTEGER,
                               description="Unique practitioner identifier."),
                    MartColumn(name="clinic_name", type=ColumnType.TEXT,
                               description="Display name of the practitioner's clinic."),
                    MartColumn(name="specialty", type=ColumnType.TEXT,
                               description="The practitioner's specialty, copied from the source."),
                    # SCOPE-NEUTRAL descriptions: council._critic_view appends
                    # them VERBATIM, so naming the status here would restate
                    # the scope and defeat the FILTER-stripping specimen.
                    MartColumn(name="attended_visit_count", type=ColumnType.INTEGER,
                               description="Count of DISTINCT visits in scope; 0 if none."),
                    MartColumn(name="walk_in_visit_count", type=ColumnType.INTEGER,
                               description="Count of DISTINCT visits in scope whose booking "
                                           "is walk_in; 0 if none."),
                    MartColumn(name="procedure_units", type=ColumnType.INTEGER,
                               description="Sum of units over the procedure rows of the "
                                           "visits in scope; 0 if none."),
                    MartColumn(name="fee_total", type=ColumnType.DECIMAL,
                               description="Sum of fee over the DISTINCT visits in scope, at "
                                           "the visit grain; 0 if none."),
                    MartColumn(name="caseload_band", type=ColumnType.TEXT,
                               description="'busy', 'light' or 'idle' from "
                                           "attended_visit_count as the rules state."),
                ),
                plan=mart_plan(),
            ),
        ),
        populations=populations,
        reference=ReferenceSolution(
            implementation_id="metrology_clinic_visits_ref",
            dialect="duckdb",
            sql_by_mart={MART_NAME: REFERENCE_SQL},
            load_notes=(
                "Load clinics from the postgres load SQL, practitioners from the "
                "REST pages, visits from the mongodb jsonl and procedures from "
                "the flat csv into DuckDB tables of the same names."
            ),
            provenance="Constructed from the mart plan in the family spec.",
            version="1",
        ),
        attack_cases=_attack_cases()
        + el_attack_cases(
            tables,
            backend_assignments=backends,
            backends=len(backends),
            populations=populations,
        ),
    )


# --- Specimen declarations -------------------------------------------------

_m = _metrology  # vocabulary constants are read lazily from metrology

_MEASURES = ("attended_visit_count", "walk_in_visit_count", "procedure_units", "fee_total")

#: Zero-default restatements every measure description carries.
_ZERO_IF_NONE = tuple((column, "; 0 if none", "") for column in _MEASURES)

#: Join-blindness CLASS vocabulary for this family (population names belong
#: on the anchor axis only).
_JOIN_BLIND_TERMS = (
    "inner join", "left join", "no visits", "without visits",
    "without any visits", "without an attended", "no attended", "unmatched",
    "cannot distinguish", "indistinguishable", "never exercised",
    "not exercised", "would still score", "full reward",
)

_DECOY_NOTES = (
    "Note on ordering: the rule above fixes the output row order completely; "
    "there is no remaining tie to break.",
    "Note on empty results: practitioners with no attended visits are still "
    "emitted, with zero (never NULL) measures and caseload_band 'idle', "
    "exactly as the rules state.",
    "Note on constants: no output column is constant — every measure and the "
    "band vary across practitioners and across populations.",
    "Note on sources: every column and table named in the rules above appears "
    "in the public source schema; nothing needed is missing.",
)

#: The counterfactual conditions REWORDED: same distinguishing properties, but
#: the string "no visits" is gone.
_REWORDED_COUNTERFACTUAL_CONDITIONS = (
    "Literal constructed rows only — four practitioners at two clinics, restated.",
    "9011: a practitioner who has never seen a single patient -> every measure "
    "0, caseload_band 'idle'.",
    "9012: exactly two attended visits, one of them recorded twice as an exact "
    "duplicate row, plus one cancelled visit; the walk-in carries two "
    "procedure rows (3 + 4 units) and a fee of 25 -> attended_visit_count 2 "
    "(row-grain counting gives 3), walk_in_visit_count 1, procedure_units 7, "
    "fee_total 65, caseload_band 'light'.",
    "9021: three attended visits of one procedure each, one a walk-in -> "
    "attended_visit_count 3, caseload_band 'busy'.",
    "9022: a practitioner whose only visit was cancelled -> every measure 0, "
    "caseload_band 'idle'.",
    "One attended visit with NULL practitioner_id carries a fee of 99 and a "
    "procedure row; it contributes to no output row.",
)


def _ambiguity() -> tuple[AmbiguityVariant, ...]:
    m = _m()
    return (
        AmbiguityVariant(
            "ambiguity-no-null-rule",
            frozenset({7}),
            # DE-LEAK: '0 if none' restates the deleted COALESCE rule in the view.
            {"columns": _ZERO_IF_NONE},
            "prose omits the COALESCE-to-zero null-default rule",
            ("coalesce", "null", "zero", "default", "no attended", "empty", "no visits"),
            _MEASURES,
        ),
        AmbiguityVariant(
            "ambiguity-no-dedupe",
            frozenset({1}),
            # DE-LEAK: three descriptions and the surviving aggregate rule each
            # restate the deleted dedupe rule via DISTINCT.
            {
                "columns": (
                    ("attended_visit_count", "Count of DISTINCT visits", "Count of visits"),
                    ("walk_in_visit_count", "Count of DISTINCT visits", "Count of visits"),
                    ("fee_total", "over the DISTINCT visits in scope", "over the visits in scope"),
                ),
                "ops": (
                    (6, "attended_visit_count = COUNT(DISTINCT visit_id) over the visits in scope",
                     "attended_visit_count = COUNT of the visits in scope"),
                    (6, "walk_in_visit_count = COUNT(DISTINCT visit_id) over",
                     "walk_in_visit_count = COUNT of"),
                    (6, "SUM of fee over the DISTINCT visits in scope",
                     "SUM of fee over the visits in scope"),
                ),
            },
            "prose omits the duplicate-visit-row dedupe rule",
            ("duplicate", "dedup", "distinct", "twice", "repeated", "more than once", "double"),
            ("visit_id", "visit row", "attended_visit_count"),
        ),
        AmbiguityVariant(
            # CONTRADICTION: rule 7 keeps fee at the visit grain verbatim while
            # the description puts it at the procedure-row grain.
            "ambiguity-fee-contradicts-grain",
            frozenset(),
            {
                "columns": (
                    (
                        "fee_total",
                        "Sum of fee over the DISTINCT visits in scope, at the visit grain",
                        "Sum of fee over every procedure row of the visits in scope, so a "
                        "visit with three procedure rows contributes its fee three times",
                    ),
                )
            },
            "mart column description contradicts the surviving visit-grain rule "
            "about how fee_total sums fees",
            m._CONTRADICTION_TERMS,
            ("fee_total",),
        ),
        AmbiguityVariant(
            # CONTRADICTION on the CASE ladder: rule 9 says 'busy' from three.
            "ambiguity-band-contradicts-thresholds",
            frozenset(),
            {
                "columns": (
                    (
                        "caseload_band",
                        "from attended_visit_count as the rules state",
                        "from attended_visit_count: 'busy' from two attended visits "
                        "upward, 'light' for exactly one, else 'idle'",
                    ),
                )
            },
            "mart column description contradicts the surviving caseload_band "
            "thresholds",
            m._CONTRADICTION_TERMS,
            ("caseload_band",),
        ),
        AmbiguityVariant(
            "ambiguity-no-procedure-fanout-rule",
            frozenset({2}),
            # DE-LEAK: procedure_units' description restated the deleted rule.
            {
                "columns": (
                    (
                        "procedure_units",
                        "Sum of units over the procedure rows of the visits in scope",
                        "Procedure volume of the visits in scope",
                    ),
                )
            },
            "prose omits how a visit's procedure units are computed from its "
            "procedure rows",
            ("not specified", "unspecified", "undefined", "ambiguous", "unclear",
             "does not say", "two readings", "interpret", "how", "grain",
             "duplicate", "per visit", "per procedure"),
            ("procedure_units", "units", "procedures", "procedure row"),
        ),
        AmbiguityVariant(
            "ambiguity-no-left-join-rule",
            frozenset({4}),
            # DE-LEAK, three times over: the grain sentence, '0 if none' and the
            # COALESCE rule each answer whether a visit-less practitioner appears
            # at all; the ladder's 'idle' branch is the fourth restatement.
            {
                "grain": (", including practitioners with no visits", ""),
                "columns": _ZERO_IF_NONE + (
                    ("caseload_band", "'busy', 'light' or 'idle' from",
                     "'busy' or 'light' from"),
                ),
                "ops": (
                    (
                        7,
                        "COALESCE the four measures to 0 for practitioners without "
                        "in-scope visits (never NULL).",
                        "COALESCE the four measures to 0 wherever the aggregate would "
                        "be NULL (the mart never emits NULL measures).",
                    ),
                    (8, "'light' when it is 1 or 2, else 'idle'", "otherwise 'light'"),
                ),
            },
            "prose never says whether practitioners with no in-scope visits appear",
            ("inner join", "left join", "outer join", "not specified", "unspecified",
             "undefined", "ambiguous", "unclear", "does not say", "two readings",
             "interpret", "omitted", "dropped", "retained"),
            ("practitioners with no", "no attended", "without visits", "no visits",
             MART_NAME),
        ),
        AmbiguityVariant(
            # ANCHORS DELIBERATELY NARROW (see the demo's no-filter specimen).
            "ambiguity-no-filter",
            frozenset({0}),
            {},
            "prose never states WHICH visits count as attended",
            ("which visits", "filter", "all visits", "every visit", "not specified",
             "unspecified", "undefined", "ambiguous", "unclear", "does not say",
             "two readings", "interpret"),
            ("status", "cancelled"),
        ),
    )


#: Matched-only conditions: every practitioner has an attended visit, so the
#: INNER JOIN is indistinguishable. None contains "no visits"/"no attended".
_MATCHED_ONLY_CONDITIONS = {
    P.PRIMARY: (
        "Approximately 120 practitioners across about 40 clinics.",
        "Cancelled visits are present.",
        "Every practitioner has at least one attended visit.",
        "Some ATTENDED visits have NULL practitioner_id and carry procedure rows; "
        "they belong to no practitioner and contribute to no output row.",
        "Attended visits may carry several procedure rows.",
    ),
    P.RESAMPLED: (
        "Same generator and conditions as primary; new seed and new id ranges.",
    ),
}

_MATCHED_COUNTERFACTUAL_ROWS = {
    "clinics": COUNTERFACTUAL_LITERAL_ROWS["clinics"],
    "practitioners": COUNTERFACTUAL_LITERAL_ROWS["practitioners"],
    "visits": (
        {"visit_id": 90111, "practitioner_id": 9011, "status": "attended",
         "booking": "scheduled", "fee": 20.0},
        {"visit_id": 90121, "practitioner_id": 9012, "status": "attended",
         "booking": "walk_in", "fee": 25.0},
        {"visit_id": 90211, "practitioner_id": 9021, "status": "attended",
         "booking": "scheduled", "fee": 10.0},
        {"visit_id": 90221, "practitioner_id": 9022, "status": "attended",
         "booking": "scheduled", "fee": 15.0},
    ),
    "procedures": (
        {"visit_id": 90111, "procedure_code": "consult", "units": 2},
        {"visit_id": 90121, "procedure_code": "xray", "units": 3},
        {"visit_id": 90121, "procedure_code": "consult", "units": 4},
        {"visit_id": 90211, "procedure_code": "therapy", "units": 1},
        {"visit_id": 90221, "procedure_code": "cleaning", "units": 5},
    ),
}

#: Every visit is attended everywhere: the status filter is invisible.
_NO_CANCELLED_CONDITIONS = {
    P.DEVELOPMENT: (
        "Tiny debug data: exactly two clinics and three practitioners.",
        "Every visit in this population is attended.",
        "No NULL practitioner_id, no duplicate rows.",
    ),
    P.PRIMARY: (
        "Approximately 120 practitioners across about 40 clinics.",
        "Every visit in this population is attended.",
        "Some practitioners have no visits at all.",
        "Some visits have NULL practitioner_id and carry procedure rows; they "
        "belong to no practitioner and contribute to no output row.",
        "Attended visits may carry several procedure rows (fee_total counts a "
        "fee once per visit, never once per procedure row).",
    ),
    P.RESAMPLED: (
        "Same generator and conditions as primary; new seed and new id ranges.",
    ),
    P.STRESS: (
        "A few hot practitioners hold a large share of all visits.",
        "Exact-duplicate visit rows are present (correct logic must dedupe).",
        "Exact-duplicate procedure rows are also present; procedure rows are "
        "not deduplicated.",
        "Ties: distinct practitioners with identical totals.",
        "Every visit in this population is attended.",
    ),
    P.COUNTERFACTUAL: (
        "Literal constructed rows: four practitioners; every visit present is "
        "attended, and one practitioner has none.",
    ),
}

_NO_CANCELLED_COUNTERFACTUAL_ROWS = {
    "clinics": COUNTERFACTUAL_LITERAL_ROWS["clinics"],
    "practitioners": COUNTERFACTUAL_LITERAL_ROWS["practitioners"],
    "visits": tuple(
        row for row in COUNTERFACTUAL_LITERAL_ROWS["visits"]
        if row["status"] == "attended"
    ),
    "procedures": tuple(
        row for row in COUNTERFACTUAL_LITERAL_ROWS["procedures"]
        if row["visit_id"] != 90123
    ),
}

#: No population declares duplicate visit rows: the dedupe rule is unenforced.
_NO_DUPLICATE_CONDITIONS = {
    P.STRESS: (
        "A few hot practitioners hold a large share of all visits.",
        "Exact-duplicate procedure rows are present; procedure rows are not "
        "deduplicated.",
        "Ties: distinct practitioners with identical totals.",
        "Every practitioner has at least one attended visit "
        "(INNER JOIN is indistinguishable here by design).",
    ),
    P.COUNTERFACTUAL: (
        "Literal constructed rows only — four practitioners at two clinics.",
        "9011: a practitioner with no visits -> every measure 0, caseload_band 'idle'.",
        "9012: two attended visits plus one cancelled visit; the walk-in carries "
        "two procedure rows (3 + 4 units) and a fee of 25 -> attended_visit_count "
        "2, walk_in_visit_count 1, procedure_units 7, fee_total 65, "
        "caseload_band 'light'.",
        "9021: three attended visits of one procedure each, one a walk-in -> "
        "attended_visit_count 3, caseload_band 'busy'.",
        "9022: only a cancelled visit -> every measure 0, caseload_band 'idle'.",
        "One attended visit with NULL practitioner_id carries a fee of 99 and a "
        "procedure row; it contributes to no output row.",
    ),
}

_NO_DUPLICATE_COUNTERFACTUAL_ROWS = {
    **COUNTERFACTUAL_LITERAL_ROWS,
    "visits": tuple(
        row for i, row in enumerate(COUNTERFACTUAL_LITERAL_ROWS["visits"]) if i != 1
    ),
}

#: No population declares a visit with NULL practitioner_id: the exclusion
#: rule is unenforced.
_NO_NULL_PRACTITIONER_CONDITIONS = {
    P.PRIMARY: (
        "Approximately 120 practitioners across about 40 clinics.",
        "Cancelled visits are present.",
        "Some practitioners have no visits at all.",
        "Some practitioners have visits but no attended visits.",
        "Every visit carries the practitioner_id of the practitioner who saw the patient.",
        "Attended visits may carry several procedure rows (fee_total counts a "
        "fee once per visit, never once per procedure row).",
    ),
    P.COUNTERFACTUAL: (
        "Literal constructed rows only — four practitioners at two clinics.",
        "9011: a practitioner with no visits -> every measure 0, caseload_band 'idle'.",
        "9012: two attended visits, one of them recorded twice as an exact "
        "duplicate row, plus one cancelled visit; the walk-in carries two "
        "procedure rows (3 + 4 units) and a fee of 25 -> attended_visit_count 2 "
        "(row-grain counting gives 3), walk_in_visit_count 1, procedure_units "
        "7, fee_total 65, caseload_band 'light'.",
        "9021: three attended visits of one procedure each, one a walk-in -> "
        "attended_visit_count 3, caseload_band 'busy'.",
        "9022: only a cancelled visit -> every measure 0, caseload_band 'idle'.",
    ),
}

_NO_NULL_PRACTITIONER_COUNTERFACTUAL_ROWS = {
    **COUNTERFACTUAL_LITERAL_ROWS,
    "visits": tuple(
        row for row in COUNTERFACTUAL_LITERAL_ROWS["visits"]
        if row["practitioner_id"] is not None
    ),
    "procedures": tuple(
        row for row in COUNTERFACTUAL_LITERAL_ROWS["procedures"]
        if row["visit_id"] != 90901
    ),
}

#: Every visit carries exactly ONE procedure row everywhere, so the fee's
#: grain (once per visit vs once per procedure row) and the units fan-out are
#: both blind.
_SINGLE_PROCEDURE_CONDITIONS = {
    P.DEVELOPMENT: (
        "Tiny debug data: exactly two clinics and three practitioners.",
        "Every practitioner has at least one attended visit "
        "(INNER JOIN is indistinguishable here by design).",
        "Every visit carries exactly one procedure row.",
        "No NULL practitioner_id, no duplicate rows.",
    ),
    P.PRIMARY: (
        "Approximately 120 practitioners across about 40 clinics.",
        "Cancelled visits are present.",
        "Some practitioners have no visits at all.",
        "Some practitioners have visits but no attended visits.",
        "Some ATTENDED visits have NULL practitioner_id; they belong to no "
        "practitioner and contribute to no output row.",
        "Every visit carries exactly one procedure row.",
    ),
    P.RESAMPLED: (
        "Same generator and conditions as primary; new seed and new id ranges "
        "(memorization check).",
    ),
    P.STRESS: (
        "A few hot practitioners hold a large share of all visits.",
        "Exact-duplicate visit rows are present (correct logic must dedupe).",
        "Every visit carries exactly one procedure row.",
        "Ties: distinct practitioners with identical totals.",
        "Every practitioner has at least one attended visit "
        "(INNER JOIN is indistinguishable here by design).",
    ),
    P.COUNTERFACTUAL: (
        "Literal constructed rows only — four practitioners at two clinics.",
        "9011: a practitioner with no visits -> every measure 0, caseload_band 'idle'.",
        "9012: two attended visits, one recorded twice as an exact duplicate "
        "row, plus one cancelled visit; each visit carries a single procedure "
        "row -> attended_visit_count 2, walk_in_visit_count 1, fee_total 65, "
        "caseload_band 'light'.",
        "9021: three attended visits of one procedure each, one a walk-in -> "
        "attended_visit_count 3, caseload_band 'busy'.",
        "9022: only a cancelled visit -> every measure 0, caseload_band 'idle'.",
        "One attended visit with NULL practitioner_id carries a fee of 99 and a "
        "procedure row; it contributes to no output row.",
    ),
}

_SINGLE_PROCEDURE_COUNTERFACTUAL_ROWS = {
    **COUNTERFACTUAL_LITERAL_ROWS,
    "procedures": (
        {"visit_id": 90121, "procedure_code": "consult", "units": 2},
        {"visit_id": 90122, "procedure_code": "xray", "units": 7},
        {"visit_id": 90123, "procedure_code": "cleaning", "units": 9},
        {"visit_id": 90211, "procedure_code": "therapy", "units": 1},
        {"visit_id": 90212, "procedure_code": "therapy", "units": 1},
        {"visit_id": 90213, "procedure_code": "consult", "units": 1},
        {"visit_id": 90221, "procedure_code": "cleaning", "units": 1},
        {"visit_id": 90901, "procedure_code": "xray", "units": 2},
    ),
}

#: EVERY discriminating condition moved into `development`.
_DEVELOPMENT_ONLY_CONDITIONS = {
    P.DEVELOPMENT: (
        "Tiny debug data: exactly two clinics and three practitioners.",
        "Cancelled visits are present.",
        "Some practitioners have no visits at all.",
        "Exact-duplicate visit rows are present.",
        "Some ATTENDED visits have NULL practitioner_id.",
        "Attended visits may carry several procedure rows.",
    ),
    P.PRIMARY: (
        "Approximately 120 practitioners across about 40 clinics.",
        "Every practitioner has at least one attended visit.",
        "Every visit is attended, carries a practitioner_id, and appears once.",
        "Every visit carries exactly one procedure row.",
    ),
    P.RESAMPLED: (
        "Same generator and conditions as primary; new seed and new id ranges.",
    ),
    P.STRESS: (
        "A few hot practitioners hold a large share of all visits.",
        "Every practitioner has at least one attended visit.",
        "Every visit is attended, carries a practitioner_id, and appears once.",
        "Every visit carries exactly one procedure row.",
    ),
    P.COUNTERFACTUAL: (
        "Literal constructed rows: four practitioners, each with exactly one "
        "attended single-procedure visit.",
    ),
}

_DEVELOPMENT_ONLY_COUNTERFACTUAL_ROWS = {
    "clinics": COUNTERFACTUAL_LITERAL_ROWS["clinics"],
    "practitioners": COUNTERFACTUAL_LITERAL_ROWS["practitioners"],
    "visits": (
        {"visit_id": 90111, "practitioner_id": 9011, "status": "attended",
         "booking": "scheduled", "fee": 20.0},
        {"visit_id": 90121, "practitioner_id": 9012, "status": "attended",
         "booking": "scheduled", "fee": 25.0},
        {"visit_id": 90211, "practitioner_id": 9021, "status": "attended",
         "booking": "scheduled", "fee": 10.0},
        {"visit_id": 90221, "practitioner_id": 9022, "status": "attended",
         "booking": "scheduled", "fee": 15.0},
    ),
    "procedures": (
        {"visit_id": 90111, "procedure_code": "consult", "units": 2},
        {"visit_id": 90121, "procedure_code": "xray", "units": 3},
        {"visit_id": 90211, "procedure_code": "therapy", "units": 1},
        {"visit_id": 90221, "procedure_code": "cleaning", "units": 5},
    ),
}


def _population() -> tuple[PopulationVariant, ...]:
    m = _m()
    population_names = ("counterfactual", "primary", "resampled", "stress", "development")
    return (
        PopulationVariant(
            "population-dropped-counterfactual",
            _MATCHED_ONLY_CONDITIONS,
            "counterfactual population removed; remaining conditions guarantee "
            "every practitioner an attended visit",
            _JOIN_BLIND_TERMS,
            population_names,
            drop=(P.COUNTERFACTUAL,),
        ),
        PopulationVariant(
            "population-neutered-counterfactual",
            {
                **_MATCHED_ONLY_CONDITIONS,
                P.COUNTERFACTUAL: (
                    "Literal constructed rows: four practitioners, each with "
                    "exactly one attended visit.",
                ),
            },
            "counterfactual rows replaced: every practitioner has an attended "
            "visit, so the wrong join is indistinguishable",
            _JOIN_BLIND_TERMS,
            population_names,
            literal_rows=_MATCHED_COUNTERFACTUAL_ROWS,
        ),
        PopulationVariant(
            "population-no-cancelled-visits",
            _NO_CANCELLED_CONDITIONS,
            "every visit in every population is attended, so the status filter "
            "is never exercised",
            m._FILTER_BLIND_TERMS,
            ("status", "cancelled", "attended visit", "filter"),
            literal_rows=_NO_CANCELLED_COUNTERFACTUAL_ROWS,
        ),
        PopulationVariant(
            "population-no-duplicate-visits",
            _NO_DUPLICATE_CONDITIONS,
            "no population declares duplicate visit rows, so the dedupe rule is "
            "never exercised",
            m._DEDUPE_BLIND_TERMS,
            ("stress", "visit row", "visit_id", "counterfactual"),
            literal_rows=_NO_DUPLICATE_COUNTERFACTUAL_ROWS,
        ),
        PopulationVariant(
            "population-no-null-practitioner-visits",
            _NO_NULL_PRACTITIONER_CONDITIONS,
            "no population declares a visit with a NULL practitioner_id, so the "
            "rule excluding those visits is never exercised",
            m._BLIND_TERMS + ("null",),
            ("primary", "practitioner_id", "resampled", "stress", "counterfactual"),
            literal_rows=_NO_NULL_PRACTITIONER_COUNTERFACTUAL_ROWS,
        ),
        PopulationVariant(
            "population-single-procedure-visits",
            _SINGLE_PROCEDURE_CONDITIONS,
            "every visit carries exactly one procedure row, so summing fees at "
            "the procedure-row grain is indistinguishable from the visit grain",
            m._BLIND_TERMS + ("fan-out", "fanout", "grain", "procedure", "once per"),
            ("primary", "stress", "counterfactual", "resampled", "fee_total",
             "procedure_units"),
            literal_rows=_SINGLE_PROCEDURE_COUNTERFACTUAL_ROWS,
        ),
        PopulationVariant(
            "population-discriminators-in-development",
            _DEVELOPMENT_ONLY_CONDITIONS,
            "every distinguishing condition sits in the solver-visible "
            "development split; no GRADED population can catch wrong logic",
            m._BLIND_TERMS + ("development",),
            ("primary", "resampled", "stress", "counterfactual"),
            literal_rows=_DEVELOPMENT_ONLY_COUNTERFACTUAL_ROWS,
        ),
    )


def _shortcut() -> tuple[ShortcutVariant, ...]:
    m = _m()
    terms = m._SHORTCUT_DETECTION_TERMS
    return (
        ShortcutVariant(
            "shortcut-constant-columns",
            "mart column descriptions announce constant outputs",
            terms,
            ("fee_total", "attended_visit_count", MART_NAME),
            suffix=" Always exactly the same value for every practitioner in every population.",
        ),
        ShortcutVariant(
            "shortcut-identity-columns",
            "mart column descriptions make every measure a function of the key alone",
            terms + ("key alone", "from the key", "copy", "identity", "echo"),
            ("practitioner_id", "fee_total", "attended_visit_count"),
            suffix=(
                " For every practitioner in every population this equals the "
                "practitioner_id itself, so it can be produced from the key alone."
            ),
        ),
        ShortcutVariant(
            "shortcut-constant-one-measure",
            "ONE measure is declared constant while the others still vary",
            terms,
            ("attended_visit_count",),
            columns=("attended_visit_count",),
            suffix=" This is exactly 1 for every practitioner in every population.",
        ),
        ShortcutVariant(
            "shortcut-derivable-measure",
            "one measure is a fixed multiple of another, so it need never be "
            "computed from the sources",
            terms + ("derive", "derivable", "function of", "multiple of",
                     "closed-form", "closed form", "determines the other",
                     "determined by", "ten times", "10 *", "10x"),
            ("fee_total", "attended_visit_count"),
            columns=("fee_total",),
            suffix=(
                " For every practitioner in every population this is exactly ten "
                "times attended_visit_count, so one measure determines the other."
            ),
        ),
        ShortcutVariant(
            "shortcut-dev-only-population",
            "only the tiny development population is graded",
            terms,
            ("development", "three practitioners", "single population",
             "only population", "one population"),
            keep=(P.DEVELOPMENT,),
        ),
        ShortcutVariant(
            "shortcut-counterfactual-only-population",
            "only the four-row counterfactual population is graded",
            terms,
            ("counterfactual", "four practitioners", "single population",
             "only population", "one population"),
            keep=(P.COUNTERFACTUAL,),
        ),
        ShortcutVariant(
            "shortcut-tiny-populations-only",
            "only the two tiny populations are graded: seven practitioners in "
            "total, enumerable end to end",
            terms,
            ("development", "counterfactual", "three practitioners",
             "four practitioners", "seven practitioners"),
            keep=(P.DEVELOPMENT, P.COUNTERFACTUAL),
        ),
    )


def _feasibility() -> tuple[FeasibilityVariant, ...]:
    return (
        FeasibilityVariant(
            "feasibility-missing-status",
            "public schema lacks visits.status; the attended-visit filter is unsatisfiable",
            ("status",),
            table="visits", column="status",
        ),
        FeasibilityVariant(
            "feasibility-missing-procedures",
            "public schema lacks the procedures table; procedure_units cannot be computed",
            ("procedures", "procedure row", "units", "procedure_units"),
            table="procedures", column=None,
        ),
        FeasibilityVariant(
            "feasibility-missing-units",
            "public schema lacks procedures.units; procedure_units cannot be computed",
            ("units", "procedure_units"),
            table="procedures", column="units",
        ),
        FeasibilityVariant(
            "feasibility-missing-fee",
            "public schema lacks visits.fee; fee_total cannot be computed",
            ("fee", "fee_total"),
            table="visits", column="fee",
        ),
        FeasibilityVariant(
            "feasibility-missing-practitioner-clinic",
            "public schema lacks practitioners.clinic_id; no practitioner can be "
            "joined to a clinic, so clinic_name is unreachable",
            ("clinic_id", "clinic_name", "clinics", "practitioners"),
            table="practitioners", column="clinic_id",
        ),
        FeasibilityVariant(
            "feasibility-unpublished-rate-input",
            "the mart states fee_total is converted with visits.currency_rate, a "
            "column the public schema never publishes",
            ("currency_rate", "currency rate", "fee_total"),
            mart_column="fee_total",
            suffix=(
                " Every fee is converted to the reporting currency by multiplying "
                "it by the currency_rate column of the visits table before it is "
                "added in."
            ),
        ),
        FeasibilityVariant(
            "feasibility-unpublished-referral-input",
            "the mart states walk_in_visit_count is restricted by "
            "visits.referral_source, a column the public schema never publishes",
            ("referral_source", "referral source", "walk_in_visit_count"),
            mart_column="walk_in_visit_count",
            suffix=(
                " Only visits whose referral_source column on the visits table "
                "equals 'self' are counted."
            ),
        ),
    )


def _definition() -> FamilyDefinition:
    return FamilyDefinition(
        name=FAMILY_NAME,
        task=task,
        canary_guid=CANARY_GUID,
        decoy_notes=_DECOY_NOTES,
        reworded_counterfactual_conditions=_REWORDED_COUNTERFACTUAL_CONDITIONS,
        ambiguity=_ambiguity(),
        population=_population(),
        shortcut=_shortcut(),
        feasibility=_feasibility(),
    )


def family():
    """The built `FixtureFamily` (memoized by the package `__init__`)."""
    return build_family(_definition())
