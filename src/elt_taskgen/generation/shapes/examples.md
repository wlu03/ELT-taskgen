# Mart-plan shape examples

A mart-plan shape is a reusable relational query. The generator selects a shape
from schema evidence, and the corresponding builder returns a concrete plan.
The plan specifies the output grain, relational operations, computed columns,
null behavior, and cases that generated test data must cover. An adapter may
rename generic plan columns to match the source schema.



## Quick reference

| Shape | Output grain | Defining operation | Example | Evidence |
|---|---|---|---|---|
| `fan_out_rollup` | One row per parent | Two-hop left joins and grouped measures | `motion_paths_performance_metrics_rollup` | Batch release |
| `aggregate_then_filter` | Selected parent groups | Post-aggregate threshold | Accounts with at least two subscriptions | Executed proof fixture |
| `argmax_profile` | One row per parent | Maximum-measure row with deterministic tie-breaking | `deals_participants_top` | Batch release |
| `latest_snapshot` | One row per parent | Latest timestamp with row-key tie-breaking | `sessions_wifi_stats_snapshot` | Batch release |
| `status_cohort_union` | One row per entity and cohort | Independently aggregated branches stacked with `UNION ALL` | `sensors_motion_data_cohorts` | Batch release |
| `measure_state_distribution` | One row per entity and measure state | Present and absent branches stacked with `UNION ALL` | `deals_participants_distribution` | Batch release |
| `categorical_ladder` | One row per parent | Declared-domain `CASE` mappings and bands | `sensors_motion_data_bands` | Batch release |
| `temporal_grid` | One row per entity and period | Period aggregation followed by windows | Monthly account activity | Executed proof fixture |
| `orphan_coverage` | One row per owner-reference group | Activity-side grain with a left owner lookup | Player-role coverage | Executed proof fixture (WikiDBs adapter) |

## `fan_out_rollup`

Builder: [`fan_out_rollup.py`](fan_out_rollup.py)

This shape aggregates a two-hop relationship:

```text
parent <- bridge -> child
```

The query starts from the parent table and uses a left join for each
relationship. It reports the number of bridge rows and the number of distinct
child rows that match those bridge rows. It also computes filtered counts and
sums, totals with defined null handling, a ratio with zero-denominator handling,
and categorical bands. A parent with no bridge rows remains in the output with
the declared zero values.

### Pipeline example

The generated mart `motion_paths_performance_metrics_rollup` appears in the
[3D path or motion tracking data model][motion-path-model]. It uses these
relations:

```text
motion_paths <- performance_metrics -> robots
```

The mart has one row per `motion_paths.path_id`. It counts all linked
`performance_metrics`, counts distinct `robots`, counts rows whose
`metric_name` is `cycle time`, sums all `metric_value` values, sums only the
cycle-time values, and calculates their share of the total. It also assigns a
size band and a `has_links` flag.

Two metric rows can refer to the same robot, so `link_count` and
`distinct_child_count` can differ. An inner join would exclude a motion path
with no metrics, while this shape requires a row with zero values.

## `aggregate_then_filter`

Builder: [`aggregate_then_filter.py`](aggregate_then_filter.py)

This shape first computes a complete `fan_out_rollup` and then retains groups
where `link_count >= 2`. The condition applies after aggregation. A raw-row
`WHERE` condition is not equivalent because it changes the rows used by the
aggregates rather than filtering the aggregate results.

### Executed proof example

No batch release contains this shape because its registry entry is not
auto-selected. The [plan-library proof fixture][plan-library-proof] executes it
over:

```text
accounts <- subscriptions -> plans
```

The counterfactual population produces this output row:

| Account | Subscription count | Distinct plans | Paid count | Total amount | Paid amount | Paid share |
|---:|---:|---:|---:|---:|---:|---:|
| `900` | 2 | 2 | 1 | 50 | 10 | 0.2 |

The childless account and the account with one subscription do not appear.
Account `900` has exactly two subscriptions, so the fixture checks that the
threshold is inclusive and is applied after grouping.

## `argmax_profile`

Builder: [`argmax_profile.py`](argmax_profile.py)

`argmax_profile` selects the row with the greatest measure and returns
attributes from that row. Calculating `MAX(measure)` alone is insufficient
because the selected label and, when a unique row key is available, the
selected row identifier must come from the same source row.

The ordering is deterministic. The measure sorts descending with nulls last.
The label then sorts ascending with nulls last. If a unique row key is
available, it provides the final ascending tie-break. The mart also reports the
number of rows tied at the maximum, the total measure, the selected measure's
share of the total, and whether the maximum is empty, unique, or tied.

### Pipeline example

The generated `deals_participants_top` mart appears in the
[dlt Pipedrive data model][pipedrive-model]. It groups `deals_participants` by
deal and selects the row with the largest `participant_share`. A tie is resolved
by the smallest `participant_role`, then the smallest participant row `id`.
The output includes the selected role and row id, the top share, the tie count,
the total share, the ratio, and the tie state.

Computing `MAX(participant_share)` and `MAX(participant_role)` independently
can return values from different source rows. That query does not satisfy the
same-row requirement.

## `latest_snapshot`

Builder: [`latest_snapshot.py`](latest_snapshot.py)

This shape selects one activity row per parent. It sorts rows by timestamp
descending and uses a unique row key as the final ascending tie-break. Every
snapshot column comes from the selected row. It also reports the row count and
amount across all activity rows, plus the selected amount's share of the total
amount.

Rows with null timestamps sort after rows with non-null timestamps. A parent
with no activity remains in the output with the declared defaults. The row-key
tie-break is required because timestamps can be equal.

### Pipeline example

The generated `sessions_wifi_stats_snapshot` mart appears in the
[SchemaPile hostview data model][hostview-model]. It joins `sessions` to
`wifi_stats`, orders by `wifi_stats.logged_at DESC NULLS LAST`, and breaks ties
with the smallest `wifi_stats.id`. The output sets `latest_row_id` from
`wifi_stats.id`, `latest_amount` from `r_speed`, and `latest_label` from
`guid`. The mart separately reports total `r_speed` and the latest value's
share of that total.

Selecting the latest `guid` and latest `r_speed` in separate subqueries can
return values from different source rows.

## `status_cohort_union`

Builder: [`status_cohort_union.py`](status_cohort_union.py)

This shape computes each status cohort in a separate branch. Each branch
filters and aggregates its input independently, then assigns a cohort label.
The query combines the branches with `UNION ALL`. A separate branch emits
entities with no linked activity.

A row with a null status satisfies neither the passing nor the failing
predicate. Its presence means that the entity has activity, so the entity also
does not qualify for `no_activity`. That cohort contains only entities with no
linked rows.

### Pipeline example

The generated `sensors_motion_data_cohorts` mart appears in the
[3D motion tracking data model][sensor-model]. Its source chain is:

```text
sensors <- motion_data
```

The passing branch contains rows where `data_quality = 'good'`. The failing
branch contains rows where data quality is `bad` or `uncertain`. The
`no_activity` branch contains sensors with no motion rows. Each output row
contains the cohort's row count, distinct status count, total and maximum
`x_position`, and the maximum's share of the total.

The output grain is `(sensor_id, cohort)`. A sensor with both passing and
failing rows therefore produces two output rows. The shape does not combine
the cohorts into separate columns on one row.

## `measure_state_distribution`

Builder: [`measure_state_distribution.py`](measure_state_distribution.py)

This shape produces separate summaries for present and absent measures. Real
activity rows with non-null measures are in the `present` branch. Real
activity rows with null measures are in the `absent` branch. The placeholder
row for an entity with no activity is also in the `absent` branch.

The absent state's `row_count` distinguishes the two cases:

- A real row with a null measure contributes a positive row count.
- An entity with no activity reports a row count of zero.

The shape combines the two summaries with `UNION ALL`.

### Pipeline example

The generated `deals_participants_distribution` mart appears in the
[dlt Pipedrive data model][pipedrive-model]. It classifies
`deals_participants.participant_share` as present or absent for each deal. For
each state it reports the participant-row count, distinct non-missing share
count, total share, maximum share, and maximum-to-total ratio.

The output distinguishes a deal that has participant rows with null shares
from a deal that has no participant rows.

## `categorical_ladder`

Builder: [`categorical_ladder.py`](categorical_ladder.py)

This shape uses categorical values declared in the source schema. An ordered
`CASE` expression maps named values, and an explicit fallback handles declared
values that have no named mapping. The shape also calculates status counts, a
passing ratio, and a second `CASE` expression that assigns a band from the
rounded ratio.

Generated data must include a declared domain value that is not handled by an
earlier `WHEN` clause. Otherwise, a test cannot detect removal of the `ELSE`
branch.

### Pipeline example

The generated `sensors_motion_data_bands` mart appears in the
[3D motion tracking data model][sensor-model]. The `sensors.status` domain is
declared as `active`, `inactive`, and `maintenance`. The generated mapping is:

| Source value | `status_group` |
|---|---|
| `active` | `active` |
| `inactive` | `other_1` |
| `maintenance` | `unmapped` |

The same mart counts `motion_data` rows by data quality, computes the fraction
whose quality is `good`, and assigns `no_activity`, `low`, `medium`, or `high`.
At the `0.5` and `0.8` boundaries, the value is assigned to the higher band.
The band uses the rounded ratio reported by the mart.

## `temporal_grid`

Builder: [`temporal_grid.py`](temporal_grid.py)

This shape produces one row per entity and calendar month. It aggregates each
month before applying window functions. The window calculations produce a
running amount and the amount from the previous output period. The shape uses
those values to calculate a period share and assign an `up`, `flat`, or
`down` trend.

The builder raises `ShapeNotSelectable` when the activity-side owner reference
or timestamp is nullable because both values define the output grain. The
previous-period value refers to the preceding period present in the output. The
shape does not generate rows for missing calendar months.

### Executed proof example

No available batch release selected this shape. The
[plan-library proof fixture][plan-library-proof] executes it over timestamped
`subscriptions` for `accounts`. For account `904`, the generated rows are:

| Period | Period amount | Running amount | Previous period amount | Share | Trend |
|---|---:|---:|---:|---:|---|
| 2024-01 | 40 | 40 | 0 | 1.0 | `up` |
| 2024-03 | 10 | 50 | 40 | 0.2 | `down` |
| 2024-05 | 10 | 60 | 10 | 0.1667 | `flat` |

For the March row, the previous output period is January. The shape does not
generate a zero-valued February row.

## `orphan_coverage`

Builder: [`orphan_coverage.py`](orphan_coverage.py)

`orphan_coverage` starts from the activity table and left-joins the owner
table. Activity rows therefore contribute to the aggregates even when their
owner reference is null or does not match an owner row.

The shape groups rows by the original owner reference. Each non-null reference
has a separate group, including references that do not match an owner. All null
references use the literal `__orphan__` group. The owner name is included when
a matching owner exists. The output reports matched and orphan counts, amounts,
a match rate, and a `complete`, `partial`, or `orphaned` category.

### Executed proof example

No batch release contains this shape because its registry entry is not
auto-selected. The [WikiDBs adapter proof][wikidbs-proof] generates an adapter
task from a synthetic WikiDBs fixture:

```text
zroles <- players
```

Player `Cal` has a null `role_name` and score 30. After adapter conversion,
population generation, reference compilation, and DuckDB execution, the mart
contains:

| Owner bucket | Owner label | Player count | Matched | Orphaned | Total score | Match rate | Band |
|---|---|---:|---:|---:|---:|---:|---|
| `__orphan__` | `(unmatched)` | 1 | 0 | 1 | 30 | 0.0 | `orphaned` |

An inner join would omit this row. The fixture verifies that the activity-side
left join retains it.

[batch50]: ../../../../corpus/batch50_20260919/README.md
[motion-path-model]: ../../../../corpus/batch50_20260919/synsql__3d_path_or_motion_tracking__motion_paths_performance_metrics_rollup/data_model.yaml
[pipedrive-model]: ../../../../corpus/batch50_20260919/dlt__pipedrive/data_model.yaml
[hostview-model]: ../../../../corpus/batch50_20260919/schemapile__github_com_inria_muse_hostview_processing_4f4676010329__162575_init_hostview_sql/data_model.yaml
[sensor-model]: ../../../../corpus/batch50_20260919/synsql__3d_motion_tracking_and_analysis__sensors_motion_data_cohorts/data_model.yaml
[plan-library-proof]: ../../../../tests/test_plan_library.py
[wikidbs-proof]: ../../../../tests/test_adapters_wikidbs.py
