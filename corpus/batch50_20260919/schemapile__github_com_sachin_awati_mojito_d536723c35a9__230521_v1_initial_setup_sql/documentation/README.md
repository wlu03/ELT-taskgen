# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Sachin Awati Mojito

## Specification

PROJECT OVERVIEW

This project builds three analytical marts over the Mojito localization schema (230521_V1__Initial_Setup.sql). Each mart is described in its own labelled section below. Every value in a mart is determined entirely by the source rows; no rule depends on the physical order in which rows arrive.

Source tables and the extraction backend each must be read from:

- Table asset is extracted from the rest backend.
- Table asset_extraction is extracted from the rest backend.
- Table asset_integrity_checker is extracted from the s3 backend.
- Table asset_integrity_checker_aud is extracted from the s3 backend.
- Table asset_text_unit is extracted from the s3 backend.
- Table asset_text_unit_to_tm_text_unit is extracted from the rest backend.
- Table authority is extracted from the s3 backend.
- Table drop is extracted from the s3 backend.
- Table group_authorities is extracted from the s3 backend.
- Table group_members is extracted from the rest backend.
- Table groups is extracted from the s3 backend.
- Table locale is extracted from the rest backend.
- Table pollable_task is extracted from the files backend.
- Table repository is extracted from the rest backend.
- Table repository_aud is extracted from the rest backend.
- Table repository_locale is extracted from the postgres backend.
- Table repository_locale_aud is extracted from the s3 backend.
- Table repository_locale_statistic is extracted from the mongodb backend.
- Table repository_statistic is extracted from the mongodb backend.
- Table revchanges is extracted from the rest backend.
- Table revinfo is extracted from the rest backend.
- Table tm is extracted from the files backend.
- Table tm_text_unit is extracted from the files backend.
- Table tm_text_unit_current_variant is extracted from the rest backend.
- Table tm_text_unit_current_variant_aud is extracted from the postgres backend.
- Table tm_text_unit_variant is extracted from the files backend.
- Table tm_text_unit_variant_comment is extracted from the postgres backend.
- Table translation_kit is extracted from the s3 backend.
- Table translation_kit_not_found_text_unit_ids is extracted from the mongodb backend.
- Table translation_kit_text_unit is extracted from the postgres backend.
- Table user is extracted from the s3 backend.

Relationships in the source schema, each labelled exactly as the schema labels it:

- Child table asset column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table asset column last_successful_asset_extraction_id references parent table asset_extraction column id; optional (may be NULL or dangling).
- Child table asset column repository_id references parent table repository column id; required.
- Child table asset_extraction column asset_id references parent table asset column id; optional (may be NULL or dangling).
- Child table asset_extraction column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table asset_extraction column pollable_task_id references parent table pollable_task column id; optional (may be NULL or dangling).
- Child table asset_integrity_checker column repository_id references parent table repository column id; required.
- Child table asset_integrity_checker_aud column rev references parent table revinfo column rev; required.
- Child table asset_integrity_checker_aud column revend references parent table revinfo column rev; optional (may be NULL or dangling).
- Child table asset_text_unit column asset_extraction_id references parent table asset_extraction column id; optional (may be NULL or dangling).
- Child table asset_text_unit column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table asset_text_unit_to_tm_text_unit column asset_extraction_id references parent table asset_extraction column id; optional (may be NULL or dangling).
- Child table asset_text_unit_to_tm_text_unit column asset_text_unit_id references parent table asset_text_unit column id; optional (may be NULL or dangling).
- Child table asset_text_unit_to_tm_text_unit column tm_text_unit_id references parent table tm_text_unit column id; optional (may be NULL or dangling).
- Child table authority column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table authority column user_id references parent table user column id; required.
- Child table drop column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table drop column export_pollable_task_id references parent table pollable_task column id; optional (may be NULL or dangling).
- Child table drop column import_pollable_task_id references parent table pollable_task column id; optional (may be NULL or dangling).
- Child table drop column repository_id references parent table repository column id; optional (may be NULL or dangling).
- Child table group_authorities column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table group_authorities column group_id references parent table groups column id; required.
- Child table group_members column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table group_members column group_id references parent table groups column id; required.
- Child table group_members column username references parent table user column id; required.
- Child table groups column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table pollable_task column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table repository column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table repository column repository_statistic_id references parent table repository_statistic column id; optional (may be NULL or dangling).
- Child table repository column tm_id references parent table tm column id; optional (may be NULL or dangling).
- Child table repository_aud column rev references parent table revinfo column rev; required.
- Child table repository_aud column revend references parent table revinfo column rev; optional (may be NULL or dangling).
- Child table repository_locale column locale_id references parent table locale column id; required.
- Child table repository_locale column repository_id references parent table repository column id; required.
- Child table repository_locale_aud column rev references parent table revinfo column rev; required.
- Child table repository_locale_aud column revend references parent table revinfo column rev; optional (may be NULL or dangling).
- Child table repository_locale_statistic column locale_id references parent table locale column id; required.
- Child table repository_locale_statistic column repository_statistic_id references parent table repository_statistic column id; optional (may be NULL or dangling).
- Child table repository_statistic column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table revchanges column rev references parent table revinfo column rev; required.
- Child table tm column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table tm_text_unit column asset_id references parent table asset column id; required.
- Child table tm_text_unit column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table tm_text_unit column tm_id references parent table tm column id; optional (may be NULL or dangling).
- Child table tm_text_unit_current_variant column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table tm_text_unit_current_variant column locale_id references parent table locale column id; optional (may be NULL or dangling).
- Child table tm_text_unit_current_variant column tm_id references parent table tm column id; optional (may be NULL or dangling).
- Child table tm_text_unit_current_variant column tm_text_unit_id references parent table tm_text_unit column id; optional (may be NULL or dangling).
- Child table tm_text_unit_current_variant column tm_text_unit_variant_id references parent table tm_text_unit_variant column id; optional (may be NULL or dangling).
- Child table tm_text_unit_current_variant_aud column rev references parent table revinfo column rev; required.
- Child table tm_text_unit_current_variant_aud column revend references parent table revinfo column rev; optional (may be NULL or dangling).
- Child table tm_text_unit_variant column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table tm_text_unit_variant column locale_id references parent table locale column id; optional (may be NULL or dangling).
- Child table tm_text_unit_variant column tm_text_unit_id references parent table tm_text_unit column id; optional (may be NULL or dangling).
- Child table tm_text_unit_variant_comment column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table tm_text_unit_variant_comment column tm_text_unit_variant_id references parent table tm_text_unit_variant column id; optional (may be NULL or dangling).
- Child table translation_kit column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table translation_kit column drop_id references parent table drop column id; optional (may be NULL or dangling).
- Child table translation_kit column locale_id references parent table locale column id; optional (may be NULL or dangling).
- Child table translation_kit_not_found_text_unit_ids column translation_kit_id references parent table translation_kit column id; required.
- Child table translation_kit_text_unit column created_by_user_id references parent table user column id; optional (may be NULL or dangling).
- Child table translation_kit_text_unit column tm_text_unit_id references parent table tm_text_unit column id; optional (may be NULL or dangling).
- Child table translation_kit_text_unit column tm_text_unit_variant_id references parent table tm_text_unit_variant column id; optional (may be NULL or dangling).
- Child table translation_kit_text_unit column translation_kit_id references parent table translation_kit column id; optional (may be NULL or dangling).

================================================================
MART user_translation_kit_text_unit_snapshot — Per-user latest-row snapshot over linked translation_kit_text_unit activity in the 230521_V1__Initial_Setup.sql schema.
================================================================

Grain: one row per user (id), INCLUDING user rows with no linked translation_kit_text_unit rows.

Key column: parent_key.

Output columns:

- parent_key (bigint): identifier of the user row. One row per value.
- parent_name (text): common_name of the user row, copied unchanged.
- event_count (bigint): number of translation_kit_text_unit rows for this user row; 0 when there are none. Every linked translation_kit_text_unit row counts, whether or not it carries a detected_language_probability value. An user row kept with no translation_kit_text_unit row reports 0 here, never 1: its placeholder holds no translation_kit_text_unit row to count.
- lifetime_amount (float): total of detected_language_probability over all matching translation_kit_text_unit rows; 0 when there are no rows and when none of those rows carries an detected_language_probability value; a row with no detected_language_probability value adds nothing, so a group with some values totals the values it has.
- latest_row_id (bigint): id of the row with the latest created_date; ties take the smallest id. It is 0 when there are no rows. Every translation_kit_text_unit row of the user row ranks, whether or not it carries a detected_language_probability value: the latest created_date wins even when that row's detected_language_probability is missing.
- latest_amount (float): detected_language_probability from that same latest row; 0 when there are no rows or when the winning value is missing.
- latest_label (text): detected_language from that same latest row; '(none)' when there are no rows or when the winning value is missing.
- latest_amount_share (float): latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose detected_language_probability is missing, so such a row gives 0.0.

Rules, each stated as the outcome it produces:

1. Read source table user.

2. Read source table translation_kit_text_unit.

3. From source table user there is one row per user row, keyed by id, carrying parent_key and parent_name.

4. Each user row is matched with the rows of translation_kit_text_unit whose created_by_user_id equals that user row's parent_key, carrying the translation_kit_text_unit columns created_by_user_id and id; preservation is left-sided on the user side, so a user row with no matching translation_kit_text_unit row is retained and receives the stated empty snapshot values.

5. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count and lifetime_amount for that row's matching rows.

6. For each parent_key the single row at which the ordering measure — created_date — is largest survives, ties broken by the smallest id, and latest_row_id, latest_amount and latest_label are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

7. The extremal row's attributes are attached to the grouped measures by matching parent_key to parent_key; preservation is left-sided on the measures, so a group with no rows at all keeps its measures.

8. The mart columns are named parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label; lifetime_amount reports its declared default of 0 — never NULL — for a group with no matching rows. For lifetime_amount, the default also applies to a group none of whose real rows carries an input value.

9. Guarded ratio, carried beside parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label: latest_amount_share is latest_amount divided by lifetime_amount as a fraction (units: fraction), rounded to 4 decimal places, and is 0.0 when the denominator lifetime_amount is 0 or has no value. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose detected_language_probability is missing, so such a row gives 0.0.

10. Deterministic output order: rows appear sorted in ascending parent_key sequence.

================================================================
MART tm_text_unit_variant_translation_kit_text_unit_distribution — Per-(tm_text_unit_variant, measure state) distribution of linked translation_kit_text_unit activity in the 230521_V1__Initial_Setup.sql schema.
================================================================

Grain: one row per (id, measure state) pair represented among linked translation_kit_text_unit rows; the absent state includes missing detected_language_probability values and a no-activity row for a tm_text_unit_variant row with no links. A linked translation_kit_text_unit row whose detected_language_probability has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state.

Output columns:

- entity_key (bigint): identifier of the tm_text_unit_variant row.
- measure_state (text): 'present' for a linked translation_kit_text_unit row whose detected_language_probability has a value; 'absent' when detected_language_probability is missing, including a tm_text_unit_variant row with no linked translation_kit_text_unit row. A linked translation_kit_text_unit row whose detected_language_probability has a value belongs only to the present state and never to the absent state.
- entity_name (text): comment of the tm_text_unit_variant row, copied unchanged.
- row_count (bigint): number of linked translation_kit_text_unit rows in this entity/state cell; a absent cell holding real translation_kit_text_unit rows whose detected_language_probability is missing COUNTS those rows, and only the placeholder cell of a tm_text_unit_variant row with no linked translation_kit_text_unit row at all reports 0.
- distinct_amount_count (bigint): number of unique non-missing detected_language_probability values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no detected_language_probability value at all — both for a tm_text_unit_variant row with no linked translation_kit_text_unit row and for an absent cell whose rows all have a missing detected_language_probability.
- total_amount (float): total of detected_language_probability in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an detected_language_probability value.
- max_amount (float): largest detected_language_probability in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an detected_language_probability value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules, each stated as the outcome it produces:

1. Read source table tm_text_unit_variant.

2. Read source table translation_kit_text_unit.

3. From source table tm_text_unit_variant, each id is carried as entity_key and its comment is carried as entity_name into the measure-state calculation.

4. The linked translation_kit_text_unit rows — those whose tm_text_unit_variant_id equals the entity's entity_key — are brought into each tm_text_unit_variant entity, carrying entity_key, entity_name and the translation_kit_text_unit columns id and tm_text_unit_variant_id; preservation is left-sided on the entity side, so an entity with no linked row is retained so its absent state is visible.

5. The present measure-state rows, carrying entity_key and entity_name, are the ones kept here: a real translation_kit_text_unit row whose detected_language_probability has a value.

6. There is one row per tm_text_unit_variant entity that has at least one row in the present measure state, and no row here for an entity with none, reporting, for entity_key and entity_name, row_count as the row count, distinct_amount_count as how many different non-missing detected_language_probability values occur (each different value counted once, however many rows repeat it), total_amount as the total detected_language_probability, and max_amount as the largest detected_language_probability.

7. Beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction (units: fraction), rounded to 4 decimal places; 0.0 when total_amount is 0.

8. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state 'present', the present measure state.

9. The absent measure-state rows, carrying entity_key and entity_name, are the ones kept here: detected_language_probability is missing, including the retained placeholder for a tm_text_unit_variant row with no translation_kit_text_unit rows. A real translation_kit_text_unit row whose detected_language_probability has a value belongs only to the present state and never to this absent state.

10. There is one row per tm_text_unit_variant entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting, for entity_key and entity_name, row_count as the row count, distinct_amount_count as how many different non-missing detected_language_probability values occur (each different value counted once, however many rows repeat it), total_amount as the total detected_language_probability, and max_amount as the largest detected_language_probability.

11. Beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction (units: fraction), rounded to 4 decimal places; 0.0 when total_amount is 0.

12. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state 'absent', the absent measure state.

13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share, keeping all rows from both: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

14. Deterministic output order: rows appear sorted in ascending entity_key sequence, then ascending measure_state sequence.

================================================================
MART tm_text_unit_translation_kit_text_unit_top — Per-tm_text_unit extremes over linked translation_kit_text_unit rows in the 230521_V1__Initial_Setup.sql schema: WHICH row is largest, not how large it is.
================================================================

Grain: one row per tm_text_unit (id), INCLUDING tm_text_unit rows with no linked translation_kit_text_unit rows.

Key column: parent_key.

Output columns:

- parent_key (bigint): identifier of the tm_text_unit row. One row per value.
- parent_name (text): comment of the tm_text_unit row, copied unchanged.
- top_measure (float): the largest detected_language_probability itself; 0 when the parent has no translation_kit_text_unit rows, and 0 when none of its rows carries a detected_language_probability value.
- tied_count (bigint): how many translation_kit_text_unit rows are tied at that largest detected_language_probability. 1 when exactly one row carries that largest detected_language_probability; 0 when there are no rows or when none of the rows carries a detected_language_probability value; a row with no detected_language_probability value never ties: only a row whose detected_language_probability value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.
- child_count (bigint): number of translation_kit_text_unit rows for this tm_text_unit row; 0 when there are none. Every linked translation_kit_text_unit row counts, whether or not it carries a detected_language_probability value. A tm_text_unit row kept with no translation_kit_text_unit row reports 0 here, never 1: its placeholder holds no translation_kit_text_unit row to count.
- total_measure (float): total of detected_language_probability over all of them; 0 when the parent has no translation_kit_text_unit rows, and 0 when none of its rows carries a detected_language_probability value (rows with no detected_language_probability value add nothing).
- top_label (text): the detected_language of the translation_kit_text_unit row with the LARGEST detected_language_probability for this tm_text_unit row. Ties in detected_language_probability are broken by taking the SMALLEST detected_language under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no detected_language value sorts after every labelled row; rows tied on both are resolved by the smallest id. A row with no detected_language_probability value still ranks, after every row that has one, so a parent holding at least one translation_kit_text_unit row always has a winning row — when NONE of its rows carries a detected_language_probability value the winner is the one the tie-break alone selects, not the no-rows default. The literal '(none)' when the parent has no translation_kit_text_unit rows at all, and '(none)' when the winning row has no detected_language value.
- top_row_id (bigint): the id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real translation_kit_text_unit row whenever the parent has any. This includes when none of them carries a detected_language_probability value. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no translation_kit_text_unit rows, or none of its rows carries a detected_language_probability value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a detected_language_probability value equal to the largest detected_language_probability value among the parent's rows; a row with no detected_language_probability value never holds the maximum. So a parent whose translation_kit_text_unit rows all lack a detected_language_probability value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

Rules, each stated as the outcome it produces:

1. Read source table tm_text_unit.

2. Read source table translation_kit_text_unit.

3. From source table tm_text_unit there is one row per tm_text_unit row, keyed by id, carrying parent_key and parent_name.

4. The rows of translation_kit_text_unit whose tm_text_unit_id equals the parent's parent_key are brought in, carrying the translation_kit_text_unit columns tm_text_unit_id and id; preservation is left-sided on the tm_text_unit side, so a tm_text_unit row with no translation_kit_text_unit rows still appears, with the declared defaults.

5. Within each parent_key group, the matched rows are ranked under an explicit total order — the measure detected_language_probability first, then the declared tie-break — so the extremal row is a function of the input and not of row order.

6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

7. For each parent_key the single row at which the ordering measure detected_language_probability is largest survives, ties broken by the smallest detected_language under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no detected_language value sorts after every row that has one), then the smallest id, and top_label and top_row_id are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

8. The extremal row's attributes are attached to the grouped measures by matching parent_key to parent_key; preservation is left-sided on the measures, so a group with no rows at all keeps its measures.

9. The mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults of 0 — never NULL — for a group with no matching rows. For top_measure and total_measure, the default also applies to a group none of whose real rows carries an input value.

10. Guarded ratio, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when the denominator total_measure is 0 or has no value.

11. Beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, tie_state is a categorical mapping with no numeric boundary: 'empty' when no row holds a maximum at all — the parent has no translation_kit_text_unit rows, or none of its rows carries a detected_language_probability value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do; equivalently tie_state follows tied_count, 'empty' when tied_count is 0, 'unique' when it is 1, otherwise 'tied' when it is 2 or more, and it is never null or blank. A row holds the maximum only when it carries a detected_language_probability value equal to the largest detected_language_probability value among the parent's rows; a row with no detected_language_probability value never holds the maximum, so a parent whose translation_kit_text_unit rows all lack a detected_language_probability value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

12. Deterministic output order: rows appear sorted in ascending parent_key sequence.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `user_translation_kit_text_unit_snapshot`

- Grain: One row per user (id), INCLUDING user rows with no linked translation_kit_text_unit rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share

```text
Mart 'user_translation_kit_text_unit_snapshot' has 10 declared semantic rules:
1. [source] Read source table user. (public source tables: user)
2. [source] Read source table translation_kit_text_unit. (public source tables: translation_kit_text_unit)
3. [derive] One row per user row, keyed by id. (public source tables: user | public carried/output columns: parent_key, parent_name)
4. [join] Bring in translation_kit_text_unit; a user row with no matching translation_kit_text_unit row is retained and receives the stated empty snapshot values. (public source tables: translation_kit_text_unit | public carried/output columns: created_by_user_id, id | join preservation: left | condition public identifiers: translation_kit_text_unit, created_by_user_id, parent_key)
5. [aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count, lifetime_amount for that row's matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount)
6. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest id, and take latest_row_id, latest_amount, latest_label from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, latest_row_id, latest_amount, latest_label)
7. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
8. [derive] Name the mart columns; lifetime_amount reports its declared default — never NULL — for a group with no matching rows. For lifetime_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label)
9. [ratio] Guarded ratios: latest_amount_share — latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose detected_language_probability is missing, so such a row gives 0.0. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `tm_text_unit_variant_translation_kit_text_unit_distribution`

- Grain: One row per (id, measure state) pair represented among linked translation_kit_text_unit rows; the absent state includes missing detected_language_probability values and a no-activity row for a tm_text_unit_variant row with no links. A linked translation_kit_text_unit row whose detected_language_probability has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'tm_text_unit_variant_translation_kit_text_unit_distribution' has 14 declared semantic rules:
1. [source] Read source table tm_text_unit_variant. (public source tables: tm_text_unit_variant)
2. [source] Read source table translation_kit_text_unit. (public source tables: translation_kit_text_unit)
3. [derive] Carry each id and its comment into the measure-state calculation. (public source tables: tm_text_unit_variant | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked translation_kit_text_unit rows into each tm_text_unit_variant entity; retain an entity with no linked row so its absent state is visible. (public source tables: translation_kit_text_unit | public carried/output columns: entity_key, entity_name, id, tm_text_unit_variant_id | join preservation: left | condition public identifiers: translation_kit_text_unit, tm_text_unit_variant_id, entity_key)
5. [filter] Keep the present measure-state rows: a real translation_kit_text_unit row whose detected_language_probability has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per tm_text_unit_variant entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing detected_language_probability values occur (each different value counted once, however many rows repeat it), total detected_language_probability, and largest detected_language_probability. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: detected_language_probability is missing, including the retained placeholder for a tm_text_unit_variant row with no translation_kit_text_unit rows. A real translation_kit_text_unit row whose detected_language_probability has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per tm_text_unit_variant entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing detected_language_probability values occur (each different value counted once, however many rows repeat it), total detected_language_probability, and largest detected_language_probability. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `tm_text_unit_translation_kit_text_unit_top`

- Grain: One row per tm_text_unit (id), INCLUDING tm_text_unit rows with no linked translation_kit_text_unit rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'tm_text_unit_translation_kit_text_unit_top' has 12 declared semantic rules:
1. [source] Read source table tm_text_unit. (public source tables: tm_text_unit)
2. [source] Read source table translation_kit_text_unit. (public source tables: translation_kit_text_unit)
3. [derive] One row per tm_text_unit row, keyed by id. (public source tables: tm_text_unit | public carried/output columns: parent_key, parent_name)
4. [join] Bring in translation_kit_text_unit: a tm_text_unit row with no translation_kit_text_unit rows still appears, with the declared defaults. (public source tables: translation_kit_text_unit | public carried/output columns: tm_text_unit_id, id | join preservation: left | condition public identifiers: translation_kit_text_unit, tm_text_unit_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest detected_language under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no detected_language value sorts after every row that has one), then the smallest id, and take top_label, top_row_id from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no translation_kit_text_unit rows, or none of its rows carries a detected_language_probability value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a detected_language_probability value equal to the largest detected_language_probability value among the parent's rows; a row with no detected_language_probability value never holds the maximum. So a parent whose translation_kit_text_unit rows all lack a detected_language_probability value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### asset  (source backend: rest)
Source table asset.

- `content`: text NOT NULL — Column content of table asset.
- `content_md5`: text NULL — Column content_md5 of table asset.
- `created_by_user_id`: bigint NULL — Column created_by_user_id of table asset.
- `created_date`: timestamp NULL — Column created_date of table asset.
- `id`: bigint NOT NULL — Column id of table asset.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table asset.
- `last_successful_asset_extraction_id`: bigint NULL — Column last_successful_asset_extraction_id of table asset.
- `path`: text NOT NULL — Column path of table asset.
- `repository_id`: bigint NOT NULL — Column repository_id of table asset.
- primary key: id

### asset_extraction  (source backend: rest)
Source table asset_extraction.

- `asset_id`: bigint NULL — Column asset_id of table asset_extraction.
- `content_md5`: text NULL — Column content_md5 of table asset_extraction.
- `created_by_user_id`: bigint NULL — Column created_by_user_id of table asset_extraction.
- `created_date`: timestamp NULL — Column created_date of table asset_extraction.
- `id`: bigint NOT NULL — Column id of table asset_extraction.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table asset_extraction.
- `pollable_task_id`: bigint NULL — Column pollable_task_id of table asset_extraction.
- primary key: id

### asset_integrity_checker  (source backend: s3)
Source table asset_integrity_checker.

- `asset_extension`: text NOT NULL — Column asset_extension of table asset_integrity_checker.
- `id`: bigint NOT NULL — Column id of table asset_integrity_checker.
- `integrity_checker_type`: text NOT NULL — Column integrity_checker_type of table asset_integrity_checker.
- `repository_id`: bigint NOT NULL — Column repository_id of table asset_integrity_checker.
- primary key: id

### asset_integrity_checker_aud  (source backend: s3)
Source table asset_integrity_checker_aud.

- `asset_extension`: text NULL — Column asset_extension of table asset_integrity_checker_aud.
- `id`: bigint NOT NULL — Column id of table asset_integrity_checker_aud.
- `integrity_checker_type`: text NULL — Column integrity_checker_type of table asset_integrity_checker_aud.
- `repository_id`: bigint NULL — Column repository_id of table asset_integrity_checker_aud.
- `rev`: integer NOT NULL — Column rev of table asset_integrity_checker_aud.
- `revend`: integer NULL — Column revend of table asset_integrity_checker_aud.
- `revtype`: integer NULL — Column revtype of table asset_integrity_checker_aud.
- primary key: id, rev

### asset_text_unit  (source backend: s3)
Source table asset_text_unit.

- `asset_extraction_id`: bigint NULL — Column asset_extraction_id of table asset_text_unit.
- `comment`: text NULL — Column comment of table asset_text_unit.
- `content`: text NULL — Column content of table asset_text_unit.
- `content_md5`: text NULL — Column content_md5 of table asset_text_unit.
- `created_by_user_id`: bigint NULL — Column created_by_user_id of table asset_text_unit.
- `created_date`: timestamp NULL — Column created_date of table asset_text_unit.
- `id`: bigint NOT NULL — Column id of table asset_text_unit.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table asset_text_unit.
- `md5`: text NULL — Column md5 of table asset_text_unit.
- `name`: text NULL — Column name of table asset_text_unit.
- primary key: id

### asset_text_unit_to_tm_text_unit  (source backend: rest)
Source table asset_text_unit_to_tm_text_unit.

- `asset_extraction_id`: bigint NULL — Column asset_extraction_id of table asset_text_unit_to_tm_text_unit.
- `asset_text_unit_id`: bigint NULL — Column asset_text_unit_id of table asset_text_unit_to_tm_text_unit.
- `id`: bigint NOT NULL — Column id of table asset_text_unit_to_tm_text_unit.
- `tm_text_unit_id`: bigint NULL — Column tm_text_unit_id of table asset_text_unit_to_tm_text_unit.
- primary key: id

### authority  (source backend: s3)
Source table authority.

- `authority`: text NULL — Column authority of table authority.
- `created_by_user_id`: bigint NULL — Column created_by_user_id of table authority.
- `created_date`: timestamp NULL — Column created_date of table authority.
- `id`: bigint NOT NULL — Column id of table authority.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table authority.
- `user_id`: bigint NOT NULL — Column user_id of table authority.
- primary key: id

### drop  (source backend: s3)
Source table drop.

- `canceled`: boolean NULL — Column canceled of table drop.
- `created_by_user_id`: bigint NULL — Column created_by_user_id of table drop.
- `created_date`: timestamp NULL — Column created_date of table drop.
- `drop_exporter_config`: text NULL — Column drop_exporter_config of table drop.
- `drop_exporter_type`: text NULL — Column drop_exporter_type of table drop.
- `export_pollable_task_id`: bigint NULL — Column export_pollable_task_id of table drop.
- `id`: bigint NOT NULL — Column id of table drop.
- `import_pollable_task_id`: bigint NULL — Column import_pollable_task_id of table drop.
- `last_imported_date`: timestamp NULL — Column last_imported_date of table drop.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table drop.
- `name`: text NULL — Column name of table drop.
- `repository_id`: bigint NULL — Column repository_id of table drop.
- primary key: id

### group_authorities  (source backend: s3)
Source table group_authorities.

- `authority`: text NOT NULL — Column authority of table group_authorities.
- `created_by_user_id`: bigint NULL — Column created_by_user_id of table group_authorities.
- `created_date`: timestamp NULL — Column created_date of table group_authorities.
- `group_id`: bigint NOT NULL — Column group_id of table group_authorities.
- `id`: bigint NOT NULL — Column id of table group_authorities.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table group_authorities.
- primary key: id

### group_members  (source backend: rest)
Source table group_members.

- `created_by_user_id`: bigint NULL — Column created_by_user_id of table group_members.
- `created_date`: timestamp NULL — Column created_date of table group_members.
- `group_id`: bigint NOT NULL — Column group_id of table group_members.
- `id`: bigint NOT NULL — Column id of table group_members.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table group_members.
- `username`: bigint NOT NULL — Column username of table group_members.
- primary key: id

### groups  (source backend: s3)
Source table groups.

- `created_by_user_id`: bigint NULL — Column created_by_user_id of table groups.
- `created_date`: timestamp NULL — Column created_date of table groups.
- `group_name`: text NOT NULL — Column group_name of table groups.
- `id`: bigint NOT NULL — Column id of table groups.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table groups.
- primary key: id

### locale  (source backend: rest)
Source table locale.

- `bcp47_tag`: text NOT NULL — Column bcp47_tag of table locale.
- `id`: bigint NOT NULL — Column id of table locale.
- primary key: id

### pollable_task  (source backend: files)
Source table pollable_task.

- `created_by_user_id`: bigint NULL — Column created_by_user_id of table pollable_task.
- `created_date`: timestamp NULL — Column created_date of table pollable_task.
- `error_message`: text NULL — Column error_message of table pollable_task.
- `error_stacks`: text NULL — Column error_stacks of table pollable_task.
- `expected_sub_task_number`: integer NOT NULL — Column expected_sub_task_number of table pollable_task.
- `finished_date`: timestamp NULL — Column finished_date of table pollable_task.
- `id`: bigint NOT NULL — Column id of table pollable_task.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table pollable_task.
- `message`: text NULL — Column message of table pollable_task.
- `name`: text NOT NULL — Column name of table pollable_task.
- `parent_task_id`: bigint NULL — Column parent_task_id of table pollable_task.
- `timeout`: bigint NULL — Column timeout of table pollable_task.
- primary key: id

### repository  (source backend: rest)
Source table repository.

- `created_by_user_id`: bigint NULL — Column created_by_user_id of table repository.
- `created_date`: timestamp NULL — Column created_date of table repository.
- `description`: text NULL — Column description of table repository.
- `drop_exporter_type`: text NULL — Column drop_exporter_type of table repository.
- `id`: bigint NOT NULL — Column id of table repository.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table repository.
- `name`: text NOT NULL — Column name of table repository.
- `repository_statistic_id`: bigint NULL — Column repository_statistic_id of table repository.
- `tm_id`: bigint NULL — Column tm_id of table repository.
- primary key: id

### repository_aud  (source backend: rest)
Source table repository_aud.

- `created_by_user_id`: bigint NULL — Column created_by_user_id of table repository_aud.
- `description`: text NULL — Column description of table repository_aud.
- `drop_exporter_type`: text NULL — Column drop_exporter_type of table repository_aud.
- `id`: bigint NOT NULL — Column id of table repository_aud.
- `name`: text NULL — Column name of table repository_aud.
- `repository_statistic_id`: bigint NULL — Column repository_statistic_id of table repository_aud.
- `rev`: integer NOT NULL — Column rev of table repository_aud.
- `revend`: integer NULL — Column revend of table repository_aud.
- `revtype`: integer NULL — Column revtype of table repository_aud.
- `tm_id`: bigint NULL — Column tm_id of table repository_aud.
- primary key: id, rev

### repository_locale  (source backend: postgres)
Source table repository_locale.

- `id`: bigint NOT NULL — Column id of table repository_locale.
- `locale_id`: bigint NOT NULL — Column locale_id of table repository_locale.
- `parent_locale`: bigint NULL — Column parent_locale of table repository_locale.
- `repository_id`: bigint NOT NULL — Column repository_id of table repository_locale.
- `to_be_fully_translated`: boolean NULL — Column to_be_fully_translated of table repository_locale.
- primary key: id

### repository_locale_aud  (source backend: s3)
Source table repository_locale_aud.

- `id`: bigint NOT NULL — Column id of table repository_locale_aud.
- `locale_id`: bigint NULL — Column locale_id of table repository_locale_aud.
- `parent_locale`: bigint NULL — Column parent_locale of table repository_locale_aud.
- `repository_id`: bigint NULL — Column repository_id of table repository_locale_aud.
- `rev`: integer NOT NULL — Column rev of table repository_locale_aud.
- `revend`: integer NULL — Column revend of table repository_locale_aud.
- `revtype`: integer NULL — Column revtype of table repository_locale_aud.
- `to_be_fully_translated`: boolean NULL — Column to_be_fully_translated of table repository_locale_aud.
- primary key: id, rev

### repository_locale_statistic  (source backend: mongodb)
Source table repository_locale_statistic.

- `id`: bigint NOT NULL — Column id of table repository_locale_statistic.
- `include_in_file_count`: bigint NULL — Column include_in_file_count of table repository_locale_statistic.
- `locale_id`: bigint NOT NULL — Column locale_id of table repository_locale_statistic.
- `repository_statistic_id`: bigint NULL — Column repository_statistic_id of table repository_locale_statistic.
- `review_needed_count`: bigint NULL — Column review_needed_count of table repository_locale_statistic.
- `translated_count`: bigint NULL — Column translated_count of table repository_locale_statistic.
- `translation_needed_count`: bigint NULL — Column translation_needed_count of table repository_locale_statistic.
- primary key: id

### repository_statistic  (source backend: mongodb)
Source table repository_statistic.

- `created_by_user_id`: bigint NULL — Column created_by_user_id of table repository_statistic.
- `created_date`: timestamp NULL — Column created_date of table repository_statistic.
- `id`: bigint NOT NULL — Column id of table repository_statistic.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table repository_statistic.
- `uncommented_text_unit_count`: bigint NULL — Column uncommented_text_unit_count of table repository_statistic.
- `unused_text_unit_count`: bigint NULL — Column unused_text_unit_count of table repository_statistic.
- `unused_text_unit_word_count`: bigint NULL — Column unused_text_unit_word_count of table repository_statistic.
- `used_text_unit_count`: bigint NULL — Column used_text_unit_count of table repository_statistic.
- `used_text_unit_word_count`: bigint NULL — Column used_text_unit_word_count of table repository_statistic.
- primary key: id

### revchanges  (source backend: rest)
Source table revchanges.

- `entityname`: text NULL — Column entityname of table revchanges.
- `rev`: integer NOT NULL — Column rev of table revchanges.

### revinfo  (source backend: rest)
Source table revinfo.

- `rev`: integer NOT NULL — Column rev of table revinfo.
- `revtstmp`: bigint NULL — Column revtstmp of table revinfo.
- primary key: rev

### tm  (source backend: files)
Source table tm.

- `created_by_user_id`: bigint NULL — Column created_by_user_id of table tm.
- `created_date`: timestamp NULL — Column created_date of table tm.
- `id`: bigint NOT NULL — Column id of table tm.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table tm.
- primary key: id

### tm_text_unit  (source backend: files)
Source table tm_text_unit.

- `asset_id`: bigint NOT NULL — Column asset_id of table tm_text_unit.
- `comment`: text NULL — Column comment of table tm_text_unit.
- `content`: text NULL — Column content of table tm_text_unit.
- `content_md5`: text NULL — Column content_md5 of table tm_text_unit.
- `created_by_user_id`: bigint NULL — Column created_by_user_id of table tm_text_unit.
- `created_date`: timestamp NULL — Column created_date of table tm_text_unit.
- `id`: bigint NOT NULL — Column id of table tm_text_unit.
- `md5`: text NULL — Column md5 of table tm_text_unit.
- `name`: text NULL — Column name of table tm_text_unit.
- `tm_id`: bigint NULL — Column tm_id of table tm_text_unit.
- primary key: id

### tm_text_unit_current_variant  (source backend: rest)
Source table tm_text_unit_current_variant.

- `created_by_user_id`: bigint NULL — Column created_by_user_id of table tm_text_unit_current_variant.
- `created_date`: timestamp NULL — Column created_date of table tm_text_unit_current_variant.
- `id`: bigint NOT NULL — Column id of table tm_text_unit_current_variant.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table tm_text_unit_current_variant.
- `locale_id`: bigint NULL — Column locale_id of table tm_text_unit_current_variant.
- `tm_id`: bigint NULL — Column tm_id of table tm_text_unit_current_variant.
- `tm_text_unit_id`: bigint NULL — Column tm_text_unit_id of table tm_text_unit_current_variant.
- `tm_text_unit_variant_id`: bigint NULL — Column tm_text_unit_variant_id of table tm_text_unit_current_variant.
- primary key: id

### tm_text_unit_current_variant_aud  (source backend: postgres)
Source table tm_text_unit_current_variant_aud.

- `created_by_user_id`: bigint NULL — Column created_by_user_id of table tm_text_unit_current_variant_aud.
- `id`: bigint NOT NULL — Column id of table tm_text_unit_current_variant_aud.
- `locale_id`: bigint NULL — Column locale_id of table tm_text_unit_current_variant_aud.
- `rev`: integer NOT NULL — Column rev of table tm_text_unit_current_variant_aud.
- `revend`: integer NULL — Column revend of table tm_text_unit_current_variant_aud.
- `revtype`: integer NULL — Column revtype of table tm_text_unit_current_variant_aud.
- `tm_id`: bigint NULL — Column tm_id of table tm_text_unit_current_variant_aud.
- `tm_text_unit_id`: bigint NULL — Column tm_text_unit_id of table tm_text_unit_current_variant_aud.
- `tm_text_unit_variant_id`: bigint NULL — Column tm_text_unit_variant_id of table tm_text_unit_current_variant_aud.
- primary key: id, rev

### tm_text_unit_variant  (source backend: files)
Source table tm_text_unit_variant.

- `comment`: text NULL — Column comment of table tm_text_unit_variant.
- `content`: text NULL — Column content of table tm_text_unit_variant.
- `content_md5`: text NULL — Column content_md5 of table tm_text_unit_variant.
- `created_by_user_id`: bigint NULL — Column created_by_user_id of table tm_text_unit_variant.
- `created_date`: timestamp NULL — Column created_date of table tm_text_unit_variant.
- `id`: bigint NOT NULL — Column id of table tm_text_unit_variant.
- `included_in_localized_file`: boolean NULL — Column included_in_localized_file of table tm_text_unit_variant.
- `locale_id`: bigint NULL — Column locale_id of table tm_text_unit_variant.
- `status`: text NOT NULL — Column status of table tm_text_unit_variant.
- `tm_text_unit_id`: bigint NULL — Column tm_text_unit_id of table tm_text_unit_variant.
- primary key: id

### tm_text_unit_variant_comment  (source backend: postgres)
Source table tm_text_unit_variant_comment.

- `content`: text NULL — Column content of table tm_text_unit_variant_comment.
- `created_by_user_id`: bigint NULL — Column created_by_user_id of table tm_text_unit_variant_comment.
- `created_date`: timestamp NULL — Column created_date of table tm_text_unit_variant_comment.
- `id`: bigint NOT NULL — Column id of table tm_text_unit_variant_comment.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table tm_text_unit_variant_comment.
- `severity`: text NULL — Column severity of table tm_text_unit_variant_comment.
- `tm_text_unit_variant_id`: bigint NULL — Column tm_text_unit_variant_id of table tm_text_unit_variant_comment.
- `type`: text NULL — Column type of table tm_text_unit_variant_comment.
- primary key: id

### translation_kit  (source backend: s3)
Source table translation_kit.

- `created_by_user_id`: bigint NULL — Column created_by_user_id of table translation_kit.
- `created_date`: timestamp NULL — Column created_date of table translation_kit.
- `drop_id`: bigint NULL — Column drop_id of table translation_kit.
- `id`: bigint NOT NULL — Column id of table translation_kit.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table translation_kit.
- `locale_id`: bigint NULL — Column locale_id of table translation_kit.
- `num_bad_language_detections`: integer NULL — Column num_bad_language_detections of table translation_kit.
- `num_source_equals_target`: integer NULL — Column num_source_equals_target of table translation_kit.
- `num_translated_translation_kit_units`: integer NULL — Column num_translated_translation_kit_units of table translation_kit.
- `num_translation_kit_units`: integer NULL — Column num_translation_kit_units of table translation_kit.
- `type`: integer NULL — Column type of table translation_kit.
- primary key: id

### translation_kit_not_found_text_unit_ids  (source backend: mongodb)
Source table translation_kit_not_found_text_unit_ids.

- `not_found_text_unit_ids`: text NULL — Column not_found_text_unit_ids of table translation_kit_not_found_text_unit_ids.
- `translation_kit_id`: bigint NOT NULL — Column translation_kit_id of table translation_kit_not_found_text_unit_ids.

### translation_kit_text_unit  (source backend: postgres)
Source table translation_kit_text_unit.

- `created_by_user_id`: bigint NULL — Column created_by_user_id of table translation_kit_text_unit.
- `created_date`: timestamp NULL — Column created_date of table translation_kit_text_unit.
- `detected_language`: text NULL — Column detected_language of table translation_kit_text_unit.
- `detected_language_exception`: text NULL — Column detected_language_exception of table translation_kit_text_unit.
- `detected_language_expected`: text NULL — Column detected_language_expected of table translation_kit_text_unit.
- `detected_language_probability`: float NULL — Column detected_language_probability of table translation_kit_text_unit.
- `id`: bigint NOT NULL — Column id of table translation_kit_text_unit.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table translation_kit_text_unit.
- `source_equals_target`: boolean NULL — Column source_equals_target of table translation_kit_text_unit.
- `tm_text_unit_id`: bigint NULL — Column tm_text_unit_id of table translation_kit_text_unit.
- `tm_text_unit_variant_id`: bigint NULL — Column tm_text_unit_variant_id of table translation_kit_text_unit.
- `translation_kit_id`: bigint NULL — Column translation_kit_id of table translation_kit_text_unit.
- primary key: id

### user  (source backend: s3)
Source table user.

- `common_name`: text NULL — Column common_name of table user.
- `created_by_user_id`: bigint NULL — Column created_by_user_id of table user.
- `created_date`: timestamp NULL — Column created_date of table user.
- `enabled`: boolean NULL — Column enabled of table user.
- `given_name`: text NULL — Column given_name of table user.
- `id`: bigint NOT NULL — Column id of table user.
- `last_modified_date`: timestamp NULL — Column last_modified_date of table user.
- `password`: text NULL — Column password of table user.
- `surname`: text NULL — Column surname of table user.
- `username`: text NOT NULL — Column username of table user.
- primary key: id

### Relationships

- asset(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- asset(last_successful_asset_extraction_id) -> asset_extraction(id) [optional (may be NULL/dangling)]
- asset(repository_id) -> repository(id) [required]
- asset_extraction(asset_id) -> asset(id) [optional (may be NULL/dangling)]
- asset_extraction(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- asset_extraction(pollable_task_id) -> pollable_task(id) [optional (may be NULL/dangling)]
- asset_integrity_checker(repository_id) -> repository(id) [required]
- asset_integrity_checker_aud(rev) -> revinfo(rev) [required]
- asset_integrity_checker_aud(revend) -> revinfo(rev) [optional (may be NULL/dangling)]
- asset_text_unit(asset_extraction_id) -> asset_extraction(id) [optional (may be NULL/dangling)]
- asset_text_unit(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- asset_text_unit_to_tm_text_unit(asset_extraction_id) -> asset_extraction(id) [optional (may be NULL/dangling)]
- asset_text_unit_to_tm_text_unit(asset_text_unit_id) -> asset_text_unit(id) [optional (may be NULL/dangling)]
- asset_text_unit_to_tm_text_unit(tm_text_unit_id) -> tm_text_unit(id) [optional (may be NULL/dangling)]
- authority(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- authority(user_id) -> user(id) [required]
- drop(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- drop(export_pollable_task_id) -> pollable_task(id) [optional (may be NULL/dangling)]
- drop(import_pollable_task_id) -> pollable_task(id) [optional (may be NULL/dangling)]
- drop(repository_id) -> repository(id) [optional (may be NULL/dangling)]
- group_authorities(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- group_authorities(group_id) -> groups(id) [required]
- group_members(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- group_members(group_id) -> groups(id) [required]
- group_members(username) -> user(id) [required]
- groups(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- pollable_task(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- repository(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- repository(repository_statistic_id) -> repository_statistic(id) [optional (may be NULL/dangling)]
- repository(tm_id) -> tm(id) [optional (may be NULL/dangling)]
- repository_aud(rev) -> revinfo(rev) [required]
- repository_aud(revend) -> revinfo(rev) [optional (may be NULL/dangling)]
- repository_locale(locale_id) -> locale(id) [required]
- repository_locale(repository_id) -> repository(id) [required]
- repository_locale_aud(rev) -> revinfo(rev) [required]
- repository_locale_aud(revend) -> revinfo(rev) [optional (may be NULL/dangling)]
- repository_locale_statistic(locale_id) -> locale(id) [required]
- repository_locale_statistic(repository_statistic_id) -> repository_statistic(id) [optional (may be NULL/dangling)]
- repository_statistic(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- revchanges(rev) -> revinfo(rev) [required]
- tm(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- tm_text_unit(asset_id) -> asset(id) [required]
- tm_text_unit(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- tm_text_unit(tm_id) -> tm(id) [optional (may be NULL/dangling)]
- tm_text_unit_current_variant(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- tm_text_unit_current_variant(locale_id) -> locale(id) [optional (may be NULL/dangling)]
- tm_text_unit_current_variant(tm_id) -> tm(id) [optional (may be NULL/dangling)]
- tm_text_unit_current_variant(tm_text_unit_id) -> tm_text_unit(id) [optional (may be NULL/dangling)]
- tm_text_unit_current_variant(tm_text_unit_variant_id) -> tm_text_unit_variant(id) [optional (may be NULL/dangling)]
- tm_text_unit_current_variant_aud(rev) -> revinfo(rev) [required]
- tm_text_unit_current_variant_aud(revend) -> revinfo(rev) [optional (may be NULL/dangling)]
- tm_text_unit_variant(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- tm_text_unit_variant(locale_id) -> locale(id) [optional (may be NULL/dangling)]
- tm_text_unit_variant(tm_text_unit_id) -> tm_text_unit(id) [optional (may be NULL/dangling)]
- tm_text_unit_variant_comment(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- tm_text_unit_variant_comment(tm_text_unit_variant_id) -> tm_text_unit_variant(id) [optional (may be NULL/dangling)]
- translation_kit(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- translation_kit(drop_id) -> drop(id) [optional (may be NULL/dangling)]
- translation_kit(locale_id) -> locale(id) [optional (may be NULL/dangling)]
- translation_kit_not_found_text_unit_ids(translation_kit_id) -> translation_kit(id) [required]
- translation_kit_text_unit(created_by_user_id) -> user(id) [optional (may be NULL/dangling)]
- translation_kit_text_unit(tm_text_unit_id) -> tm_text_unit(id) [optional (may be NULL/dangling)]
- translation_kit_text_unit(tm_text_unit_variant_id) -> tm_text_unit_variant(id) [optional (may be NULL/dangling)]
- translation_kit_text_unit(translation_kit_id) -> translation_kit(id) [optional (may be NULL/dangling)]

