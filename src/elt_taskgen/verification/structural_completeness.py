"""Check that every referenced task object exists on all published surfaces.

The gate covers operation tables, columns, joins, keys, relationships, rendered
documentation and schemas, and declared column kinds. It is pure, deterministic, and
does not parse prose.
"""

from __future__ import annotations

from elt_taskgen.models import GateResult, MartOpKind, MartSpec, TaskIR

__all__ = [
    "GATE_NAME",
    "check_structural_completeness",
    "structural_completeness_gate",
]

GATE_NAME = "structural-completeness"


# Plan reference resolution (the namespace compile_plan_sql binds)

def _produced_aliases(op) -> set[str]:
    """Output aliases the op's own select list produces.

    Delegates to `generation/mart_plan._produced_aliases` (ONE definition of
    the rule); the lazy import keeps `verification` off the module-scope import
    edge into `reference/solution`.
    """
    from elt_taskgen.generation.mart_plan import _produced_aliases as aliases

    return set(aliases(op))


def _binding(op) -> str:
    """The relation name this op binds for LATER ops to reference.

    Exactly `compile_plan_sql`'s rule: ``details['name']`` when present, else
    the op's single table. TIE_BREAK binds nothing (ordering, not a relation).
    """
    if op.kind is MartOpKind.TIE_BREAK:
        return ""
    return op.details.get("name") or (op.tables[0] if op.tables else "")


def _check_plan(task: TaskIR, mart: MartSpec) -> list[str]:
    """Checks A, B, C and G for one mart's plan."""
    problems: list[str] = []
    plan = mart.plan
    where = f"mart {mart.name!r} plan"

    table_names = {t.name for t in task.tables}
    all_task_cols = {c.name for t in task.tables for c in t.columns}
    mart_cols = {c.name for c in mart.columns}
    produced: set[str] = set()
    bound: set[str] = set()

    for idx, op in enumerate(plan.ops):
        loc = f"{where} op[{idx}] ({op.kind.value})"

        # --- A. every referenced table exists (published, or bound upstream) --
        missing_tables = [
            t for t in op.tables if t not in table_names and t not in bound
        ]
        if missing_tables:
            problems.append(
                f"{loc}: names source table(s) {missing_tables} that the "
                f"published source schemas do not contain "
                f"(published: {sorted(table_names)})"
            )

        known_op_tables = [t for t in op.tables if t in table_names]
        touches_intermediate = any(
            t in bound and t not in table_names for t in op.tables
        )

        # --- B. every referenced column resolves ------------------------------
        if known_op_tables and not touches_intermediate:
            valid_cols: set[str] = set()
            for name in known_op_tables:
                valid_cols |= {c.name for c in task.table(name).columns}
            scope = f"the columns of {known_op_tables}"
        else:
            # An intermediate relation is opaque, so resolution falls back to
            # the task-wide set — the same concession `validate_plan` makes.
            valid_cols = set(all_task_cols)
            scope = "the columns of any published source table"
        valid_cols |= mart_cols | produced | _produced_aliases(op)
        missing_cols = [c for c in op.columns if c not in valid_cols]
        if missing_cols:
            problems.append(
                f"{loc}: names column(s) {missing_cols} that resolve to nothing "
                f"— not in {scope}, not a {mart.name!r} output column, not "
                f"produced by an earlier op"
            )

        # --- C. a source-to-source join has a declared relationship -----------
        if (
            op.kind is MartOpKind.JOIN
            and not missing_tables
            and not touches_intermediate
            and len(known_op_tables) == 2
        ):
            a, b = known_op_tables
            backing = [
                rel
                for rel in task.relationships
                if {rel.child_table, rel.parent_table} == {a, b}
            ]
            if not backing:
                problems.append(
                    f"{loc}: joins {a!r} to {b!r}, but the published schemas "
                    "declare no relationship between them — the solver is given "
                    "no key to join on"
                )
            elif op.columns:
                rel_cols: set[str] = set()
                for rel in backing:
                    rel_cols |= set(rel.child_columns) | set(rel.parent_columns)
                if not set(op.columns) & rel_cols:
                    problems.append(
                        f"{loc}: join columns {list(op.columns)} overlap none of "
                        f"the published relationship keys {sorted(rel_cols)} "
                        f"between {a!r} and {b!r}"
                    )

        binding = _binding(op)
        if binding:
            bound.add(binding)
        produced |= set(op.columns)

    # --- G. every DECLARED column kind is the kind the ops imply ------------
    # Deferred import, same reason as `_produced_aliases`: one certifier, no edge.
    from elt_taskgen.generation.mart_plan import column_kind_problems

    problems.extend(f"{where}: {p}" for p in column_kind_problems(mart))

    return problems


# Declared keys and relationships (checks D and E)

def _check_keys_and_relationships(task: TaskIR) -> list[str]:
    problems: list[str] = []
    columns_by_table = {
        t.name: {c.name for c in t.columns} for t in task.tables
    }

    for table in task.tables:
        have = columns_by_table[table.name]
        for group, label in (
            (table.primary_key, "primary key"),
            (table.business_key, "business key"),
        ):
            missing = [c for c in group if c not in have]
            if missing:
                problems.append(
                    f"table {table.name!r}: declared {label} names column(s) "
                    f"{missing} the table does not publish "
                    f"(published: {sorted(have)})"
                )

    for idx, rel in enumerate(task.relationships):
        edge = (
            f"{rel.child_table}({', '.join(rel.child_columns)}) -> "
            f"{rel.parent_table}({', '.join(rel.parent_columns)})"
        )
        for side, tname, cols in (
            ("child", rel.child_table, rel.child_columns),
            ("parent", rel.parent_table, rel.parent_columns),
        ):
            if tname not in columns_by_table:
                problems.append(
                    f"relationship[{idx}] {edge}: {side} table {tname!r} is not "
                    "a published source table"
                )
                continue
            missing = [c for c in cols if c not in columns_by_table[tname]]
            if missing:
                problems.append(
                    f"relationship[{idx}] {edge}: {side} column(s) {missing} are "
                    f"not published on table {tname!r}"
                )
    return problems


# The published block must agree with itself (check F)

def _markdown_surface(
    lines: list[str],
) -> tuple[dict[str, list[str]], list[tuple[str, tuple[str, ...]]],
           list[tuple[str, str, tuple[str, ...]]]]:
    """Parse the shipped ``## Source tables`` block back into structure.

    Returns (columns per table, (table, key columns), (child, parent, named
    columns)). Parsing the RENDERED TEXT is the point: a block that contradicts
    itself on the page the solver reads is invisible to an IR-level check.
    """
    columns: dict[str, list[str]] = {}
    keys: list[tuple[str, tuple[str, ...]]] = []
    rels: list[tuple[str, str, tuple[str, ...]]] = []
    current = ""
    in_relationships = False
    for raw in lines:
        line = raw.strip()
        if line.startswith("### Relationships"):
            in_relationships = True
            current = ""
            continue
        if line.startswith("### "):
            in_relationships = False
            current = line[4:].split("  (source backend:")[0].strip()
            columns.setdefault(current, [])
            continue
        if in_relationships and line.startswith("- ") and "->" in line:
            body = line[2:].split(" [")[0].strip()
            child_part, _, parent_part = body.partition("->")
            child, _, child_cols = child_part.strip().partition("(")
            parent, _, parent_cols = parent_part.strip().partition("(")
            named = tuple(
                c.strip()
                for c in (child_cols.rstrip(")") + "," + parent_cols.rstrip(")")).split(",")
                if c.strip()
            )
            rels.append((child.strip(), parent.strip(), named))
            continue
        if not current or not line.startswith("- "):
            continue
        body = line[2:]
        for label in ("primary key:", "business key:"):
            if body.startswith(label):
                keys.append(
                    (
                        current,
                        tuple(
                            c.strip()
                            for c in body[len(label):].split(",")
                            if c.strip()
                        ),
                    )
                )
                break
        else:
            if body.startswith("`") and "`" in body[1:]:
                columns[current].append(body[1: body.index("`", 1)])
    return columns, keys, rels


def _check_published_block(task: TaskIR) -> list[str]:
    """The two shipped public files must agree with the IR and each other.

    Calls the exporter's OWN functions, never a re-derivation, so the bytes
    checked here are the bytes the bundle ships.
    """
    import csv
    import io

    from elt_taskgen.export.eltbench import _source_schema_markdown, schema_csv

    problems: list[str] = []
    md_columns, md_keys, md_rels = _markdown_surface(_source_schema_markdown(task))
    ir_columns = {t.name: [c.name for c in t.columns] for t in task.tables}

    missing_from_md = sorted(set(ir_columns) - set(md_columns))
    extra_in_md = sorted(set(md_columns) - set(ir_columns))
    if missing_from_md or extra_in_md:
        problems.append(
            "documentation.md '## Source tables' block does not publish the "
            f"declared tables: missing {missing_from_md}, unexpected "
            f"{extra_in_md}"
        )

    for table in task.tables:
        published = md_columns.get(table.name)
        if published is None:
            continue
        if published != ir_columns[table.name]:
            problems.append(
                f"documentation.md publishes table {table.name!r} with columns "
                f"{published}, but the task declares {ir_columns[table.name]}"
            )
        rows = list(csv.reader(io.StringIO(schema_csv(table))))
        csv_columns = [row[0] for row in rows[1:] if row]
        if csv_columns != ir_columns[table.name]:
            problems.append(
                f"schemas/{table.name}.csv publishes columns {csv_columns}, but "
                f"the task declares {ir_columns[table.name]} — the two shipped "
                "files disagree about what the table contains"
            )

    # SELF-CONSISTENCY: the block must not name a column it does not list.
    for table_name, key_columns in md_keys:
        listed = md_columns.get(table_name, [])
        missing = [c for c in key_columns if c not in listed]
        if missing:
            problems.append(
                f"documentation.md declares a key on {table_name!r} over "
                f"column(s) {missing} that the SAME block does not list "
                f"(it lists {listed}) — the published schema contradicts itself"
            )
    for child, parent, named in md_rels:
        for side in (child, parent):
            if side not in md_columns:
                problems.append(
                    f"documentation.md prints a relationship {child} -> {parent} "
                    f"naming table {side!r}, which the SAME block does not "
                    "publish — the published schema contradicts itself"
                )
        listed = set(md_columns.get(child, [])) | set(md_columns.get(parent, []))
        missing = [c for c in named if c not in listed]
        if missing:
            problems.append(
                f"documentation.md prints a relationship {child} -> {parent} "
                f"over column(s) {missing} that the SAME block does not list on "
                "either endpoint — the published schema contradicts itself"
            )
    return problems


# Public entry points

def check_structural_completeness(task: TaskIR) -> list[str]:
    """Every referenced-but-absent object, named with where it was referenced.

    Empty list = everything referenced is present, every declared column kind is
    what its plan implies, and the shipped schema block agrees with itself and
    `schemas/<table>.csv`. Fail closed: callers refuse BEFORE any live call.
    """
    problems: list[str] = []
    for mart in task.marts:
        problems.extend(_check_plan(task, mart))
    problems.extend(_check_keys_and_relationships(task))
    problems.extend(_check_published_block(task))
    return problems


def structural_completeness_gate(task: TaskIR) -> GateResult:
    """GateResult view of the check (red iff any referenced object is absent)."""
    problems = check_structural_completeness(task)
    return GateResult(
        gate=GATE_NAME,
        passed=not problems,
        details=(
            "every source table, column, key and relationship the mart plans "
            "and the published schemas reference is present, every declared "
            "mart column kind is the kind its plan implies, and the shipped "
            "source-schema block agrees with itself"
            if not problems
            else "; ".join(problems)
        ),
        evidence={
            "problem_count": str(len(problems)),
            "tables": ",".join(t.name for t in task.tables),
            "marts": ",".join(m.name for m in task.marts),
        },
    )
