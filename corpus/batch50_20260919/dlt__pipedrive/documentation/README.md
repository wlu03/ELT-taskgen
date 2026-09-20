# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# dlt extraction: pipedrive

## Specification

PROJECT OVERVIEW

This project models data extracted from a Pipedrive CRM account by a dlt extraction pipeline and lands four analytical marts. The solver is given the extracted landing tables described below and must produce the four marts exactly as specified.

Source tables and the extraction backend each one must be extracted from:

- Table activities is extracted from the postgres backend. It is the dlt resource 'activities' at endpoint /activities, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key of the resource), update_time (timestamp, the incremental cursor) and payload (json, nullable, the raw endpoint record body).
- Table activity_types is extracted from the files backend. It is the dlt resource 'activity_types' at endpoint /activity_types, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor) and payload (json, nullable).
- Table custom_fields_mapping is extracted from the s3 backend. It is the dlt transformer 'custom_fields_mapping' at endpoint /custom_fields_mapping, write_disposition=replace. Its columns are options (json, nullable, a dlt column hint with data_type=json) and payload (json, nullable). It has no primary key.
- Table deals is extracted from the files backend. It is the dlt resource 'deals' at endpoint /deals, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor), deal_title (text, nullable, a curator-authored column) and payload (json, nullable).
- Table deals_flow is extracted from the rest backend. It is the dlt transformer 'deals_flow' at endpoint /deals_flow, write_disposition=merge. Its columns are id (bigint, primary key), _deals_id (bigint, the parent link to deals), flow_value (decimal, nullable), flow_object_kind (text, nullable) and payload (json, nullable).
- Table deals_participants is extracted from the s3 backend. It is the dlt transformer 'deals_participants' at endpoint /deals_participants, write_disposition=merge. Its columns are id (bigint, primary key), _deals_id (bigint, the parent link to deals), participant_share (decimal, nullable), participant_role (text, nullable) and payload (json, nullable).
- Table files is extracted from the mongodb backend. It is the dlt resource 'files' at endpoint /files, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor) and payload (json, nullable).
- Table filters is extracted from the files backend. It is the dlt resource 'filters' at endpoint /filters, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor) and payload (json, nullable).
- Table leads is extracted from the files backend. It is the dlt resource 'leads' at endpoint /leads, write_disposition=merge, with incremental cursor path 'update_time'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor) and payload (json, nullable).
- Table notes is extracted from the postgres backend. It is the dlt resource 'notes' at endpoint /notes, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor) and payload (json, nullable).
- Table organizations is extracted from the mongodb backend. It is the dlt resource 'organizations' at endpoint /organizations, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor) and payload (json, nullable).
- Table persons is extracted from the rest backend. It is the dlt resource 'persons' at endpoint /persons, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor) and payload (json, nullable).
- Table pipelines is extracted from the postgres backend. It is the dlt resource 'pipelines' at endpoint /pipelines, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor) and payload (json, nullable).
- Table products is extracted from the rest backend. It is the dlt resource 'products' at endpoint /products, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor) and payload (json, nullable).
- Table projects is extracted from the postgres backend. It is the dlt resource 'projects' at endpoint /projects, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor) and payload (json, nullable).
- Table stages is extracted from the s3 backend. It is the dlt resource 'stages' at endpoint /stages, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor) and payload (json, nullable).
- Table tasks is extracted from the s3 backend. It is the dlt resource 'tasks' at endpoint /tasks, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor) and payload (json, nullable).
- Table users is extracted from the files backend. It is the dlt resource 'users' at endpoint /users, write_disposition=merge, with incremental cursor path 'update_time|modified'. Its columns are id (bigint, primary key), update_time (timestamp, the incremental cursor) and payload (json, nullable).

Relationships between the source tables:

- The child table deals_flow, through its key column _deals_id, refers to the parent table deals through its key column id. This relationship is required.
- The child table deals_participants, through its key column _deals_id, refers to the parent table deals through its key column id. This relationship is required.

Conventions used throughout: text comparisons are plain case-sensitive comparisons of the stored text under the warehouse default collation, in which every uppercase letter sorts before every lowercase one. All ratios are fractions between 0 and 1, never percentages, and are rounded to 4 decimal places.

================================================================
Mart deals_participants_distribution — the per-(deals, measure state) distribution of extracted deals_participants records.

Grain: one row per (id, measure state) pair represented among linked deals_participants rows; the absent state includes missing participant_share values and a no-activity row for a deals row with no links. A linked deals_participants row whose participant_share has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state.

Output columns:

- entity_key (bigint): identifier of the deals row.
- measure_state (text): 'present' for a linked deals_participants row whose participant_share has a value; 'absent' when participant_share is missing, including a deals row with no linked deals_participants row. A linked deals_participants row whose participant_share has a value belongs only to the present state and never to the absent state.
- entity_name (text): deal_title of the deals row, copied unchanged.
- row_count (bigint): number of linked deals_participants rows in this entity/state cell; an absent cell holding real deals_participants rows whose participant_share is missing COUNTS those rows, and only the placeholder cell of a deals row with no linked deals_participants row at all reports 0.
- distinct_amount_count (bigint): number of unique non-missing participant_share values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no participant_share value at all — both for a deals row with no linked deals_participants row and for an absent cell whose rows all have a missing participant_share.
- total_amount (decimal): total of participant_share in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an participant_share value.
- max_amount (decimal): largest participant_share in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an participant_share value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules, in order:

1. The source table deals is read.
2. The source table deals_participants is read.
3. From source table deals, each id is carried as entity_key and its deal_title is carried as entity_name into the measure-state calculation.
4. The linked deals_participants rows are brought into each deals entity, matching a deals_participants row to an entity when its _deals_id equals that entity_key; preservation is left-sided on the deals side, so an entity with no linked row is retained so its absent state is visible, and entity_key, entity_name, id and _deals_id are carried.
5. The present measure-state rows are kept, carrying entity_key and entity_name: a real deals_participants row whose participant_share has a value.
6. There is one row per deals entity that has at least one row in the present measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count as the row count, distinct_amount_count as how many different non-missing participant_share values occur (each different value counted once, however many rows repeat it), total_amount as the total participant_share, and max_amount as the largest participant_share.
7. For those present-state rows carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; the result is 0.0 when total_amount is 0.
8. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state as the present measure state.
9. The absent measure-state rows are kept, carrying entity_key and entity_name: participant_share is missing, including the retained placeholder for a deals row with no deals_participants rows. A real deals_participants row whose participant_share has a value belongs only to the present state and never to this absent state.
10. There is one row per deals entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count as the row count, distinct_amount_count as how many different non-missing participant_share values occur (each different value counted once, however many rows repeat it), total_amount as the total participant_share, and max_amount as the largest participant_share.
11. For those absent-state rows carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; the result is 0.0 when total_amount is 0.
12. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state as the absent measure state.
13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share, keeping all rows: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.
14. Deterministic output order: rows appear sorted by entity_key ascending, then by measure_state ascending.

================================================================
Mart deals_participants_top — per-deals extremes over extracted deals_participants records: WHICH record is largest, not how large it is.

Grain: one row per deals (id), INCLUDING deals rows with no linked deals_participants rows.

Key column: parent_key.

Output columns:

- parent_key (bigint): identifier of the deals row. One row per value.
- parent_name (text): deal_title of the deals row, copied unchanged.
- top_measure (decimal): the largest participant_share itself; 0 when the parent has no deals_participants rows, and 0 when none of its rows carries a participant_share value.
- tied_count (bigint): how many deals_participants rows are tied at that largest participant_share. 1 when exactly one row carries that largest participant_share; 0 when there are no rows or when none of the rows carries a participant_share value; a row with no participant_share value never ties: only a row whose participant_share value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.
- child_count (bigint): number of deals_participants rows for this deals row; 0 when there are none. Every linked deals_participants row counts, whether or not it carries a participant_share value. A deals row kept with no deals_participants row reports 0 here, never 1: its placeholder holds no deals_participants row to count.
- total_measure (decimal): total of participant_share over all of them; 0 when the parent has no deals_participants rows, and 0 when none of its rows carries a participant_share value (rows with no participant_share value add nothing).
- top_label (text): the participant_role of the deals_participants row with the LARGEST participant_share for this deals row. Ties in participant_share are broken by taking the SMALLEST participant_role under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no participant_role value sorts after every labelled row; rows tied on both are resolved by the smallest id. A row with no participant_share value still ranks, after every row that has one, so a parent holding at least one deals_participants row always has a winning row — when NONE of its rows carries a participant_share value the winner is the one the tie-break alone selects, not the no-rows default. The literal '(none)' when the parent has no deals_participants rows at all, and '(none)' when the winning row has no participant_role value.
- top_row_id (bigint): the id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real deals_participants row whenever the parent has any. This includes when none of them carries a participant_share value. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no deals_participants rows, or none of its rows carries a participant_share value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a participant_share value equal to the largest participant_share value among the parent's rows; a row with no participant_share value never holds the maximum. So a parent whose deals_participants rows all lack a participant_share value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

Rules, in order:

1. The source table deals is read.
2. The source table deals_participants is read.
3. From source table deals there is one row per deals row, keyed by id, carrying parent_key and parent_name.
4. The deals_participants rows are brought in, matching a deals_participants row when its _deals_id equals parent_key and carrying _deals_id and id; preservation is left-sided on the deals side, so a deals row with no deals_participants rows still appears, with the declared defaults.
5. The rows brought together are ranked within each parent_key under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order.
6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.
7. For each parent_key the single row at which the ordering measure is largest is kept, ties broken by the smallest participant_role under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no participant_role value sorts after every row that has one), then the smallest id, and top_label and top_row_id are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.
8. The extremal row's attributes are attached to the measures of the same parent_key; preservation is left-sided on the measures, so a group with no rows at all keeps its measures.
9. The mart columns parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id are named; top_measure and total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure and total_measure, the default also applies to a group none of whose real rows carries an input value.
10. Guarded ratio, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and it is 0.0 when the denominator total_measure is 0 or NULL.
11. Beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, tie_state is 'empty' when no row holds a maximum at all — the parent has no deals_participants rows, or none of its rows carries a participant_share value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do; this is a categorical mapping with no numeric boundary. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more; a row holds the maximum only when it carries a participant_share value equal to the largest participant_share value among the parent's rows, a row with no participant_share value never holds the maximum, so a parent whose deals_participants rows all lack a participant_share value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.
12. Deterministic output order: rows appear sorted by parent_key ascending.

================================================================
Mart dim_deals — one row per 'deals' record extracted from the API.

Grain: one row per deals (id).

Key column: id.

Output columns:

- id (bigint): deals primary key column 'id'.
- deals_flow_count (bigint): number of 'deals_flow' records extracted for this 'deals' (0 when none).
- last_update_time (timestamp): latest 'update_time' seen for this 'deals' record (the incremental cursor of the resource). It is taken from the deals side only: its value comes from update_time on the deals rows, never from the matching deals_flow rows (deals_flow carries no update_time at all). Because id is the primary key of deals, each deals row carries exactly one update_time, so the value reported for an id is that deals row's own update_time; how many deals_flow rows match does not change it, and a deals row with no matching deals_flow rows still reports whatever update_time holds on the deals side.

Rules, in order:

1. The source table deals is read.
2. The source table deals_flow is read.
3. The mart key column id is formed from source table deals.
4. The deals_flow rows are brought in from source table deals_flow, matching a deals_flow row when its _deals_id equals the deals id and carrying id and _deals_id; preservation is left-sided on the deals side, so every deals record appears whether or not it has matching deals_flow records.
5. There is one output row per id, reporting deals_flow_count for that row's matching rows. last_update_time is taken from the deals side only: its value comes from update_time on the deals rows, never from the matching rows. A deals row with no matching rows still reports whatever update_time holds on the deals side.
6. The mart columns id, deals_flow_count and last_update_time are named.
7. Deterministic output order: rows appear sorted by id ascending.

================================================================
Mart deals_activity — daily activity summary of the 'deals' resource.

Grain: one row per calendar day of update_time.

Key column: activity_date.

Output columns:

- activity_date (date): calendar day of 'update_time' (UTC date part).
- record_count (bigint): number of 'deals' records on that day.

Rules, in order:

1. The source table deals is read.
2. From source table deals, the grain is activity_date — activity_date is the UTC date part of the incremental cursor 'update_time'.
3. There is one output row per activity_date, reporting record_count for that row's matching rows.
4. The mart columns activity_date and record_count are named.
5. Deterministic output order: rows appear sorted by activity_date ascending.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `deals_participants_distribution`

- Grain: One row per (id, measure state) pair represented among linked deals_participants rows; the absent state includes missing participant_share values and a no-activity row for a deals row with no links. A linked deals_participants row whose participant_share has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'deals_participants_distribution' has 14 declared semantic rules:
1. [source] Read source table deals. (public source tables: deals)
2. [source] Read source table deals_participants. (public source tables: deals_participants)
3. [derive] Carry each id and its deal_title into the measure-state calculation. (public source tables: deals | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked deals_participants rows into each deals entity; retain an entity with no linked row so its absent state is visible. (public source tables: deals_participants | public carried/output columns: entity_key, entity_name, id, _deals_id | join preservation: left | condition public identifiers: deals_participants, _deals_id, entity_key)
5. [filter] Keep the present measure-state rows: a real deals_participants row whose participant_share has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per deals entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing participant_share values occur (each different value counted once, however many rows repeat it), total participant_share, and largest participant_share. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: participant_share is missing, including the retained placeholder for a deals row with no deals_participants rows. A real deals_participants row whose participant_share has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per deals entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing participant_share values occur (each different value counted once, however many rows repeat it), total participant_share, and largest participant_share. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `deals_participants_top`

- Grain: One row per deals (id), INCLUDING deals rows with no linked deals_participants rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'deals_participants_top' has 12 declared semantic rules:
1. [source] Read source table deals. (public source tables: deals)
2. [source] Read source table deals_participants. (public source tables: deals_participants)
3. [derive] One row per deals row, keyed by id. (public source tables: deals | public carried/output columns: parent_key, parent_name)
4. [join] Bring in deals_participants: a deals row with no deals_participants rows still appears, with the declared defaults. (public source tables: deals_participants | public carried/output columns: _deals_id, id | join preservation: left | condition public identifiers: deals_participants, _deals_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest participant_role under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no participant_role value sorts after every row that has one), then the smallest id, and take top_label, top_row_id from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no deals_participants rows, or none of its rows carries a participant_share value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a participant_share value equal to the largest participant_share value among the parent's rows; a row with no participant_share value never holds the maximum. So a parent whose deals_participants rows all lack a participant_share value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `dim_deals`

- Grain: one row per deals (id)
- Unique key: id
- Required columns: id, deals_flow_count, last_update_time

```text
Mart 'dim_deals' has 7 declared semantic rules:
1. [source] Read source table deals. (public source tables: deals)
2. [source] Read source table deals_flow. (public source tables: deals_flow)
3. [derive] Form the mart key columns id from source table deals. (public source tables: deals | public carried/output columns: id)
4. [join] Bring in deals_flow: every deals record appears whether or not it has matching deals_flow records. (public source tables: deals_flow | public carried/output columns: id, _deals_id | join preservation: left | condition public identifiers: deals_flow, _deals_id, id)
5. [aggregate] One output row per id, reporting deals_flow_count for that row's matching rows. last_update_time is taken from the deals side only: its value comes from update_time on the deals rows, never from the matching rows. A deals row with no matching rows still reports whatever update_time holds on the deals side. (public carried/output columns: id, deals_flow_count, last_update_time)
6. [derive] Name the mart columns. (public carried/output columns: id, deals_flow_count, last_update_time)
7. [tie_break] Deterministic output order: sort by id. (public carried/output columns: id)
```

### `deals_activity`

- Grain: one row per calendar day of update_time
- Unique key: activity_date
- Required columns: activity_date, record_count

```text
Mart 'deals_activity' has 5 declared semantic rules:
1. [source] Read source table deals. (public source tables: deals)
2. [derive] The grain is activity_date — activity_date is the UTC date part of the incremental cursor 'update_time'. (public source tables: deals | public carried/output columns: activity_date)
3. [aggregate] One output row per activity_date, reporting record_count for that row's matching rows. (public carried/output columns: activity_date, record_count)
4. [derive] Name the mart columns. (public carried/output columns: activity_date, record_count)
5. [tie_break] Deterministic output order: sort by activity_date. (public carried/output columns: activity_date)
```

## Source tables

### activities  (source backend: postgres)
dlt resource 'activities' at /activities (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'activities' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'activities' (dlt cursor path 'update_time|modified')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### activity_types  (source backend: files)
dlt resource 'activity_types' at /activity_types (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'activity_types' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'activity_types' (dlt cursor path 'update_time|modified')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### custom_fields_mapping  (source backend: s3)
dlt transformer 'custom_fields_mapping' at /custom_fields_mapping (write_disposition=replace)

- `options`: json NULL — dlt column hint (data_type=json)
- `payload`: json NULL — raw endpoint record body (synthesized schema)

### deals  (source backend: files)
dlt resource 'deals' at /deals (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'deals' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'deals' (dlt cursor path 'update_time|modified')
- `deal_title`: text NULL — curator-authored column of dlt resource 'deals' (source: curator-invented)
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### deals_flow  (source backend: rest)
dlt transformer 'deals_flow' at /deals_flow (write_disposition=merge)

- `id`: bigint NOT NULL — primary key of dlt resource 'deals_flow' (synthesized from the dlt manifest (no observed schema))
- `_deals_id`: bigint NOT NULL — parent link to 'deals' (dlt include_from_parent convention; synthesized from the dlt manifest (no observed schema))
- `flow_value`: decimal NULL — curator-authored column of dlt transformer 'deals_flow' (source: curator-invented)
- `flow_object_kind`: text NULL — curator-authored column of dlt transformer 'deals_flow' (source: curator-invented)
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### deals_participants  (source backend: s3)
dlt transformer 'deals_participants' at /deals_participants (write_disposition=merge)

- `id`: bigint NOT NULL — primary key of dlt resource 'deals_participants' (synthesized from the dlt manifest (no observed schema))
- `_deals_id`: bigint NOT NULL — parent link to 'deals' (dlt include_from_parent convention; synthesized from the dlt manifest (no observed schema))
- `participant_share`: decimal NULL — curator-authored column of dlt transformer 'deals_participants' (source: curator-invented)
- `participant_role`: text NULL — curator-authored column of dlt transformer 'deals_participants' (source: curator-invented)
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### files  (source backend: mongodb)
dlt resource 'files' at /files (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'files' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'files' (dlt cursor path 'update_time|modified')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### filters  (source backend: files)
dlt resource 'filters' at /filters (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'filters' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'filters' (dlt cursor path 'update_time|modified')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### leads  (source backend: files)
dlt resource 'leads' at /leads (write_disposition=merge, cursor=update_time)

- `id`: bigint NOT NULL — primary key of dlt resource 'leads' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'leads' (dlt cursor path 'update_time')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### notes  (source backend: postgres)
dlt resource 'notes' at /notes (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'notes' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'notes' (dlt cursor path 'update_time|modified')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### organizations  (source backend: mongodb)
dlt resource 'organizations' at /organizations (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'organizations' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'organizations' (dlt cursor path 'update_time|modified')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### persons  (source backend: rest)
dlt resource 'persons' at /persons (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'persons' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'persons' (dlt cursor path 'update_time|modified')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### pipelines  (source backend: postgres)
dlt resource 'pipelines' at /pipelines (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'pipelines' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'pipelines' (dlt cursor path 'update_time|modified')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### products  (source backend: rest)
dlt resource 'products' at /products (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'products' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'products' (dlt cursor path 'update_time|modified')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### projects  (source backend: postgres)
dlt resource 'projects' at /projects (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'projects' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'projects' (dlt cursor path 'update_time|modified')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### stages  (source backend: s3)
dlt resource 'stages' at /stages (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'stages' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'stages' (dlt cursor path 'update_time|modified')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### tasks  (source backend: s3)
dlt resource 'tasks' at /tasks (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'tasks' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'tasks' (dlt cursor path 'update_time|modified')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### users  (source backend: files)
dlt resource 'users' at /users (write_disposition=merge, cursor=update_time|modified)

- `id`: bigint NOT NULL — primary key of dlt resource 'users' (synthesized from the dlt manifest (no observed schema))
- `update_time`: timestamp NOT NULL — incremental cursor of dlt resource 'users' (dlt cursor path 'update_time|modified')
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### Relationships

- deals_flow(_deals_id) -> deals(id) [required]
- deals_participants(_deals_id) -> deals(id) [required]

