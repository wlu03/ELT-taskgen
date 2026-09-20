# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Interactis Whes Platform

## Specification

PROJECT OVERVIEW

This project builds three analytical marts from the Github Com Interactis Whes Platform schema (the 030317_start.sql schema). The marts summarise translation activity attached to suppliers, routes and points of interest.

Every source table must be read from the extraction backend named here, and only from it.

Source tables and their extraction backends:
- Table admin is extracted from the mongodb backend.
- Table ambassador_translation is extracted from the files backend.
- Table article is extracted from the postgres backend.
- Table article_translation is extracted from the s3 backend.
- Table content is extracted from the s3 backend.
- Table content_flag is extracted from the mongodb backend.
- Table content_tag is extracted from the rest backend.
- Table content_valid_time is extracted from the mongodb backend.
- Table exhibit is extracted from the mongodb backend.
- Table exhibit_rucksack is extracted from the s3 backend.
- Table exhibition_code is extracted from the s3 backend.
- Table exhibition_code_series is extracted from the mongodb backend.
- Table flag is extracted from the rest backend.
- Table flag_group is extracted from the files backend.
- Table flag_group_translation is extracted from the mongodb backend.
- Table flag_translation is extracted from the files backend.
- Table heritage is extracted from the rest backend.
- Table heritage_translation is extracted from the files backend.
- Table language is extracted from the rest backend.
- Table media is extracted from the s3 backend.
- Table media_translation is extracted from the mongodb backend.
- Table page is extracted from the mongodb backend.
- Table page_translation is extracted from the mongodb backend.
- Table poi is extracted from the rest backend.
- Table poi_translation is extracted from the mongodb backend.
- Table related_tag is extracted from the s3 backend.
- Table route is extracted from the postgres backend.
- Table route_translation is extracted from the s3 backend.
- Table rucksack is extracted from the mongodb backend.
- Table supplier is extracted from the mongodb backend.
- Table supplier_translation is extracted from the postgres backend.
- Table tag is extracted from the postgres backend.
- Table tag_translation is extracted from the postgres backend.
- Table valid_time is extracted from the postgres backend.

Relationships between the source tables. Each line states the child table with its key columns, the parent table with its key columns, and whether the relationship is required or optional.
- Child table admin with key heritage_id refers to parent table heritage with key id; this relationship is optional (may be NULL or dangling).
- Child table ambassador_translation with key heritage_id refers to parent table heritage with key id; this relationship is optional (may be NULL or dangling).
- Child table ambassador_translation with key poi_id refers to parent table poi with key id; this relationship is optional (may be NULL or dangling).
- Child table article with key content_id refers to parent table content with key id; this relationship is optional (may be NULL or dangling).
- Child table article_translation with key article_id refers to parent table article with key id; this relationship is optional (may be NULL or dangling).
- Child table article_translation with key language_id refers to parent table language with key id; this relationship is optional (may be NULL or dangling).
- Child table content with key heritage_id refers to parent table heritage with key id; this relationship is optional (may be NULL or dangling).
- Child table content_flag with key content_id refers to parent table content with key id; this relationship is optional (may be NULL or dangling).
- Child table content_flag with key flag_id refers to parent table flag with key id; this relationship is optional (may be NULL or dangling).
- Child table content_tag with key content_id refers to parent table content with key id; this relationship is optional (may be NULL or dangling).
- Child table content_tag with key tag_id refers to parent table tag with key id; this relationship is optional (may be NULL or dangling).
- Child table content_valid_time with key content_id refers to parent table content with key id; this relationship is optional (may be NULL or dangling).
- Child table content_valid_time with key valid_time_id refers to parent table valid_time with key id; this relationship is optional (may be NULL or dangling).
- Child table exhibit with key content_id refers to parent table content with key id; this relationship is optional (may be NULL or dangling).
- Child table exhibit_rucksack with key content_id refers to parent table content with key id; this relationship is optional (may be NULL or dangling).
- Child table exhibit_rucksack with key exhibit_id refers to parent table exhibit with key id; this relationship is optional (may be NULL or dangling).
- Child table exhibit_rucksack with key rucksackid refers to parent table rucksack with key id; this relationship is optional (may be NULL or dangling).
- Child table exhibition_code with key exhibition_code_series_id refers to parent table exhibition_code_series with key id; this relationship is optional (may be NULL or dangling).
- Child table exhibition_code_series with key heritage_id refers to parent table heritage with key id; this relationship is optional (may be NULL or dangling).
- Child table flag with key flag_group_id refers to parent table flag_group with key id; this relationship is optional (may be NULL or dangling).
- Child table flag_group_translation with key flag_group_id refers to parent table flag_group with key id; this relationship is optional (may be NULL or dangling).
- Child table flag_group_translation with key language_id refers to parent table language with key id; this relationship is optional (may be NULL or dangling).
- Child table flag_translation with key flag_id refers to parent table flag with key id; this relationship is optional (may be NULL or dangling).
- Child table flag_translation with key language_id refers to parent table language with key id; this relationship is optional (may be NULL or dangling).
- Child table heritage_translation with key heritage_id refers to parent table heritage with key id; this relationship is optional (may be NULL or dangling).
- Child table heritage_translation with key language_id refers to parent table language with key id; this relationship is optional (may be NULL or dangling).
- Child table media with key content_id refers to parent table content with key id; this relationship is optional (may be NULL or dangling).
- Child table media with key heritage_id refers to parent table heritage with key id; this relationship is optional (may be NULL or dangling).
- Child table media with key page_id refers to parent table page with key id; this relationship is optional (may be NULL or dangling).
- Child table media_translation with key language_id refers to parent table language with key id; this relationship is optional (may be NULL or dangling).
- Child table media_translation with key media_id refers to parent table media with key id; this relationship is optional (may be NULL or dangling).
- Child table page_translation with key language_id refers to parent table language with key id; this relationship is optional (may be NULL or dangling).
- Child table page_translation with key page_id refers to parent table page with key id; this relationship is optional (may be NULL or dangling).
- Child table poi with key content_id refers to parent table content with key id; this relationship is optional (may be NULL or dangling).
- Child table poi_translation with key language_id refers to parent table language with key id; this relationship is optional (may be NULL or dangling).
- Child table poi_translation with key poi_id refers to parent table poi with key id; this relationship is optional (may be NULL or dangling).
- Child table related_tag with key related_tag_id refers to parent table tag with key id; this relationship is optional (may be NULL or dangling).
- Child table related_tag with key tag_id refers to parent table tag with key id; this relationship is optional (may be NULL or dangling).
- Child table route with key content_id refers to parent table content with key id; this relationship is optional (may be NULL or dangling).
- Child table route_translation with key language_id refers to parent table language with key id; this relationship is optional (may be NULL or dangling).
- Child table route_translation with key route_id refers to parent table route with key id; this relationship is optional (may be NULL or dangling).
- Child table supplier with key content_id refers to parent table content with key id; this relationship is optional (may be NULL or dangling).
- Child table supplier_translation with key language_id refers to parent table language with key id; this relationship is optional (may be NULL or dangling).
- Child table supplier_translation with key supplier_id refers to parent table supplier with key id; this relationship is optional (may be NULL or dangling).
- Child table tag_translation with key language_id refers to parent table language with key id; this relationship is optional (may be NULL or dangling).
- Child table tag_translation with key tag_id refers to parent table tag with key id; this relationship is optional (may be NULL or dangling).

=========================================================
MART 1 — supplier_supplier_translation_distribution: Per-(supplier, measure state) distribution of linked supplier_translation activity in the 030317_start.sql schema.
=========================================================

Grain. One row per (id, measure state) pair represented among linked supplier_translation rows; the absent state includes missing created_at values and a no-activity row for a supplier row with no links. A linked supplier_translation row whose created_at has a value belongs only to the present state and never to the absent state.

Key columns. The key columns of this mart are entity_key and measure_state together.

Output columns.
- entity_key (integer): Identifier of the supplier row.
- measure_state (text): 'present' for a linked supplier_translation row whose created_at has a value; 'absent' when created_at is missing, including a supplier row with no linked supplier_translation row. A linked supplier_translation row whose created_at has a value belongs only to the present state and never to the absent state.
- entity_name (text): address_addition of the supplier row, copied unchanged.
- row_count (bigint): Number of linked supplier_translation rows in this entity/state cell; a absent cell holding real supplier_translation rows whose created_at is missing COUNTS those rows, and only the placeholder cell of a supplier row with no linked supplier_translation row at all reports 0.
- distinct_amount_count (bigint): Number of unique non-missing created_at values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no created_at value at all — both for a supplier row with no linked supplier_translation row and for an absent cell whose rows all have a missing created_at.
- total_amount (integer): Sum of created_at in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an created_at value.
- max_amount (integer): Largest created_at in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an created_at value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules that produce this mart.

Rule 1. Source table supplier is read in full, as it stands in the mongodb backend.

Rule 2. Source table supplier_translation is read in full, as it stands in the postgres backend.

Rule 3. From supplier, each id becomes entity_key and its address_addition becomes entity_name, and both entity_key and entity_name are carried into the measure-state calculation.

Rule 4. The linked supplier_translation rows are brought into each supplier entity by matching supplier_translation.supplier_id to entity_key, carrying entity_key, entity_name, the supplier_translation id and supplier_id; preservation is left-sided on the supplier side, so an entity with no linked row is retained, as a placeholder, so that its absent state is visible.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are those where a real supplier_translation row is matched and its created_at has a value; those rows are kept for the present state.

Rule 6. For the present measure state there is one row per supplier entity that has at least one such row, and no row here for an entity with none; each such entity reports entity_key, entity_name, row_count as the number of those rows, distinct_amount_count as how many different non-missing created_at values occur (each different value counted once, however many rows repeat it), total_amount as the total created_at, and max_amount as the largest created_at.

Rule 7. For each present-state row, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and it is 0.0 when total_amount is 0; the row still reports entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 8. Each of these present-state rows is labelled with measure_state 'present', and carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are those where created_at is missing, including the retained placeholder for a supplier row with no supplier_translation rows; those rows are kept for the absent state. A real supplier_translation row whose created_at has a value belongs only to the present state and never to this absent state.

Rule 10. For the absent measure state there is one row per supplier entity that has at least one such row, and no row here for an entity with none; each such entity reports entity_key, entity_name, row_count as the number of those rows, distinct_amount_count as how many different non-missing created_at values occur (each different value counted once, however many rows repeat it), total_amount as the total created_at, and max_amount as the largest created_at.

Rule 11. For each absent-state row, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and it is 0.0 when total_amount is 0; the row still reports entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 12. Each of these absent-state rows is labelled with measure_state 'absent', and carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. The output order is deterministic: rows appear in ascending entity_key order, and within one entity in ascending measure_state order.

=========================================================
MART 2 — route_route_translation_top: Per-route extremes over linked route_translation rows in the 030317_start.sql schema: WHICH row is largest, not how large it is.
=========================================================

Grain. One row per route (id), INCLUDING route rows with no linked route_translation rows.

Key columns. The key column of this mart is parent_key.

Output columns.
- parent_key (integer): Identifier of the route row. One row per value.
- parent_name (text): arrival_station of the route row, copied unchanged.
- top_measure (integer): The largest created_at itself; 0 when the parent has no route_translation rows, and 0 when none of its rows carries a created_at value.
- tied_count (bigint): How many route_translation rows are tied at that largest created_at. 1 when exactly one row carries that largest created_at; 0 when there are no rows or when none of the rows carries a created_at value; a row with no created_at value never ties: only a row whose created_at value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.
- child_count (bigint): Number of route_translation rows for this route row; 0 when there are none. Every linked route_translation row counts, whether or not it carries a created_at value. A route row kept with no route_translation row reports 0 here, never 1: its placeholder holds no route_translation row to count.
- total_measure (integer): Sum of created_at over all of them; 0 when the parent has no route_translation rows, and 0 when none of its rows carries a created_at value (rows with no created_at value add nothing).
- top_label (text): The catering of the route_translation row with the LARGEST created_at for this route row. Ties in created_at are broken by taking the SMALLEST catering under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no catering value sorts after every labelled row; rows tied on both are resolved by the smallest id. A row with no created_at value still ranks, after every row that has one, so a parent holding at least one route_translation row always has a winning row — when NONE of its rows carries a created_at value the winner is the one the tie-break alone selects, not the no-rows default. The literal '(none)' when the parent has no route_translation rows at all, and '(none)' when the winning row has no catering value.
- top_row_id (integer): The id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real route_translation row whenever the parent has any. This includes when none of them carries a created_at value. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no route_translation rows, or none of its rows carries a created_at value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more.

Rules that produce this mart.

Rule 1. Source table route is read in full, as it stands in the postgres backend.

Rule 2. Source table route_translation is read in full, as it stands in the s3 backend.

Rule 3. From route there is one row per route row, keyed by id: that id becomes parent_key and arrival_station becomes parent_name, and both parent_key and parent_name are carried forward.

Rule 4. The route_translation rows are brought in by matching route_translation.route_id to parent_key, carrying route_id and the route_translation id; preservation is left-sided on the route side, so a route row with no route_translation rows still appears, with the declared defaults.

Rule 5. Within each parent_key the matched rows are ranked under an explicit total order — the created_at measure first, then the declared tie-break — so the extremal row is a function of the input and not of row order.

Rule 6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, and that row reports top_measure, tied_count, child_count and total_measure over that row's matching route_translation rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7. The single row kept per parent_key is the one at which the ordering measure created_at is largest, ties broken by the smallest catering under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no catering value sorts after every row that has one), then the smallest id, and top_label and top_row_id are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 8. The extremal row's attributes are attached to the grouped measures by matching on parent_key; preservation is left-sided on the measures side, so a group with no rows at all keeps its measures.

Rule 9. The mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults — never NULL — for a group with no matching rows, and for top_measure and total_measure that default also applies to a group none of whose real rows carries an input value.

Rule 10. The guarded ratio top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when total_measure is 0 or has no value; the row reports parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share.

Rule 11. Alongside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, tie_state is 'empty' when no row holds a maximum at all — the parent has no route_translation rows, or none of its rows carries a created_at value — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; equivalently tie_state follows tied_count, 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more, and it is never null or blank. A row holds the maximum only when it carries a created_at value equal to the largest created_at value among the parent's rows; a row with no created_at value never holds the maximum, so a parent whose route_translation rows all lack a created_at value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

Rule 12. The output order is deterministic: rows appear in ascending parent_key order.

=========================================================
MART 3 — poi_poi_translation_distribution: Per-(poi, measure state) distribution of linked poi_translation activity in the 030317_start.sql schema.
=========================================================

Grain. One row per (id, measure state) pair represented among linked poi_translation rows; the absent state includes missing created_at values and a no-activity row for a poi row with no links. A linked poi_translation row whose created_at has a value belongs only to the present state and never to the absent state.

Key columns. The key columns of this mart are entity_key and measure_state together.

Output columns.
- entity_key (integer): Identifier of the poi row.
- measure_state (text): 'present' for a linked poi_translation row whose created_at has a value; 'absent' when created_at is missing, including a poi row with no linked poi_translation row. A linked poi_translation row whose created_at has a value belongs only to the present state and never to the absent state.
- entity_name (text): arrival_station of the poi row, copied unchanged.
- row_count (bigint): Number of linked poi_translation rows in this entity/state cell; a absent cell holding real poi_translation rows whose created_at is missing COUNTS those rows, and only the placeholder cell of a poi row with no linked poi_translation row at all reports 0.
- distinct_amount_count (bigint): Number of unique non-missing created_at values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no created_at value at all — both for a poi row with no linked poi_translation row and for an absent cell whose rows all have a missing created_at.
- total_amount (integer): Sum of created_at in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an created_at value.
- max_amount (integer): Largest created_at in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an created_at value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules that produce this mart.

Rule 1. Source table poi is read in full, as it stands in the rest backend.

Rule 2. Source table poi_translation is read in full, as it stands in the mongodb backend.

Rule 3. From poi, each id becomes entity_key and its arrival_station becomes entity_name, and both entity_key and entity_name are carried into the measure-state calculation.

Rule 4. The linked poi_translation rows are brought into each poi entity by matching poi_translation.poi_id to entity_key, carrying entity_key, entity_name, the poi_translation id and poi_id; preservation is left-sided on the poi side, so an entity with no linked row is retained, as a placeholder, so that its absent state is visible.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are those where a real poi_translation row is matched and its created_at has a value; those rows are kept for the present state.

Rule 6. For the present measure state there is one row per poi entity that has at least one such row, and no row here for an entity with none; each such entity reports entity_key, entity_name, row_count as the number of those rows, distinct_amount_count as how many different non-missing created_at values occur (each different value counted once, however many rows repeat it), total_amount as the total created_at, and max_amount as the largest created_at.

Rule 7. For each present-state row, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and it is 0.0 when total_amount is 0; the row still reports entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 8. Each of these present-state rows is labelled with measure_state 'present', and carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are those where created_at is missing, including the retained placeholder for a poi row with no poi_translation rows; those rows are kept for the absent state. A real poi_translation row whose created_at has a value belongs only to the present state and never to this absent state.

Rule 10. For the absent measure state there is one row per poi entity that has at least one such row, and no row here for an entity with none; each such entity reports entity_key, entity_name, row_count as the number of those rows, distinct_amount_count as how many different non-missing created_at values occur (each different value counted once, however many rows repeat it), total_amount as the total created_at, and max_amount as the largest created_at.

Rule 11. For each absent-state row, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and it is 0.0 when total_amount is 0; the row still reports entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 12. Each of these absent-state rows is labelled with measure_state 'absent', and carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. The output order is deterministic: rows appear in ascending entity_key order, and within one entity in ascending measure_state order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `supplier_supplier_translation_distribution`

- Grain: One row per (id, measure state) pair represented among linked supplier_translation rows; the absent state includes missing created_at values and a no-activity row for a supplier row with no links. A linked supplier_translation row whose created_at has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'supplier_supplier_translation_distribution' has 14 declared semantic rules:
1. [source] Read source table supplier. (public source tables: supplier)
2. [source] Read source table supplier_translation. (public source tables: supplier_translation)
3. [derive] Carry each id and its address_addition into the measure-state calculation. (public source tables: supplier | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked supplier_translation rows into each supplier entity; retain an entity with no linked row so its absent state is visible. (public source tables: supplier_translation | public carried/output columns: entity_key, entity_name, id, supplier_id | join preservation: left | condition public identifiers: supplier_translation, supplier_id, entity_key)
5. [filter] Keep the present measure-state rows: a real supplier_translation row whose created_at has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per supplier entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing created_at values occur (each different value counted once, however many rows repeat it), total created_at, and largest created_at. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: created_at is missing, including the retained placeholder for a supplier row with no supplier_translation rows. A real supplier_translation row whose created_at has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per supplier entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing created_at values occur (each different value counted once, however many rows repeat it), total created_at, and largest created_at. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `route_route_translation_top`

- Grain: One row per route (id), INCLUDING route rows with no linked route_translation rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'route_route_translation_top' has 12 declared semantic rules:
1. [source] Read source table route. (public source tables: route)
2. [source] Read source table route_translation. (public source tables: route_translation)
3. [derive] One row per route row, keyed by id. (public source tables: route | public carried/output columns: parent_key, parent_name)
4. [join] Bring in route_translation: a route row with no route_translation rows still appears, with the declared defaults. (public source tables: route_translation | public carried/output columns: route_id, id | join preservation: left | condition public identifiers: route_translation, route_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest catering under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no catering value sorts after every row that has one), then the smallest id, and take top_label, top_row_id from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no route_translation rows, or none of its rows carries a created_at value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a created_at value equal to the largest created_at value among the parent's rows; a row with no created_at value never holds the maximum. So a parent whose route_translation rows all lack a created_at value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `poi_poi_translation_distribution`

- Grain: One row per (id, measure state) pair represented among linked poi_translation rows; the absent state includes missing created_at values and a no-activity row for a poi row with no links. A linked poi_translation row whose created_at has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'poi_poi_translation_distribution' has 14 declared semantic rules:
1. [source] Read source table poi. (public source tables: poi)
2. [source] Read source table poi_translation. (public source tables: poi_translation)
3. [derive] Carry each id and its arrival_station into the measure-state calculation. (public source tables: poi | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked poi_translation rows into each poi entity; retain an entity with no linked row so its absent state is visible. (public source tables: poi_translation | public carried/output columns: entity_key, entity_name, id, poi_id | join preservation: left | condition public identifiers: poi_translation, poi_id, entity_key)
5. [filter] Keep the present measure-state rows: a real poi_translation row whose created_at has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per poi entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing created_at values occur (each different value counted once, however many rows repeat it), total created_at, and largest created_at. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: created_at is missing, including the retained placeholder for a poi row with no poi_translation rows. A real poi_translation row whose created_at has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per poi entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing created_at values occur (each different value counted once, however many rows repeat it), total created_at, and largest created_at. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### admin  (source backend: mongodb)
Source table admin.

- `auth_key`: text NULL — Column auth_key of table admin.
- `created_at`: integer NULL — Column created_at of table admin.
- `email`: text NULL — Column email of table admin.
- `heritage_id`: integer NULL — Column heritage_id of table admin.
- `id`: integer NOT NULL — Column id of table admin.
- `password_hash`: text NULL — Column password_hash of table admin.
- `password_reset_token`: text NULL — Column password_reset_token of table admin.
- `role`: integer NULL — Column role of table admin.
- `status`: integer NULL — Column status of table admin.
- `updated_at`: integer NULL — Column updated_at of table admin.
- primary key: id

### ambassador_translation  (source backend: files)
Source table ambassador_translation.

- `city`: text NULL — Column city of table ambassador_translation.
- `created_at`: integer NULL — Column created_at of table ambassador_translation.
- `favorite_place`: text NULL — Column favorite_place of table ambassador_translation.
- `firstname`: text NULL — Column firstname of table ambassador_translation.
- `heritage_id`: integer NULL — Column heritage_id of table ambassador_translation.
- `id`: integer NOT NULL — Column id of table ambassador_translation.
- `image_name`: text NULL — Column image_name of table ambassador_translation.
- `lastname`: text NULL — Column lastname of table ambassador_translation.
- `poi_id`: integer NULL — Column poi_id of table ambassador_translation.
- `quote`: text NULL — Column quote of table ambassador_translation.
- `type`: integer NULL — Column type of table ambassador_translation.
- `updated_at`: integer NULL — Column updated_at of table ambassador_translation.
- `zip`: text NULL — Column zip of table ambassador_translation.
- primary key: id

### article  (source backend: postgres)
Source table article.

- `content_id`: integer NULL — Column content_id of table article.
- `created_at`: integer NULL — Column created_at of table article.
- `id`: integer NOT NULL — Column id of table article.
- `updated_at`: integer NULL — Column updated_at of table article.
- primary key: id

### article_translation  (source backend: s3)
Source table article_translation.

- `article_id`: integer NULL — Column article_id of table article_translation.
- `created_at`: integer NULL — Column created_at of table article_translation.
- `description`: text NULL — Column description of table article_translation.
- `excerpt`: text NULL — Column excerpt of table article_translation.
- `id`: integer NOT NULL — Column id of table article_translation.
- `language_id`: integer NULL — Column language_id of table article_translation.
- `slug`: text NULL — Column slug of table article_translation.
- `title`: text NULL — Column title of table article_translation.
- `updated_at`: integer NULL — Column updated_at of table article_translation.
- `youtube_id`: text NULL — Column youtube_id of table article_translation.
- primary key: id

### content  (source backend: s3)
Source table content.

- `created_at`: integer NULL — Column created_at of table content.
- `featured`: boolean NULL — Column featured of table content.
- `heritage_id`: integer NULL — Column heritage_id of table content.
- `hidden`: boolean NULL — Column hidden of table content.
- `id`: integer NOT NULL — Column id of table content.
- `priority`: integer NULL — Column priority of table content.
- `published`: boolean NULL — Column published of table content.
- `type`: integer NULL — Column type of table content.
- `updated_at`: integer NULL — Column updated_at of table content.
- primary key: id

### content_flag  (source backend: mongodb)
Source table content_flag.

- `content_id`: integer NULL — Column content_id of table content_flag.
- `created_at`: integer NULL — Column created_at of table content_flag.
- `flag_id`: integer NULL — Column flag_id of table content_flag.
- `id`: integer NOT NULL — Column id of table content_flag.
- `updated_at`: integer NULL — Column updated_at of table content_flag.
- primary key: id

### content_tag  (source backend: rest)
Source table content_tag.

- `content_id`: integer NULL — Column content_id of table content_tag.
- `created_at`: integer NULL — Column created_at of table content_tag.
- `id`: integer NOT NULL — Column id of table content_tag.
- `tag_id`: integer NULL — Column tag_id of table content_tag.
- `updated_at`: integer NULL — Column updated_at of table content_tag.
- primary key: id

### content_valid_time  (source backend: mongodb)
Source table content_valid_time.

- `content_id`: integer NULL — Column content_id of table content_valid_time.
- `created_at`: integer NULL — Column created_at of table content_valid_time.
- `id`: integer NOT NULL — Column id of table content_valid_time.
- `updated_at`: integer NULL — Column updated_at of table content_valid_time.
- `valid_time_id`: integer NULL — Column valid_time_id of table content_valid_time.
- primary key: id

### exhibit  (source backend: mongodb)
Source table exhibit.

- `active`: boolean NULL — Column active of table exhibit.
- `content_id`: integer NULL — Column content_id of table exhibit.
- `created_at`: integer NULL — Column created_at of table exhibit.
- `id`: integer NOT NULL — Column id of table exhibit.
- `type`: integer NULL — Column type of table exhibit.
- `updated_at`: integer NULL — Column updated_at of table exhibit.
- primary key: id

### exhibit_rucksack  (source backend: s3)
Source table exhibit_rucksack.

- `content_id`: integer NULL — Column content_id of table exhibit_rucksack.
- `created_at`: integer NULL — Column created_at of table exhibit_rucksack.
- `exhibit_id`: integer NULL — Column exhibit_id of table exhibit_rucksack.
- `id`: integer NOT NULL — Column id of table exhibit_rucksack.
- `rucksackid`: integer NULL — Column rucksackId of table exhibit_rucksack.
- `updated_at`: integer NULL — Column updated_at of table exhibit_rucksack.
- primary key: id

### exhibition_code  (source backend: s3)
Source table exhibition_code.

- `code`: text NOT NULL — Column code of table exhibition_code.
- `exhibition_code_series_id`: integer NULL — Column exhibition_code_series_id of table exhibition_code.
- `id`: integer NOT NULL — Column id of table exhibition_code.
- primary key: id

### exhibition_code_series  (source backend: mongodb)
Source table exhibition_code_series.

- `code_count`: integer NULL — Column code_count of table exhibition_code_series.
- `created_at`: integer NULL — Column created_at of table exhibition_code_series.
- `heritage_id`: integer NULL — Column heritage_id of table exhibition_code_series.
- `id`: integer NOT NULL — Column id of table exhibition_code_series.
- `updated_at`: integer NULL — Column updated_at of table exhibition_code_series.
- primary key: id

### flag  (source backend: rest)
Source table flag.

- `created_at`: integer NULL — Column created_at of table flag.
- `flag_group_id`: integer NULL — Column flag_group_id of table flag.
- `hidden`: boolean NULL — Column hidden of table flag.
- `id`: integer NOT NULL — Column id of table flag.
- `label`: boolean NULL — Column label of table flag.
- `operator`: text NULL — Column operator of table flag.
- `order`: integer NULL — Column order of table flag.
- `updated_at`: integer NULL — Column updated_at of table flag.
- primary key: id

### flag_group  (source backend: files)
Source table flag_group.

- `created_at`: integer NULL — Column created_at of table flag_group.
- `hidden`: boolean NULL — Column hidden of table flag_group.
- `id`: integer NOT NULL — Column id of table flag_group.
- `operator`: text NULL — Column operator of table flag_group.
- `order`: integer NULL — Column order of table flag_group.
- `updated_at`: integer NULL — Column updated_at of table flag_group.
- primary key: id

### flag_group_translation  (source backend: mongodb)
Source table flag_group_translation.

- `created_at`: integer NULL — Column created_at of table flag_group_translation.
- `flag_group_id`: integer NULL — Column flag_group_id of table flag_group_translation.
- `id`: integer NOT NULL — Column id of table flag_group_translation.
- `language_id`: integer NULL — Column language_id of table flag_group_translation.
- `title`: text NULL — Column title of table flag_group_translation.
- `updated_at`: integer NULL — Column updated_at of table flag_group_translation.
- primary key: id

### flag_translation  (source backend: files)
Source table flag_translation.

- `created_at`: integer NULL — Column created_at of table flag_translation.
- `disclaimer`: text NULL — Column disclaimer of table flag_translation.
- `flag_id`: integer NULL — Column flag_id of table flag_translation.
- `id`: integer NOT NULL — Column id of table flag_translation.
- `language_id`: integer NULL — Column language_id of table flag_translation.
- `title`: text NULL — Column title of table flag_translation.
- `updated_at`: integer NULL — Column updated_at of table flag_translation.
- primary key: id

### heritage  (source backend: rest)
Source table heritage.

- `created_at`: integer NULL — Column created_at of table heritage.
- `geom`: text NULL — Column geom of table heritage.
- `hidden`: boolean NULL — Column hidden of table heritage.
- `id`: integer NOT NULL — Column id of table heritage.
- `map_position_x`: float NULL — Column map_position_x of table heritage.
- `map_position_y`: float NULL — Column map_position_y of table heritage.
- `priority`: integer NULL — Column priority of table heritage.
- `published`: boolean NULL — Column published of table heritage.
- `updated_at`: integer NULL — Column updated_at of table heritage.
- primary key: id

### heritage_translation  (source backend: files)
Source table heritage_translation.

- `created_at`: integer NULL — Column created_at of table heritage_translation.
- `description`: text NULL — Column description of table heritage_translation.
- `heritage_id`: integer NULL — Column heritage_id of table heritage_translation.
- `id`: integer NOT NULL — Column id of table heritage_translation.
- `language_id`: integer NULL — Column language_id of table heritage_translation.
- `link_text`: text NULL — Column link_text of table heritage_translation.
- `link_url`: text NULL — Column link_url of table heritage_translation.
- `name`: text NULL — Column name of table heritage_translation.
- `short_name`: text NULL — Column short_name of table heritage_translation.
- `slug`: text NULL — Column slug of table heritage_translation.
- `updated_at`: integer NULL — Column updated_at of table heritage_translation.
- primary key: id

### language  (source backend: rest)
Source table language.

- `code`: text NULL — Column code of table language.
- `id`: integer NOT NULL — Column id of table language.
- `name`: text NULL — Column name of table language.
- primary key: id

### media  (source backend: s3)
Source table media.

- `content_id`: integer NULL — Column content_id of table media.
- `created_at`: integer NULL — Column created_at of table media.
- `exif`: text NULL — Column exif of table media.
- `filename`: text NULL — Column filename of table media.
- `heritage_id`: integer NULL — Column heritage_id of table media.
- `id`: integer NOT NULL — Column id of table media.
- `order`: integer NULL — Column order of table media.
- `page_id`: integer NULL — Column page_id of table media.
- `updated_at`: integer NULL — Column updated_at of table media.
- primary key: id

### media_translation  (source backend: mongodb)
Source table media_translation.

- `author`: text NULL — Column author of table media_translation.
- `copyright`: text NULL — Column copyright of table media_translation.
- `created_at`: integer NULL — Column created_at of table media_translation.
- `description`: text NULL — Column description of table media_translation.
- `id`: integer NOT NULL — Column id of table media_translation.
- `language_id`: integer NULL — Column language_id of table media_translation.
- `media_id`: integer NULL — Column media_id of table media_translation.
- `title`: text NULL — Column title of table media_translation.
- `updated_at`: integer NULL — Column updated_at of table media_translation.
- primary key: id

### page  (source backend: mongodb)
Source table page.

- `created_at`: integer NULL — Column created_at of table page.
- `id`: integer NOT NULL — Column id of table page.
- `name`: text NULL — Column name of table page.
- `updated_at`: integer NULL — Column updated_at of table page.
- primary key: id

### page_translation  (source backend: mongodb)
Source table page_translation.

- `created_at`: integer NULL — Column created_at of table page_translation.
- `description`: text NULL — Column description of table page_translation.
- `id`: integer NOT NULL — Column id of table page_translation.
- `language_id`: integer NULL — Column language_id of table page_translation.
- `page_id`: integer NULL — Column page_id of table page_translation.
- `slug`: text NULL — Column slug of table page_translation.
- `title`: text NULL — Column title of table page_translation.
- `updated_at`: integer NULL — Column updated_at of table page_translation.
- primary key: id

### poi  (source backend: rest)
Source table poi.

- `arrival_station`: text NULL — Column arrival_station of table poi.
- `arrival_url`: text NULL — Column arrival_url of table poi.
- `content_id`: integer NULL — Column content_id of table poi.
- `created_at`: integer NULL — Column created_at of table poi.
- `geom`: text NULL — Column geom of table poi.
- `id`: integer NOT NULL — Column id of table poi.
- `updated_at`: integer NULL — Column updated_at of table poi.
- primary key: id

### poi_translation  (source backend: mongodb)
Source table poi_translation.

- `created_at`: integer NULL — Column created_at of table poi_translation.
- `description`: text NULL — Column description of table poi_translation.
- `directions`: text NULL — Column directions of table poi_translation.
- `id`: integer NOT NULL — Column id of table poi_translation.
- `language_id`: integer NULL — Column language_id of table poi_translation.
- `poi_id`: integer NULL — Column poi_id of table poi_translation.
- `remarks`: text NULL — Column remarks of table poi_translation.
- `slug`: text NULL — Column slug of table poi_translation.
- `title`: text NULL — Column title of table poi_translation.
- `updated_at`: integer NULL — Column updated_at of table poi_translation.
- `youtube_id`: text NULL — Column youtube_id of table poi_translation.
- primary key: id

### related_tag  (source backend: s3)
Source table related_tag.

- `created_at`: integer NULL — Column created_at of table related_tag.
- `id`: integer NOT NULL — Column id of table related_tag.
- `related_tag_id`: integer NULL — Column related_tag_id of table related_tag.
- `tag_id`: integer NULL — Column tag_id of table related_tag.
- `updated_at`: integer NULL — Column updated_at of table related_tag.
- primary key: id

### route  (source backend: postgres)
Source table route.

- `arrival_station`: text NULL — Column arrival_station of table route.
- `arrival_url`: text NULL — Column arrival_url of table route.
- `ascent`: integer NULL — Column ascent of table route.
- `content_id`: integer NULL — Column content_id of table route.
- `created_at`: integer NULL — Column created_at of table route.
- `departure_station`: text NULL — Column departure_station of table route.
- `departure_url`: text NULL — Column departure_url of table route.
- `descent`: integer NULL — Column descent of table route.
- `difficulty`: integer NULL — Column difficulty of table route.
- `distance_in_km`: integer NULL — Column distance_in_km of table route.
- `duration_in_min`: integer NULL — Column duration_in_min of table route.
- `end_altitude`: integer NULL — Column end_altitude of table route.
- `geom`: text NULL — Column geom of table route.
- `id`: integer NOT NULL — Column id of table route.
- `max_altitude`: integer NULL — Column max_altitude of table route.
- `min_altitude`: integer NULL — Column min_altitude of table route.
- `print_available`: boolean NULL — Column print_available of table route.
- `profile`: text NULL — Column profile of table route.
- `start_altitude`: integer NULL — Column start_altitude of table route.
- `updated_at`: integer NULL — Column updated_at of table route.
- primary key: id

### route_translation  (source backend: s3)
Source table route_translation.

- `catering`: text NULL — Column catering of table route_translation.
- `created_at`: integer NULL — Column created_at of table route_translation.
- `description`: text NULL — Column description of table route_translation.
- `directions`: text NULL — Column directions of table route_translation.
- `id`: integer NOT NULL — Column id of table route_translation.
- `language_id`: integer NULL — Column language_id of table route_translation.
- `options`: text NULL — Column options of table route_translation.
- `remarks`: text NULL — Column remarks of table route_translation.
- `route_id`: integer NULL — Column route_id of table route_translation.
- `slug`: text NULL — Column slug of table route_translation.
- `title`: text NULL — Column title of table route_translation.
- `updated_at`: integer NULL — Column updated_at of table route_translation.
- `youtube_id`: text NULL — Column youtube_id of table route_translation.
- primary key: id

### rucksack  (source backend: mongodb)
Source table rucksack.

- `code`: text NULL — Column code of table rucksack.
- `created_at`: integer NULL — Column created_at of table rucksack.
- `id`: integer NOT NULL — Column id of table rucksack.
- `updated_at`: integer NULL — Column updated_at of table rucksack.
- `web_access_at`: integer NULL — Column web_access_at of table rucksack.
- `wnf_access_at`: integer NULL — Column wnf_access_at of table rucksack.
- primary key: id

### supplier  (source backend: mongodb)
Source table supplier.

- `address_addition`: text NULL — Column address_addition of table supplier.
- `city`: text NULL — Column city of table supplier.
- `content_id`: integer NULL — Column content_id of table supplier.
- `created_at`: integer NULL — Column created_at of table supplier.
- `email`: text NULL — Column email of table supplier.
- `geom`: text NULL — Column geom of table supplier.
- `id`: integer NOT NULL — Column id of table supplier.
- `phone`: text NULL — Column phone of table supplier.
- `street`: text NULL — Column street of table supplier.
- `street_number`: text NULL — Column street_number of table supplier.
- `updated_at`: integer NULL — Column updated_at of table supplier.
- `url`: text NULL — Column url of table supplier.
- `zip`: text NULL — Column zip of table supplier.
- primary key: id

### supplier_translation  (source backend: postgres)
Source table supplier_translation.

- `created_at`: integer NULL — Column created_at of table supplier_translation.
- `id`: integer NOT NULL — Column id of table supplier_translation.
- `language_id`: integer NULL — Column language_id of table supplier_translation.
- `name`: text NULL — Column name of table supplier_translation.
- `name_affix`: text NULL — Column name_affix of table supplier_translation.
- `remarks`: text NULL — Column remarks of table supplier_translation.
- `supplier_id`: integer NULL — Column supplier_id of table supplier_translation.
- `updated_at`: integer NULL — Column updated_at of table supplier_translation.
- primary key: id

### tag  (source backend: postgres)
Source table tag.

- `active`: boolean NULL — Column active of table tag.
- `created_at`: integer NULL — Column created_at of table tag.
- `id`: integer NOT NULL — Column id of table tag.
- `updated_at`: integer NULL — Column updated_at of table tag.
- primary key: id

### tag_translation  (source backend: postgres)
Source table tag_translation.

- `created_at`: integer NULL — Column created_at of table tag_translation.
- `description`: text NULL — Column description of table tag_translation.
- `id`: integer NOT NULL — Column id of table tag_translation.
- `language_id`: integer NULL — Column language_id of table tag_translation.
- `tag_id`: integer NULL — Column tag_id of table tag_translation.
- `title`: text NULL — Column title of table tag_translation.
- `updated_at`: integer NULL — Column updated_at of table tag_translation.
- primary key: id

### valid_time  (source backend: postgres)
Source table valid_time.

- `end_day`: integer NULL — Column end_day of table valid_time.
- `end_month`: integer NULL — Column end_month of table valid_time.
- `end_time`: timestamp NULL — Column end_time of table valid_time.
- `end_year`: integer NULL — Column end_year of table valid_time.
- `id`: integer NOT NULL — Column id of table valid_time.
- `reuse`: integer NULL — Column reuse of table valid_time.
- `start_day`: integer NULL — Column start_day of table valid_time.
- `start_month`: integer NULL — Column start_month of table valid_time.
- `start_time`: timestamp NULL — Column start_time of table valid_time.
- `start_year`: integer NULL — Column start_year of table valid_time.
- `title`: text NULL — Column title of table valid_time.
- primary key: id

### Relationships

- admin(heritage_id) -> heritage(id) [optional (may be NULL/dangling)]
- ambassador_translation(heritage_id) -> heritage(id) [optional (may be NULL/dangling)]
- ambassador_translation(poi_id) -> poi(id) [optional (may be NULL/dangling)]
- article(content_id) -> content(id) [optional (may be NULL/dangling)]
- article_translation(article_id) -> article(id) [optional (may be NULL/dangling)]
- article_translation(language_id) -> language(id) [optional (may be NULL/dangling)]
- content(heritage_id) -> heritage(id) [optional (may be NULL/dangling)]
- content_flag(content_id) -> content(id) [optional (may be NULL/dangling)]
- content_flag(flag_id) -> flag(id) [optional (may be NULL/dangling)]
- content_tag(content_id) -> content(id) [optional (may be NULL/dangling)]
- content_tag(tag_id) -> tag(id) [optional (may be NULL/dangling)]
- content_valid_time(content_id) -> content(id) [optional (may be NULL/dangling)]
- content_valid_time(valid_time_id) -> valid_time(id) [optional (may be NULL/dangling)]
- exhibit(content_id) -> content(id) [optional (may be NULL/dangling)]
- exhibit_rucksack(content_id) -> content(id) [optional (may be NULL/dangling)]
- exhibit_rucksack(exhibit_id) -> exhibit(id) [optional (may be NULL/dangling)]
- exhibit_rucksack(rucksackid) -> rucksack(id) [optional (may be NULL/dangling)]
- exhibition_code(exhibition_code_series_id) -> exhibition_code_series(id) [optional (may be NULL/dangling)]
- exhibition_code_series(heritage_id) -> heritage(id) [optional (may be NULL/dangling)]
- flag(flag_group_id) -> flag_group(id) [optional (may be NULL/dangling)]
- flag_group_translation(flag_group_id) -> flag_group(id) [optional (may be NULL/dangling)]
- flag_group_translation(language_id) -> language(id) [optional (may be NULL/dangling)]
- flag_translation(flag_id) -> flag(id) [optional (may be NULL/dangling)]
- flag_translation(language_id) -> language(id) [optional (may be NULL/dangling)]
- heritage_translation(heritage_id) -> heritage(id) [optional (may be NULL/dangling)]
- heritage_translation(language_id) -> language(id) [optional (may be NULL/dangling)]
- media(content_id) -> content(id) [optional (may be NULL/dangling)]
- media(heritage_id) -> heritage(id) [optional (may be NULL/dangling)]
- media(page_id) -> page(id) [optional (may be NULL/dangling)]
- media_translation(language_id) -> language(id) [optional (may be NULL/dangling)]
- media_translation(media_id) -> media(id) [optional (may be NULL/dangling)]
- page_translation(language_id) -> language(id) [optional (may be NULL/dangling)]
- page_translation(page_id) -> page(id) [optional (may be NULL/dangling)]
- poi(content_id) -> content(id) [optional (may be NULL/dangling)]
- poi_translation(language_id) -> language(id) [optional (may be NULL/dangling)]
- poi_translation(poi_id) -> poi(id) [optional (may be NULL/dangling)]
- related_tag(related_tag_id) -> tag(id) [optional (may be NULL/dangling)]
- related_tag(tag_id) -> tag(id) [optional (may be NULL/dangling)]
- route(content_id) -> content(id) [optional (may be NULL/dangling)]
- route_translation(language_id) -> language(id) [optional (may be NULL/dangling)]
- route_translation(route_id) -> route(id) [optional (may be NULL/dangling)]
- supplier(content_id) -> content(id) [optional (may be NULL/dangling)]
- supplier_translation(language_id) -> language(id) [optional (may be NULL/dangling)]
- supplier_translation(supplier_id) -> supplier(id) [optional (may be NULL/dangling)]
- tag_translation(language_id) -> language(id) [optional (may be NULL/dangling)]
- tag_translation(tag_id) -> tag(id) [optional (may be NULL/dangling)]

