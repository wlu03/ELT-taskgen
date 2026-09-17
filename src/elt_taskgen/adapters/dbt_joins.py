"""Recover relationships from compiled dbt join predicates."""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from elt_taskgen.models import Relationship


def recover_join_relationships(spec) -> tuple[Relationship, ...]:
    """Recover optional relationships whose join sides resolve unambiguously.

    ``id`` ownership determines direction. One-to-one joins are omitted;
    co-grain joins require matching names and exactly one aggregated side.
    """
    from elt_taskgen.adapters.dbt import (
        _from_and_joins,
        _source_resolver,
        _source_uids_by_name,
        staging_alias_map,
    )

    uids_by_name = _source_uids_by_name(spec)
    source_cols_by_uid = {
        s.unique_id: {c.name.lower() for c in s.columns} for s in spec.sources
    }
    amap = staging_alias_map(spec)

    def source_columns(table: str) -> set[str]:
        uids = uids_by_name.get(table, [])
        return source_cols_by_uid[uids[0]] if len(uids) == 1 else set()

    recovered: dict[tuple, Relationship] = {}
    for model in sorted(spec.models, key=lambda m: m.unique_id):
        sql = model.compiled_code
        if not sql.strip():
            continue
        try:
            tree = sqlglot.parse_one(sql, read="duckdb")
        except Exception:
            # Unparseable model: contributes no edges rather than a false claim.
            continue
        ctes = {c.alias_or_name: c for c in tree.find_all(exp.CTE)}
        resolve = _source_resolver(spec, model, tree)

        def grain_of(name: str, _ctes=ctes) -> frozenset[str] | None:
            """Return the CTE's resolved output grouping, if decidable."""
            cte = _ctes.get(name)
            if cte is None:
                return None
            sel = cte.this
            group = sel.args.get("group") if isinstance(sel, exp.Select) else None
            if group is None:
                return None
            outputs = [e.alias_or_name.lower() for e in sel.expressions]
            cols: list[str] = []
            for e in group.expressions:
                if isinstance(e, exp.Column):
                    cols.append(e.name.lower())
                elif isinstance(e, exp.Literal) and e.is_int:
                    i = int(e.name) - 1
                    if not (0 <= i < len(outputs) and outputs[i]):
                        return None
                    cols.append(outputs[i])
                else:
                    return None
            return frozenset(c for c in cols if c != "source_relation")

        def src_col(table: str, col: str) -> str | None:
            if col in source_columns(table):
                return col
            return amap.get(col, {}).get(table)

        for join in tree.find_all(exp.Join):
            on = join.args.get("on")
            if on is None:
                continue
            # Table aliases of the enclosing SELECT (`... stg_b as b on b.id`)
            # resolve to nothing alone, so map alias -> relation name first.
            enclosing = join.find_ancestor(exp.Select)
            alias_map: dict[str, str] = {}
            if enclosing is not None:
                for relation in _from_and_joins(enclosing):
                    if isinstance(relation, exp.Table) and relation.alias:
                        alias_map[relation.alias] = relation.name

            def relation_of(name: str, _alias_map=alias_map) -> str:
                return _alias_map.get(name, name)

            terms: list[tuple[str, str, str, str]] = []
            for eq in on.find_all(exp.EQ):
                left, right = eq.left, eq.right
                if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                    continue
                if "source_relation" in (left.name.lower(), right.name.lower()):
                    continue
                terms.append(
                    (left.table, left.name.lower(), right.table, right.name.lower())
                )
            if not terms:
                continue
            lqs = {t[0] for t in terms}
            rqs = {t[2] for t in terms}
            if len(lqs) != 1 or len(rqs) != 1:
                continue
            lq, rq = relation_of(lqs.pop()), relation_of(rqs.pop())
            lres, rres = resolve(lq), resolve(rq)
            if (
                len(lres) != 1
                or len(rres) != 1
                or any(x.startswith("?") for x in lres | rres)
            ):
                continue
            lt, rt = next(iter(lres)), next(iter(rres))
            sided: list[tuple[str, str, str, str]] = []
            resolved_ok = True
            for _lq, lc, _rq, rc in terms:
                ls, rs = src_col(lt, lc), src_col(rt, rc)
                if ls is None or rs is None:
                    resolved_ok = False
                    break
                sided.append((lc, ls, rc, rs))
            if not resolved_ok:
                continue
            l_own = all(ls == "id" for _, ls, _, _ in sided)
            r_own = all(rs == "id" for _, _, _, rs in sided)
            if l_own and r_own:
                continue  # rung 3: genuine 1:1, refused rather than guessed
            if r_own:
                child, parent = lt, rt
                child_cols = tuple(ls for _, ls, _, _ in sided)
                parent_cols = tuple(rs for _, _, _, rs in sided)
            elif l_own:
                child, parent = rt, lt
                child_cols = tuple(rs for _, _, _, rs in sided)
                parent_cols = tuple(ls for _, ls, _, _ in sided)
            else:
                # Co-grain rung: names must match, exactly one side aggregated.
                if not all(lc == rc for lc, _, rc, _ in sided):
                    continue
                key = frozenset(lc for lc, _, _, _ in sided)
                l_agg = grain_of(lq) == key
                r_agg = grain_of(rq) == key
                if l_agg == r_agg:
                    continue  # both or neither: undecidable, refused
                if l_agg:
                    child, parent = lt, rt
                    child_cols = tuple(ls for _, ls, _, _ in sided)
                    parent_cols = tuple(rs for _, _, _, rs in sided)
                else:
                    child, parent = rt, lt
                    child_cols = tuple(rs for _, _, _, rs in sided)
                    parent_cols = tuple(ls for _, ls, _, _ in sided)
            key_tuple = (child, child_cols, parent, parent_cols)
            if key_tuple not in recovered:
                recovered[key_tuple] = Relationship(
                    child_table=child,
                    child_columns=child_cols,
                    parent_table=parent,
                    parent_columns=parent_cols,
                    required=False,
                )
    return tuple(
        sorted(
            recovered.values(),
            key=lambda r: (r.child_table, r.child_columns, r.parent_table),
        )
    )
