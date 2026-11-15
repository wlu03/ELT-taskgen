"""Business-rule oracle using Python collections, independent of plan SQL."""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from elt_taskgen.generation.advanced_evidence import ASOF, COHORT


def stamp(value):
    return None if value is None else datetime.fromisoformat(str(value))


def rounded(value):
    return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def oracle(e, recipe, rows):
    key = lambda r, cols: tuple(r[c] for c in cols)
    if recipe == ASOF:
        versions = defaultdict(list)
        for row in rows[e.history]:
            k = key(row, e.history_links)
            if None not in k and row[e.valid_from] is not None:
                versions[k].append(row)
        result = []
        for row in rows[e.fact]:
            eligible = [h for h in versions[key(row, e.links)] if row[e.event_time] is not None
                        and stamp(h[e.valid_from]) <= stamp(row[e.event_time])]
            chosen = max(eligible, key=lambda h: (stamp(h[e.valid_from]), key(h, e.history_keys))) if eligible else None
            result.append({**dict(zip(e.row_keys, key(row, e.row_keys), strict=True)),
                           "historical_value": chosen[e.attribute] if chosen else None,
                           "matched_from": stamp(chosen[e.valid_from]) if chosen else None})
        return result
    if recipe == COHORT:
        members, sizes, active = {}, defaultdict(int), defaultdict(set)
        for row in rows[e.parent]:
            joined = stamp(row[e.parent_time])
            if joined is not None:
                month = joined.date().replace(day=1)
                members[key(row, e.keys)] = (joined, month)
                sizes[month] += 1
        for row in rows[e.fact]:
            member = key(row, e.links)
            if member not in members or row[e.event_time] is None:
                continue
            joined, cohort = members[member]
            event = stamp(row[e.event_time])
            offset = (event.year - joined.year) * 12 + event.month - joined.month
            if event >= joined and 0 <= offset <= 2:
                active[cohort, offset].add(member)
        return [{"cohort_month": cohort, "month_index": offset, "cohort_size": size,
                 "active_members": len(active[cohort, offset]),
                 "retention_fraction": rounded(len(active[cohort, offset]) / size)}
                for cohort, size in sizes.items() for offset in range(3)]
    amounts = {key(row, e.keys): float(row[e.amount] or 0) for row in rows[e.parent]}
    totals = defaultdict(float)
    for row in rows[e.fact]:
        totals[key(row, e.links)] += max(0, float(row[e.weight] or 0))
    result = []
    for row in rows[e.fact]:
        k = key(row, e.links)
        fraction = max(0, float(row[e.weight] or 0)) / totals[k] if totals[k] and None not in k else 0.0
        result.append({**dict(zip(e.row_keys, key(row, e.row_keys), strict=True)),
                       "weight_fraction": rounded(fraction), "allocated_amount": rounded(amounts.get(k, 0) * fraction)})
    return result
