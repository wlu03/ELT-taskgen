# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Concourse Concourse

## Specification

PROJECT OVERVIEW

This project builds three analytical marts from the Concourse CI operational schema (the 402976_1510262030_initial_schema.up.sql schema). Each source table must be extracted from the backend named here, and only from that backend.

Source tables and their extraction backends:
- Source table base_resource_types is extracted from the files backend.
- Source table build_events is extracted from the s3 backend.
- Source table build_image_resource_caches is extracted from the postgres backend.
- Source table build_inputs is extracted from the rest backend.
- Source table build_outputs is extracted from the rest backend.
- Source table builds is extracted from the s3 backend.
- Source table cache_invalidator is extracted from the rest backend.
- Source table containers is extracted from the files backend.
- Source table independent_build_inputs is extracted from the postgres backend.
- Source table jobs is extracted from the mongodb backend.
- Source table jobs_serial_groups is extracted from the postgres backend.
- Source table next_build_inputs is extracted from the rest backend.
- Source table pipelines is extracted from the rest backend.
- Source table pipes is extracted from the postgres backend.
- Source table resource_cache_uses is extracted from the s3 backend.
- Source table resource_caches is extracted from the mongodb backend.
- Source table resource_config_check_sessions is extracted from the postgres backend.
- Source table resource_configs is extracted from the s3 backend.
- Source table resource_types is extracted from the rest backend.
- Source table resources is extracted from the mongodb backend.
- Source table teams is extracted from the files backend.
- Source table versioned_resources is extracted from the s3 backend.
- Source table volumes is extracted from the s3 backend.
- Source table worker_base_resource_types is extracted from the mongodb backend.
- Source table worker_resource_caches is extracted from the s3 backend.
- Source table worker_resource_config_check_sessions is extracted from the rest backend.
- Source table worker_task_caches is extracted from the postgres backend.
- Source table workers is extracted from the files backend.

RELATIONSHIPS BETWEEN SOURCE TABLES

Each line states a child table with its key column(s), the parent table with its key column(s), and whether the relationship is required or optional. An optional relationship may hold no value or may point at a parent row that does not exist.

- Child table build_image_resource_caches (build_id) refers to parent table builds (id); this relationship is required.
- Child table build_image_resource_caches (resource_cache_id) refers to parent table resource_caches (id); this relationship is optional.
- Child table build_inputs (build_id) refers to parent table builds (id); this relationship is optional.
- Child table build_inputs (versioned_resource_id) refers to parent table versioned_resources (id); this relationship is optional.
- Child table build_outputs (build_id) refers to parent table builds (id); this relationship is optional.
- Child table build_outputs (versioned_resource_id) refers to parent table versioned_resources (id); this relationship is optional.
- Child table builds (job_id) refers to parent table jobs (id); this relationship is optional.
- Child table builds (pipeline_id) refers to parent table pipelines (id); this relationship is optional.
- Child table builds (team_id) refers to parent table teams (id); this relationship is required.
- Child table containers (build_id) refers to parent table builds (id); this relationship is optional.
- Child table containers (team_id) refers to parent table teams (id); this relationship is optional.
- Child table containers (worker_name) refers to parent table workers (name); this relationship is optional.
- Child table containers (worker_resource_config_check_session_id) refers to parent table worker_resource_config_check_sessions (id); this relationship is optional.
- Child table independent_build_inputs (job_id) refers to parent table jobs (id); this relationship is required.
- Child table independent_build_inputs (version_id) refers to parent table versioned_resources (id); this relationship is required.
- Child table jobs (pipeline_id) refers to parent table pipelines (id); this relationship is required.
- Child table jobs_serial_groups (job_id) refers to parent table jobs (id); this relationship is optional.
- Child table next_build_inputs (job_id) refers to parent table jobs (id); this relationship is required.
- Child table next_build_inputs (version_id) refers to parent table versioned_resources (id); this relationship is required.
- Child table pipelines (team_id) refers to parent table teams (id); this relationship is required.
- Child table pipes (team_id) refers to parent table teams (id); this relationship is required.
- Child table resource_cache_uses (build_id) refers to parent table builds (id); this relationship is optional.
- Child table resource_cache_uses (container_id) refers to parent table containers (id); this relationship is optional.
- Child table resource_cache_uses (resource_cache_id) refers to parent table resource_caches (id); this relationship is optional.
- Child table resource_caches (resource_config_id) refers to parent table resource_configs (id); this relationship is optional.
- Child table resource_config_check_sessions (resource_config_id) refers to parent table resource_configs (id); this relationship is optional.
- Child table resource_configs (base_resource_type_id) refers to parent table base_resource_types (id); this relationship is optional.
- Child table resource_configs (resource_cache_id) refers to parent table resource_caches (id); this relationship is optional.
- Child table resource_types (pipeline_id) refers to parent table pipelines (id); this relationship is optional.
- Child table resource_types (resource_config_id) refers to parent table resource_configs (id); this relationship is optional.
- Child table resources (pipeline_id) refers to parent table pipelines (id); this relationship is required.
- Child table resources (resource_config_id) refers to parent table resource_configs (id); this relationship is optional.
- Child table versioned_resources (resource_id) refers to parent table resources (id); this relationship is optional.
- Child table volumes (container_id) refers to parent table containers (id); this relationship is optional.
- Child table volumes (team_id) refers to parent table teams (id); this relationship is optional.
- Child table volumes (worker_base_resource_type_id) refers to parent table worker_base_resource_types (id); this relationship is optional.
- Child table volumes (worker_name) refers to parent table workers (name); this relationship is required.
- Child table volumes (worker_resource_cache_id) refers to parent table worker_resource_caches (id); this relationship is optional.
- Child table volumes (worker_task_cache_id) refers to parent table worker_task_caches (id); this relationship is optional.
- Child table worker_base_resource_types (base_resource_type_id) refers to parent table base_resource_types (id); this relationship is optional.
- Child table worker_base_resource_types (worker_name) refers to parent table workers (name); this relationship is optional.
- Child table worker_resource_caches (resource_cache_id) refers to parent table resource_caches (id); this relationship is optional.
- Child table worker_resource_caches (worker_base_resource_type_id) refers to parent table worker_base_resource_types (id); this relationship is optional.
- Child table worker_resource_config_check_sessions (resource_config_check_session_id) refers to parent table resource_config_check_sessions (id); this relationship is optional.
- Child table worker_resource_config_check_sessions (team_id) refers to parent table teams (id); this relationship is optional.
- Child table worker_resource_config_check_sessions (worker_base_resource_type_id) refers to parent table worker_base_resource_types (id); this relationship is optional.
- Child table worker_task_caches (job_id) refers to parent table jobs (id); this relationship is optional.
- Child table worker_task_caches (worker_name) refers to parent table workers (name); this relationship is optional.
- Child table workers (team_id) refers to parent table teams (id); this relationship is optional.

General conventions: every numeric measure described below as defaulting to 0 is reported as 0 and never as a missing value; every fraction is rounded to 4 decimal places. Membership of each mart is stated only by that mart's own grain sentence.

=== Mart workers_containers_snapshot — Per-workers latest-row snapshot over linked containers activity in the 402976_1510262030_initial_schema.up.sql schema ===

Grain: one row per workers (name), INCLUDING workers rows with no linked containers rows.

Key column: parent_key.

Rule 1: source table workers is read in full as one of the two inputs of this mart.

Rule 2: source table containers is read in full as the other input of this mart.

Rule 3: source table workers contributes one row per workers row, keyed by name, and that row carries parent_key and parent_name.

Rule 4: the containers rows are brought in, matching containers on its worker_name to the parent_key value of the workers row, carrying worker_name and name; preservation is left-sided toward workers, so a workers row with no matching containers row is retained and receives the stated empty snapshot values.

Rule 5: there is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group, and each such row reports event_count and lifetime_amount over that row's matching rows.

Rule 6: for each parent_key the single row at which the ordering measure best_if_used_by is largest survives, ties broken by the smallest id, and latest_row_id, latest_amount and latest_label are taken from that winning row; EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 7: the extremal row's attributes are attached to the grouped measures by matching on parent_key; preservation is left-sided toward the grouped measures, so a group with no rows at all keeps its measures.

Rule 8: the mart columns are named parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label; lifetime_amount reports its declared default of 0 — never NULL — for a group with no matching rows, and for lifetime_amount the default also applies to a group none of whose real rows carries an input value.

Rule 9: the guarded ratio latest_amount_share, reported beside parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label, is latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places, and is 0.0 when lifetime_amount is 0 or has no value; the division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose image_check_container_id is missing, so such a row gives 0.0.

Rule 10: the deterministic output order is ascending parent_key.

Output columns:
- parent_key (text): identifier of the workers row; one output row per value.
- parent_name (text): addr of the workers row, copied unchanged.
- event_count (bigint): number of containers rows for this workers row; 0 when there are none. Every linked containers row counts, whether or not it carries an image_check_container_id value. A workers row kept with no containers row reports 0 here, never 1: its placeholder holds no containers row to count.
- lifetime_amount (integer): the total of image_check_container_id over all matching containers rows; 0 when there are no rows and when none of those rows carries an image_check_container_id value; a row with no image_check_container_id value adds nothing, so a group with some values totals the values it has.
- latest_row_id (integer): id of the row with the latest best_if_used_by; ties take the smallest id. It is 0 when there are no rows. Every containers row of the workers row ranks, whether or not it carries a image_check_container_id value: the latest best_if_used_by wins even when that row's image_check_container_id is missing.
- latest_amount (integer): image_check_container_id from that same latest row; 0 when there are no rows or when the winning value is missing.
- latest_label (text): handle from that same latest row; '(none)' when there are no rows.
- latest_amount_share (float): latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose image_check_container_id is missing, so such a row gives 0.0.

=== Mart teams_workers_distribution — Per-(teams, measure state) distribution of linked workers activity in the 402976_1510262030_initial_schema.up.sql schema ===

Grain: one row per (id, measure state) pair represented among linked workers rows; the absent state includes missing active_containers values and a no-activity row for a teams row with no links. A linked workers row whose active_containers has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state.

Rule 1: source table teams is read in full as one of the two inputs of this mart.

Rule 2: source table workers is read in full as the other input of this mart.

Rule 3: each teams id and its auth are carried into the measure-state calculation as entity_key and entity_name.

Rule 4: the linked workers rows are brought into each teams entity, matching workers on its team_id to the entity_key value, carrying entity_key, entity_name, id and team_id; preservation is left-sided toward teams, so an entity with no linked row is retained and its absent state is visible.

Rule 5: the present measure-state rows are the ones kept for the present summary: a real workers row whose active_containers has a value, carrying entity_key and entity_name.

Rule 6: there is one present-state row per teams entity that has at least one row in the present measure state, and no row here for an entity with none, reporting entity_key, entity_name, row count as row_count, how many different non-missing active_containers values occur as distinct_amount_count (each different value counted once, however many rows repeat it), total active_containers as total_amount, and largest active_containers as max_amount.

Rule 7: for the present-state summary, reported beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8: these measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state 'present', the present measure state.

Rule 9: the absent measure-state rows are the ones kept for the absent summary: active_containers is missing, including the retained placeholder for a teams row with no workers rows, carrying entity_key and entity_name; a real workers row whose active_containers has a value belongs only to the present state and never to this absent state.

Rule 10: there is one absent-state row per teams entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting entity_key, entity_name, row count as row_count, how many different non-missing active_containers values occur as distinct_amount_count (each different value counted once, however many rows repeat it), total active_containers as total_amount, and largest active_containers as max_amount.

Rule 11: for the absent-state summary, reported beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12: these measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state 'absent', the absent measure state.

Rule 13: the present-state summary and the absent-state summary are stacked into one output list of entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, keeping all rows from both, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14: the deterministic output order is ascending entity_key, then ascending measure_state.

Output columns:
- entity_key (integer): identifier of the teams row.
- measure_state (text): 'present' for a linked workers row whose active_containers has a value; 'absent' when active_containers is missing, including a teams row with no linked workers row. A linked workers row whose active_containers has a value belongs only to the present state and never to the absent state.
- entity_name (text): auth of the teams row, copied unchanged.
- row_count (bigint): number of linked workers rows in this entity/state cell; a absent cell holding real workers rows whose active_containers is missing COUNTS those rows, and only the placeholder cell of a teams row with no linked workers row at all reports 0.
- distinct_amount_count (bigint): number of unique non-missing active_containers values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no active_containers value at all — both for a teams row with no linked workers row and for an absent cell whose rows all have a missing active_containers.
- total_amount (integer): the total of active_containers in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an active_containers value.
- max_amount (integer): largest active_containers in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an active_containers value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

=== Mart resources_versioned_resources_top — Per-resources extremes over linked versioned_resources rows in the 402976_1510262030_initial_schema.up.sql schema: WHICH row is largest, not how large it is ===

Grain: one row per resources (id), INCLUDING resources rows with no linked versioned_resources rows.

Key column: parent_key.

Rule 1: source table resources is read in full as one of the two inputs of this mart.

Rule 2: source table versioned_resources is read in full as the other input of this mart.

Rule 3: source table resources contributes one row per resources row, keyed by id, and that row carries parent_key and parent_name.

Rule 4: the versioned_resources rows are brought in, matching versioned_resources on its resource_id to the parent_key value, carrying resource_id and id; preservation is left-sided toward resources, so a resources row with no versioned_resources rows still appears, with the declared defaults.

Rule 5: within each parent_key group the matched rows are ranked under an explicit total order — the measure check_order first, then the declared tie-break — so the extremal row is a function of the input and not of row order.

Rule 6: there is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group, and each such row reports top_measure, tied_count, child_count and total_measure over that row's matching rows; a group with no qualifying rows still appears, reporting 0, and a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7: for each parent_key the single row at which the ordering measure check_order is largest survives, ties broken by the smallest metadata under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then the smallest id, and top_label and top_row_id are taken from that winning row; the ordering measure is required on every real input row, so a non-empty group always has a winning row, and the declared defaults belong only to a group with NO rows.

Rule 8: the extremal row's attributes are attached to the grouped measures by matching on parent_key; preservation is left-sided toward the grouped measures, so a group with no rows at all keeps its measures.

Rule 9: the mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults of 0 — never NULL — for a group with no matching rows.

Rule 10: the guarded ratio top_measure_share, reported beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id, is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and is 0.0 when total_measure is 0 or has no value.

Rule 11: tie_state, reported beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, is 'empty' when no row holds a maximum at all — the parent has no versioned_resources rows — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, otherwise 'tied' when it is 2 or more, and it is never null or blank.

Rule 12: the deterministic output order is ascending parent_key.

Output columns:
- parent_key (integer): identifier of the resources row; one output row per value.
- parent_name (text): check_error of the resources row, copied unchanged.
- top_measure (integer): the largest check_order itself; 0 when the parent has no versioned_resources rows.
- tied_count (bigint): how many versioned_resources rows are tied at that largest check_order; 1 when exactly one row carries that largest check_order; 0 when there are no rows.
- child_count (bigint): number of versioned_resources rows for this resources row; 0 when there are none. A resources row kept with no versioned_resources row reports 0 here, never 1: its placeholder holds no versioned_resources row to count.
- total_measure (integer): the total of check_order over all of them; 0 when the parent has no versioned_resources rows.
- top_label (text): the metadata of the versioned_resources row with the LARGEST check_order for this resources row. Ties in check_order are broken by taking the SMALLEST metadata under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one); rows tied on both are resolved by the smallest id. The literal '(none)' when the parent has no versioned_resources rows at all.
- top_row_id (integer): the id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real versioned_resources row whenever the parent has any. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no versioned_resources rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `workers_containers_snapshot`

- Grain: One row per workers (name), INCLUDING workers rows with no linked containers rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share

```text
Mart 'workers_containers_snapshot' has 10 declared semantic rules:
1. [source] Read source table workers. (public source tables: workers)
2. [source] Read source table containers. (public source tables: containers)
3. [derive] One row per workers row, keyed by name. (public source tables: workers | public carried/output columns: parent_key, parent_name)
4. [join] Bring in containers; a workers row with no matching containers row is retained and receives the stated empty snapshot values. (public source tables: containers | public carried/output columns: worker_name, name | join preservation: left | condition public identifiers: containers, worker_name, parent_key)
5. [aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count, lifetime_amount for that row's matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount)
6. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest id, and take latest_row_id, latest_amount, latest_label from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, latest_row_id, latest_amount, latest_label)
7. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
8. [derive] Name the mart columns; lifetime_amount reports its declared default — never NULL — for a group with no matching rows. For lifetime_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label)
9. [ratio] Guarded ratios: latest_amount_share — latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose image_check_container_id is missing, so such a row gives 0.0. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `teams_workers_distribution`

- Grain: One row per (id, measure state) pair represented among linked workers rows; the absent state includes missing active_containers values and a no-activity row for a teams row with no links. A linked workers row whose active_containers has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'teams_workers_distribution' has 14 declared semantic rules:
1. [source] Read source table teams. (public source tables: teams)
2. [source] Read source table workers. (public source tables: workers)
3. [derive] Carry each id and its auth into the measure-state calculation. (public source tables: teams | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked workers rows into each teams entity; retain an entity with no linked row so its absent state is visible. (public source tables: workers | public carried/output columns: entity_key, entity_name, id, team_id | join preservation: left | condition public identifiers: workers, team_id, entity_key)
5. [filter] Keep the present measure-state rows: a real workers row whose active_containers has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per teams entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing active_containers values occur (each different value counted once, however many rows repeat it), total active_containers, and largest active_containers. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: active_containers is missing, including the retained placeholder for a teams row with no workers rows. A real workers row whose active_containers has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per teams entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing active_containers values occur (each different value counted once, however many rows repeat it), total active_containers, and largest active_containers. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `resources_versioned_resources_top`

- Grain: One row per resources (id), INCLUDING resources rows with no linked versioned_resources rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'resources_versioned_resources_top' has 12 declared semantic rules:
1. [source] Read source table resources. (public source tables: resources)
2. [source] Read source table versioned_resources. (public source tables: versioned_resources)
3. [derive] One row per resources row, keyed by id. (public source tables: resources | public carried/output columns: parent_key, parent_name)
4. [join] Bring in versioned_resources: a resources row with no versioned_resources rows still appears, with the declared defaults. (public source tables: versioned_resources | public carried/output columns: resource_id, id | join preservation: left | condition public identifiers: versioned_resources, resource_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest metadata under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then the smallest id, and take top_label, top_row_id from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no versioned_resources rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### base_resource_types  (source backend: files)
Source table base_resource_types.

- `id`: integer NOT NULL — Column id of table base_resource_types.
- `name`: text NOT NULL — Column name of table base_resource_types.
- primary key: id

### build_events  (source backend: s3)
Source table build_events.

- `build_id`: integer NULL — Column build_id of table build_events.
- `event_id`: integer NOT NULL — Column event_id of table build_events.
- `payload`: text NOT NULL — Column payload of table build_events.
- `type`: text NOT NULL — Column type of table build_events.
- `version`: text NOT NULL — Column version of table build_events.

### build_image_resource_caches  (source backend: postgres)
Source table build_image_resource_caches.

- `build_id`: integer NOT NULL — Column build_id of table build_image_resource_caches.
- `resource_cache_id`: integer NULL — Column resource_cache_id of table build_image_resource_caches.

### build_inputs  (source backend: rest)
Source table build_inputs.

- `build_id`: integer NULL — Column build_id of table build_inputs.
- `modified_time`: timestamp NOT NULL — Column modified_time of table build_inputs.
- `name`: text NOT NULL — Column name of table build_inputs.
- `versioned_resource_id`: integer NULL — Column versioned_resource_id of table build_inputs.

### build_outputs  (source backend: rest)
Source table build_outputs.

- `build_id`: integer NULL — Column build_id of table build_outputs.
- `explicit`: boolean NOT NULL — Column explicit of table build_outputs.
- `modified_time`: timestamp NOT NULL — Column modified_time of table build_outputs.
- `versioned_resource_id`: integer NULL — Column versioned_resource_id of table build_outputs.

### builds  (source backend: s3)
Source table builds.

- `completed`: boolean NOT NULL — Column completed of table builds.
- `end_time`: timestamp NULL — Column end_time of table builds.
- `engine`: text NULL — Column engine of table builds.
- `engine_metadata`: text NULL — Column engine_metadata of table builds.
- `id`: integer NOT NULL — Column id of table builds.
- `interceptible`: boolean NULL — Column interceptible of table builds.
- `job_id`: integer NULL — Column job_id of table builds.
- `manually_triggered`: boolean NULL — Column manually_triggered of table builds.
- `name`: text NOT NULL — Column name of table builds.
- `nonce`: text NULL — Column nonce of table builds.
- `pipeline_id`: integer NULL — Column pipeline_id of table builds.
- `public_plan`: json NULL — Column public_plan of table builds.
- `reap_time`: timestamp NULL — Column reap_time of table builds.
- `scheduled`: boolean NOT NULL — Column scheduled of table builds.
- `start_time`: timestamp NULL — Column start_time of table builds.
- `status`: text NOT NULL — Column status of table builds.
- `team_id`: integer NOT NULL — Column team_id of table builds.
- primary key: id

### cache_invalidator  (source backend: rest)
Source table cache_invalidator.

- `last_invalidated`: timestamp NOT NULL — Column last_invalidated of table cache_invalidator.

### containers  (source backend: files)
Source table containers.

- `best_if_used_by`: timestamp NULL — Column best_if_used_by of table containers.
- `build_id`: integer NULL — Column build_id of table containers.
- `discontinued`: boolean NOT NULL — Column discontinued of table containers.
- `handle`: text NOT NULL — Column handle of table containers.
- `hijacked`: boolean NOT NULL — Column hijacked of table containers.
- `id`: integer NOT NULL — Column id of table containers.
- `image_check_container_id`: integer NULL — Column image_check_container_id of table containers.
- `image_get_container_id`: integer NULL — Column image_get_container_id of table containers.
- `meta_attempt`: text NOT NULL — Column meta_attempt of table containers.
- `meta_build_id`: integer NOT NULL — Column meta_build_id of table containers.
- `meta_build_name`: text NOT NULL — Column meta_build_name of table containers.
- `meta_job_id`: integer NOT NULL — Column meta_job_id of table containers.
- `meta_job_name`: text NOT NULL — Column meta_job_name of table containers.
- `meta_pipeline_id`: integer NOT NULL — Column meta_pipeline_id of table containers.
- `meta_pipeline_name`: text NOT NULL — Column meta_pipeline_name of table containers.
- `meta_process_user`: text NOT NULL — Column meta_process_user of table containers.
- `meta_step_name`: text NOT NULL — Column meta_step_name of table containers.
- `meta_type`: text NOT NULL — Column meta_type of table containers.
- `meta_working_directory`: text NOT NULL — Column meta_working_directory of table containers.
- `pipeline_id`: integer NULL — Column pipeline_id of table containers.
- `plan_id`: text NULL — Column plan_id of table containers.
- `resource_id`: integer NULL — Column resource_id of table containers.
- `state`: text NOT NULL — Column state of table containers.
- `team_id`: integer NULL — Column team_id of table containers.
- `worker_name`: text NULL — Column worker_name of table containers.
- `worker_resource_config_check_session_id`: integer NULL — Column worker_resource_config_check_session_id of table containers.
- primary key: id

### independent_build_inputs  (source backend: postgres)
Source table independent_build_inputs.

- `first_occurrence`: boolean NOT NULL — Column first_occurrence of table independent_build_inputs.
- `id`: integer NOT NULL — Column id of table independent_build_inputs.
- `input_name`: text NOT NULL — Column input_name of table independent_build_inputs.
- `job_id`: integer NOT NULL — Column job_id of table independent_build_inputs.
- `version_id`: integer NOT NULL — Column version_id of table independent_build_inputs.
- primary key: id

### jobs  (source backend: mongodb)
Source table jobs.

- `active`: boolean NOT NULL — Column active of table jobs.
- `build_number_seq`: integer NOT NULL — Column build_number_seq of table jobs.
- `config`: text NOT NULL — Column config of table jobs.
- `first_logged_build_id`: integer NOT NULL — Column first_logged_build_id of table jobs.
- `id`: integer NOT NULL — Column id of table jobs.
- `inputs_determined`: boolean NOT NULL — Column inputs_determined of table jobs.
- `interruptible`: boolean NOT NULL — Column interruptible of table jobs.
- `max_in_flight_reached`: boolean NOT NULL — Column max_in_flight_reached of table jobs.
- `name`: text NOT NULL — Column name of table jobs.
- `nonce`: text NULL — Column nonce of table jobs.
- `paused`: boolean NULL — Column paused of table jobs.
- `pipeline_id`: integer NOT NULL — Column pipeline_id of table jobs.
- primary key: id

### jobs_serial_groups  (source backend: postgres)
Source table jobs_serial_groups.

- `id`: integer NOT NULL — Column id of table jobs_serial_groups.
- `job_id`: integer NULL — Column job_id of table jobs_serial_groups.
- `serial_group`: text NOT NULL — Column serial_group of table jobs_serial_groups.
- primary key: id

### next_build_inputs  (source backend: rest)
Source table next_build_inputs.

- `first_occurrence`: boolean NOT NULL — Column first_occurrence of table next_build_inputs.
- `id`: integer NOT NULL — Column id of table next_build_inputs.
- `input_name`: text NOT NULL — Column input_name of table next_build_inputs.
- `job_id`: integer NOT NULL — Column job_id of table next_build_inputs.
- `version_id`: integer NOT NULL — Column version_id of table next_build_inputs.
- primary key: id

### pipelines  (source backend: rest)
Source table pipelines.

- `groups`: json NULL — Column groups of table pipelines.
- `id`: integer NOT NULL — Column id of table pipelines.
- `last_scheduled`: timestamp NOT NULL — Column last_scheduled of table pipelines.
- `name`: text NOT NULL — Column name of table pipelines.
- `ordering`: integer NOT NULL — Column ordering of table pipelines.
- `paused`: boolean NULL — Column paused of table pipelines.
- `public`: boolean NOT NULL — Column public of table pipelines.
- `team_id`: integer NOT NULL — Column team_id of table pipelines.
- `version`: integer NOT NULL — Column version of table pipelines.
- primary key: id

### pipes  (source backend: postgres)
Source table pipes.

- `id`: text NOT NULL — Column id of table pipes.
- `team_id`: integer NOT NULL — Column team_id of table pipes.
- `url`: text NULL — Column url of table pipes.
- primary key: id

### resource_cache_uses  (source backend: s3)
Source table resource_cache_uses.

- `build_id`: integer NULL — Column build_id of table resource_cache_uses.
- `container_id`: integer NULL — Column container_id of table resource_cache_uses.
- `resource_cache_id`: integer NULL — Column resource_cache_id of table resource_cache_uses.

### resource_caches  (source backend: mongodb)
Source table resource_caches.

- `id`: integer NOT NULL — Column id of table resource_caches.
- `metadata`: text NULL — Column metadata of table resource_caches.
- `params_hash`: text NOT NULL — Column params_hash of table resource_caches.
- `resource_config_id`: integer NULL — Column resource_config_id of table resource_caches.
- `version`: text NOT NULL — Column version of table resource_caches.
- primary key: id

### resource_config_check_sessions  (source backend: postgres)
Source table resource_config_check_sessions.

- `expires_at`: timestamp NULL — Column expires_at of table resource_config_check_sessions.
- `id`: integer NOT NULL — Column id of table resource_config_check_sessions.
- `resource_config_id`: integer NULL — Column resource_config_id of table resource_config_check_sessions.
- primary key: id

### resource_configs  (source backend: s3)
Source table resource_configs.

- `base_resource_type_id`: integer NULL — Column base_resource_type_id of table resource_configs.
- `id`: integer NOT NULL — Column id of table resource_configs.
- `resource_cache_id`: integer NULL — Column resource_cache_id of table resource_configs.
- `source_hash`: text NOT NULL — Column source_hash of table resource_configs.
- primary key: id

### resource_types  (source backend: rest)
Source table resource_types.

- `active`: boolean NOT NULL — Column active of table resource_types.
- `config`: text NOT NULL — Column config of table resource_types.
- `id`: integer NOT NULL — Column id of table resource_types.
- `last_checked`: timestamp NOT NULL — Column last_checked of table resource_types.
- `name`: text NOT NULL — Column name of table resource_types.
- `nonce`: text NULL — Column nonce of table resource_types.
- `pipeline_id`: integer NULL — Column pipeline_id of table resource_types.
- `resource_config_id`: integer NULL — Column resource_config_id of table resource_types.
- `type`: text NOT NULL — Column type of table resource_types.
- `version`: text NULL — Column version of table resource_types.
- primary key: id

### resources  (source backend: mongodb)
Source table resources.

- `active`: boolean NOT NULL — Column active of table resources.
- `check_error`: text NULL — Column check_error of table resources.
- `config`: text NOT NULL — Column config of table resources.
- `id`: integer NOT NULL — Column id of table resources.
- `last_checked`: timestamp NOT NULL — Column last_checked of table resources.
- `name`: text NOT NULL — Column name of table resources.
- `nonce`: text NULL — Column nonce of table resources.
- `paused`: boolean NULL — Column paused of table resources.
- `pipeline_id`: integer NOT NULL — Column pipeline_id of table resources.
- `resource_config_id`: integer NULL — Column resource_config_id of table resources.
- primary key: id

### teams  (source backend: files)
Source table teams.

- `admin`: boolean NULL — Column admin of table teams.
- `auth`: text NULL — Column auth of table teams.
- `basic_auth`: json NULL — Column basic_auth of table teams.
- `id`: integer NOT NULL — Column id of table teams.
- `name`: text NOT NULL — Column name of table teams.
- `nonce`: text NULL — Column nonce of table teams.
- primary key: id

### versioned_resources  (source backend: s3)
Source table versioned_resources.

- `check_order`: integer NOT NULL — Column check_order of table versioned_resources.
- `enabled`: boolean NOT NULL — Column enabled of table versioned_resources.
- `id`: integer NOT NULL — Column id of table versioned_resources.
- `metadata`: text NOT NULL — Column metadata of table versioned_resources.
- `modified_time`: timestamp NOT NULL — Column modified_time of table versioned_resources.
- `resource_id`: integer NULL — Column resource_id of table versioned_resources.
- `type`: text NOT NULL — Column type of table versioned_resources.
- `version`: text NOT NULL — Column version of table versioned_resources.
- primary key: id

### volumes  (source backend: s3)
Source table volumes.

- `container_id`: integer NULL — Column container_id of table volumes.
- `handle`: text NOT NULL — Column handle of table volumes.
- `host_path_version`: text NULL — Column host_path_version of table volumes.
- `id`: integer NOT NULL — Column id of table volumes.
- `original_volume_handle`: text NULL — Column original_volume_handle of table volumes.
- `output_name`: text NULL — Column output_name of table volumes.
- `parent_id`: integer NULL — Column parent_id of table volumes.
- `parent_state`: text NULL — Column parent_state of table volumes.
- `path`: text NULL — Column path of table volumes.
- `replicated_from`: text NULL — Column replicated_from of table volumes.
- `resource_hash`: text NULL — Column resource_hash of table volumes.
- `resource_version`: text NULL — Column resource_version of table volumes.
- `state`: text NOT NULL — Column state of table volumes.
- `team_id`: integer NULL — Column team_id of table volumes.
- `worker_base_resource_type_id`: integer NULL — Column worker_base_resource_type_id of table volumes.
- `worker_name`: text NOT NULL — Column worker_name of table volumes.
- `worker_resource_cache_id`: integer NULL — Column worker_resource_cache_id of table volumes.
- `worker_task_cache_id`: integer NULL — Column worker_task_cache_id of table volumes.
- primary key: id

### worker_base_resource_types  (source backend: mongodb)
Source table worker_base_resource_types.

- `base_resource_type_id`: integer NULL — Column base_resource_type_id of table worker_base_resource_types.
- `id`: integer NOT NULL — Column id of table worker_base_resource_types.
- `image`: text NOT NULL — Column image of table worker_base_resource_types.
- `version`: text NOT NULL — Column version of table worker_base_resource_types.
- `worker_name`: text NULL — Column worker_name of table worker_base_resource_types.
- primary key: id

### worker_resource_caches  (source backend: s3)
Source table worker_resource_caches.

- `id`: integer NOT NULL — Column id of table worker_resource_caches.
- `resource_cache_id`: integer NULL — Column resource_cache_id of table worker_resource_caches.
- `worker_base_resource_type_id`: integer NULL — Column worker_base_resource_type_id of table worker_resource_caches.
- primary key: id

### worker_resource_config_check_sessions  (source backend: rest)
Source table worker_resource_config_check_sessions.

- `id`: integer NOT NULL — Column id of table worker_resource_config_check_sessions.
- `resource_config_check_session_id`: integer NULL — Column resource_config_check_session_id of table worker_resource_config_check_sessions.
- `team_id`: integer NULL — Column team_id of table worker_resource_config_check_sessions.
- `worker_base_resource_type_id`: integer NULL — Column worker_base_resource_type_id of table worker_resource_config_check_sessions.
- primary key: id

### worker_task_caches  (source backend: postgres)
Source table worker_task_caches.

- `id`: integer NOT NULL — Column id of table worker_task_caches.
- `job_id`: integer NULL — Column job_id of table worker_task_caches.
- `path`: text NOT NULL — Column path of table worker_task_caches.
- `step_name`: text NOT NULL — Column step_name of table worker_task_caches.
- `worker_name`: text NULL — Column worker_name of table worker_task_caches.
- primary key: id

### workers  (source backend: files)
Source table workers.

- `active_containers`: integer NULL — Column active_containers of table workers.
- `addr`: text NULL — Column addr of table workers.
- `baggageclaim_url`: text NULL — Column baggageclaim_url of table workers.
- `expires`: timestamp NULL — Column expires of table workers.
- `http_proxy_url`: text NULL — Column http_proxy_url of table workers.
- `https_proxy_url`: text NULL — Column https_proxy_url of table workers.
- `name`: text NOT NULL — Column name of table workers.
- `no_proxy`: text NULL — Column no_proxy of table workers.
- `platform`: text NULL — Column platform of table workers.
- `resource_types`: text NULL — Column resource_types of table workers.
- `start_time`: integer NULL — Column start_time of table workers.
- `state`: text NOT NULL — Column state of table workers.
- `tags`: text NULL — Column tags of table workers.
- `team_id`: integer NULL — Column team_id of table workers.
- `version`: text NULL — Column version of table workers.
- primary key: name

### Relationships

- build_image_resource_caches(build_id) -> builds(id) [required]
- build_image_resource_caches(resource_cache_id) -> resource_caches(id) [optional (may be NULL/dangling)]
- build_inputs(build_id) -> builds(id) [optional (may be NULL/dangling)]
- build_inputs(versioned_resource_id) -> versioned_resources(id) [optional (may be NULL/dangling)]
- build_outputs(build_id) -> builds(id) [optional (may be NULL/dangling)]
- build_outputs(versioned_resource_id) -> versioned_resources(id) [optional (may be NULL/dangling)]
- builds(job_id) -> jobs(id) [optional (may be NULL/dangling)]
- builds(pipeline_id) -> pipelines(id) [optional (may be NULL/dangling)]
- builds(team_id) -> teams(id) [required]
- containers(build_id) -> builds(id) [optional (may be NULL/dangling)]
- containers(team_id) -> teams(id) [optional (may be NULL/dangling)]
- containers(worker_name) -> workers(name) [optional (may be NULL/dangling)]
- containers(worker_resource_config_check_session_id) -> worker_resource_config_check_sessions(id) [optional (may be NULL/dangling)]
- independent_build_inputs(job_id) -> jobs(id) [required]
- independent_build_inputs(version_id) -> versioned_resources(id) [required]
- jobs(pipeline_id) -> pipelines(id) [required]
- jobs_serial_groups(job_id) -> jobs(id) [optional (may be NULL/dangling)]
- next_build_inputs(job_id) -> jobs(id) [required]
- next_build_inputs(version_id) -> versioned_resources(id) [required]
- pipelines(team_id) -> teams(id) [required]
- pipes(team_id) -> teams(id) [required]
- resource_cache_uses(build_id) -> builds(id) [optional (may be NULL/dangling)]
- resource_cache_uses(container_id) -> containers(id) [optional (may be NULL/dangling)]
- resource_cache_uses(resource_cache_id) -> resource_caches(id) [optional (may be NULL/dangling)]
- resource_caches(resource_config_id) -> resource_configs(id) [optional (may be NULL/dangling)]
- resource_config_check_sessions(resource_config_id) -> resource_configs(id) [optional (may be NULL/dangling)]
- resource_configs(base_resource_type_id) -> base_resource_types(id) [optional (may be NULL/dangling)]
- resource_configs(resource_cache_id) -> resource_caches(id) [optional (may be NULL/dangling)]
- resource_types(pipeline_id) -> pipelines(id) [optional (may be NULL/dangling)]
- resource_types(resource_config_id) -> resource_configs(id) [optional (may be NULL/dangling)]
- resources(pipeline_id) -> pipelines(id) [required]
- resources(resource_config_id) -> resource_configs(id) [optional (may be NULL/dangling)]
- versioned_resources(resource_id) -> resources(id) [optional (may be NULL/dangling)]
- volumes(container_id) -> containers(id) [optional (may be NULL/dangling)]
- volumes(team_id) -> teams(id) [optional (may be NULL/dangling)]
- volumes(worker_base_resource_type_id) -> worker_base_resource_types(id) [optional (may be NULL/dangling)]
- volumes(worker_name) -> workers(name) [required]
- volumes(worker_resource_cache_id) -> worker_resource_caches(id) [optional (may be NULL/dangling)]
- volumes(worker_task_cache_id) -> worker_task_caches(id) [optional (may be NULL/dangling)]
- worker_base_resource_types(base_resource_type_id) -> base_resource_types(id) [optional (may be NULL/dangling)]
- worker_base_resource_types(worker_name) -> workers(name) [optional (may be NULL/dangling)]
- worker_resource_caches(resource_cache_id) -> resource_caches(id) [optional (may be NULL/dangling)]
- worker_resource_caches(worker_base_resource_type_id) -> worker_base_resource_types(id) [optional (may be NULL/dangling)]
- worker_resource_config_check_sessions(resource_config_check_session_id) -> resource_config_check_sessions(id) [optional (may be NULL/dangling)]
- worker_resource_config_check_sessions(team_id) -> teams(id) [optional (may be NULL/dangling)]
- worker_resource_config_check_sessions(worker_base_resource_type_id) -> worker_base_resource_types(id) [optional (may be NULL/dangling)]
- worker_task_caches(job_id) -> jobs(id) [optional (may be NULL/dangling)]
- worker_task_caches(worker_name) -> workers(name) [optional (may be NULL/dangling)]
- workers(team_id) -> teams(id) [optional (may be NULL/dangling)]

