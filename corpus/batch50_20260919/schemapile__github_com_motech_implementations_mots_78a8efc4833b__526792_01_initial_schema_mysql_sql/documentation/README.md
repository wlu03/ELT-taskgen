# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Motech Implementations Mots

## Specification

PROJECT OVERVIEW

This project builds three analytical marts from a health-worker training and IVR platform (the 526792_01_initial_schema.mysql.sql schema). The marts summarize unit progress activity, multiple-choice answer-option distributions, and per-module-progress extremes.

Source tables and the extraction backend each one must be read from:

- Source table assigned_course must be extracted from the mongodb backend.
- Source table assigned_modules must be extracted from the s3 backend.
- Source table call_detail_record must be extracted from the files backend.
- Source table call_detail_record_data must be extracted from the rest backend.
- Source table call_flow_element must be extracted from the rest backend.
- Source table call_flow_element_log must be extracted from the rest backend.
- Source table choice must be extracted from the mongodb backend.
- Source table chw_group must be extracted from the mongodb backend.
- Source table client must be extracted from the postgres backend.
- Source table community_health_worker must be extracted from the files backend.
- Source table course must be extracted from the rest backend.
- Source table course_module must be extracted from the rest backend.
- Source table district must be extracted from the files backend.
- Source table district_assignment_log must be extracted from the postgres backend.
- Source table facility must be extracted from the files backend.
- Source table ivr_config must be extracted from the rest backend.
- Source table ivr_config_call_status_map must be extracted from the rest backend.
- Source table ivr_config_languages must be extracted from the mongodb backend.
- Source table jasper_template_parameter_options must be extracted from the files backend.
- Source table jasper_template_supported_formats must be extracted from the mongodb backend.
- Source table jasper_templates must be extracted from the rest backend.
- Source table message must be extracted from the s3 backend.
- Source table message_log must be extracted from the postgres backend.
- Source table module must be extracted from the rest backend.
- Source table module_assignment must be extracted from the postgres backend.
- Source table module_progress must be extracted from the s3 backend.
- Source table multiple_choice_question must be extracted from the postgres backend.
- Source table multiple_choice_question_log must be extracted from the mongodb backend.
- Source table sector must be extracted from the mongodb backend.
- Source table template_parameters must be extracted from the s3 backend.
- Source table unit must be extracted from the rest backend.
- Source table unit_progress must be extracted from the postgres backend.
- Source table user must be extracted from the postgres backend.
- Source table user_log must be extracted from the files backend.
- Source table user_permission must be extracted from the rest backend.
- Source table user_role must be extracted from the rest backend.
- Source table user_role_permissions must be extracted from the files backend.
- Source table users_roles must be extracted from the files backend.
- Source table village must be extracted from the rest backend.

Relationships between the source tables, each labelled exactly as the source schema labels it:

- Child table assigned_course with key course_id refers to parent table course with key id; this relationship is optional (may be NULL or dangling).
- Child table assigned_course with key health_worker_id refers to parent table community_health_worker with key id; this relationship is optional (may be NULL or dangling).
- Child table assigned_modules with key health_worker_id refers to parent table community_health_worker with key id; this relationship is optional (may be NULL or dangling).
- Child table call_detail_record_data with key cdr_id refers to parent table call_detail_record with key id; this relationship is required.
- Child table call_flow_element with key unit_id refers to parent table unit with key id; this relationship is optional (may be NULL or dangling).
- Child table call_flow_element_log with key call_flow_element_id refers to parent table call_flow_element with key id; this relationship is required.
- Child table call_flow_element_log with key unit_progress_id refers to parent table unit_progress with key id; this relationship is required.
- Child table choice with key question_id refers to parent table multiple_choice_question with key call_flow_element_id; this relationship is optional (may be NULL or dangling).
- Child table community_health_worker with key district_id refers to parent table district with key id; this relationship is required.
- Child table community_health_worker with key facility_id refers to parent table facility with key id; this relationship is optional (may be NULL or dangling).
- Child table community_health_worker with key group_id refers to parent table chw_group with key id; this relationship is optional (may be NULL or dangling).
- Child table community_health_worker with key sector_id refers to parent table sector with key id; this relationship is optional (may be NULL or dangling).
- Child table community_health_worker with key village_id refers to parent table village with key id; this relationship is optional (may be NULL or dangling).
- Child table course_module with key course_id refers to parent table course with key id; this relationship is required.
- Child table course_module with key module_id refers to parent table module with key id; this relationship is required.
- Child table district with key owner_id refers to parent table user with key id; this relationship is required.
- Child table district_assignment_log with key district_id refers to parent table district with key id; this relationship is optional (may be NULL or dangling).
- Child table district_assignment_log with key facility_id refers to parent table facility with key id; this relationship is optional (may be NULL or dangling).
- Child table district_assignment_log with key group_id refers to parent table chw_group with key id; this relationship is optional (may be NULL or dangling).
- Child table district_assignment_log with key module_id refers to parent table module with key id; this relationship is required.
- Child table district_assignment_log with key owner_id refers to parent table user with key id; this relationship is required.
- Child table district_assignment_log with key sector_id refers to parent table sector with key id; this relationship is optional (may be NULL or dangling).
- Child table facility with key owner_id refers to parent table user with key id; this relationship is required.
- Child table facility with key sector_id refers to parent table sector with key id; this relationship is required.
- Child table ivr_config_call_status_map with key ivr_config_id refers to parent table ivr_config with key id; this relationship is required.
- Child table ivr_config_languages with key ivr_config_id refers to parent table ivr_config with key id; this relationship is required.
- Child table jasper_template_parameter_options with key id refers to parent table template_parameters with key id; this relationship is required.
- Child table jasper_template_supported_formats with key jasper_template_id refers to parent table jasper_templates with key id; this relationship is required.
- Child table message with key call_flow_element_id refers to parent table call_flow_element with key id; this relationship is required.
- Child table message_log with key call_flow_element_log_id refers to parent table call_flow_element_log with key id; this relationship is required.
- Child table module_assignment with key assigned_modules_id refers to parent table assigned_modules with key id; this relationship is required.
- Child table module_assignment with key module_id refers to parent table module with key id; this relationship is required.
- Child table module_progress with key chw_id refers to parent table community_health_worker with key id; this relationship is required.
- Child table module_progress with key course_module_id refers to parent table course_module with key id; this relationship is required.
- Child table multiple_choice_question with key call_flow_element_id refers to parent table call_flow_element with key id; this relationship is required.
- Child table multiple_choice_question_log with key call_flow_element_log_id refers to parent table call_flow_element_log with key id; this relationship is required.
- Child table multiple_choice_question_log with key response_id refers to parent table choice with key id; this relationship is optional (may be NULL or dangling).
- Child table sector with key district_id refers to parent table district with key id; this relationship is required.
- Child table sector with key owner_id refers to parent table user with key id; this relationship is required.
- Child table template_parameters with key template_id refers to parent table jasper_templates with key id; this relationship is required.
- Child table unit with key module_id refers to parent table module with key id; this relationship is optional (may be NULL or dangling).
- Child table unit_progress with key module_progress_id refers to parent table module_progress with key id; this relationship is required.
- Child table unit_progress with key unit_id refers to parent table unit with key id; this relationship is required.
- Child table user_log with key user_id refers to parent table user with key id; this relationship is required.
- Child table user_role_permissions with key permission_id refers to parent table user_permission with key id; this relationship is required.
- Child table user_role_permissions with key role_id refers to parent table user_role with key id; this relationship is required.
- Child table users_roles with key role_id refers to parent table user_role with key id; this relationship is required.
- Child table users_roles with key user_id refers to parent table user with key id; this relationship is required.
- Child table village with key facility_id refers to parent table facility with key id; this relationship is required.
- Child table village with key owner_id refers to parent table user with key id; this relationship is required.

Throughout, text comparisons use a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one).

=====================================================================
MART unit_unit_progress_snapshot — Per-unit latest-row snapshot over linked unit_progress activity in the 526792_01_initial_schema.mysql.sql schema.
=====================================================================

Grain: one row per unit (id), INCLUDING unit rows with no linked unit_progress rows.

Key column: parent_key.

Rule 1. Source table unit is read in full as an input of this mart.

Rule 2. Source table unit_progress is read in full as an input of this mart.

Rule 3. From source table unit there is one row per unit row, keyed by id; that row carries parent_key and parent_name.

Rule 4. The unit_progress rows whose unit_id matches parent_key are brought in from source table unit_progress, carrying unit_id and id; preservation is left-sided on the unit side, so a unit row with no matching unit_progress row is retained and receives the stated empty snapshot values.

Rule 5. There is one output row per parent_key, carrying parent_name beside the keys: a parent_key value identifies one source row for the carried columns, so parent_name takes one value per parent_key and never splits a group, and each parent_key reports event_count and lifetime_amount over that row's matching rows.

Rule 6. For each parent_key the single row at which the ordering measure — created_date — is largest survives, ties broken by the smallest id under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), and latest_row_id, latest_amount and latest_label are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 7. The extremal row's attributes are attached to the grouped measures on matching parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

Rule 8. The mart columns are named parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label; lifetime_amount reports its declared default of 0 — never NULL — for a group with no matching rows.

Rule 9. Guarded ratio: alongside parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label, the column latest_amount_share is latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; it is 0.0 when lifetime_amount is 0, and 0.0 whenever the denominator is 0 or has no value.

Rule 10. Deterministic output order: rows appear sorted in ascending parent_key sequence.

Output columns:

- parent_key (text): identifier of the unit row; there is one row per value.
- parent_name (text): the continuation_question_ivr_id of the unit row, copied unchanged.
- event_count (bigint): number of unit_progress rows for this unit row; 0 when there are none. A unit row kept with no unit_progress row reports 0 here, never 1: its placeholder holds no unit_progress row to count.
- lifetime_amount (integer): the total of number_of_replays over all matching unit_progress rows; 0 when there are no rows.
- latest_row_id (text): the id of the row with the latest created_date; ties take the smallest id under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one). It is the literal '(none)' when there are no rows.
- latest_amount (integer): number_of_replays from that same latest row; 0 when there are no rows.
- latest_label (text): status from that same latest row; '(none)' when there are no rows.
- latest_amount_share (float): latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0.

=====================================================================
MART multiple_choice_question_choice_distribution — Per-(multiple_choice_question, measure state) distribution of linked choice activity in the 526792_01_initial_schema.mysql.sql schema.
=====================================================================

Grain: one row per (call_flow_element_id, measure state) pair represented by linked choice rows, plus one absent no-activity row for a multiple_choice_question row with no links. Because choice_id is required, no linked choice row belongs to the absent state.

Key columns: entity_key and measure_state.

Rule 1. Source table multiple_choice_question is read in full as an input of this mart.

Rule 2. Source table choice is read in full as an input of this mart.

Rule 3. From source table multiple_choice_question, each call_flow_element_id and its question_type are carried into the measure-state calculation as entity_key and entity_name.

Rule 4. The linked choice rows from source table choice — those whose question_id matches entity_key — are brought into each multiple_choice_question entity, carrying entity_key, entity_name, call_flow_element_id and question_id; preservation is left-sided on the multiple_choice_question side, so an entity with no linked row is retained and its absent state is visible.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are the ones kept here: a real choice row; choice_id is required on every such row.

Rule 6. There is one row per multiple_choice_question entity that has at least one row in the present measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount — that is, the row count, how many different choice_id values occur (each different value counted once, however many rows repeat it), the total choice_id, and the largest choice_id.

Rule 7. For those present-state measures, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures are labelled as the present measure state, so measure_state reads 'present' beside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are the ones kept here: the retained placeholder for a multiple_choice_question row with no choice rows; no real row can enter this state because choice_id is required.

Rule 10. There is one row per multiple_choice_question entity with no linked choice row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked choice row, reporting entity_key, entity_name and a row_count of 0, 0 different choice_id values as distinct_amount_count, a total choice_id of 0 as total_amount and a largest choice_id of 0 as max_amount.

Rule 11. For those absent-state measures, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures are labelled as the absent measure state, so measure_state reads 'absent' beside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. Deterministic output order: rows appear sorted in ascending order by entity_key, and then by measure_state within an entity.

Output columns:

- entity_key (text): identifier of the multiple_choice_question row.
- measure_state (text): 'present' for a linked choice row; 'absent' only for a multiple_choice_question row with no linked choice row. choice_id is required on every real choice row.
- entity_name (text): question_type of the multiple_choice_question row, copied unchanged.
- row_count (bigint): number of linked choice rows in this entity/state cell; 0 for a no-activity absent cell.
- distinct_amount_count (bigint): number of unique choice_id values in this cell; each unique value is counted once, however many rows repeat it; 0 for a no-activity absent cell.
- total_amount (integer): the total of choice_id in this cell; 0 for a no-activity absent cell.
- max_amount (integer): largest choice_id in this cell; 0 for a no-activity absent cell.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

=====================================================================
MART module_progress_unit_progress_top — Per-module_progress extremes over linked unit_progress rows in the 526792_01_initial_schema.mysql.sql schema: WHICH row is largest, not how large it is.
=====================================================================

Grain: one row per module_progress (id), INCLUDING module_progress rows with no linked unit_progress rows.

Key column: parent_key.

Rule 1. Source table module_progress is read in full as an input of this mart.

Rule 2. Source table unit_progress is read in full as an input of this mart.

Rule 3. From source table module_progress there is one row per module_progress row, keyed by id; that row carries parent_key and parent_name.

Rule 4. The unit_progress rows whose module_progress_id matches parent_key are brought in from source table unit_progress, carrying module_progress_id and id; preservation is left-sided on the module_progress side, so a module_progress row with no unit_progress rows still appears, with the declared defaults.

Rule 5. Within each parent_key group the matched rows are ranked under an explicit total order — the measure first, then the declared tie-break — so the extremal row is a function of the input and not of row order.

Rule 6. There is one output row per parent_key, carrying parent_name beside the keys: a parent_key value identifies one source row for the carried columns, so parent_name takes one value per parent_key and never splits a group, and each parent_key reports top_measure, tied_count, child_count and total_measure over that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7. For each parent_key the single row at which the ordering measure — number_of_replays — is largest survives, ties broken by the smallest status under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then the smallest id under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), and top_label and top_row_id are taken from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows.

Rule 8. The extremal row's attributes are attached to the grouped measures on matching parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

Rule 9. The mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults of 0 — never NULL — for a group with no matching rows.

Rule 10. Guarded ratio: alongside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id, the column top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0, and 0.0 whenever the denominator is 0 or has no value.

Rule 11. Beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, tie_state is 'empty' when no row holds a maximum at all — the parent has no unit_progress rows — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; this categorical mapping has no numeric boundary. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, otherwise 'tied' when it is 2 or more, and it is never null or blank.

Rule 12. Deterministic output order: rows appear sorted in ascending parent_key sequence.

Output columns:

- parent_key (text): identifier of the module_progress row; there is one row per value.
- parent_name (text): status of the module_progress row, copied unchanged.
- top_measure (integer): the largest number_of_replays itself; 0 when the parent has no unit_progress rows.
- tied_count (bigint): how many unit_progress rows are tied at that largest number_of_replays; 1 when exactly one row carries that largest number_of_replays; 0 when there are no rows.
- child_count (bigint): number of unit_progress rows for this module_progress row; 0 when there are none. A module_progress row kept with no unit_progress row reports 0 here, never 1: its placeholder holds no unit_progress row to count.
- total_measure (integer): the total of number_of_replays over all of them; 0 when the parent has no unit_progress rows.
- top_label (text): the status of the unit_progress row with the LARGEST number_of_replays for this module_progress row. Ties in number_of_replays are broken by taking the SMALLEST status under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one); rows tied on both are resolved by the smallest id under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one). It is the literal '(none)' when the parent has no unit_progress rows at all.
- top_row_id (text): the id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real unit_progress row whenever the parent has any. It is the literal '(none)' when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no unit_progress rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `unit_unit_progress_snapshot`

- Grain: One row per unit (id), INCLUDING unit rows with no linked unit_progress rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share

```text
Mart 'unit_unit_progress_snapshot' has 10 declared semantic rules:
1. [source] Read source table unit. (public source tables: unit)
2. [source] Read source table unit_progress. (public source tables: unit_progress)
3. [derive] One row per unit row, keyed by id. (public source tables: unit | public carried/output columns: parent_key, parent_name)
4. [join] Bring in unit_progress; a unit row with no matching unit_progress row is retained and receives the stated empty snapshot values. (public source tables: unit_progress | public carried/output columns: unit_id, id | join preservation: left | condition public identifiers: unit_progress, unit_id, parent_key)
5. [aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count, lifetime_amount for that row's matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount)
6. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest id under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), and take latest_row_id, latest_amount, latest_label from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, latest_row_id, latest_amount, latest_label)
7. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
8. [derive] Name the mart columns; lifetime_amount reports its declared default — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label)
9. [ratio] Guarded ratios: latest_amount_share — latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `multiple_choice_question_choice_distribution`

- Grain: One row per (call_flow_element_id, measure state) pair represented by linked choice rows, plus one absent no-activity row for a multiple_choice_question row with no links. Because choice_id is required, no linked choice row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'multiple_choice_question_choice_distribution' has 14 declared semantic rules:
1. [source] Read source table multiple_choice_question. (public source tables: multiple_choice_question)
2. [source] Read source table choice. (public source tables: choice)
3. [derive] Carry each call_flow_element_id and its question_type into the measure-state calculation. (public source tables: multiple_choice_question | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked choice rows into each multiple_choice_question entity; retain an entity with no linked row so its absent state is visible. (public source tables: choice | public carried/output columns: entity_key, entity_name, call_flow_element_id, question_id | join preservation: left | condition public identifiers: choice, question_id, entity_key)
5. [filter] Keep the present measure-state rows: a real choice row; choice_id is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per multiple_choice_question entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different choice_id values occur (each different value counted once, however many rows repeat it), total choice_id, and largest choice_id. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a multiple_choice_question row with no choice rows; no real row can enter this state because choice_id is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per multiple_choice_question entity with no linked choice row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked choice row, reporting a row count of 0, 0 different choice_id values, a total choice_id of 0 and a largest choice_id of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `module_progress_unit_progress_top`

- Grain: One row per module_progress (id), INCLUDING module_progress rows with no linked unit_progress rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'module_progress_unit_progress_top' has 12 declared semantic rules:
1. [source] Read source table module_progress. (public source tables: module_progress)
2. [source] Read source table unit_progress. (public source tables: unit_progress)
3. [derive] One row per module_progress row, keyed by id. (public source tables: module_progress | public carried/output columns: parent_key, parent_name)
4. [join] Bring in unit_progress: a module_progress row with no unit_progress rows still appears, with the declared defaults. (public source tables: unit_progress | public carried/output columns: module_progress_id, id | join preservation: left | condition public identifiers: unit_progress, module_progress_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest status under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then the smallest id under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), and take top_label, top_row_id from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no unit_progress rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### assigned_course  (source backend: mongodb)
Source table assigned_course.

- `course_id`: text NULL — Column course_id of table assigned_course.
- `created_date`: timestamp NULL — Column created_date of table assigned_course.
- `end_date`: timestamp NULL — Column end_date of table assigned_course.
- `health_worker_id`: text NULL — Column health_worker_id of table assigned_course.
- `id`: text NOT NULL — Column id of table assigned_course.
- `start_date`: timestamp NOT NULL — Column start_date of table assigned_course.
- `updated_date`: timestamp NULL — Column updated_date of table assigned_course.
- primary key: id

### assigned_modules  (source backend: s3)
Source table assigned_modules.

- `created_date`: timestamp NULL — Column created_date of table assigned_modules.
- `health_worker_id`: text NULL — Column health_worker_id of table assigned_modules.
- `id`: text NOT NULL — Column id of table assigned_modules.
- `updated_date`: timestamp NULL — Column updated_date of table assigned_modules.
- primary key: id

### call_detail_record  (source backend: files)
Source table call_detail_record.

- `call_log_id`: text NOT NULL — Column call_log_id of table call_detail_record.
- `call_status`: text NOT NULL — Column call_status of table call_detail_record.
- `chw_ivr_id`: text NOT NULL — Column chw_ivr_id of table call_detail_record.
- `created_date`: timestamp NULL — Column created_date of table call_detail_record.
- `id`: text NOT NULL — Column id of table call_detail_record.
- `incoming_call_id`: text NULL — Column incoming_call_id of table call_detail_record.
- `ivr_config_name`: text NOT NULL — Column ivr_config_name of table call_detail_record.
- `outgoing_call_id`: text NULL — Column outgoing_call_id of table call_detail_record.
- `updated_date`: timestamp NULL — Column updated_date of table call_detail_record.
- primary key: id

### call_detail_record_data  (source backend: rest)
Source table call_detail_record_data.

- `cdr_id`: text NOT NULL — Column cdr_id of table call_detail_record_data.
- `name`: text NOT NULL — Column name of table call_detail_record_data.
- `value`: text NULL — Column value of table call_detail_record_data.
- primary key: cdr_id, name

### call_flow_element  (source backend: rest)
Source table call_flow_element.

- `content`: text NULL — Column content of table call_flow_element.
- `created_date`: timestamp NULL — Column created_date of table call_flow_element.
- `id`: text NOT NULL — Column id of table call_flow_element.
- `ivr_id`: text NULL — Column ivr_id of table call_flow_element.
- `ivr_name`: text NULL — Column ivr_name of table call_flow_element.
- `list_order`: integer NOT NULL — Column list_order of table call_flow_element.
- `name`: text NOT NULL — Column name of table call_flow_element.
- `type`: text NOT NULL — Column type of table call_flow_element.
- `unit_id`: text NULL — Column unit_id of table call_flow_element.
- `updated_date`: timestamp NULL — Column updated_date of table call_flow_element.
- primary key: id

### call_flow_element_log  (source backend: rest)
Source table call_flow_element_log.

- `call_flow_element_id`: text NOT NULL — Column call_flow_element_id of table call_flow_element_log.
- `created_date`: timestamp NULL — Column created_date of table call_flow_element_log.
- `end_date`: timestamp NULL — Column end_date of table call_flow_element_log.
- `id`: text NOT NULL — Column id of table call_flow_element_log.
- `start_date`: timestamp NULL — Column start_date of table call_flow_element_log.
- `unit_progress_id`: text NOT NULL — Column unit_progress_id of table call_flow_element_log.
- `updated_date`: timestamp NULL — Column updated_date of table call_flow_element_log.
- primary key: id

### choice  (source backend: mongodb)
Source table choice.

- `choice_id`: integer NOT NULL — Column choice_id of table choice.
- `created_date`: timestamp NULL — Column created_date of table choice.
- `description`: text NULL — Column description of table choice.
- `id`: text NOT NULL — Column id of table choice.
- `ivr_name`: text NULL — Column ivr_name of table choice.
- `question_id`: text NULL — Column question_id of table choice.
- `type`: text NOT NULL — Column type of table choice.
- `updated_date`: timestamp NULL — Column updated_date of table choice.
- primary key: id

### chw_group  (source backend: mongodb)
Source table chw_group.

- `created_date`: timestamp NULL — Column created_date of table chw_group.
- `id`: text NOT NULL — Column id of table chw_group.
- `name`: text NOT NULL — Column name of table chw_group.
- `updated_date`: timestamp NULL — Column updated_date of table chw_group.
- primary key: id

### client  (source backend: postgres)
Source table client.

- `access_token_validity_seconds`: integer NULL — Column access_token_validity_seconds of table client.
- `additional_information`: text NULL — Column additional_information of table client.
- `authorities`: text NOT NULL — Column authorities of table client.
- `authorized_grant_types`: text NOT NULL — Column authorized_grant_types of table client.
- `client_id`: text NOT NULL — Column client_id of table client.
- `client_secret`: text NULL — Column client_secret of table client.
- `refresh_token_validity_seconds`: integer NULL — Column refresh_token_validity_seconds of table client.
- `registered_redirect_uris`: text NULL — Column registered_redirect_uris of table client.
- `resource_ids`: text NULL — Column resource_ids of table client.
- `scope`: text NULL — Column scope of table client.
- primary key: client_id

### community_health_worker  (source backend: files)
Source table community_health_worker.

- `chw_id`: text NOT NULL — Column chw_id of table community_health_worker.
- `chw_name`: text NOT NULL — Column chw_name of table community_health_worker.
- `created_date`: timestamp NULL — Column created_date of table community_health_worker.
- `district_id`: text NOT NULL — Column district_id of table community_health_worker.
- `facility_id`: text NULL — Column facility_id of table community_health_worker.
- `gender`: text NULL — Column gender of table community_health_worker.
- `group_id`: text NULL — Column group_id of table community_health_worker.
- `id`: text NOT NULL — Column id of table community_health_worker.
- `ivr_id`: text NULL — Column ivr_id of table community_health_worker.
- `phone_number`: text NULL — Column phone_number of table community_health_worker.
- `preferred_language`: text NOT NULL — Column preferred_language of table community_health_worker.
- `sector_id`: text NULL — Column sector_id of table community_health_worker.
- `selected`: boolean NOT NULL — Column selected of table community_health_worker.
- `updated_date`: timestamp NULL — Column updated_date of table community_health_worker.
- `village_id`: text NULL — Column village_id of table community_health_worker.
- primary key: id

### course  (source backend: rest)
Source table course.

- `choose_module_question_ivr_id`: text NULL — Column choose_module_question_ivr_id of table course.
- `created_date`: timestamp NULL — Column created_date of table course.
- `description`: text NULL — Column description of table course.
- `id`: text NOT NULL — Column id of table course.
- `ivr_id`: text NULL — Column ivr_id of table course.
- `ivr_name`: text NULL — Column ivr_name of table course.
- `menu_intro_message_ivr_id`: text NULL — Column menu_intro_message_ivr_id of table course.
- `name`: text NULL — Column name of table course.
- `no_modules_message_ivr_id`: text NULL — Column no_modules_message_ivr_id of table course.
- `previous_version_id`: text NULL — Column previous_version_id of table course.
- `status`: text NOT NULL — Column status of table course.
- `updated_date`: timestamp NULL — Column updated_date of table course.
- `version`: integer NOT NULL — Column version of table course.
- primary key: id

### course_module  (source backend: rest)
Source table course_module.

- `course_id`: text NOT NULL — Column course_id of table course_module.
- `created_date`: timestamp NULL — Column created_date of table course_module.
- `id`: text NOT NULL — Column id of table course_module.
- `ivr_id`: text NULL — Column ivr_id of table course_module.
- `ivr_name`: text NULL — Column ivr_name of table course_module.
- `list_order`: integer NOT NULL — Column list_order of table course_module.
- `module_id`: text NOT NULL — Column module_id of table course_module.
- `previous_version_id`: text NULL — Column previous_version_id of table course_module.
- `start_module_question_ivr_id`: text NULL — Column start_module_question_ivr_id of table course_module.
- `updated_date`: timestamp NULL — Column updated_date of table course_module.
- primary key: id

### district  (source backend: files)
Source table district.

- `created_date`: timestamp NULL — Column created_date of table district.
- `id`: text NOT NULL — Column id of table district.
- `ivr_group_id`: text NULL — Column ivr_group_id of table district.
- `name`: text NOT NULL — Column name of table district.
- `owner_id`: text NOT NULL — Column owner_id of table district.
- `updated_date`: timestamp NULL — Column updated_date of table district.
- primary key: id

### district_assignment_log  (source backend: postgres)
Source table district_assignment_log.

- `created_date`: timestamp NULL — Column created_date of table district_assignment_log.
- `district_id`: text NULL — Column district_id of table district_assignment_log.
- `facility_id`: text NULL — Column facility_id of table district_assignment_log.
- `group_id`: text NULL — Column group_id of table district_assignment_log.
- `id`: text NOT NULL — Column id of table district_assignment_log.
- `module_id`: text NOT NULL — Column module_id of table district_assignment_log.
- `owner_id`: text NOT NULL — Column owner_id of table district_assignment_log.
- `sector_id`: text NULL — Column sector_id of table district_assignment_log.
- `updated_date`: timestamp NULL — Column updated_date of table district_assignment_log.
- primary key: id

### facility  (source backend: files)
Source table facility.

- `created_date`: timestamp NULL — Column created_date of table facility.
- `id`: text NOT NULL — Column id of table facility.
- `incharge_email`: text NULL — Column incharge_email of table facility.
- `incharge_full_name`: text NULL — Column incharge_full_name of table facility.
- `incharge_phone`: text NULL — Column incharge_phone of table facility.
- `name`: text NOT NULL — Column name of table facility.
- `owner_id`: text NOT NULL — Column owner_id of table facility.
- `sector_id`: text NOT NULL — Column sector_id of table facility.
- `updated_date`: timestamp NULL — Column updated_date of table facility.
- primary key: id

### ivr_config  (source backend: rest)
Source table ivr_config.

- `base_url`: text NOT NULL — Column base_url of table ivr_config.
- `call_log_id_field`: text NOT NULL — Column call_log_id_field of table ivr_config.
- `call_status_field`: text NOT NULL — Column call_status_field of table ivr_config.
- `chw_ivr_id_field`: text NOT NULL — Column chw_ivr_id_field of table ivr_config.
- `created_date`: timestamp NULL — Column created_date of table ivr_config.
- `default_users_group_id`: text NOT NULL — Column default_users_group_id of table ivr_config.
- `detect_voicemail_action`: boolean NOT NULL — Column detect_voicemail_action of table ivr_config.
- `id`: text NOT NULL — Column id of table ivr_config.
- `incoming_call_id_field`: text NOT NULL — Column incoming_call_id_field of table ivr_config.
- `main_menu_tree_id`: text NULL — Column main_menu_tree_id of table ivr_config.
- `module_assigned_message_id`: text NOT NULL — Column module_assigned_message_id of table ivr_config.
- `name`: text NOT NULL — Column name of table ivr_config.
- `outgoing_call_id_field`: text NOT NULL — Column outgoing_call_id_field of table ivr_config.
- `retry_attempts_long`: integer NOT NULL — Column retry_attempts_long of table ivr_config.
- `retry_attempts_short`: integer NOT NULL — Column retry_attempts_short of table ivr_config.
- `retry_delay_long`: integer NOT NULL — Column retry_delay_long of table ivr_config.
- `retry_delay_short`: integer NOT NULL — Column retry_delay_short of table ivr_config.
- `send_sms_if_voice_fails`: boolean NOT NULL — Column send_sms_if_voice_fails of table ivr_config.
- `updated_date`: timestamp NULL — Column updated_date of table ivr_config.
- primary key: id

### ivr_config_call_status_map  (source backend: rest)
Source table ivr_config_call_status_map.

- `ivr_config_id`: text NOT NULL — Column ivr_config_id of table ivr_config_call_status_map.
- `ivr_status`: text NOT NULL — Column ivr_status of table ivr_config_call_status_map.
- `status`: text NOT NULL — Column status of table ivr_config_call_status_map.
- primary key: ivr_config_id, ivr_status

### ivr_config_languages  (source backend: mongodb)
Source table ivr_config_languages.

- `ivr_config_id`: text NOT NULL — Column ivr_config_id of table ivr_config_languages.
- `ivr_language_id`: text NOT NULL — Column ivr_language_id of table ivr_config_languages.
- `language`: text NOT NULL — Column language of table ivr_config_languages.
- primary key: ivr_config_id, language

### jasper_template_parameter_options  (source backend: files)
Source table jasper_template_parameter_options.

- `id`: text NOT NULL — Column id of table jasper_template_parameter_options.
- `options`: text NULL — Column options of table jasper_template_parameter_options.

### jasper_template_supported_formats  (source backend: mongodb)
Source table jasper_template_supported_formats.

- `jasper_template_id`: text NOT NULL — Column jasper_template_id of table jasper_template_supported_formats.
- `supported_formats`: text NULL — Column supported_formats of table jasper_template_supported_formats.

### jasper_templates  (source backend: rest)
Source table jasper_templates.

- `created_date`: timestamp NULL — Column created_date of table jasper_templates.
- `data`: text NULL — Column data of table jasper_templates.
- `description`: text NULL — Column description of table jasper_templates.
- `id`: text NOT NULL — Column id of table jasper_templates.
- `json_output`: text NULL — Column json_output of table jasper_templates.
- `json_output_version`: bigint NULL — Column json_output_version of table jasper_templates.
- `name`: text NOT NULL — Column name of table jasper_templates.
- `type`: text NULL — Column type of table jasper_templates.
- `updated_date`: timestamp NULL — Column updated_date of table jasper_templates.
- `visible`: boolean NULL — Column visible of table jasper_templates.
- primary key: id

### message  (source backend: s3)
Source table message.

- `call_flow_element_id`: text NOT NULL — Column call_flow_element_id of table message.
- primary key: call_flow_element_id

### message_log  (source backend: postgres)
Source table message_log.

- `call_flow_element_log_id`: text NOT NULL — Column call_flow_element_log_id of table message_log.
- primary key: call_flow_element_log_id

### module  (source backend: rest)
Source table module.

- `created_date`: timestamp NULL — Column created_date of table module.
- `description`: text NULL — Column description of table module.
- `id`: text NOT NULL — Column id of table module.
- `ivr_group`: text NULL — Column ivr_group of table module.
- `name`: text NOT NULL — Column name of table module.
- `name_code`: text NOT NULL — Column name_code of table module.
- `previous_version_id`: text NULL — Column previous_version_id of table module.
- `status`: text NOT NULL — Column status of table module.
- `updated_date`: timestamp NULL — Column updated_date of table module.
- `version`: integer NOT NULL — Column version of table module.
- primary key: id

### module_assignment  (source backend: postgres)
Source table module_assignment.

- `assigned_modules_id`: text NOT NULL — Column assigned_modules_id of table module_assignment.
- `module_id`: text NOT NULL — Column module_id of table module_assignment.
- primary key: assigned_modules_id, module_id

### module_progress  (source backend: s3)
Source table module_progress.

- `chw_id`: text NOT NULL — Column chw_id of table module_progress.
- `course_module_id`: text NOT NULL — Column course_module_id of table module_progress.
- `created_date`: timestamp NULL — Column created_date of table module_progress.
- `current_unit_number`: integer NOT NULL — Column current_unit_number of table module_progress.
- `end_date`: timestamp NULL — Column end_date of table module_progress.
- `id`: text NOT NULL — Column id of table module_progress.
- `interrupted`: boolean NOT NULL — Column interrupted of table module_progress.
- `start_date`: timestamp NULL — Column start_date of table module_progress.
- `status`: text NOT NULL — Column status of table module_progress.
- `updated_date`: timestamp NULL — Column updated_date of table module_progress.
- primary key: id

### multiple_choice_question  (source backend: postgres)
Source table multiple_choice_question.

- `call_flow_element_id`: text NOT NULL — Column call_flow_element_id of table multiple_choice_question.
- `question_type`: text NOT NULL — Column question_type of table multiple_choice_question.
- primary key: call_flow_element_id

### multiple_choice_question_log  (source backend: mongodb)
Source table multiple_choice_question_log.

- `call_flow_element_log_id`: text NOT NULL — Column call_flow_element_log_id of table multiple_choice_question_log.
- `number_of_attempts`: integer NOT NULL — Column number_of_attempts of table multiple_choice_question_log.
- `response_id`: text NULL — Column response_id of table multiple_choice_question_log.
- primary key: call_flow_element_log_id

### sector  (source backend: mongodb)
Source table sector.

- `created_date`: timestamp NULL — Column created_date of table sector.
- `district_id`: text NOT NULL — Column district_id of table sector.
- `id`: text NOT NULL — Column id of table sector.
- `name`: text NOT NULL — Column name of table sector.
- `owner_id`: text NOT NULL — Column owner_id of table sector.
- `updated_date`: timestamp NULL — Column updated_date of table sector.
- primary key: id

### template_parameters  (source backend: s3)
Source table template_parameters.

- `created_date`: timestamp NULL — Column created_date of table template_parameters.
- `data_type`: text NULL — Column data_type of table template_parameters.
- `default_value`: text NULL — Column default_value of table template_parameters.
- `description`: text NULL — Column description of table template_parameters.
- `display_name`: text NULL — Column display_name of table template_parameters.
- `id`: text NOT NULL — Column id of table template_parameters.
- `name`: text NULL — Column name of table template_parameters.
- `required`: boolean NOT NULL — Column required of table template_parameters.
- `template_id`: text NOT NULL — Column template_id of table template_parameters.
- `updated_date`: timestamp NULL — Column updated_date of table template_parameters.
- primary key: id

### unit  (source backend: rest)
Source table unit.

- `allow_replay`: boolean NOT NULL — Column allow_replay of table unit.
- `continuation_question_ivr_id`: text NULL — Column continuation_question_ivr_id of table unit.
- `created_date`: timestamp NULL — Column created_date of table unit.
- `description`: text NULL — Column description of table unit.
- `id`: text NOT NULL — Column id of table unit.
- `ivr_id`: text NULL — Column ivr_id of table unit.
- `ivr_name`: text NULL — Column ivr_name of table unit.
- `list_order`: integer NOT NULL — Column list_order of table unit.
- `module_id`: text NULL — Column module_id of table unit.
- `name`: text NOT NULL — Column name of table unit.
- `updated_date`: timestamp NULL — Column updated_date of table unit.
- primary key: id

### unit_progress  (source backend: postgres)
Source table unit_progress.

- `created_date`: timestamp NULL — Column created_date of table unit_progress.
- `id`: text NOT NULL — Column id of table unit_progress.
- `module_progress_id`: text NOT NULL — Column module_progress_id of table unit_progress.
- `number_of_replays`: integer NOT NULL — Column number_of_replays of table unit_progress.
- `status`: text NOT NULL — Column status of table unit_progress.
- `unit_id`: text NOT NULL — Column unit_id of table unit_progress.
- `updated_date`: timestamp NULL — Column updated_date of table unit_progress.
- primary key: id

### user  (source backend: postgres)
Source table user.

- `created_date`: timestamp NULL — Column created_date of table user.
- `email`: text NULL — Column email of table user.
- `enabled`: boolean NOT NULL — Column enabled of table user.
- `id`: text NOT NULL — Column id of table user.
- `name`: text NULL — Column name of table user.
- `password`: text NOT NULL — Column password of table user.
- `updated_date`: timestamp NULL — Column updated_date of table user.
- `username`: text NOT NULL — Column username of table user.
- primary key: id

### user_log  (source backend: files)
Source table user_log.

- `id`: text NOT NULL — Column id of table user_log.
- `login_date`: timestamp NULL — Column login_date of table user_log.
- `logout_date`: timestamp NULL — Column logout_date of table user_log.
- `user_id`: text NOT NULL — Column user_id of table user_log.
- primary key: id

### user_permission  (source backend: rest)
Source table user_permission.

- `created_date`: timestamp NULL — Column created_date of table user_permission.
- `display_name`: text NULL — Column display_name of table user_permission.
- `id`: text NOT NULL — Column id of table user_permission.
- `name`: text NOT NULL — Column name of table user_permission.
- `readonly`: boolean NOT NULL — Column readonly of table user_permission.
- `updated_date`: timestamp NULL — Column updated_date of table user_permission.
- primary key: id

### user_role  (source backend: rest)
Source table user_role.

- `created_date`: timestamp NULL — Column created_date of table user_role.
- `id`: text NOT NULL — Column id of table user_role.
- `name`: text NOT NULL — Column name of table user_role.
- `readonly`: boolean NOT NULL — Column readonly of table user_role.
- `updated_date`: timestamp NULL — Column updated_date of table user_role.
- primary key: id

### user_role_permissions  (source backend: files)
Source table user_role_permissions.

- `permission_id`: text NOT NULL — Column permission_id of table user_role_permissions.
- `role_id`: text NOT NULL — Column role_id of table user_role_permissions.
- primary key: permission_id, role_id

### users_roles  (source backend: files)
Source table users_roles.

- `role_id`: text NOT NULL — Column role_id of table users_roles.
- `user_id`: text NOT NULL — Column user_id of table users_roles.
- primary key: role_id, user_id

### village  (source backend: rest)
Source table village.

- `created_date`: timestamp NULL — Column created_date of table village.
- `facility_id`: text NOT NULL — Column facility_id of table village.
- `id`: text NOT NULL — Column id of table village.
- `name`: text NOT NULL — Column name of table village.
- `owner_id`: text NOT NULL — Column owner_id of table village.
- `updated_date`: timestamp NULL — Column updated_date of table village.
- primary key: id

### Relationships

- assigned_course(course_id) -> course(id) [optional (may be NULL/dangling)]
- assigned_course(health_worker_id) -> community_health_worker(id) [optional (may be NULL/dangling)]
- assigned_modules(health_worker_id) -> community_health_worker(id) [optional (may be NULL/dangling)]
- call_detail_record_data(cdr_id) -> call_detail_record(id) [required]
- call_flow_element(unit_id) -> unit(id) [optional (may be NULL/dangling)]
- call_flow_element_log(call_flow_element_id) -> call_flow_element(id) [required]
- call_flow_element_log(unit_progress_id) -> unit_progress(id) [required]
- choice(question_id) -> multiple_choice_question(call_flow_element_id) [optional (may be NULL/dangling)]
- community_health_worker(district_id) -> district(id) [required]
- community_health_worker(facility_id) -> facility(id) [optional (may be NULL/dangling)]
- community_health_worker(group_id) -> chw_group(id) [optional (may be NULL/dangling)]
- community_health_worker(sector_id) -> sector(id) [optional (may be NULL/dangling)]
- community_health_worker(village_id) -> village(id) [optional (may be NULL/dangling)]
- course_module(course_id) -> course(id) [required]
- course_module(module_id) -> module(id) [required]
- district(owner_id) -> user(id) [required]
- district_assignment_log(district_id) -> district(id) [optional (may be NULL/dangling)]
- district_assignment_log(facility_id) -> facility(id) [optional (may be NULL/dangling)]
- district_assignment_log(group_id) -> chw_group(id) [optional (may be NULL/dangling)]
- district_assignment_log(module_id) -> module(id) [required]
- district_assignment_log(owner_id) -> user(id) [required]
- district_assignment_log(sector_id) -> sector(id) [optional (may be NULL/dangling)]
- facility(owner_id) -> user(id) [required]
- facility(sector_id) -> sector(id) [required]
- ivr_config_call_status_map(ivr_config_id) -> ivr_config(id) [required]
- ivr_config_languages(ivr_config_id) -> ivr_config(id) [required]
- jasper_template_parameter_options(id) -> template_parameters(id) [required]
- jasper_template_supported_formats(jasper_template_id) -> jasper_templates(id) [required]
- message(call_flow_element_id) -> call_flow_element(id) [required]
- message_log(call_flow_element_log_id) -> call_flow_element_log(id) [required]
- module_assignment(assigned_modules_id) -> assigned_modules(id) [required]
- module_assignment(module_id) -> module(id) [required]
- module_progress(chw_id) -> community_health_worker(id) [required]
- module_progress(course_module_id) -> course_module(id) [required]
- multiple_choice_question(call_flow_element_id) -> call_flow_element(id) [required]
- multiple_choice_question_log(call_flow_element_log_id) -> call_flow_element_log(id) [required]
- multiple_choice_question_log(response_id) -> choice(id) [optional (may be NULL/dangling)]
- sector(district_id) -> district(id) [required]
- sector(owner_id) -> user(id) [required]
- template_parameters(template_id) -> jasper_templates(id) [required]
- unit(module_id) -> module(id) [optional (may be NULL/dangling)]
- unit_progress(module_progress_id) -> module_progress(id) [required]
- unit_progress(unit_id) -> unit(id) [required]
- user_log(user_id) -> user(id) [required]
- user_role_permissions(permission_id) -> user_permission(id) [required]
- user_role_permissions(role_id) -> user_role(id) [required]
- users_roles(role_id) -> user_role(id) [required]
- users_roles(user_id) -> user(id) [required]
- village(facility_id) -> facility(id) [required]
- village(owner_id) -> user(id) [required]

