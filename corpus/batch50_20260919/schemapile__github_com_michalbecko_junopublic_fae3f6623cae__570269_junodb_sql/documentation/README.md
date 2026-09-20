# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Michalbecko Junopublic

## Specification

PROJECT OVERVIEW

This project builds three analytical marts over the 570269_junodb.sql schema, a test-management and issue-tracking database. The specification below states, for every mart, which rows exist, what each row means, what each value is, and in what order the rows appear.

Source tables and their extraction backends. Each table named here must be extracted from the backend named beside it, and nowhere else:
- Table address is extracted from the postgres backend.
- Table billing_address is extracted from the s3 backend.
- Table client is extracted from the rest backend.
- Table issue is extracted from the s3 backend.
- Table issue_multimedia is extracted from the rest backend.
- Table log is extracted from the mongodb backend.
- Table login_picture is extracted from the rest backend.
- Table mailer is extracted from the rest backend.
- Table mailer_attachment is extracted from the mongodb backend.
- Table mailer_default is extracted from the rest backend.
- Table menuitem is extracted from the s3 backend.
- Table multimedia is extracted from the rest backend.
- Table multimedia_folder is extracted from the rest backend.
- Table project is extracted from the mongodb backend.
- Table project_role is extracted from the files backend.
- Table role is extracted from the mongodb backend.
- Table role_privilege is extracted from the files backend.
- Table settings is extracted from the files backend.
- Table tag_test_case is extracted from the postgres backend.
- Table tag_test_set is extracted from the mongodb backend.
- Table test_case is extracted from the postgres backend.
- Table test_case_multimedia is extracted from the s3 backend.
- Table test_case_run is extracted from the mongodb backend.
- Table test_plan is extracted from the postgres backend.
- Table test_plan_case is extracted from the postgres backend.
- Table test_plan_sprint is extracted from the s3 backend.
- Table test_plan_sprint_case is extracted from the s3 backend.
- Table test_plan_sprint_case_user is extracted from the postgres backend.
- Table test_set is extracted from the mongodb backend.
- Table test_step is extracted from the mongodb backend.
- Table test_step_execution is extracted from the postgres backend.
- Table user is extracted from the s3 backend.
- Table user_web_role is extracted from the rest backend.

Relationships between the source tables. Each line states a child table with its key column, the parent table with its key column, and whether the relationship is required or optional:
- Child table client with key address_id refers to parent table address with key id; this relationship is optional (may be NULL or dangling).
- Child table client with key billing_address_id refers to parent table billing_address with key id; this relationship is optional (may be NULL or dangling).
- Child table client with key multimedia_id refers to parent table multimedia with key id; this relationship is optional (may be NULL or dangling).
- Child table issue with key assigned_id refers to parent table user with key id; this relationship is optional (may be NULL or dangling).
- Child table issue with key creator_id refers to parent table user with key id; this relationship is optional (may be NULL or dangling).
- Child table issue with key project_id refers to parent table project with key id; this relationship is optional (may be NULL or dangling).
- Child table issue with key test_plan_sprint_case_id refers to parent table test_plan_sprint_case with key id; this relationship is optional (may be NULL or dangling).
- Child table issue with key test_plan_sprint_id refers to parent table test_plan_sprint with key id; this relationship is optional (may be NULL or dangling).
- Child table issue with key test_step_execution_id refers to parent table test_step_execution with key id; this relationship is optional (may be NULL or dangling).
- Child table issue with key test_step_id refers to parent table test_step with key id; this relationship is optional (may be NULL or dangling).
- Child table issue_multimedia with key issue_id refers to parent table issue with key id; this relationship is optional (may be NULL or dangling).
- Child table issue_multimedia with key multimedia_id refers to parent table multimedia with key id; this relationship is optional (may be NULL or dangling).
- Child table log with key creator_id refers to parent table user with key id; this relationship is optional (may be NULL or dangling).
- Child table mailer_attachment with key mailer_id refers to parent table mailer with key id; this relationship is optional (may be NULL or dangling).
- Child table mailer_attachment with key multimedia_id refers to parent table multimedia with key id; this relationship is optional (may be NULL or dangling).
- Child table multimedia with key multimedia_folder_id refers to parent table multimedia_folder with key id; this relationship is optional (may be NULL or dangling).
- Child table project with key creator_id refers to parent table user with key id; this relationship is required.
- Child table project_role with key project_id refers to parent table project with key id; this relationship is required.
- Child table project_role with key role_id refers to parent table role with key id; this relationship is required.
- Child table project_role with key user_id refers to parent table user with key id; this relationship is required.
- Child table role_privilege with key role_id refers to parent table role with key id; this relationship is required.
- Child table tag_test_case with key test_case_id refers to parent table test_case with key id; this relationship is optional (may be NULL or dangling).
- Child table tag_test_set with key test_set_id refers to parent table test_set with key id; this relationship is optional (may be NULL or dangling).
- Child table test_case with key creator_id refers to parent table user with key id; this relationship is required.
- Child table test_case with key test_set_id refers to parent table test_set with key id; this relationship is required.
- Child table test_case_multimedia with key multimedia_id refers to parent table multimedia with key id; this relationship is required.
- Child table test_case_multimedia with key test_case_id refers to parent table test_case with key id; this relationship is required.
- Child table test_case_run with key creator_id refers to parent table user with key id; this relationship is required.
- Child table test_case_run with key test_plan_sprint_case_id refers to parent table test_plan_sprint_case with key id; this relationship is required.
- Child table test_plan with key creator_id refers to parent table user with key id; this relationship is required.
- Child table test_plan with key project_id refers to parent table project with key id; this relationship is required.
- Child table test_plan_case with key test_case_id refers to parent table test_case with key id; this relationship is required.
- Child table test_plan_case with key test_plan_id refers to parent table test_plan with key id; this relationship is required.
- Child table test_plan_sprint with key creator_id refers to parent table user with key id; this relationship is required.
- Child table test_plan_sprint with key test_plan_id refers to parent table test_plan with key id; this relationship is required.
- Child table test_plan_sprint_case with key test_plan_case_id refers to parent table test_plan_case with key id; this relationship is required.
- Child table test_plan_sprint_case with key test_plan_sprint_id refers to parent table test_plan_sprint with key id; this relationship is required.
- Child table test_plan_sprint_case_user with key test_plan_sprint_case_id refers to parent table test_plan_sprint_case with key id; this relationship is optional (may be NULL or dangling).
- Child table test_plan_sprint_case_user with key user_id refers to parent table user with key id; this relationship is optional (may be NULL or dangling).
- Child table test_set with key creator_id refers to parent table user with key id; this relationship is optional (may be NULL or dangling).
- Child table test_set with key project_id refers to parent table project with key id; this relationship is required.
- Child table test_step with key creator_id refers to parent table user with key id; this relationship is optional (may be NULL or dangling).
- Child table test_step with key test_case_id refers to parent table test_case with key id; this relationship is required.
- Child table test_step_execution with key creator_id refers to parent table user with key id; this relationship is required.
- Child table test_step_execution with key test_plan_sprint_case_id refers to parent table test_plan_sprint_case with key id; this relationship is required.
- Child table test_step_execution with key test_step_id refers to parent table test_step with key id; this relationship is required.
- Child table user_web_role with key role_id refers to parent table role with key id; this relationship is optional (may be NULL or dangling).
- Child table user_web_role with key user_id refers to parent table user with key id; this relationship is optional (may be NULL or dangling).

Throughout, "missing" means the value is NULL. Every rounding instruction means rounding to 4 decimal places.

=======================================================================
MART 1 — user_test_case_snapshot: a per-user latest-row snapshot over linked test_case activity in the 570269_junodb.sql schema.
=======================================================================

Grain: one row per user (id), INCLUDING user rows with no linked test_case rows.

Key column: parent_key.

Rules of this mart, one passage each.

Rule 1. The source table user is read in full, supplying the population of this mart.

Rule 2. The source table test_case is read in full, supplying the activity attributed to each user row.

Rule 3. There is one row per user row of source table user, keyed by its id, and that row carries parent_key and parent_name.

Rule 4. Each user row takes in the test_case rows attributed to it, matching test_case on its creator_id against parent_key and carrying the test_case columns creator_id and id; preservation is left-sided toward the user row, so a user row with no matching test_case row is retained and receives the stated empty snapshot values.

Rule 5. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group, and each such row reports event_count and lifetime_amount over that row's matching rows.

Rule 6. For each parent_key, the single row at which the ordering measure create_date is largest survives, ties broken by the smallest id, and latest_row_id, latest_amount and latest_label are taken from that winning row; the ordering measure create_date is required on every real input row, so a non-empty group always has a winning row, and the declared defaults belong only to a group with NO rows.

Rule 7. The attributes of that extremal row sit beside the measures of the same parent_key, matching on parent_key; preservation is left-sided toward the measures, so a group with no rows at all keeps its measures.

Rule 8. The mart columns are named parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label; lifetime_amount reports its declared default of 0 — never NULL — for a group with no matching rows, and for lifetime_amount that default also applies to a group none of whose real rows carries an input value.

Rule 9. Guarded ratio, carried beside parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label: latest_amount_share is latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places, and is 0.0 when the denominator lifetime_amount is 0 or NULL. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose approved is missing, so such a row gives 0.0.

Rule 10. Deterministic output order: rows appear sorted in ascending parent_key sequence.

Output columns of user_test_case_snapshot.

parent_key (integer): the identifier of the user row; there is one row per value.

parent_name (text): the e_mail of the user row, copied unchanged.

event_count (bigint): the number of test_case rows for this user row; 0 when there are none. Every linked test_case row counts, whether or not it carries an approved value. A user row kept with no test_case row reports 0 here, never 1: its placeholder holds no test_case row to count.

lifetime_amount (integer): the total of approved over all matching test_case rows; 0 when there are no rows and when none of those rows carries an approved value; a row with no approved value adds nothing, so a group with some values totals the values it has.

latest_row_id (integer): the id of the row with the latest create_date; ties take the smallest id. It is 0 when there are no rows. Every test_case row of the user row ranks, whether or not it carries a approved value: the latest create_date wins even when that row's approved is missing.

latest_amount (integer): approved from that same latest row; 0 when there are no rows or when the winning value is missing.

latest_label (text): description from that same latest row; the literal '(none)' when there are no rows or when the winning value is missing.

latest_amount_share (float): latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose approved is missing, so such a row gives 0.0.

=======================================================================
MART 2 — test_step_issue_distribution: a per-(test_step, measure state) distribution of linked issue activity in the 570269_junodb.sql schema.
=======================================================================

Grain: one row per (id, measure state) pair represented among linked issue rows; the absent state includes missing issue_type_id values and a no-activity row for a test_step row with no links. A linked issue row whose issue_type_id has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state.

Rules of this mart, one passage each.

Rule 1. The source table test_step is read in full, supplying the entities of this mart.

Rule 2. The source table issue is read in full, supplying the activity attributed to each test_step entity.

Rule 3. Each id of source table test_step and its expected_result are carried into the measure-state calculation as entity_key and entity_name.

Rule 4. The linked issue rows are brought into each test_step entity, matching issue on its test_step_id against entity_key and carrying entity_key, entity_name, the issue id and test_step_id; preservation is left-sided toward the test_step entity, so an entity with no linked row is retained and its absent state is visible.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are the rows that survive this condition: a real issue row whose issue_type_id has a value.

Rule 6. There is one row per test_step entity that has at least one row in the present measure state, and no row here for an entity with none; each such row carries entity_key and entity_name and reports row_count as the row count, distinct_amount_count as how many different non-missing issue_type_id values occur (each different value counted once, however many rows repeat it), total_amount as the total issue_type_id, and max_amount as the largest issue_type_id.

Rule 7. Beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures — carried as entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state holding the present measure state, the literal value 'present'.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are the rows where issue_type_id is missing, including the retained placeholder for a test_step row with no issue rows; a real issue row whose issue_type_id has a value belongs only to the present state and never to this absent state.

Rule 10. There is one row per test_step entity that has at least one row in the absent measure state, and no row here for an entity with none; each such row carries entity_key and entity_name and reports row_count as the row count, distinct_amount_count as how many different non-missing issue_type_id values occur (each different value counted once, however many rows repeat it), total_amount as the total issue_type_id, and max_amount as the largest issue_type_id.

Rule 11. Beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures — carried as entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state holding the absent measure state, the literal value 'absent'.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other; all rows of both are kept.

Rule 14. Deterministic output order: rows appear sorted in ascending entity_key sequence, and within one entity in ascending measure_state sequence.

Output columns of test_step_issue_distribution.

entity_key (integer): the identifier of the test_step row.

measure_state (text): 'present' for a linked issue row whose issue_type_id has a value; 'absent' when issue_type_id is missing, including a test_step row with no linked issue row. A linked issue row whose issue_type_id has a value belongs only to the present state and never to the absent state.

entity_name (text): the expected_result of the test_step row, copied unchanged.

row_count (bigint): the number of linked issue rows in this entity/state cell; an absent cell holding real issue rows whose issue_type_id is missing COUNTS those rows, and only the placeholder cell of a test_step row with no linked issue row at all reports 0.

distinct_amount_count (bigint): the number of unique non-missing issue_type_id values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no issue_type_id value at all — both for a test_step row with no linked issue row and for an absent cell whose rows all have a missing issue_type_id.

total_amount (integer): the total of issue_type_id in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an issue_type_id value.

max_amount (integer): the largest issue_type_id in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an issue_type_id value.

max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

=======================================================================
MART 3 — test_set_test_case_top: per-test_set extremes over linked test_case rows in the 570269_junodb.sql schema — WHICH row is largest, not how large it is.
=======================================================================

Grain: one row per test_set (id), INCLUDING test_set rows with no linked test_case rows.

Key column: parent_key.

Rules of this mart, one passage each.

Rule 1. The source table test_set is read in full, supplying the population of this mart.

Rule 2. The source table test_case is read in full, supplying the rows attributed to each test_set row.

Rule 3. There is one row per test_set row of source table test_set, keyed by its id, and that row carries parent_key and parent_name.

Rule 4. Each test_set row takes in the test_case rows attributed to it, matching test_case on its test_set_id against parent_key and carrying the test_case columns test_set_id and id; preservation is left-sided toward the test_set row, so a test_set row with no test_case rows still appears, with the declared defaults.

Rule 5. Within each parent_key group the matched rows are ranked under an explicit total order — the measure approved first, then the declared tie-break — so that the extremal row is a function of the input and not of row order.

Rule 6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group, and each such row reports top_measure, tied_count, child_count and total_measure over that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7. For each parent_key, the single row at which the ordering measure approved is largest survives, ties broken by the smallest description under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no description value sorts after every row that has one — then by the smallest id, and top_label and top_row_id are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 8. The attributes of that extremal row sit beside the measures of the same parent_key, matching on parent_key; preservation is left-sided toward the measures, so a group with no rows at all keeps its measures.

Rule 9. The mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults of 0 — never NULL — for a group with no matching rows, and for top_measure and total_measure that default also applies to a group none of whose real rows carries an input value.

Rule 10. Guarded ratio, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when the denominator total_measure is 0 or NULL.

Rule 11. tie_state, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, is 'empty' when no row holds a maximum at all — the parent has no test_case rows, or none of its rows carries a approved value — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; equivalently tie_state follows tied_count, 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more, and it is never null or blank. A row holds the maximum only when it carries a approved value equal to the largest approved value among the parent's rows; a row with no approved value never holds the maximum, so a parent whose test_case rows all lack a approved value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

Rule 12. Deterministic output order: rows appear sorted in ascending parent_key sequence.

Output columns of test_set_test_case_top.

parent_key (integer): the identifier of the test_set row; there is one row per value.

parent_name (text): the description of the test_set row, copied unchanged.

top_measure (integer): the largest approved itself; 0 when the parent has no test_case rows, and 0 when none of its rows carries a approved value.

tied_count (bigint): how many test_case rows are tied at that largest approved. It is 1 when exactly one row carries that largest approved; 0 when there are no rows or when none of the rows carries a approved value; a row with no approved value never ties: only a row whose approved value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.

child_count (bigint): the number of test_case rows for this test_set row; 0 when there are none. Every linked test_case row counts, whether or not it carries an approved value. A test_set row kept with no test_case row reports 0 here, never 1: its placeholder holds no test_case row to count.

total_measure (integer): the total of approved over all of them; 0 when the parent has no test_case rows, and 0 when none of its rows carries a approved value (rows with no approved value add nothing).

top_label (text): the description of the test_case row with the LARGEST approved for this test_set row. Ties in approved are broken by taking the SMALLEST description under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no description value sorts after every labelled row; rows tied on both are resolved by the smallest id. A row with no approved value still ranks, after every row that has one, so a parent holding at least one test_case row always has a winning row — when NONE of its rows carries a approved value the winner is the one the tie-break alone selects, not the no-rows default. It is the literal '(none)' when the parent has no test_case rows at all, and '(none)' when the winning row has no description value.

top_row_id (integer): the id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real test_case row whenever the parent has any. This includes when none of them carries a approved value. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.

top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.

tie_state (text): 'empty' when no row holds a maximum at all — the parent has no test_case rows, or none of its rows carries a approved value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a approved value equal to the largest approved value among the parent's rows; a row with no approved value never holds the maximum. So a parent whose test_case rows all lack a approved value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `user_test_case_snapshot`

- Grain: One row per user (id), INCLUDING user rows with no linked test_case rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share

```text
Mart 'user_test_case_snapshot' has 10 declared semantic rules:
1. [source] Read source table user. (public source tables: user)
2. [source] Read source table test_case. (public source tables: test_case)
3. [derive] One row per user row, keyed by id. (public source tables: user | public carried/output columns: parent_key, parent_name)
4. [join] Bring in test_case; a user row with no matching test_case row is retained and receives the stated empty snapshot values. (public source tables: test_case | public carried/output columns: creator_id, id | join preservation: left | condition public identifiers: test_case, creator_id, parent_key)
5. [aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count, lifetime_amount for that row's matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount)
6. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest id, and take latest_row_id, latest_amount, latest_label from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, latest_row_id, latest_amount, latest_label)
7. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
8. [derive] Name the mart columns; lifetime_amount reports its declared default — never NULL — for a group with no matching rows. For lifetime_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label)
9. [ratio] Guarded ratios: latest_amount_share — latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose approved is missing, so such a row gives 0.0. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `test_step_issue_distribution`

- Grain: One row per (id, measure state) pair represented among linked issue rows; the absent state includes missing issue_type_id values and a no-activity row for a test_step row with no links. A linked issue row whose issue_type_id has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'test_step_issue_distribution' has 14 declared semantic rules:
1. [source] Read source table test_step. (public source tables: test_step)
2. [source] Read source table issue. (public source tables: issue)
3. [derive] Carry each id and its expected_result into the measure-state calculation. (public source tables: test_step | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked issue rows into each test_step entity; retain an entity with no linked row so its absent state is visible. (public source tables: issue | public carried/output columns: entity_key, entity_name, id, test_step_id | join preservation: left | condition public identifiers: issue, test_step_id, entity_key)
5. [filter] Keep the present measure-state rows: a real issue row whose issue_type_id has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per test_step entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing issue_type_id values occur (each different value counted once, however many rows repeat it), total issue_type_id, and largest issue_type_id. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: issue_type_id is missing, including the retained placeholder for a test_step row with no issue rows. A real issue row whose issue_type_id has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per test_step entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing issue_type_id values occur (each different value counted once, however many rows repeat it), total issue_type_id, and largest issue_type_id. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `test_set_test_case_top`

- Grain: One row per test_set (id), INCLUDING test_set rows with no linked test_case rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'test_set_test_case_top' has 12 declared semantic rules:
1. [source] Read source table test_set. (public source tables: test_set)
2. [source] Read source table test_case. (public source tables: test_case)
3. [derive] One row per test_set row, keyed by id. (public source tables: test_set | public carried/output columns: parent_key, parent_name)
4. [join] Bring in test_case: a test_set row with no test_case rows still appears, with the declared defaults. (public source tables: test_case | public carried/output columns: test_set_id, id | join preservation: left | condition public identifiers: test_case, test_set_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest description under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no description value sorts after every row that has one), then the smallest id, and take top_label, top_row_id from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no test_case rows, or none of its rows carries a approved value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a approved value equal to the largest approved value among the parent's rows; a row with no approved value never holds the maximum. So a parent whose test_case rows all lack a approved value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### address  (source backend: postgres)
Source table address.

- `city`: text NULL — Column city of table address.
- `id`: integer NOT NULL — Column id of table address.
- `psc`: text NULL — Column psc of table address.
- `state`: text NULL — Column state of table address.
- `street`: text NULL — Column street of table address.
- `type`: text NULL — Column type of table address.
- primary key: id

### billing_address  (source backend: s3)
Source table billing_address.

- `city`: text NULL — Column city of table billing_address.
- `company_name`: text NULL — Column company_name of table billing_address.
- `dic`: text NULL — Column dic of table billing_address.
- `icdph`: text NULL — Column icdph of table billing_address.
- `ico`: text NULL — Column ico of table billing_address.
- `id`: integer NOT NULL — Column id of table billing_address.
- `psc`: text NULL — Column psc of table billing_address.
- `state`: text NULL — Column state of table billing_address.
- `street`: text NULL — Column street of table billing_address.
- primary key: id

### client  (source backend: rest)
Source table client.

- `address_id`: integer NULL — Column address_id of table client.
- `billing_address_id`: integer NULL — Column billing_address_id of table client.
- `e_mail`: text NULL — Column e_mail of table client.
- `favicon_id`: integer NULL — Column favicon_id of table client.
- `id`: integer NOT NULL — Column id of table client.
- `multimedia_id`: integer NULL — Column multimedia_id of table client.
- `name`: text NULL — Column name of table client.
- `telephone_number`: text NULL — Column telephone_number of table client.
- primary key: id

### issue  (source backend: s3)
Source table issue.

- `assigned_id`: integer NULL — Column assigned_id of table issue.
- `create_date`: timestamp NOT NULL — Column create_date of table issue.
- `creator_id`: integer NULL — Column creator_id of table issue.
- `description`: text NULL — Column description of table issue.
- `id`: integer NOT NULL — Column id of table issue.
- `issue_type_id`: integer NULL — Column issue_type_id of table issue.
- `name`: text NULL — Column name of table issue.
- `priority_id`: integer NULL — Column priority_id of table issue.
- `project_id`: integer NULL — Column project_id of table issue.
- `status`: integer NULL — Column status of table issue.
- `test_plan_sprint_case_id`: integer NULL — Column test_plan_sprint_case_id of table issue.
- `test_plan_sprint_id`: integer NULL — Column test_plan_sprint_id of table issue.
- `test_step_execution_id`: integer NULL — Column test_step_execution_id of table issue.
- `test_step_id`: integer NULL — Column test_step_id of table issue.
- primary key: id

### issue_multimedia  (source backend: rest)
Source table issue_multimedia.

- `id`: integer NOT NULL — Column id of table issue_multimedia.
- `issue_id`: integer NULL — Column issue_id of table issue_multimedia.
- `multimedia_id`: integer NULL — Column multimedia_id of table issue_multimedia.
- primary key: id

### log  (source backend: mongodb)
Source table log.

- `action_id`: integer NULL — Column action_id of table log.
- `create_date`: timestamp NOT NULL — Column create_date of table log.
- `creator_id`: integer NULL — Column creator_id of table log.
- `description`: text NULL — Column description of table log.
- `id`: integer NOT NULL — Column id of table log.
- `ip`: text NULL — Column ip of table log.
- `privilege_id`: integer NULL — Column privilege_id of table log.
- `tab_id`: integer NULL — Column tab_id of table log.
- primary key: id

### login_picture  (source backend: rest)
Source table login_picture.

- `id`: integer NOT NULL — Column id of table login_picture.
- `multimedia_id`: integer NULL — Column multimedia_id of table login_picture.
- primary key: id

### mailer  (source backend: rest)
Source table mailer.

- `body`: text NULL — Column body of table mailer.
- `id`: integer NOT NULL — Column id of table mailer.
- `recipient`: text NULL — Column recipient of table mailer.
- `sent_date`: timestamp NOT NULL — Column sent_date of table mailer.
- `subject`: text NULL — Column subject of table mailer.
- primary key: id

### mailer_attachment  (source backend: mongodb)
Source table mailer_attachment.

- `id`: integer NOT NULL — Column id of table mailer_attachment.
- `mailer_id`: integer NULL — Column mailer_id of table mailer_attachment.
- `multimedia_id`: integer NULL — Column multimedia_id of table mailer_attachment.
- primary key: id

### mailer_default  (source backend: rest)
Source table mailer_default.

- `body`: text NULL — Column body of table mailer_default.
- `id`: integer NOT NULL — Column id of table mailer_default.
- `name`: text NULL — Column name of table mailer_default.
- `subject`: text NULL — Column subject of table mailer_default.
- primary key: id

### menuitem  (source backend: s3)
Source table menuitem.

- `glyphicon`: text NULL — Column glyphicon of table menuitem.
- `id`: integer NOT NULL — Column id of table menuitem.
- `link`: text NULL — Column link of table menuitem.
- `menuitem_id`: integer NULL — Column menuitem_id of table menuitem.
- `name`: text NULL — Column name of table menuitem.
- `privilege_id`: integer NOT NULL — Column privilege_id of table menuitem.
- `sort`: integer NOT NULL — Column sort of table menuitem.
- primary key: id

### multimedia  (source backend: rest)
Source table multimedia.

- `datein`: timestamp NOT NULL — Column datein of table multimedia.
- `id`: integer NOT NULL — Column id of table multimedia.
- `multimedia_folder_id`: integer NULL — Column multimedia_folder_id of table multimedia.
- `name`: text NULL — Column name of table multimedia.
- `path`: text NULL — Column path of table multimedia.
- `size`: text NULL — Column size of table multimedia.
- `type`: text NULL — Column type of table multimedia.
- primary key: id

### multimedia_folder  (source backend: rest)
Source table multimedia_folder.

- `datein`: timestamp NOT NULL — Column datein of table multimedia_folder.
- `id`: integer NOT NULL — Column id of table multimedia_folder.
- `name`: text NULL — Column name of table multimedia_folder.
- primary key: id

### project  (source backend: mongodb)
Source table project.

- `create_date`: timestamp NOT NULL — Column create_date of table project.
- `creator_id`: integer NOT NULL — Column creator_id of table project.
- `description`: text NULL — Column description of table project.
- `id`: integer NOT NULL — Column id of table project.
- `name`: text NOT NULL — Column name of table project.
- `name_safe`: text NOT NULL — Column name_safe of table project.
- primary key: id

### project_role  (source backend: files)
Source table project_role.

- `id`: integer NOT NULL — Column id of table project_role.
- `project_id`: integer NOT NULL — Column project_id of table project_role.
- `role_id`: integer NOT NULL — Column role_id of table project_role.
- `user_id`: integer NOT NULL — Column user_id of table project_role.
- primary key: id

### role  (source backend: mongodb)
Source table role.

- `id`: integer NOT NULL — Column id of table role.
- `is_for_project`: integer NULL — Column is_for_project of table role.
- `name`: text NOT NULL — Column name of table role.
- `name_safe`: text NOT NULL — Column name_safe of table role.
- primary key: id

### role_privilege  (source backend: files)
Source table role_privilege.

- `id`: integer NOT NULL — Column id of table role_privilege.
- `privilege_id`: integer NOT NULL — Column privilege_id of table role_privilege.
- `role_id`: integer NOT NULL — Column role_id of table role_privilege.
- primary key: id

### settings  (source backend: files)
Source table settings.

- `date_update`: timestamp NOT NULL — Column date_update of table settings.
- `description`: text NULL — Column description of table settings.
- `id`: integer NOT NULL — Column id of table settings.
- `option`: text NULL — Column option of table settings.
- primary key: id

### tag_test_case  (source backend: postgres)
Source table tag_test_case.

- `end_date`: date NULL — Column end_date of table tag_test_case.
- `id`: integer NOT NULL — Column id of table tag_test_case.
- `name`: text NULL — Column name of table tag_test_case.
- `start_date`: date NULL — Column start_date of table tag_test_case.
- `test_case_id`: integer NULL — Column test_case_id of table tag_test_case.
- primary key: id

### tag_test_set  (source backend: mongodb)
Source table tag_test_set.

- `id`: integer NOT NULL — Column id of table tag_test_set.
- `name`: text NULL — Column name of table tag_test_set.
- `test_set_id`: integer NULL — Column test_set_id of table tag_test_set.
- primary key: id

### test_case  (source backend: postgres)
Source table test_case.

- `approved`: integer NULL — Column approved of table test_case.
- `create_date`: timestamp NOT NULL — Column create_date of table test_case.
- `creator_id`: integer NOT NULL — Column creator_id of table test_case.
- `description`: text NULL — Column description of table test_case.
- `id`: integer NOT NULL — Column id of table test_case.
- `name`: text NULL — Column name of table test_case.
- `priority`: integer NULL — Column priority of table test_case.
- `result`: text NULL — Column result of table test_case.
- `test_set_id`: integer NOT NULL — Column test_set_id of table test_case.
- primary key: id

### test_case_multimedia  (source backend: s3)
Source table test_case_multimedia.

- `id`: integer NOT NULL — Column id of table test_case_multimedia.
- `multimedia_id`: integer NOT NULL — Column multimedia_id of table test_case_multimedia.
- `test_case_id`: integer NOT NULL — Column test_case_id of table test_case_multimedia.
- primary key: id

### test_case_run  (source backend: mongodb)
Source table test_case_run.

- `creator_id`: integer NOT NULL — Column creator_id of table test_case_run.
- `endtime`: timestamp NULL — Column endtime of table test_case_run.
- `id`: integer NOT NULL — Column id of table test_case_run.
- `starttime`: timestamp NOT NULL — Column starttime of table test_case_run.
- `test_plan_sprint_case_id`: integer NOT NULL — Column test_plan_sprint_case_id of table test_case_run.
- primary key: id

### test_plan  (source backend: postgres)
Source table test_plan.

- `create_date`: timestamp NOT NULL — Column create_date of table test_plan.
- `creator_id`: integer NOT NULL — Column creator_id of table test_plan.
- `id`: integer NOT NULL — Column id of table test_plan.
- `name`: text NOT NULL — Column name of table test_plan.
- `project_id`: integer NOT NULL — Column project_id of table test_plan.
- primary key: id

### test_plan_case  (source backend: postgres)
Source table test_plan_case.

- `id`: integer NOT NULL — Column id of table test_plan_case.
- `test_case_id`: integer NOT NULL — Column test_case_id of table test_plan_case.
- `test_plan_id`: integer NOT NULL — Column test_plan_id of table test_plan_case.
- primary key: id

### test_plan_sprint  (source backend: s3)
Source table test_plan_sprint.

- `create_date`: timestamp NOT NULL — Column create_date of table test_plan_sprint.
- `creator_id`: integer NOT NULL — Column creator_id of table test_plan_sprint.
- `end_date`: timestamp NOT NULL — Column end_date of table test_plan_sprint.
- `id`: integer NOT NULL — Column id of table test_plan_sprint.
- `name`: text NOT NULL — Column name of table test_plan_sprint.
- `start_date`: timestamp NOT NULL — Column start_date of table test_plan_sprint.
- `test_plan_id`: integer NOT NULL — Column test_plan_id of table test_plan_sprint.
- primary key: id

### test_plan_sprint_case  (source backend: s3)
Source table test_plan_sprint_case.

- `forced_status_id`: integer NULL — Column forced_status_id of table test_plan_sprint_case.
- `id`: integer NOT NULL — Column id of table test_plan_sprint_case.
- `status_id`: integer NULL — Column status_id of table test_plan_sprint_case.
- `test_plan_case_id`: integer NOT NULL — Column test_plan_case_id of table test_plan_sprint_case.
- `test_plan_sprint_id`: integer NOT NULL — Column test_plan_sprint_id of table test_plan_sprint_case.
- primary key: id

### test_plan_sprint_case_user  (source backend: postgres)
Source table test_plan_sprint_case_user.

- `id`: integer NOT NULL — Column id of table test_plan_sprint_case_user.
- `test_plan_sprint_case_id`: integer NULL — Column test_plan_sprint_case_id of table test_plan_sprint_case_user.
- `user_id`: integer NULL — Column user_id of table test_plan_sprint_case_user.
- primary key: id

### test_set  (source backend: mongodb)
Source table test_set.

- `create_date`: timestamp NOT NULL — Column create_date of table test_set.
- `creator_id`: integer NULL — Column creator_id of table test_set.
- `description`: text NULL — Column description of table test_set.
- `id`: integer NOT NULL — Column id of table test_set.
- `name`: text NULL — Column name of table test_set.
- `project_id`: integer NOT NULL — Column project_id of table test_set.
- primary key: id

### test_step  (source backend: mongodb)
Source table test_step.

- `create_date`: timestamp NOT NULL — Column create_date of table test_step.
- `creator_id`: integer NULL — Column creator_id of table test_step.
- `expected_result`: text NULL — Column expected_result of table test_step.
- `id`: integer NOT NULL — Column id of table test_step.
- `precondition`: text NULL — Column precondition of table test_step.
- `test_case_id`: integer NOT NULL — Column test_case_id of table test_step.
- primary key: id

### test_step_execution  (source backend: postgres)
Source table test_step_execution.

- `create_date`: timestamp NOT NULL — Column create_date of table test_step_execution.
- `creator_id`: integer NOT NULL — Column creator_id of table test_step_execution.
- `id`: integer NOT NULL — Column id of table test_step_execution.
- `status_id`: integer NOT NULL — Column status_id of table test_step_execution.
- `test_plan_sprint_case_id`: integer NOT NULL — Column test_plan_sprint_case_id of table test_step_execution.
- `test_step_id`: integer NOT NULL — Column test_step_id of table test_step_execution.
- primary key: id

### user  (source backend: s3)
Source table user.

- `archive`: integer NULL — Column archive of table user.
- `date`: timestamp NOT NULL — Column date of table user.
- `e_mail`: text NULL — Column e_mail of table user.
- `id`: integer NOT NULL — Column id of table user.
- `name`: text NULL — Column name of table user.
- `password`: text NULL — Column password of table user.
- `phone_number`: text NULL — Column phone_number of table user.
- `super_admin`: integer NULL — Column super_admin of table user.
- `surname`: text NULL — Column surname of table user.
- primary key: id

### user_web_role  (source backend: rest)
Source table user_web_role.

- `id`: integer NOT NULL — Column id of table user_web_role.
- `role_id`: integer NULL — Column role_id of table user_web_role.
- `user_id`: integer NULL — Column user_id of table user_web_role.
- primary key: id

### Relationships

- client(address_id) -> address(id) [optional (may be NULL/dangling)]
- client(billing_address_id) -> billing_address(id) [optional (may be NULL/dangling)]
- client(multimedia_id) -> multimedia(id) [optional (may be NULL/dangling)]
- issue(assigned_id) -> user(id) [optional (may be NULL/dangling)]
- issue(creator_id) -> user(id) [optional (may be NULL/dangling)]
- issue(project_id) -> project(id) [optional (may be NULL/dangling)]
- issue(test_plan_sprint_case_id) -> test_plan_sprint_case(id) [optional (may be NULL/dangling)]
- issue(test_plan_sprint_id) -> test_plan_sprint(id) [optional (may be NULL/dangling)]
- issue(test_step_execution_id) -> test_step_execution(id) [optional (may be NULL/dangling)]
- issue(test_step_id) -> test_step(id) [optional (may be NULL/dangling)]
- issue_multimedia(issue_id) -> issue(id) [optional (may be NULL/dangling)]
- issue_multimedia(multimedia_id) -> multimedia(id) [optional (may be NULL/dangling)]
- log(creator_id) -> user(id) [optional (may be NULL/dangling)]
- mailer_attachment(mailer_id) -> mailer(id) [optional (may be NULL/dangling)]
- mailer_attachment(multimedia_id) -> multimedia(id) [optional (may be NULL/dangling)]
- multimedia(multimedia_folder_id) -> multimedia_folder(id) [optional (may be NULL/dangling)]
- project(creator_id) -> user(id) [required]
- project_role(project_id) -> project(id) [required]
- project_role(role_id) -> role(id) [required]
- project_role(user_id) -> user(id) [required]
- role_privilege(role_id) -> role(id) [required]
- tag_test_case(test_case_id) -> test_case(id) [optional (may be NULL/dangling)]
- tag_test_set(test_set_id) -> test_set(id) [optional (may be NULL/dangling)]
- test_case(creator_id) -> user(id) [required]
- test_case(test_set_id) -> test_set(id) [required]
- test_case_multimedia(multimedia_id) -> multimedia(id) [required]
- test_case_multimedia(test_case_id) -> test_case(id) [required]
- test_case_run(creator_id) -> user(id) [required]
- test_case_run(test_plan_sprint_case_id) -> test_plan_sprint_case(id) [required]
- test_plan(creator_id) -> user(id) [required]
- test_plan(project_id) -> project(id) [required]
- test_plan_case(test_case_id) -> test_case(id) [required]
- test_plan_case(test_plan_id) -> test_plan(id) [required]
- test_plan_sprint(creator_id) -> user(id) [required]
- test_plan_sprint(test_plan_id) -> test_plan(id) [required]
- test_plan_sprint_case(test_plan_case_id) -> test_plan_case(id) [required]
- test_plan_sprint_case(test_plan_sprint_id) -> test_plan_sprint(id) [required]
- test_plan_sprint_case_user(test_plan_sprint_case_id) -> test_plan_sprint_case(id) [optional (may be NULL/dangling)]
- test_plan_sprint_case_user(user_id) -> user(id) [optional (may be NULL/dangling)]
- test_set(creator_id) -> user(id) [optional (may be NULL/dangling)]
- test_set(project_id) -> project(id) [required]
- test_step(creator_id) -> user(id) [optional (may be NULL/dangling)]
- test_step(test_case_id) -> test_case(id) [required]
- test_step_execution(creator_id) -> user(id) [required]
- test_step_execution(test_plan_sprint_case_id) -> test_plan_sprint_case(id) [required]
- test_step_execution(test_step_id) -> test_step(id) [required]
- user_web_role(role_id) -> role(id) [optional (may be NULL/dangling)]
- user_web_role(user_id) -> user(id) [optional (may be NULL/dangling)]

