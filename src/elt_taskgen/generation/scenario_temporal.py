"""Interval union, ordered inactivity bursts, and dense activity lifecycles."""
from elt_taskgen.generation.mart_plan import quote as q
from elt_taskgen.models import JoinType, MartOpKind as K


def window(b, source, select, stem, partition, order):
    return b.add(K.WINDOW, (source,), stem, select=select,
                 details={"partition_by": partition, "order_by": order})


def interval_ops(b, e, variant):
    g = tuple(f"g_{i}" for i in range(len(e.keys)))
    ids = tuple(f"record_{i}" for i in range(len(e.row_keys)))
    parents = b.project(e.parent, ", ".join([
        *(f'{q(k)} AS {x}' for k, x in zip(e.keys, g)),
        f'CAST({q(e.parent_start)} AS TIMESTAMP) AS window_start',
        f'CAST({q(e.parent_end)} AS TIMESTAMP) AS window_end']), "reporting_windows")
    facts = b.project(e.fact, ", ".join([
        *(f'{q(k)} AS {x}' for k, x in zip(e.links, g)),
        *(f'{q(k)} AS {x}' for k, x in zip(e.row_keys, ids)),
        f'CAST({q(e.start)} AS TIMESTAMP) AS starts', f'CAST({q(e.end)} AS TIMESTAMP) AS ends']), "activity_intervals")
    keys = tuple(k for i, k in enumerate(g) if variant != f"missing_key_{i}")
    joined = b.join(parents, facts, keys, (*ids, "starts", "ends"), "associated_intervals", kind=JoinType.INNER)
    valid = b.filter(joined, "window_end > window_start AND ends > starts AND starts < window_end AND ends > window_start", "valid_intersections")
    clip_start, clip_end = ("starts", "ends") if variant == "no_clip" else ("GREATEST(starts, window_start)", "LEAST(ends, window_end)")
    clipped = b.project(valid, f'*, {clip_start} AS lo, {clip_end} AS hi', "clipped_intervals")
    counts = b.aggregate(clipped, g, {"interval_count": "COUNT(*)"}, "interval_counts")
    partition, order = ", ".join(g), ", ".join(("lo", *ids))
    previous = (f'LAG(hi) OVER (PARTITION BY {partition} ORDER BY {order})' if variant == "previous_end_only" else
                f'MAX(hi) OVER (PARTITION BY {partition} ORDER BY {order} ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)')
    prior = window(b, clipped, f'*, {previous} AS previous_end', "prior_coverage", partition, order)
    marked = window(b, prior, f'*, SUM(CASE WHEN previous_end IS NULL OR lo > previous_end THEN 1 ELSE 0 END) OVER '
                    f'(PARTITION BY {partition} ORDER BY {order} ROWS UNBOUNDED PRECEDING) AS island', "coverage_islands", partition, order)
    merged = b.aggregate(marked, (*g, "island"), {"lo": "MIN(lo)", "hi": "MAX(hi)"}, "merged_intervals")
    elapsed = b.project(clipped if variant == "sum_overlaps" else merged,
                        "*, DATE_DIFF('microsecond', lo, hi) / 1000000.0 AS seconds", "elapsed_coverage")
    totals = b.aggregate(elapsed, g, {"covered_seconds": "SUM(seconds)"}, "coverage_totals")
    current = b.join(parents, totals, g, ("covered_seconds",), "all_window_coverage",
                     kind=JoinType.INNER if variant == "inner_parent" else JoinType.LEFT)
    current = b.join(current, counts, g, ("interval_count",), "all_interval_counts")
    b.project(current, ", ".join([*(f'{x} AS {q(k)}' for k, x in zip(e.keys, g)),
        "COALESCE(interval_count, 0) AS interval_count", "COALESCE(covered_seconds, 0.0) AS covered_seconds"]), "mart_output")


def session_ops(b, e, variant):
    g = tuple(f"g_{i}" for i in range(len(e.keys)))
    ids = tuple(f"record_{i}" for i in range(len(e.row_keys)))
    parents = b.project(e.parent, ", ".join(f'{q(k)} AS {x}' for k, x in zip(e.keys, g)), "entity_keys")
    facts = b.project(e.fact, ", ".join([
        *(f'{q(k)} AS {x}' for k, x in zip(e.links, g)),
        *(f'{q(k)} AS {x}' for k, x in zip(e.row_keys, ids)),
        f'CAST({q(e.start)} AS TIMESTAMP) AS occurred_at']), "dated_events")
    facts = b.filter(facts, "occurred_at IS NOT NULL", "known_event_times")
    keys = tuple(k for i, k in enumerate(g) if variant != f"missing_key_{i}")
    joined = b.join(parents, facts, keys, (*ids, "occurred_at"), "matched_events", kind=JoinType.INNER)
    direction = "DESC" if variant == "reverse_ties" else "ASC"
    order = "occurred_at ASC, " + ", ".join(f'{x} {direction}' for x in ids)
    partition = "1" if variant == "global_partition" else ", ".join(g)
    prior = window(b, joined,
        f'*, LAG(occurred_at) OVER (PARTITION BY {partition} ORDER BY {order}) AS previous_event', "event_predecessors", partition, order)
    gap = ">=" if variant == "inclusive_gap" else ">"
    marked = b.project(prior, f"*, CASE WHEN previous_event IS NULL OR occurred_at - previous_event {gap} INTERVAL '30 minutes' THEN 1 ELSE 0 END AS new_burst", "burst_boundaries")
    numbered = window(b, marked,
        f'*, SUM(new_burst) OVER (PARTITION BY {partition} ORDER BY {order} ROWS UNBOUNDED PRECEDING) AS burst_number', "numbered_bursts", partition, order)
    parts = partition + ", burst_number"
    numbered = window(b, numbered,
        f'*, ROW_NUMBER() OVER (PARTITION BY {parts} ORDER BY {order}) AS position_in_burst, '
        f'COUNT(*) OVER (PARTITION BY {parts} ORDER BY {order} ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS burst_event_count', "burst_positions", parts, order)
    b.project(numbered, ", ".join([*(f'{x} AS {q(k)}' for k, x in zip(e.row_keys, ids)),
        "burst_number", "position_in_burst", "burst_event_count"]), "mart_output")


def lifecycle_ops(b, e, variant):
    g = tuple(f"g_{i}" for i in range(len(e.keys)))
    parents = b.project(e.parent, ", ".join([*(f'{q(k)} AS {x}' for k, x in zip(e.keys, g)),
        f'CAST({q(e.parent_start)} AS TIMESTAMP) AS joined_at']), "dated_entities")
    parents = b.filter(parents, "joined_at IS NOT NULL", "known_creation_dates")
    facts = b.project(e.fact, ", ".join([*(f'{q(k)} AS {x}' for k, x in zip(e.links, g)),
        f'CAST({q(e.start)} AS TIMESTAMP) AS event_at']), "event_dates")
    keys = tuple(k for i, k in enumerate(g) if variant != f"missing_key_{i}")
    joined = b.join(parents, facts, keys, ("event_at",), "entity_activity", kind=JoinType.INNER)
    valid = b.filter(joined, "event_at IS NOT NULL" if variant == "before_creation" else "event_at >= joined_at", "eligible_events")
    month = "CAST(FLOOR(DATE_DIFF('day', joined_at, event_at)/30.0) AS INTEGER)" if variant == "thirty_day_month" else "DATE_DIFF('month', joined_at, event_at)"
    indexed = b.project(valid, f'*, {month} AS month_index', "calendar_activity")
    counts = b.aggregate(indexed, (*g, "month_index"), {"event_count": "COUNT(*)"}, "monthly_events")
    branches = [b.project(parents, f'*, {i} AS month_index', f'month_{i}') for i in range(6)]
    grid = branches[0]
    for branch in branches[1:]:
        grid = b.add(K.UNION, (grid, branch), "entity_calendar", details={"mode": "all"})
    current = b.join(grid, counts, (*g, "month_index"), ("event_count",), "complete_months")
    current = b.project(current, ", ".join((*g, "month_index", "COALESCE(event_count, 0) AS event_count")), "zero_activity_months")
    if variant == "observed_only":
        current = b.filter(current, "event_count > 0", "active_months_only")
    partition = ", ".join(g)
    state = window(b, current,
        f'*, COALESCE(LAG(event_count) OVER (PARTITION BY {partition} ORDER BY month_index), 0) AS previous_count, '
        f'COALESCE(MAX(event_count) OVER (PARTITION BY {partition} ORDER BY month_index ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS ever_active',
        "lifecycle_memory", partition, "month_index")
    first = "previous_count = 0" if variant == "restart_is_new" else "ever_active = 0"
    b.project(state, ", ".join([*(f'{x} AS {q(k)}' for k, x in zip(e.keys, g)), "month_index", "event_count",
        f"CASE WHEN event_count > 0 AND {first} THEN 'new' WHEN event_count > 0 AND previous_count = 0 THEN 'reactivated' "
        "WHEN event_count = 0 AND previous_count > 0 THEN 'stopped' WHEN event_count > previous_count THEN 'increased' "
        "WHEN event_count < previous_count THEN 'decreased' WHEN event_count > 0 THEN 'unchanged' ELSE 'inactive' END AS activity_state"]), "mart_output")
