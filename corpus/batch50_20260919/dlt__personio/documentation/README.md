# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# dlt extraction: personio

## Specification

PROJECT OVERVIEW

This project builds four analytical marts from data extracted by a dlt pipeline against the Personio API. Every source table below must be read from the extraction backend named beside it, and only from that backend.

Source tables and their extraction backends:
- Source table absence_types must be extracted from the rest backend. It holds the dlt resource 'absence_types' at /company/time-off-types with write_disposition=replace; its columns are id (bigint, the primary key of the resource) and payload (json, nullable, the raw endpoint record body). Its primary key is id.
- Source table absences must be extracted from the s3 backend. It holds the dlt resource 'absences' at /company/time-offs with write_disposition=merge and the incremental cursor updated_at; its columns are id (bigint, primary key), updated_at (timestamp, the incremental cursor at dlt cursor path 'updated_at') and payload (json, nullable). Its primary key is id.
- Source table attendances must be extracted from the s3 backend. It holds the dlt resource 'attendances' at /company/attendances with write_disposition=merge and the incremental cursor updated_at; its columns are id (bigint, primary key), updated_at (timestamp, the incremental cursor at dlt cursor path 'updated_at') and payload (json, nullable). Its primary key is id.
- Source table custom_reports must be extracted from the s3 backend. It holds the dlt transformer 'custom_reports' at /custom_reports with write_disposition=merge; its columns are report_id (bigint), item_id (bigint), _custom_reports_list_id (bigint, the parent link to custom_reports_list), report_figure (decimal, nullable), report_caption (text, nullable) and payload (json, nullable). Its primary key is the pair report_id, item_id.
- Source table custom_reports_list must be extracted from the postgres backend. It holds the dlt resource 'custom_reports_list' at /company/custom-reports/reports with write_disposition=replace; its columns are id (bigint, primary key), report_headline (text, nullable) and payload (json, nullable). Its primary key is id.
- Source table document_categories must be extracted from the s3 backend. It holds the dlt resource 'document_categories' at /company/document-categories with write_disposition=replace; its columns are id (bigint, primary key) and payload (json, nullable). Its primary key is id.
- Source table employees must be extracted from the postgres backend. It holds the dlt resource 'employees' at /company/employees with write_disposition=merge and the incremental cursor last_modified_at; its columns are id (bigint, primary key), last_modified_at (timestamp, the incremental cursor at dlt cursor path 'last_modified_at'), employee_division (text, nullable) and payload (json, nullable). Its primary key is id.
- Source table employees_absences_balance must be extracted from the mongodb backend. It holds the dlt transformer 'employees_absences_balance' at /employees_absences_balance with write_disposition=merge; its columns are employee_id (bigint), id (bigint), _employees_id (bigint, the parent link to employees), absence_balance_days (decimal, nullable), absence_policy_headline (text, nullable) and payload (json, nullable). Its primary key is the pair employee_id, id.
- Source table projects must be extracted from the files backend. It holds the dlt resource 'projects' at /company/attendances/projects with write_disposition=replace; its columns are id (bigint, primary key) and payload (json, nullable). Its primary key is id.

Relationships between the source tables:
- The child table custom_reports, through its key _custom_reports_list_id, refers to the parent table custom_reports_list, through its key id. This relationship is required.
- The child table employees_absences_balance, through its key _employees_id, refers to the parent table employees, through its key id. This relationship is required.

Throughout, "a linked employees_absences_balance row" of an employees row means an employees_absences_balance row whose _employees_id equals that employees row's id.

=== Mart employees_absences_balance_distribution: per-(employees, measure state) distribution of extracted employees_absences_balance records ===

Grain: one row per (id, measure state) pair represented among linked employees_absences_balance rows; the absent state includes missing absence_balance_days values and a no-activity row for a employees row with no links. A linked employees_absences_balance row whose absence_balance_days has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state together identify an output row.

Output columns:
- entity_key (bigint): the identifier of the employees row.
- measure_state (text): 'present' for a linked employees_absences_balance row whose absence_balance_days has a value; 'absent' when absence_balance_days is missing, including a employees row with no linked employees_absences_balance row. A linked employees_absences_balance row whose absence_balance_days has a value belongs only to the present state and never to the absent state.
- entity_name (text): the employee_division of the employees row, copied unchanged.
- row_count (bigint): the number of linked employees_absences_balance rows in this entity/state cell; an absent cell holding real employees_absences_balance rows whose absence_balance_days is missing COUNTS those rows, and only the placeholder cell of an employees row with no linked employees_absences_balance row at all reports 0.
- distinct_amount_count (bigint): the number of unique non-missing absence_balance_days values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no absence_balance_days value at all — both for an employees row with no linked employees_absences_balance row and for an absent cell whose rows all have a missing absence_balance_days.
- total_amount (decimal): the total of absence_balance_days in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an absence_balance_days value.
- max_amount (decimal): the largest absence_balance_days in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an absence_balance_days value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules:

1. The source table employees is read.

2. The source table employees_absences_balance is read.

3. From source table employees, each id is carried into the measure-state calculation as entity_key and its employee_division is carried as entity_name.

4. The linked employees_absences_balance rows are brought into each employees entity, matching employees_absences_balance rows by _employees_id to entity_key; preservation is left-sided, so an entity with no linked row is retained so its absent state is visible, carrying entity_key, entity_name, id and _employees_id.

5. The present measure-state rows are kept: a real employees_absences_balance row whose absence_balance_days has a value, carrying entity_key and entity_name.

6. Among the present measure-state rows there is one row per employees entity that has at least one row in the present measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount — that is, the row count, how many different non-missing absence_balance_days values occur (each different value counted once, however many rows repeat it), the total absence_balance_days, and the largest absence_balance_days.

7. For those present-state rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0. The row then carries entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

8. These measures are labelled as the present measure state, so measure_state reads 'present' on them, and the row carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

9. The absent measure-state rows are kept: absence_balance_days is missing, including the retained placeholder for a employees row with no employees_absences_balance rows, carrying entity_key and entity_name. A real employees_absences_balance row whose absence_balance_days has a value belongs only to the present state and never to this absent state.

10. Among the absent measure-state rows there is one row per employees entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount — that is, the row count, how many different non-missing absence_balance_days values occur (each different value counted once, however many rows repeat it), the total absence_balance_days, and the largest absence_balance_days.

11. For those absent-state rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0. The row then carries entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

12. These measures are labelled as the absent measure state, so measure_state reads 'absent' on them, and the row carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

13. The present-state summary and the absent-state summary are stacked into one output list, keeping all rows of both: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other; each such row carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

14. Deterministic output order: rows appear in ascending entity_key order, then in ascending measure_state order.

=== Mart employees_absences_balance_top: per-employees extremes over extracted employees_absences_balance records — WHICH record is largest, not how large it is ===

Grain: one row per employees (id), INCLUDING employees rows with no linked employees_absences_balance rows.

Key column: parent_key.

Output columns:
- parent_key (bigint): the identifier of the employees row. One row per value.
- parent_name (text): the employee_division of the employees row, copied unchanged.
- top_measure (decimal): the largest absence_balance_days itself; 0 when the parent has no employees_absences_balance rows, and 0 when none of its rows carries a absence_balance_days value.
- tied_count (bigint): how many employees_absences_balance rows are tied at that largest absence_balance_days. It is 1 when exactly one row carries that largest absence_balance_days; 0 when there are no rows or when none of the rows carries a absence_balance_days value; a row with no absence_balance_days value never ties: only a row whose absence_balance_days value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.
- child_count (bigint): the number of employees_absences_balance rows for this employees row; 0 when there are none. Every linked employees_absences_balance row counts, whether or not it carries an absence_balance_days value. An employees row kept with no employees_absences_balance row reports 0 here, never 1: its placeholder holds no employees_absences_balance row to count.
- total_measure (decimal): the total of absence_balance_days over all of them; 0 when the parent has no employees_absences_balance rows, and 0 when none of its rows carries a absence_balance_days value (rows with no absence_balance_days value add nothing).
- top_label (text): the absence_policy_headline of the employees_absences_balance row with the LARGEST absence_balance_days for this employees row. Ties in absence_balance_days are broken by taking the SMALLEST absence_policy_headline under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no absence_policy_headline value sorts after every labelled row. A row with no absence_balance_days value still ranks, after every row that has one, so a parent holding at least one employees_absences_balance row always has a winning row — when NONE of its rows carries a absence_balance_days value the winner is the one the tie-break alone selects, not the no-rows default. The value is the literal '(none)' when the parent has no employees_absences_balance rows at all, and '(none)' when the winning row has no absence_policy_headline value.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no employees_absences_balance rows, or none of its rows carries a absence_balance_days value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a absence_balance_days value equal to the largest absence_balance_days value among the parent's rows; a row with no absence_balance_days value never holds the maximum. So a parent whose employees_absences_balance rows all lack a absence_balance_days value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label names.

Rules:

1. The source table employees is read.

2. The source table employees_absences_balance is read.

3. There is one row per employees row of source table employees, keyed by id, carrying parent_key and parent_name.

4. The employees_absences_balance rows are brought in, matching employees_absences_balance rows by _employees_id to parent_key and carrying _employees_id and id; preservation is left-sided, so a employees row with no employees_absences_balance rows still appears, with the declared defaults.

5. The rows brought together are ranked within each parent_key group under an explicit total order — the measure first, then the declared tie-break — so that the extremal row is a function of the input and not of row order.

6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

7. The single row per parent_key at which the ordering measure is largest is kept, ties broken by the smallest absence_policy_headline under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) and a row with no absence_policy_headline value sorting after every row that has one, and top_label is taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

8. The extremal row's attributes are attached to the grouped measures by matching on parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

9. The mart columns are named: parent_key, parent_name, top_measure, tied_count, child_count, total_measure and top_label; top_measure and total_measure report their declared defaults — never NULL — for a group with no matching rows, and for top_measure and total_measure the default also applies to a group none of whose real rows carries an input value.

10. Guarded ratios: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when the denominator total_measure is 0 or has no value; the row carries parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_measure_share.

11. tie_state is 'empty' when no row holds a maximum at all — the parent has no employees_absences_balance rows, or none of its rows carries a absence_balance_days value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do; equivalently tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more, and the row carries parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_measure_share and tie_state. A row holds the maximum only when it carries a absence_balance_days value equal to the largest absence_balance_days value among the parent's rows; a row with no absence_balance_days value never holds the maximum, so a parent whose employees_absences_balance rows all lack a absence_balance_days value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label names.

12. Deterministic output order: rows are sorted in ascending parent_key order.

=== Mart dim_employees: one row per 'employees' record extracted from the API ===

Grain: one row per employees (id).

Key column: id.

Output columns:
- id (bigint): the employees primary key column 'id'.
- employees_absences_balance_count (bigint): the number of 'employees_absences_balance' records extracted for this 'employees' (0 when none).
- last_last_modified_at (timestamp): the latest 'last_modified_at' seen for this 'employees' record (the incremental cursor of the resource).

Rules:

1. The source table employees is read.

2. The source table employees_absences_balance is read.

3. The mart key column id is formed from source table employees.

4. The employees_absences_balance rows are brought in, matching employees_absences_balance rows by _employees_id to id and carrying id and _employees_id; preservation is left-sided, so every employees record appears whether or not it has matching employees_absences_balance records.

5. There is one output row per id, reporting employees_absences_balance_count for that row's matching rows; last_last_modified_at is taken from the employees side only: its value comes from last_modified_at on the employees rows, never from the matching rows. An employees row with no matching rows still reports whatever last_modified_at holds on the employees side.

6. The mart columns are named id, employees_absences_balance_count and last_last_modified_at.

7. Deterministic output order: rows are sorted in ascending id order.

=== Mart employees_activity: daily activity summary of the 'employees' resource ===

Grain: one row per calendar day of last_modified_at.

Key column: activity_date.

Output columns:
- activity_date (date): the calendar day of 'last_modified_at' (UTC date part).
- record_count (bigint): the number of 'employees' records on that day.

Rules:

1. The source table employees is read.

2. From source table employees, the grain is activity_date — activity_date is the UTC date part of the incremental cursor 'last_modified_at'.

3. There is one output row per activity_date, reporting record_count for that row's matching rows.

4. The mart columns are named activity_date and record_count.

5. Deterministic output order: rows are sorted in ascending activity_date order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `employees_absences_balance_distribution`

- Grain: One row per (id, measure state) pair represented among linked employees_absences_balance rows; the absent state includes missing absence_balance_days values and a no-activity row for a employees row with no links. A linked employees_absences_balance row whose absence_balance_days has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'employees_absences_balance_distribution' has 14 declared semantic rules:
1. [source] Read source table employees. (public source tables: employees)
2. [source] Read source table employees_absences_balance. (public source tables: employees_absences_balance)
3. [derive] Carry each id and its employee_division into the measure-state calculation. (public source tables: employees | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked employees_absences_balance rows into each employees entity; retain an entity with no linked row so its absent state is visible. (public source tables: employees_absences_balance | public carried/output columns: entity_key, entity_name, id, _employees_id | join preservation: left | condition public identifiers: employees_absences_balance, _employees_id, entity_key)
5. [filter] Keep the present measure-state rows: a real employees_absences_balance row whose absence_balance_days has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per employees entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing absence_balance_days values occur (each different value counted once, however many rows repeat it), total absence_balance_days, and largest absence_balance_days. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: absence_balance_days is missing, including the retained placeholder for a employees row with no employees_absences_balance rows. A real employees_absences_balance row whose absence_balance_days has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per employees entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing absence_balance_days values occur (each different value counted once, however many rows repeat it), total absence_balance_days, and largest absence_balance_days. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `employees_absences_balance_top`

- Grain: One row per employees (id), INCLUDING employees rows with no linked employees_absences_balance rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_measure_share, tie_state

```text
Mart 'employees_absences_balance_top' has 12 declared semantic rules:
1. [source] Read source table employees. (public source tables: employees)
2. [source] Read source table employees_absences_balance. (public source tables: employees_absences_balance)
3. [derive] One row per employees row, keyed by id. (public source tables: employees | public carried/output columns: parent_key, parent_name)
4. [join] Bring in employees_absences_balance: a employees row with no employees_absences_balance rows still appears, with the declared defaults. (public source tables: employees_absences_balance | public carried/output columns: _employees_id, id | join preservation: left | condition public identifiers: employees_absences_balance, _employees_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest absence_policy_headline under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no absence_policy_headline value sorts after every row that has one), and take top_label from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no employees_absences_balance rows, or none of its rows carries a absence_balance_days value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a absence_balance_days value equal to the largest absence_balance_days value among the parent's rows; a row with no absence_balance_days value never holds the maximum. So a parent whose employees_absences_balance rows all lack a absence_balance_days value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label names. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `dim_employees`

- Grain: one row per employees (id)
- Unique key: id
- Required columns: id, employees_absences_balance_count, last_last_modified_at

```text
Mart 'dim_employees' has 7 declared semantic rules:
1. [source] Read source table employees. (public source tables: employees)
2. [source] Read source table employees_absences_balance. (public source tables: employees_absences_balance)
3. [derive] Form the mart key columns id from source table employees. (public source tables: employees | public carried/output columns: id)
4. [join] Bring in employees_absences_balance: every employees record appears whether or not it has matching employees_absences_balance records. (public source tables: employees_absences_balance | public carried/output columns: id, _employees_id | join preservation: left | condition public identifiers: employees_absences_balance, _employees_id, id)
5. [aggregate] One output row per id, reporting employees_absences_balance_count for that row's matching rows. last_last_modified_at is taken from the employees side only: its value comes from last_modified_at on the employees rows, never from the matching rows. An employees row with no matching rows still reports whatever last_modified_at holds on the employees side. (public carried/output columns: id, employees_absences_balance_count, last_last_modified_at)
6. [derive] Name the mart columns. (public carried/output columns: id, employees_absences_balance_count, last_last_modified_at)
7. [tie_break] Deterministic output order: sort by id. (public carried/output columns: id)
```

### `employees_activity`

- Grain: one row per calendar day of last_modified_at
- Unique key: activity_date
- Required columns: activity_date, record_count

```text
Mart 'employees_activity' has 5 declared semantic rules:
1. [source] Read source table employees. (public source tables: employees)
2. [derive] The grain is activity_date — activity_date is the UTC date part of the incremental cursor 'last_modified_at'. (public source tables: employees | public carried/output columns: activity_date)
3. [aggregate] One output row per activity_date, reporting record_count for that row's matching rows. (public carried/output columns: activity_date, record_count)
4. [derive] Name the mart columns. (public carried/output columns: activity_date, record_count)
5. [tie_break] Deterministic output order: sort by activity_date. (public carried/output columns: activity_date)
```

## Source tables

### absence_types  (source backend: rest)
dlt resource 'absence_types' at /company/time-off-types (write_disposition=replace)

- `id`: bigint NOT NULL — primary key of dlt resource 'absence_types' (synthesized from the dlt manifest (no observed schema))
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### absences  (source backend: s3)
dlt resource 'absences' at /company/time-offs (write_disposition=merge, cursor=updated_at)

- `id`: bigint NOT NULL — primary key of dlt resource 'absences' (synthesized from the dlt manifest (no observed schema))
- `updated_at`: timestamp NOT NULL — incremental cursor of dlt resource 'absences' (dlt cursor path 'updated_at')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### attendances  (source backend: s3)
dlt resource 'attendances' at /company/attendances (write_disposition=merge, cursor=updated_at)

- `id`: bigint NOT NULL — primary key of dlt resource 'attendances' (synthesized from the dlt manifest (no observed schema))
- `updated_at`: timestamp NOT NULL — incremental cursor of dlt resource 'attendances' (dlt cursor path 'updated_at')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### custom_reports  (source backend: s3)
dlt transformer 'custom_reports' at /custom_reports (write_disposition=merge)

- `report_id`: bigint NOT NULL — primary key of dlt resource 'custom_reports' (synthesized from the dlt manifest (no observed schema))
- `item_id`: bigint NOT NULL — primary key of dlt resource 'custom_reports' (synthesized from the dlt manifest (no observed schema))
- `_custom_reports_list_id`: bigint NOT NULL — parent link to 'custom_reports_list' (dlt include_from_parent convention; synthesized from the dlt manifest (no observed schema))
- `report_figure`: decimal NULL — curator-authored column of dlt transformer 'custom_reports' (source: curator-invented)
- `report_caption`: text NULL — curator-authored column of dlt transformer 'custom_reports' (source: curator-invented)
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: report_id, item_id

### custom_reports_list  (source backend: postgres)
dlt resource 'custom_reports_list' at /company/custom-reports/reports (write_disposition=replace)

- `id`: bigint NOT NULL — primary key of dlt resource 'custom_reports_list' (synthesized from the dlt manifest (no observed schema))
- `report_headline`: text NULL — curator-authored column of dlt resource 'custom_reports_list' (source: curator-invented)
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### document_categories  (source backend: s3)
dlt resource 'document_categories' at /company/document-categories (write_disposition=replace)

- `id`: bigint NOT NULL — primary key of dlt resource 'document_categories' (synthesized from the dlt manifest (no observed schema))
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### employees  (source backend: postgres)
dlt resource 'employees' at /company/employees (write_disposition=merge, cursor=last_modified_at)

- `id`: bigint NOT NULL — primary key of dlt resource 'employees' (synthesized from the dlt manifest (no observed schema))
- `last_modified_at`: timestamp NOT NULL — incremental cursor of dlt resource 'employees' (dlt cursor path 'last_modified_at')
- `employee_division`: text NULL — curator-authored column of dlt resource 'employees' (source: curator-invented)
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### employees_absences_balance  (source backend: mongodb)
dlt transformer 'employees_absences_balance' at /employees_absences_balance (write_disposition=merge)

- `employee_id`: bigint NOT NULL — primary key of dlt resource 'employees_absences_balance' (synthesized from the dlt manifest (no observed schema))
- `id`: bigint NOT NULL — primary key of dlt resource 'employees_absences_balance' (synthesized from the dlt manifest (no observed schema))
- `_employees_id`: bigint NOT NULL — parent link to 'employees' (dlt include_from_parent convention; synthesized from the dlt manifest (no observed schema))
- `absence_balance_days`: decimal NULL — curator-authored column of dlt transformer 'employees_absences_balance' (source: curator-invented)
- `absence_policy_headline`: text NULL — curator-authored column of dlt transformer 'employees_absences_balance' (source: curator-invented)
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: employee_id, id

### projects  (source backend: files)
dlt resource 'projects' at /company/attendances/projects (write_disposition=replace)

- `id`: bigint NOT NULL — primary key of dlt resource 'projects' (synthesized from the dlt manifest (no observed schema))
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### Relationships

- custom_reports(_custom_reports_list_id) -> custom_reports_list(id) [required]
- employees_absences_balance(_employees_id) -> employees(id) [required]

