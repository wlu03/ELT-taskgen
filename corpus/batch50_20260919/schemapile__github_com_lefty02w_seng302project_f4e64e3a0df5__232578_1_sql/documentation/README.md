# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Lefty02w Seng302project

## Specification

PROJECT OVERVIEW

This project builds three analytical marts over the 232578_1.sql schema of the "Github Com Lefty02w Seng302project" application. The data is spread across several extraction backends, and every source table must be extracted from the backend named for it here.

Source tables and their extraction backends:
- Table artist is extracted from the postgres backend.
- Table artist_country is extracted from the mongodb backend.
- Table artist_genre is extracted from the postgres backend.
- Table artist_profile is extracted from the files backend.
- Table artist_profile_photo is extracted from the s3 backend.
- Table attend_event is extracted from the rest backend.
- Table destination is extracted from the rest backend.
- Table destination_change is extracted from the mongodb backend.
- Table destination_photo is extracted from the files backend.
- Table destination_request is extracted from the rest backend.
- Table destination_traveller_type is extracted from the rest backend.
- Table event_artists is extracted from the rest backend.
- Table event_genres is extracted from the mongodb backend.
- Table event_photo is extracted from the files backend.
- Table event_type is extracted from the s3 backend.
- Table events is extracted from the s3 backend.
- Table follow_artist is extracted from the rest backend.
- Table follow_destination is extracted from the s3 backend.
- Table music_genre is extracted from the rest backend.
- Table nationality is extracted from the s3 backend.
- Table passport_country is extracted from the files backend.
- Table personal_photo is extracted from the s3 backend.
- Table photo is extracted from the postgres backend.
- Table profile is extracted from the rest backend.
- Table profile_nationality is extracted from the postgres backend.
- Table profile_passport_country is extracted from the files backend.
- Table profile_roles is extracted from the rest backend.
- Table profile_traveller_type is extracted from the s3 backend.
- Table roles is extracted from the s3 backend.
- Table thumbnail_link is extracted from the mongodb backend.
- Table traveller_type is extracted from the rest backend.
- Table treasure_hunt is extracted from the files backend.
- Table trip is extracted from the rest backend.
- Table trip_destination is extracted from the s3 backend.
- Table type_of_events is extracted from the rest backend.
- Table undo_stack is extracted from the rest backend.

RELATIONSHIPS BETWEEN THE SOURCE TABLES

Each statement below names the child table with its keys and the parent table with its keys, and is labelled required or optional exactly as the source schema declares it.

- Child table artist_country on artist_id refers to parent table artist on artist_id; this relationship is required.
- Child table artist_country on country_id refers to parent table passport_country on passport_country_id; this relationship is required.
- Child table artist_genre on artist_id refers to parent table artist on artist_id; this relationship is required.
- Child table artist_genre on genre_id refers to parent table music_genre on genre_id; this relationship is required.
- Child table artist_profile on artist_id refers to parent table artist on artist_id; this relationship is required.
- Child table artist_profile on profile_id refers to parent table profile on profile_id; this relationship is optional (may be NULL or dangling).
- Child table artist_profile_photo on artist_id refers to parent table artist on artist_id; this relationship is required.
- Child table artist_profile_photo on photo_id refers to parent table photo on photo_id; this relationship is required.
- Child table attend_event on event_id refers to parent table events on event_id; this relationship is required.
- Child table attend_event on profile_id refers to parent table profile on profile_id; this relationship is required.
- Child table destination on profile_id refers to parent table profile on profile_id; this relationship is required.
- Child table destination_change on request_id refers to parent table destination_request on id; this relationship is required.
- Child table destination_change on traveller_type_id refers to parent table traveller_type on traveller_type_id; this relationship is required.
- Child table destination_photo on destination_id refers to parent table destination on destination_id; this relationship is required.
- Child table destination_photo on photo_id refers to parent table photo on photo_id; this relationship is required.
- Child table destination_photo on profile_id refers to parent table profile on profile_id; this relationship is required.
- Child table destination_request on destination_id refers to parent table destination on destination_id; this relationship is optional (may be NULL or dangling).
- Child table destination_request on profile_id refers to parent table profile on profile_id; this relationship is optional (may be NULL or dangling).
- Child table destination_traveller_type on destination_id refers to parent table destination on destination_id; this relationship is required.
- Child table destination_traveller_type on traveller_type_id refers to parent table traveller_type on traveller_type_id; this relationship is required.
- Child table event_artists on artist_id refers to parent table artist on artist_id; this relationship is required.
- Child table event_artists on event_id refers to parent table events on event_id; this relationship is required.
- Child table event_genres on event_id refers to parent table events on event_id; this relationship is required.
- Child table event_genres on genre_id refers to parent table music_genre on genre_id; this relationship is required.
- Child table event_photo on event_id refers to parent table events on event_id; this relationship is required.
- Child table event_photo on photo_id refers to parent table photo on photo_id; this relationship is required.
- Child table event_type on event_id refers to parent table events on event_id; this relationship is required.
- Child table event_type on type_id refers to parent table type_of_events on type_id; this relationship is required.
- Child table events on destination_id refers to parent table destination on destination_id; this relationship is required.
- Child table follow_artist on artist_id refers to parent table artist on artist_id; this relationship is required.
- Child table follow_artist on profile_id refers to parent table profile on profile_id; this relationship is required.
- Child table follow_destination on destination_id refers to parent table destination on destination_id; this relationship is required.
- Child table follow_destination on profile_id refers to parent table profile on profile_id; this relationship is required.
- Child table personal_photo on photo_id refers to parent table photo on photo_id; this relationship is required.
- Child table personal_photo on profile_id refers to parent table profile on profile_id; this relationship is required.
- Child table profile_nationality on nationality refers to parent table nationality on nationality_id; this relationship is required.
- Child table profile_nationality on profile refers to parent table profile on profile_id; this relationship is required.
- Child table profile_passport_country on passport_country refers to parent table passport_country on passport_country_id; this relationship is required.
- Child table profile_passport_country on profile refers to parent table profile on profile_id; this relationship is required.
- Child table profile_roles on profile_id refers to parent table profile on profile_id; this relationship is required.
- Child table profile_roles on role_id refers to parent table roles on role_id; this relationship is required.
- Child table profile_traveller_type on profile refers to parent table profile on profile_id; this relationship is required.
- Child table profile_traveller_type on traveller_type refers to parent table traveller_type on traveller_type_id; this relationship is required.
- Child table thumbnail_link on photo_id refers to parent table photo on photo_id; this relationship is required.
- Child table thumbnail_link on thumbnail_id refers to parent table photo on photo_id; this relationship is required.
- Child table treasure_hunt on destination_id refers to parent table destination on destination_id; this relationship is required.
- Child table treasure_hunt on profile_id refers to parent table profile on profile_id; this relationship is required.
- Child table trip on profile_id refers to parent table profile on profile_id; this relationship is required.
- Child table trip_destination on destination_id refers to parent table destination on destination_id; this relationship is required.
- Child table trip_destination on trip_id refers to parent table trip on trip_id; this relationship is required.
- Child table undo_stack on profile_id refers to parent table profile on profile_id; this relationship is optional (may be NULL or dangling).

Throughout this document, "rounded to 4 decimal places" means ordinary half-up decimal rounding to four places, and a stated default value is produced literally: the column is never null or blank where a default is declared.

=====================================================================
MART profile_undo_stack_snapshot — Per-profile latest-row snapshot over linked undo_stack activity in the 232578_1.sql schema.
=====================================================================

Grain: one row per profile (profile_id), INCLUDING profile rows with no linked undo_stack rows.

Key column: parent_key.

Rules that build this mart:

Rule 1. The source table profile is read in full for this mart.

Rule 2. The source table undo_stack is read in full for this mart.

Rule 3. From source table profile there is exactly one row per profile row, keyed by profile_id, and that row carries parent_key and parent_name.

Rule 4. The undo_stack rows are brought in beside those profile rows by matching undo_stack on profile_id to parent_key, carrying profile_id; preservation is left-sided, so a profile row with no matching undo_stack row is retained and receives the stated empty snapshot values.

Rule 5. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group, and each such row reports event_count and lifetime_amount over that row's matching rows.

Rule 6. For each parent_key the single row at which the ordering measure — time_created — is largest survives, ties broken by the smallest entry_id, and latest_row_id, latest_amount and latest_label are taken from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows.

Rule 7. The extremal row's attributes are attached to the measures of the same parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

Rule 8. The mart columns are named parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label; lifetime_amount reports its declared default of 0 — never NULL — for a group with no matching rows, and for lifetime_amount that default also applies to a group none of whose real rows carries an input value.

Rule 9. Guarded ratio, reported beside parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label: latest_amount_share is latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places, and is 0.0 when lifetime_amount is 0 or has no value. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose object_id is missing, so such a row gives 0.0.

Rule 10. Deterministic output order: rows appear sorted in ascending parent_key sequence.

Output columns of profile_undo_stack_snapshot:

- parent_key (integer): identifier of the profile row. One row per value.
- parent_name (text): email of the profile row, copied unchanged.
- event_count (bigint): number of undo_stack rows for this profile row; 0 when there are none. Every linked undo_stack row counts, whether or not it carries an object_id value. A profile row kept with no undo_stack row reports 0 here, never 1: its placeholder holds no undo_stack row to count.
- lifetime_amount (integer): total of object_id over all matching undo_stack rows; 0 when there are no rows and when none of those rows carries an object_id value; a row with no object_id value adds nothing, so a group with some values totals the values it has.
- latest_row_id (integer): entry_id of the row with the latest time_created; ties take the smallest entry_id. It is 0 when there are no rows. Every undo_stack row of the profile row ranks, whether or not it carries a object_id value: the latest time_created wins even when that row's object_id is missing.
- latest_amount (integer): object_id from that same latest row; 0 when there are no rows or when the winning value is missing.
- latest_label (text): item_type from that same latest row; '(none)' when there are no rows or when the winning value is missing.
- latest_amount_share (float): latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose object_id is missing, so such a row gives 0.0.

=====================================================================
MART destination_treasure_hunt_distribution — Per-(destination, measure state) distribution of linked treasure_hunt activity in the 232578_1.sql schema.
=====================================================================

Grain: one row per (destination_id, measure state) pair represented by linked treasure_hunt rows, plus one absent no-activity row for a destination row with no links. Because soft_delete is required, no linked treasure_hunt row belongs to the absent state.

Key columns: entity_key and measure_state.

Rules that build this mart:

Rule 1. The source table destination is read in full for this mart.

Rule 2. The source table treasure_hunt is read in full for this mart.

Rule 3. From source table destination, each destination_id and its country are carried into the measure-state calculation as entity_key and entity_name.

Rule 4. The linked treasure_hunt rows are brought into each destination entity by matching treasure_hunt on destination_id to entity_key, carrying entity_key, entity_name and destination_id; preservation is left-sided, so an entity with no linked row is retained and its absent state is visible.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are those backed by a real treasure_hunt row; soft_delete is required on every such row.

Rule 6. There is one row per destination entity that has at least one row in the present measure state, and no row here for an entity with none, and that row reports entity_key, entity_name, row_count as the row count, distinct_amount_count as how many different soft_delete values occur (each different value counted once, however many rows repeat it), total_amount as the total soft_delete, and max_amount as the largest soft_delete.

Rule 7. Beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures are labelled with measure_state 'present', carried beside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are the retained placeholders for a destination row with no treasure_hunt rows; no real row can enter this state because soft_delete is required.

Rule 10. There is one row per destination entity with no linked treasure_hunt row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked treasure_hunt row; that row reports entity_key, entity_name, a row_count of 0, 0 different soft_delete values in distinct_amount_count, a total_amount soft_delete of 0 and a largest soft_delete of 0 in max_amount.

Rule 11. Beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share for these rows is likewise max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures are labelled with measure_state 'absent', carried beside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. Deterministic output order: rows appear sorted ascending by entity_key first, then by measure_state.

Output columns of destination_treasure_hunt_distribution:

- entity_key (integer): identifier of the destination row.
- measure_state (text): 'present' for a linked treasure_hunt row; 'absent' only for a destination row with no linked treasure_hunt row. soft_delete is required on every real treasure_hunt row.
- entity_name (text): country of the destination row, copied unchanged.
- row_count (bigint): number of linked treasure_hunt rows in this entity/state cell; 0 for a no-activity absent cell.
- distinct_amount_count (bigint): number of unique soft_delete values in this cell; each unique value is counted once, however many rows repeat it; 0 for a no-activity absent cell.
- total_amount (integer): total of soft_delete in this cell; 0 for a no-activity absent cell.
- max_amount (integer): largest soft_delete in this cell; 0 for a no-activity absent cell.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

=====================================================================
MART profile_undo_stack_top — Per-profile extremes over linked undo_stack rows in the 232578_1.sql schema: WHICH row is largest, not how large it is.
=====================================================================

Grain: one row per profile (profile_id), INCLUDING profile rows with no linked undo_stack rows.

Key column: parent_key.

Rules that build this mart:

Rule 1. The source table profile is read in full for this mart.

Rule 2. The source table undo_stack is read in full for this mart.

Rule 3. From source table profile there is exactly one row per profile row, keyed by profile_id, and that row carries parent_key and parent_name.

Rule 4. The undo_stack rows are brought in beside those profile rows by matching undo_stack on profile_id to parent_key, carrying profile_id; preservation is left-sided, so a profile row with no undo_stack rows still appears, with the declared defaults.

Rule 5. Within each parent_key group the matched rows are ranked under an explicit total order — the measure first, then the declared tie-break — so the extremal row is a function of the input and not of row order.

Rule 6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group, and each such row reports top_measure, tied_count, child_count and total_measure over that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7. For each parent_key the single row at which the ordering measure object_id is largest survives, ties broken by the smallest item_type under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), a row with no item_type value sorting after every row that has one, then by the smallest entry_id, and top_label and top_row_id are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 8. The extremal row's attributes are attached to the measures of the same parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

Rule 9. The mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults of 0 — never NULL — for a group with no matching rows, and for top_measure and total_measure that default also applies to a group none of whose real rows carries an input value.

Rule 10. Guarded ratio, reported beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when total_measure is 0 or has no value.

Rule 11. Reported beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, tie_state is 'empty' when no row holds a maximum at all — the parent has no undo_stack rows, or none of its rows carries a object_id value — 'unique' when exactly one row holds the maximum, and otherwise 'tied' when two or more do; this is a categorical mapping with no numeric boundary, tie_state follows tied_count ('empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more), and it is never null or blank. A row holds the maximum only when it carries a object_id value equal to the largest object_id value among the parent's rows; a row with no object_id value never holds the maximum, so a parent whose undo_stack rows all lack a object_id value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

Rule 12. Deterministic output order: rows appear sorted in ascending parent_key sequence.

Output columns of profile_undo_stack_top:

- parent_key (integer): identifier of the profile row. One row per value.
- parent_name (text): email of the profile row, copied unchanged.
- top_measure (integer): the largest object_id itself; 0 when the parent has no undo_stack rows, and 0 when none of its rows carries a object_id value.
- tied_count (bigint): how many undo_stack rows are tied at that largest object_id. It is 1 when exactly one row carries that largest object_id; 0 when there are no rows or when none of the rows carries a object_id value; a row with no object_id value never ties: only a row whose object_id value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.
- child_count (bigint): number of undo_stack rows for this profile row; 0 when there are none. Every linked undo_stack row counts, whether or not it carries an object_id value. A profile row kept with no undo_stack row reports 0 here, never 1: its placeholder holds no undo_stack row to count.
- total_measure (integer): total of object_id over all of them; 0 when the parent has no undo_stack rows, and 0 when none of its rows carries a object_id value (rows with no object_id value add nothing).
- top_label (text): the item_type of the undo_stack row with the LARGEST object_id for this profile row. Ties in object_id are broken by taking the SMALLEST item_type under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no item_type value sorts after every labelled row; rows tied on both are resolved by the smallest entry_id. A row with no object_id value still ranks, after every row that has one, so a parent holding at least one undo_stack row always has a winning row — when NONE of its rows carries a object_id value the winner is the one the tie-break alone selects, not the no-rows default. The literal '(none)' when the parent has no undo_stack rows at all, and '(none)' when the winning row has no item_type value.
- top_row_id (integer): the entry_id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real undo_stack row whenever the parent has any. This includes when none of them carries a object_id value. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no undo_stack rows, or none of its rows carries a object_id value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a object_id value equal to the largest object_id value among the parent's rows; a row with no object_id value never holds the maximum. So a parent whose undo_stack rows all lack a object_id value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `profile_undo_stack_snapshot`

- Grain: One row per profile (profile_id), INCLUDING profile rows with no linked undo_stack rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share

```text
Mart 'profile_undo_stack_snapshot' has 10 declared semantic rules:
1. [source] Read source table profile. (public source tables: profile)
2. [source] Read source table undo_stack. (public source tables: undo_stack)
3. [derive] One row per profile row, keyed by profile_id. (public source tables: profile | public carried/output columns: parent_key, parent_name)
4. [join] Bring in undo_stack; a profile row with no matching undo_stack row is retained and receives the stated empty snapshot values. (public source tables: undo_stack | public carried/output columns: profile_id | join preservation: left | condition public identifiers: undo_stack, profile_id, parent_key)
5. [aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count, lifetime_amount for that row's matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount)
6. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest entry_id, and take latest_row_id, latest_amount, latest_label from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, latest_row_id, latest_amount, latest_label)
7. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
8. [derive] Name the mart columns; lifetime_amount reports its declared default — never NULL — for a group with no matching rows. For lifetime_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label)
9. [ratio] Guarded ratios: latest_amount_share — latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose object_id is missing, so such a row gives 0.0. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `destination_treasure_hunt_distribution`

- Grain: One row per (destination_id, measure state) pair represented by linked treasure_hunt rows, plus one absent no-activity row for a destination row with no links. Because soft_delete is required, no linked treasure_hunt row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'destination_treasure_hunt_distribution' has 14 declared semantic rules:
1. [source] Read source table destination. (public source tables: destination)
2. [source] Read source table treasure_hunt. (public source tables: treasure_hunt)
3. [derive] Carry each destination_id and its country into the measure-state calculation. (public source tables: destination | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked treasure_hunt rows into each destination entity; retain an entity with no linked row so its absent state is visible. (public source tables: treasure_hunt | public carried/output columns: entity_key, entity_name, destination_id | join preservation: left | condition public identifiers: treasure_hunt, destination_id, entity_key)
5. [filter] Keep the present measure-state rows: a real treasure_hunt row; soft_delete is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per destination entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different soft_delete values occur (each different value counted once, however many rows repeat it), total soft_delete, and largest soft_delete. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a destination row with no treasure_hunt rows; no real row can enter this state because soft_delete is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per destination entity with no linked treasure_hunt row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked treasure_hunt row, reporting a row count of 0, 0 different soft_delete values, a total soft_delete of 0 and a largest soft_delete of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `profile_undo_stack_top`

- Grain: One row per profile (profile_id), INCLUDING profile rows with no linked undo_stack rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'profile_undo_stack_top' has 12 declared semantic rules:
1. [source] Read source table profile. (public source tables: profile)
2. [source] Read source table undo_stack. (public source tables: undo_stack)
3. [derive] One row per profile row, keyed by profile_id. (public source tables: profile | public carried/output columns: parent_key, parent_name)
4. [join] Bring in undo_stack: a profile row with no undo_stack rows still appears, with the declared defaults. (public source tables: undo_stack | public carried/output columns: profile_id | join preservation: left | condition public identifiers: undo_stack, profile_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest item_type under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no item_type value sorts after every row that has one), then the smallest entry_id, and take top_label, top_row_id from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no undo_stack rows, or none of its rows carries a object_id value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a object_id value equal to the largest object_id value among the parent's rows; a row with no object_id value never holds the maximum. So a parent whose undo_stack rows all lack a object_id value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### artist  (source backend: postgres)
Source table artist.

- `artist_id`: integer NOT NULL — Column artist_id of table artist.
- `artist_name`: text NOT NULL — Column artist_name of table artist.
- `biography`: text NOT NULL — Column biography of table artist.
- `facebook_link`: text NULL — Column facebook_link of table artist.
- `instagram_link`: text NULL — Column instagram_link of table artist.
- `members`: text NOT NULL — Column members of table artist.
- `soft_delete`: integer NULL — Column soft_delete of table artist.
- `spotify_link`: text NULL — Column spotify_link of table artist.
- `twitter_link`: text NULL — Column twitter_link of table artist.
- `verified`: integer NULL — Column verified of table artist.
- `website_link`: text NULL — Column website_link of table artist.
- primary key: artist_id

### artist_country  (source backend: mongodb)
Source table artist_country.

- `artist_id`: integer NOT NULL — Column artist_id of table artist_country.
- `country_id`: integer NOT NULL — Column country_id of table artist_country.

### artist_genre  (source backend: postgres)
Source table artist_genre.

- `artist_id`: integer NOT NULL — Column artist_id of table artist_genre.
- `genre_id`: integer NOT NULL — Column genre_id of table artist_genre.

### artist_profile  (source backend: files)
Source table artist_profile.

- `artist_id`: integer NOT NULL — Column artist_id of table artist_profile.
- `profile_id`: integer NULL — Column profile_id of table artist_profile.

### artist_profile_photo  (source backend: s3)
Source table artist_profile_photo.

- `artist_id`: integer NOT NULL — Column artist_id of table artist_profile_photo.
- `photo_id`: integer NOT NULL — Column photo_id of table artist_profile_photo.

### attend_event  (source backend: rest)
Source table attend_event.

- `attend_event_id`: integer NOT NULL — Column attend_event_id of table attend_event.
- `event_id`: integer NOT NULL — Column event_id of table attend_event.
- `profile_id`: integer NOT NULL — Column profile_id of table attend_event.
- primary key: attend_event_id

### destination  (source backend: rest)
Source table destination.

- `country`: text NOT NULL — Column country of table destination.
- `destination_id`: integer NOT NULL — Column destination_id of table destination.
- `district`: text NULL — Column district of table destination.
- `latitude`: float NULL — Column latitude of table destination.
- `longitude`: float NULL — Column longitude of table destination.
- `name`: text NOT NULL — Column name of table destination.
- `profile_id`: integer NOT NULL — Column profile_id of table destination.
- `soft_delete`: integer NOT NULL — Column soft_delete of table destination.
- `type`: text NOT NULL — Column type of table destination.
- `visible`: integer NOT NULL — Column visible of table destination.
- primary key: destination_id

### destination_change  (source backend: mongodb)
Source table destination_change.

- `action`: integer NOT NULL — Column action of table destination_change.
- `id`: integer NOT NULL — Column id of table destination_change.
- `request_id`: integer NOT NULL — Column request_id of table destination_change.
- `traveller_type_id`: integer NOT NULL — Column traveller_type_id of table destination_change.
- primary key: id

### destination_photo  (source backend: files)
Source table destination_photo.

- `destination_id`: integer NOT NULL — Column destination_id of table destination_photo.
- `destination_photo_id`: integer NOT NULL — Column destination_photo_id of table destination_photo.
- `photo_id`: integer NOT NULL — Column photo_id of table destination_photo.
- `profile_id`: integer NOT NULL — Column profile_id of table destination_photo.
- primary key: destination_photo_id

### destination_request  (source backend: rest)
Source table destination_request.

- `destination_id`: integer NULL — Column destination_Id of table destination_request.
- `id`: integer NOT NULL — Column id of table destination_request.
- `profile_id`: integer NULL — Column profile_Id of table destination_request.
- primary key: id

### destination_traveller_type  (source backend: rest)
Source table destination_traveller_type.

- `destination_id`: integer NOT NULL — Column destination_id of table destination_traveller_type.
- `id`: integer NOT NULL — Column id of table destination_traveller_type.
- `traveller_type_id`: integer NOT NULL — Column traveller_type_id of table destination_traveller_type.
- primary key: id

### event_artists  (source backend: rest)
Source table event_artists.

- `artist_id`: integer NOT NULL — Column artist_id of table event_artists.
- `event_id`: integer NOT NULL — Column event_id of table event_artists.

### event_genres  (source backend: mongodb)
Source table event_genres.

- `event_id`: integer NOT NULL — Column event_id of table event_genres.
- `genre_id`: integer NOT NULL — Column genre_id of table event_genres.

### event_photo  (source backend: files)
Source table event_photo.

- `event_id`: integer NOT NULL — Column event_id of table event_photo.
- `photo_id`: integer NOT NULL — Column photo_id of table event_photo.

### event_type  (source backend: s3)
Source table event_type.

- `event_id`: integer NOT NULL — Column event_id of table event_type.
- `type_id`: integer NOT NULL — Column type_id of table event_type.

### events  (source backend: s3)
Source table events.

- `age_restriction`: integer NULL — Column age_restriction of table events.
- `description`: text NULL — Column description of table events.
- `destination_id`: integer NOT NULL — Column destination_id of table events.
- `end_date`: timestamp NULL — Column end_date of table events.
- `event_id`: integer NOT NULL — Column event_id of table events.
- `event_name`: text NOT NULL — Column event_name of table events.
- `soft_delete`: integer NULL — Column soft_delete of table events.
- `start_date`: timestamp NULL — Column start_date of table events.
- `ticket_link`: text NULL — Column ticket_link of table events.
- `ticket_price`: float NULL — Column ticket_price of table events.
- primary key: event_id

### follow_artist  (source backend: rest)
Source table follow_artist.

- `artist_follow_id`: integer NOT NULL — Column artist_follow_id of table follow_artist.
- `artist_id`: integer NOT NULL — Column artist_id of table follow_artist.
- `profile_id`: integer NOT NULL — Column profile_id of table follow_artist.
- primary key: artist_follow_id

### follow_destination  (source backend: s3)
Source table follow_destination.

- `destination_follow_id`: integer NOT NULL — Column destination_follow_id of table follow_destination.
- `destination_id`: integer NOT NULL — Column destination_id of table follow_destination.
- `profile_id`: integer NOT NULL — Column profile_id of table follow_destination.
- primary key: destination_follow_id

### music_genre  (source backend: rest)
Source table music_genre.

- `genre`: text NOT NULL — Column genre of table music_genre.
- `genre_id`: integer NOT NULL — Column genre_Id of table music_genre.
- primary key: genre_id

### nationality  (source backend: s3)
Source table nationality.

- `nationality_id`: integer NOT NULL — Column nationality_id of table nationality.
- `nationality_name`: text NOT NULL — Column nationality_name of table nationality.
- primary key: nationality_id

### passport_country  (source backend: files)
Source table passport_country.

- `passport_country_id`: integer NOT NULL — Column passport_country_id of table passport_country.
- `passport_name`: text NOT NULL — Column passport_name of table passport_country.
- primary key: passport_country_id

### personal_photo  (source backend: s3)
Source table personal_photo.

- `is_profile_photo`: integer NOT NULL — Column is_profile_photo of table personal_photo.
- `personal_photo_id`: integer NOT NULL — Column personal_photo_id of table personal_photo.
- `photo_id`: integer NOT NULL — Column photo_id of table personal_photo.
- `profile_id`: integer NOT NULL — Column profile_id of table personal_photo.
- primary key: personal_photo_id

### photo  (source backend: postgres)
Source table photo.

- `content_type`: text NOT NULL — Column content_type of table photo.
- `name`: text NOT NULL — Column name of table photo.
- `path`: text NULL — Column path of table photo.
- `photo_id`: integer NOT NULL — Column photo_id of table photo.
- `visible`: integer NOT NULL — Column visible of table photo.
- primary key: photo_id

### profile  (source backend: rest)
Source table profile.

- `birth_date`: date NOT NULL — Column birth_date of table profile.
- `email`: text NOT NULL — Column email of table profile.
- `first_name`: text NOT NULL — Column first_name of table profile.
- `gender`: text NOT NULL — Column gender of table profile.
- `last_name`: text NOT NULL — Column last_name of table profile.
- `middle_name`: text NULL — Column middle_name of table profile.
- `password`: text NOT NULL — Column password of table profile.
- `profile_id`: integer NOT NULL — Column profile_id of table profile.
- `soft_delete`: integer NOT NULL — Column soft_delete of table profile.
- `time_created`: timestamp NOT NULL — Column time_created of table profile.
- primary key: profile_id

### profile_nationality  (source backend: postgres)
Source table profile_nationality.

- `nationality`: integer NOT NULL — Column nationality of table profile_nationality.
- `profile`: integer NOT NULL — Column profile of table profile_nationality.
- `profile_nationality_id`: integer NOT NULL — Column profile_nationality_id of table profile_nationality.
- primary key: profile_nationality_id

### profile_passport_country  (source backend: files)
Source table profile_passport_country.

- `passport_country`: integer NOT NULL — Column passport_country of table profile_passport_country.
- `profile`: integer NOT NULL — Column profile of table profile_passport_country.
- `profile_passport_country_id`: integer NOT NULL — Column profile_passport_country_id of table profile_passport_country.
- primary key: profile_passport_country_id

### profile_roles  (source backend: rest)
Source table profile_roles.

- `profile_id`: integer NOT NULL — Column profile_id of table profile_roles.
- `profile_role_id`: integer NOT NULL — Column profile_role_id of table profile_roles.
- `role_id`: integer NOT NULL — Column role_id of table profile_roles.
- primary key: profile_role_id

### profile_traveller_type  (source backend: s3)
Source table profile_traveller_type.

- `profile`: integer NOT NULL — Column profile of table profile_traveller_type.
- `profile_traveller_type_id`: integer NOT NULL — Column profile_traveller_type_id of table profile_traveller_type.
- `traveller_type`: integer NOT NULL — Column traveller_type of table profile_traveller_type.
- primary key: profile_traveller_type_id

### roles  (source backend: s3)
Source table roles.

- `role_id`: integer NOT NULL — Column role_id of table roles.
- `role_name`: text NULL — Column role_name of table roles.
- primary key: role_id

### thumbnail_link  (source backend: mongodb)
Source table thumbnail_link.

- `photo_id`: integer NOT NULL — Column photo_id of table thumbnail_link.
- `thumbnail_id`: integer NOT NULL — Column thumbnail_id of table thumbnail_link.
- primary key: photo_id, thumbnail_id

### traveller_type  (source backend: rest)
Source table traveller_type.

- `traveller_type_id`: integer NOT NULL — Column traveller_type_id of table traveller_type.
- `traveller_type_name`: text NOT NULL — Column traveller_type_name of table traveller_type.
- primary key: traveller_type_id

### treasure_hunt  (source backend: files)
Source table treasure_hunt.

- `destination_id`: integer NOT NULL — Column destination_id of table treasure_hunt.
- `end_date`: date NOT NULL — Column end_date of table treasure_hunt.
- `profile_id`: integer NOT NULL — Column profile_id of table treasure_hunt.
- `riddle`: text NOT NULL — Column riddle of table treasure_hunt.
- `soft_delete`: integer NOT NULL — Column soft_delete of table treasure_hunt.
- `start_date`: date NOT NULL — Column start_date of table treasure_hunt.
- `treasure_hunt_id`: integer NOT NULL — Column treasure_hunt_id of table treasure_hunt.
- primary key: treasure_hunt_id

### trip  (source backend: rest)
Source table trip.

- `name`: text NOT NULL — Column name of table trip.
- `profile_id`: integer NOT NULL — Column profile_id of table trip.
- `soft_delete`: integer NOT NULL — Column soft_delete of table trip.
- `trip_id`: integer NOT NULL — Column trip_id of table trip.
- primary key: trip_id

### trip_destination  (source backend: s3)
Source table trip_destination.

- `arrival`: date NULL — Column arrival of table trip_destination.
- `departure`: date NULL — Column departure of table trip_destination.
- `dest_order`: integer NULL — Column dest_order of table trip_destination.
- `destination_id`: integer NOT NULL — Column destination_id of table trip_destination.
- `trip_destination_id`: integer NOT NULL — Column trip_destination_id of table trip_destination.
- `trip_id`: integer NOT NULL — Column trip_id of table trip_destination.
- primary key: trip_destination_id

### type_of_events  (source backend: rest)
Source table type_of_events.

- `type_id`: integer NOT NULL — Column type_id of table type_of_events.
- `type_name`: text NOT NULL — Column type_name of table type_of_events.
- primary key: type_id

### undo_stack  (source backend: rest)
Source table undo_stack.

- `entry_id`: integer NOT NULL — Column entry_id of table undo_stack.
- `item_type`: text NULL — Column item_type of table undo_stack.
- `object_id`: integer NULL — Column object_id of table undo_stack.
- `profile_id`: integer NULL — Column profile_id of table undo_stack.
- `time_created`: timestamp NOT NULL — Column time_created of table undo_stack.
- primary key: entry_id

### Relationships

- artist_country(artist_id) -> artist(artist_id) [required]
- artist_country(country_id) -> passport_country(passport_country_id) [required]
- artist_genre(artist_id) -> artist(artist_id) [required]
- artist_genre(genre_id) -> music_genre(genre_id) [required]
- artist_profile(artist_id) -> artist(artist_id) [required]
- artist_profile(profile_id) -> profile(profile_id) [optional (may be NULL/dangling)]
- artist_profile_photo(artist_id) -> artist(artist_id) [required]
- artist_profile_photo(photo_id) -> photo(photo_id) [required]
- attend_event(event_id) -> events(event_id) [required]
- attend_event(profile_id) -> profile(profile_id) [required]
- destination(profile_id) -> profile(profile_id) [required]
- destination_change(request_id) -> destination_request(id) [required]
- destination_change(traveller_type_id) -> traveller_type(traveller_type_id) [required]
- destination_photo(destination_id) -> destination(destination_id) [required]
- destination_photo(photo_id) -> photo(photo_id) [required]
- destination_photo(profile_id) -> profile(profile_id) [required]
- destination_request(destination_id) -> destination(destination_id) [optional (may be NULL/dangling)]
- destination_request(profile_id) -> profile(profile_id) [optional (may be NULL/dangling)]
- destination_traveller_type(destination_id) -> destination(destination_id) [required]
- destination_traveller_type(traveller_type_id) -> traveller_type(traveller_type_id) [required]
- event_artists(artist_id) -> artist(artist_id) [required]
- event_artists(event_id) -> events(event_id) [required]
- event_genres(event_id) -> events(event_id) [required]
- event_genres(genre_id) -> music_genre(genre_id) [required]
- event_photo(event_id) -> events(event_id) [required]
- event_photo(photo_id) -> photo(photo_id) [required]
- event_type(event_id) -> events(event_id) [required]
- event_type(type_id) -> type_of_events(type_id) [required]
- events(destination_id) -> destination(destination_id) [required]
- follow_artist(artist_id) -> artist(artist_id) [required]
- follow_artist(profile_id) -> profile(profile_id) [required]
- follow_destination(destination_id) -> destination(destination_id) [required]
- follow_destination(profile_id) -> profile(profile_id) [required]
- personal_photo(photo_id) -> photo(photo_id) [required]
- personal_photo(profile_id) -> profile(profile_id) [required]
- profile_nationality(nationality) -> nationality(nationality_id) [required]
- profile_nationality(profile) -> profile(profile_id) [required]
- profile_passport_country(passport_country) -> passport_country(passport_country_id) [required]
- profile_passport_country(profile) -> profile(profile_id) [required]
- profile_roles(profile_id) -> profile(profile_id) [required]
- profile_roles(role_id) -> roles(role_id) [required]
- profile_traveller_type(profile) -> profile(profile_id) [required]
- profile_traveller_type(traveller_type) -> traveller_type(traveller_type_id) [required]
- thumbnail_link(photo_id) -> photo(photo_id) [required]
- thumbnail_link(thumbnail_id) -> photo(photo_id) [required]
- treasure_hunt(destination_id) -> destination(destination_id) [required]
- treasure_hunt(profile_id) -> profile(profile_id) [required]
- trip(profile_id) -> profile(profile_id) [required]
- trip_destination(destination_id) -> destination(destination_id) [required]
- trip_destination(trip_id) -> trip(trip_id) [required]
- undo_stack(profile_id) -> profile(profile_id) [optional (may be NULL/dangling)]

