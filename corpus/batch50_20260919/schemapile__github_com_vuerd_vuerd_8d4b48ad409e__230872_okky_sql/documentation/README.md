# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Vuerd Vuerd

## Specification

PROJECT OVERVIEW

This project builds three analytical marts over an extract of the 230872_OKKY.sql community schema (the Github Com Vuerd Vuerd SchemaPile schema). Every source table listed below must be extracted from the backend named beside it, and each table is read from that backend only.

Source tables and their extraction backends:
- Table activity is extracted from the files backend.
- Table anonymous is extracted from the rest backend.
- Table area_city_code is extracted from the s3 backend.
- Table area_district_code is extracted from the files backend.
- Table article is extracted from the mongodb backend.
- Table article_tag is extracted from the files backend.
- Table avatar is extracted from the postgres backend.
- Table avatar_tag is extracted from the s3 backend.
- Table banner is extracted from the rest backend.
- Table banner_click is extracted from the postgres backend.
- Table career is extracted from the postgres backend.
- Table category is extracted from the s3 backend.
- Table change_log is extracted from the postgres backend.
- Table company is extracted from the files backend.
- Table company_info is extracted from the rest backend.
- Table confirm_email is extracted from the mongodb backend.
- Table content is extracted from the s3 backend.
- Table content_file is extracted from the rest backend.
- Table content_vote is extracted from the files backend.
- Table file is extracted from the mongodb backend.
- Table follow is extracted from the postgres backend.
- Table job_position is extracted from the rest backend.
- Table job_position_tag is extracted from the s3 backend.
- Table logged_in is extracted from the rest backend.
- Table managed_user is extracted from the rest backend.
- Table notification is extracted from the rest backend.
- Table notification_read is extracted from the rest backend.
- Table oauthid is extracted from the files backend.
- Table opinion is extracted from the postgres backend.
- Table person is extracted from the rest backend.
- Table recruit is extracted from the mongodb backend.
- Table resume is extracted from the postgres backend.
- Table role is extracted from the mongodb backend.
- Table scrap is extracted from the s3 backend.
- Table spam_word is extracted from the files backend.
- Table tag is extracted from the rest backend.
- Table tag_similar_text is extracted from the mongodb backend.
- Table user is extracted from the mongodb backend.
- Table user_role is extracted from the mongodb backend.

RELATIONSHIPS IN THE SOURCE SCHEMA

Each statement below names the child table with its keys and the parent table with its keys, and labels the relationship required or optional exactly as the source schema declares it.

- Child table activity on article_id refers to parent table article on id; this relationship is required.
- Child table activity on avatar_id refers to parent table avatar on id; this relationship is required.
- Child table activity on content_id refers to parent table content on id; this relationship is required.
- Child table anonymous on article_id refers to parent table article on id; this relationship is required.
- Child table anonymous on content_id refers to parent table content on id; this relationship is required.
- Child table anonymous on user_id refers to parent table user on id; this relationship is required.
- Child table area_district_code on area_city_code_id refers to parent table area_city_code on id; this relationship is required.
- Child table article on author_id refers to parent table avatar on id; this relationship is optional (may be NULL or dangling).
- Child table article on category_id refers to parent table category on code; this relationship is required.
- Child table article on content_id refers to parent table content on id; this relationship is optional (may be NULL or dangling).
- Child table article on last_editor_id refers to parent table avatar on id; this relationship is optional (may be NULL or dangling).
- Child table article on selected_note_id refers to parent table content on id; this relationship is optional (may be NULL or dangling).
- Child table article_tag on article_tags_id refers to parent table article on id; this relationship is optional (may be NULL or dangling).
- Child table article_tag on tag_id refers to parent table tag on id; this relationship is optional (may be NULL or dangling).
- Child table avatar_tag on avatar_tags_id refers to parent table avatar on id; this relationship is optional (may be NULL or dangling).
- Child table avatar_tag on tag_id refers to parent table tag on id; this relationship is optional (may be NULL or dangling).
- Child table banner_click on banner_id refers to parent table banner on id; this relationship is required.
- Child table career on company_id refers to parent table company on id; this relationship is required.
- Child table career on resume_id refers to parent table resume on id; this relationship is required.
- Child table change_log on article_id refers to parent table article on id; this relationship is required.
- Child table change_log on avatar_id refers to parent table avatar on id; this relationship is optional (may be NULL or dangling).
- Child table change_log on content_id refers to parent table content on id; this relationship is optional (may be NULL or dangling).
- Child table company on manager_id refers to parent table person on id; this relationship is optional (may be NULL or dangling).
- Child table company_info on company_id refers to parent table company on id; this relationship is optional (may be NULL or dangling).
- Child table confirm_email on user_id refers to parent table user on id; this relationship is required.
- Child table content on article_id refers to parent table article on id; this relationship is optional (may be NULL or dangling).
- Child table content on author_id refers to parent table avatar on id; this relationship is optional (may be NULL or dangling).
- Child table content on last_editor_id refers to parent table avatar on id; this relationship is optional (may be NULL or dangling).
- Child table content_file on content_files_id refers to parent table content on id; this relationship is optional (may be NULL or dangling).
- Child table content_file on file_id refers to parent table file on id; this relationship is optional (may be NULL or dangling).
- Child table content_vote on article_id refers to parent table article on id; this relationship is required.
- Child table content_vote on content_id refers to parent table content on id; this relationship is required.
- Child table content_vote on voter_id refers to parent table avatar on id; this relationship is required.
- Child table follow on follower_id refers to parent table avatar on id; this relationship is required.
- Child table follow on following_id refers to parent table avatar on id; this relationship is required.
- Child table job_position on recruit_id refers to parent table recruit on id; this relationship is required.
- Child table job_position_tag on job_position_tags_id refers to parent table job_position on id; this relationship is optional (may be NULL or dangling).
- Child table job_position_tag on tag_id refers to parent table tag on id; this relationship is optional (may be NULL or dangling).
- Child table logged_in on user_id refers to parent table user on id; this relationship is required.
- Child table managed_user on user_id refers to parent table user on id; this relationship is required.
- Child table notification on article_id refers to parent table article on id; this relationship is required.
- Child table notification on content_id refers to parent table content on id; this relationship is required.
- Child table notification on receiver_id refers to parent table avatar on id; this relationship is required.
- Child table notification on sender_id refers to parent table avatar on id; this relationship is required.
- Child table notification_read on avatar_id refers to parent table avatar on id; this relationship is required.
- Child table oauthid on user_id refers to parent table user on id; this relationship is required.
- Child table opinion on author_id refers to parent table avatar on id; this relationship is required.
- Child table opinion on content_id refers to parent table content on id; this relationship is required.
- Child table person on company_id refers to parent table company on id; this relationship is optional (may be NULL or dangling).
- Child table person on resume_id refers to parent table resume on id; this relationship is optional (may be NULL or dangling).
- Child table recruit on article_id refers to parent table article on id; this relationship is required.
- Child table recruit on company_id refers to parent table company on id; this relationship is optional (may be NULL or dangling).
- Child table scrap on article_id refers to parent table article on id; this relationship is required.
- Child table scrap on avatar_id refers to parent table avatar on id; this relationship is required.
- Child table tag_similar_text on tag_id refers to parent table tag on id; this relationship is required.
- Child table user on avatar_id refers to parent table avatar on id; this relationship is required.
- Child table user on person_id refers to parent table person on id; this relationship is required.
- Child table user_role on role_id refers to parent table role on id; this relationship is required.
- Child table user_role on user_id refers to parent table user on id; this relationship is required.

Three marts are produced: user_logged_in_snapshot, person_user_distribution and content_opinion_top. Each is described in its own section below. Every rounding instruction means rounding to 4 decimal places with the usual half-away-from-zero convention of the warehouse, and every stated default value is produced literally rather than left empty.

=== Mart user_logged_in_snapshot: a per-user latest-row snapshot over linked logged_in activity in the 230872_OKKY.sql schema ===

Grain: one output row per user (id), INCLUDING user rows that have no linked logged_in rows.

Key column: parent_key is the single key column of this mart.

Rule 1 — the source table user is read, and its rows are the basis of this mart.

Rule 2 — the source table logged_in is read, and its rows supply the activity measured here.

Rule 3 — from source table user there is one row per user row, keyed by id, carrying parent_key and parent_name.

Rule 4 — the logged_in rows are brought in from source table logged_in, matching a logged_in row by its user_id to the parent_key of the user row and carrying user_id and id; preservation is left-sided, so a user row with no matching logged_in row is retained and receives the stated empty snapshot values.

Rule 5 — one output row per parent_key, carrying parent_name beside the keys: a parent_key value identifies one source row for the carried columns, so parent_name takes one value per key and never splits a group, and each such row reports event_count and lifetime_amount for that row's matching rows.

Rule 6 — for each parent_key the single row at which the ordering measure, the latest date_created, is largest survives, ties broken by the smallest id, and latest_row_id, latest_amount and latest_label are taken from that winning row; the ordering measure is required on every real input row, so a non-empty group always has a winning row, and the declared defaults belong only to a group with NO rows.

Rule 7 — the winning row's attributes are attached to the grouped measures by matching on parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

Rule 8 — the mart columns are named parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label; lifetime_amount reports its declared default of 0 — never NULL — for a group with no matching rows.

Rule 9 — a guarded ratio completes the row: latest_amount_share is latest_amount divided by lifetime_amount expressed as a fraction, rounded to 4 decimal places, and it is 0.0 when the denominator lifetime_amount is 0 or has no value; it appears beside parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label.

Rule 10 — deterministic output order: rows appear sorted in ascending parent_key order.

Output columns:
- parent_key (bigint): the identifier of the user row; there is one output row per value.
- parent_name (text): the create_ip of the user row, copied unchanged.
- event_count (bigint): the number of logged_in rows for this user row; 0 when there are none. A user row kept with no logged_in row reports 0 here, never 1: its placeholder holds no logged_in row to count.
- lifetime_amount (bigint): the total of version over all matching logged_in rows; 0 when there are no rows.
- latest_row_id (bigint): the id of the row with the latest date_created; ties take the smallest id. It is 0 when there are no rows.
- latest_amount (bigint): the version from that same latest row; 0 when there are no rows.
- latest_label (text): the remote_addr from that same latest row; the literal '(none)' when there are no rows or when the winning value is missing.
- latest_amount_share (float): latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0.

=== Mart person_user_distribution: a per-(person, measure state) distribution of linked user activity in the 230872_OKKY.sql schema ===

Grain: one output row per (id, measure state) pair represented by linked user rows, plus one absent no-activity row for a person row with no links. Because version is required, no linked user row belongs to the absent state.

Key columns: entity_key and measure_state together are the key columns of this mart.

Rule 1 — the source table person is read, and its rows are the entities of this mart.

Rule 2 — the source table user is read, and its rows supply the linked activity measured here.

Rule 3 — from source table person, each id and its email are carried into the measure-state calculation as entity_key and entity_name.

Rule 4 — the linked rows of source table user are brought into each person entity, matching a user row by its person_id to the entity_key, and carrying entity_key, entity_name, id and person_id; preservation is left-sided, so an entity with no linked row is retained and its absent state is visible.

Rule 5 — the present measure-state rows are those kept for a real user row, carrying entity_key and entity_name; version is required on every such row.

Rule 6 — in the present measure state there is one row per person entity that has at least one such row, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different version values occur (each different value counted once, however many rows repeat it), total_amount as the total version, and max_amount as the largest version.

Rule 7 — for those present-state rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it accompanies entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount.

Rule 8 — these measures are labelled with measure_state set to 'present', alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9 — the absent measure-state rows are the retained placeholders for a person row with no user rows, carrying entity_key and entity_name; no real row can enter this state because version is required.

Rule 10 — in the absent measure state there is one row per person entity with no linked user row at all, whose retained placeholder is its one row in this state, and no row here for an entity that has a linked user row; it carries entity_key and entity_name and reports a row_count of 0, 0 different version values as distinct_amount_count, a total_amount of 0 and a largest version, max_amount, of 0.

Rule 11 — for those absent-state rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it accompanies entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount.

Rule 12 — these measures are labelled with measure_state set to 'absent', alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13 — the present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14 — deterministic output order: rows appear sorted in ascending entity_key order, and within one entity in ascending measure_state order.

Output columns:
- entity_key (bigint): the identifier of the person row.
- measure_state (text): 'present' for a linked user row; 'absent' only for a person row with no linked user row. version is required on every real user row.
- entity_name (text): the email of the person row, copied unchanged.
- row_count (bigint): the number of linked user rows in this entity/state cell; 0 for a no-activity absent cell.
- distinct_amount_count (bigint): the number of unique version values in this cell; each unique value is counted once, however many rows repeat it; 0 for a no-activity absent cell.
- total_amount (bigint): the total of version in this cell; 0 for a no-activity absent cell.
- max_amount (bigint): the largest version in this cell; 0 for a no-activity absent cell.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

=== Mart content_opinion_top: per-content extremes over linked opinion rows in the 230872_OKKY.sql schema — WHICH row is largest, not how large it is ===

Grain: one output row per content (id), INCLUDING content rows with no linked opinion rows.

Key column: parent_key is the single key column of this mart.

Rule 1 — the source table content is read, and its rows are the parents of this mart.

Rule 2 — the source table opinion is read, and its rows supply the measured children.

Rule 3 — from source table content there is one row per content row, keyed by id, carrying parent_key and parent_name.

Rule 4 — the rows of source table opinion are brought in, matching an opinion row by its content_id to the parent_key of the content row and carrying content_id and id; preservation is left-sided, so a content row with no opinion rows still appears, with the declared defaults.

Rule 5 — within each parent_key the matched rows are ranked under an explicit total order — the measure version first, then the declared tie-break — so the extremal row is a function of the input and not of row order.

Rule 6 — one output row per parent_key, carrying parent_name beside the keys: a parent_key value identifies one source row for the carried columns, so parent_name takes one value per key and never splits a group, and each such row reports top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7 — for each parent_key the single row at which the ordering measure version is largest survives, ties broken by the smallest comment under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then the smallest id, and top_label and top_row_id are taken from that winning row; the ordering measure is required on every real input row, so a non-empty group always has a winning row, and the declared defaults belong only to a group with NO rows.

Rule 8 — the winning row's attributes are attached to the grouped measures by matching on parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

Rule 9 — the mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults of 0 — never NULL — for a group with no matching rows.

Rule 10 — a guarded ratio completes the row: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when the denominator total_measure is 0 or has no value; it appears beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id.

Rule 11 — tie_state is 'empty' when no row holds a maximum at all — the parent has no opinion rows — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; equivalently tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more, and it is never null or blank. It appears beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share.

Rule 12 — deterministic output order: rows appear sorted in ascending parent_key order.

Output columns:
- parent_key (bigint): the identifier of the content row; there is one output row per value.
- parent_name (text): the a_nick_name of the content row, copied unchanged.
- top_measure (bigint): the largest version itself; 0 when the parent has no opinion rows.
- tied_count (bigint): how many opinion rows are tied at that largest version; 1 when exactly one row carries that largest version; 0 when there are no rows.
- child_count (bigint): the number of opinion rows for this content row; 0 when there are none. A content row kept with no opinion row reports 0 here, never 1: its placeholder holds no opinion row to count.
- total_measure (bigint): the total of version over all of them; 0 when the parent has no opinion rows.
- top_label (text): the comment of the opinion row with the LARGEST version for this content row. Ties in version are broken by taking the SMALLEST comment under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one); rows tied on both are resolved by the smallest id. It is the literal '(none)' when the parent has no opinion rows at all.
- top_row_id (bigint): the id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real opinion row whenever the parent has any. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no opinion rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `user_logged_in_snapshot`

- Grain: One row per user (id), INCLUDING user rows with no linked logged_in rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share

```text
Mart 'user_logged_in_snapshot' has 10 declared semantic rules:
1. [source] Read source table user. (public source tables: user)
2. [source] Read source table logged_in. (public source tables: logged_in)
3. [derive] One row per user row, keyed by id. (public source tables: user | public carried/output columns: parent_key, parent_name)
4. [join] Bring in logged_in; a user row with no matching logged_in row is retained and receives the stated empty snapshot values. (public source tables: logged_in | public carried/output columns: user_id, id | join preservation: left | condition public identifiers: logged_in, user_id, parent_key)
5. [aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count, lifetime_amount for that row's matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount)
6. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest id, and take latest_row_id, latest_amount, latest_label from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, latest_row_id, latest_amount, latest_label)
7. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
8. [derive] Name the mart columns; lifetime_amount reports its declared default — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label)
9. [ratio] Guarded ratios: latest_amount_share — latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `person_user_distribution`

- Grain: One row per (id, measure state) pair represented by linked user rows, plus one absent no-activity row for a person row with no links. Because version is required, no linked user row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'person_user_distribution' has 14 declared semantic rules:
1. [source] Read source table person. (public source tables: person)
2. [source] Read source table user. (public source tables: user)
3. [derive] Carry each id and its email into the measure-state calculation. (public source tables: person | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked user rows into each person entity; retain an entity with no linked row so its absent state is visible. (public source tables: user | public carried/output columns: entity_key, entity_name, id, person_id | join preservation: left | condition public identifiers: user, person_id, entity_key)
5. [filter] Keep the present measure-state rows: a real user row; version is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per person entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different version values occur (each different value counted once, however many rows repeat it), total version, and largest version. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a person row with no user rows; no real row can enter this state because version is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per person entity with no linked user row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked user row, reporting a row count of 0, 0 different version values, a total version of 0 and a largest version of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `content_opinion_top`

- Grain: One row per content (id), INCLUDING content rows with no linked opinion rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'content_opinion_top' has 12 declared semantic rules:
1. [source] Read source table content. (public source tables: content)
2. [source] Read source table opinion. (public source tables: opinion)
3. [derive] One row per content row, keyed by id. (public source tables: content | public carried/output columns: parent_key, parent_name)
4. [join] Bring in opinion: a content row with no opinion rows still appears, with the declared defaults. (public source tables: opinion | public carried/output columns: content_id, id | join preservation: left | condition public identifiers: opinion, content_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest comment under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then the smallest id, and take top_label, top_row_id from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no opinion rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### activity  (source backend: files)
Source table activity.

- `article_id`: bigint NOT NULL — Column article_id of table activity.
- `avatar_id`: bigint NOT NULL — Column avatar_id of table activity.
- `content_id`: bigint NOT NULL — Column content_id of table activity.
- `date_created`: timestamp NOT NULL — Column date_created of table activity.
- `id`: bigint NOT NULL — Column id of table activity.
- `last_updated`: timestamp NOT NULL — Column last_updated of table activity.
- `point`: integer NOT NULL — Column point of table activity.
- `point_type`: text NOT NULL — Column point_type of table activity.
- `type`: text NOT NULL — Column type of table activity.
- `version`: bigint NOT NULL — Column version of table activity.
- primary key: id

### anonymous  (source backend: rest)
Source table anonymous.

- `article_id`: bigint NOT NULL — Column article_id of table anonymous.
- `content_id`: bigint NOT NULL — Column content_id of table anonymous.
- `id`: bigint NOT NULL — Column id of table anonymous.
- `type`: text NOT NULL — Column type of table anonymous.
- `user_id`: bigint NOT NULL — Column user_id of table anonymous.
- `version`: bigint NOT NULL — Column version of table anonymous.
- primary key: id

### area_city_code  (source backend: s3)
Source table area_city_code.

- `id`: text NOT NULL — Column id of table area_city_code.
- `name`: text NOT NULL — Column name of table area_city_code.
- `version`: bigint NOT NULL — Column version of table area_city_code.
- primary key: id

### area_district_code  (source backend: files)
Source table area_district_code.

- `area_city_code_id`: text NOT NULL — Column area_city_code_id of table area_district_code.
- `id`: text NOT NULL — Column id of table area_district_code.
- `name`: text NOT NULL — Column name of table area_district_code.
- `version`: bigint NOT NULL — Column version of table area_district_code.
- primary key: id

### article  (source backend: mongodb)
Source table article.

- `a_nick_name`: text NULL — Column a_nick_name of table article.
- `anonymity`: boolean NOT NULL — Column anonymity of table article.
- `author_id`: bigint NULL — Column author_id of table article.
- `category_id`: text NOT NULL — Column category_id of table article.
- `choice`: boolean NOT NULL — Column choice of table article.
- `content_id`: bigint NULL — Column content_id of table article.
- `create_ip`: text NULL — Column create_ip of table article.
- `date_created`: timestamp NOT NULL — Column date_created of table article.
- `enabled`: boolean NOT NULL — Column enabled of table article.
- `id`: bigint NOT NULL — Column id of table article.
- `is_recruit`: boolean NOT NULL — Column is_recruit of table article.
- `last_editor_id`: bigint NULL — Column last_editor_id of table article.
- `last_updated`: timestamp NOT NULL — Column last_updated of table article.
- `note_count`: integer NOT NULL — Column note_count of table article.
- `scrap_count`: integer NOT NULL — Column scrap_count of table article.
- `selected_note_id`: bigint NULL — Column selected_note_id of table article.
- `tag_string`: text NULL — Column tag_string of table article.
- `title`: text NOT NULL — Column title of table article.
- `version`: bigint NOT NULL — Column version of table article.
- `view_count`: integer NOT NULL — Column view_count of table article.
- `vote_count`: integer NOT NULL — Column vote_count of table article.
- primary key: id

### article_tag  (source backend: files)
Source table article_tag.

- `article_tags_id`: bigint NULL — Column article_tags_id of table article_tag.
- `tag_id`: bigint NULL — Column tag_id of table article_tag.

### avatar  (source backend: postgres)
Source table avatar.

- `activity_point`: integer NOT NULL — Column activity_point of table avatar.
- `id`: bigint NOT NULL — Column id of table avatar.
- `nickname`: text NOT NULL — Column nickname of table avatar.
- `official`: boolean NULL — Column official of table avatar.
- `picture`: text NOT NULL — Column picture of table avatar.
- `picture_type`: integer NOT NULL — Column picture_type of table avatar.
- `version`: bigint NOT NULL — Column version of table avatar.
- primary key: id

### avatar_tag  (source backend: s3)
Source table avatar_tag.

- `avatar_tags_id`: bigint NULL — Column avatar_tags_id of table avatar_tag.
- `tag_id`: bigint NULL — Column tag_id of table avatar_tag.

### banner  (source backend: rest)
Source table banner.

- `date_created`: timestamp NOT NULL — Column date_created of table banner.
- `id`: bigint NOT NULL — Column id of table banner.
- `image`: text NULL — Column image of table banner.
- `last_updated`: timestamp NOT NULL — Column last_updated of table banner.
- `name`: text NOT NULL — Column name of table banner.
- `target`: text NULL — Column target of table banner.
- `type`: text NOT NULL — Column type of table banner.
- `url`: text NOT NULL — Column url of table banner.
- `version`: bigint NOT NULL — Column version of table banner.
- `visible`: boolean NOT NULL — Column visible of table banner.
- primary key: id

### banner_click  (source backend: postgres)
Source table banner_click.

- `banner_id`: bigint NOT NULL — Column banner_id of table banner_click.
- `click_count`: integer NOT NULL — Column click_count of table banner_click.
- `date_created`: timestamp NOT NULL — Column date_created of table banner_click.
- `id`: bigint NOT NULL — Column id of table banner_click.
- `ip`: text NOT NULL — Column ip of table banner_click.
- `version`: bigint NOT NULL — Column version of table banner_click.
- primary key: id

### career  (source backend: postgres)
Source table career.

- `company_id`: bigint NOT NULL — Column company_id of table career.
- `id`: bigint NOT NULL — Column id of table career.
- `resume_id`: bigint NOT NULL — Column resume_id of table career.
- `version`: bigint NOT NULL — Column version of table career.
- primary key: id

### category  (source backend: s3)
Source table category.

- `anonymity`: boolean NULL — Column anonymity of table category.
- `code`: text NOT NULL — Column code of table category.
- `date_created`: timestamp NOT NULL — Column date_created of table category.
- `default_label`: text NOT NULL — Column default_label of table category.
- `enabled`: boolean NOT NULL — Column enabled of table category.
- `external_link`: text NULL — Column external_link of table category.
- `icon_css_names`: text NULL — Column icon_css_names of table category.
- `isurl`: boolean NOT NULL — Column isurl of table category.
- `label_code`: text NOT NULL — Column label_code of table category.
- `last_updated`: timestamp NOT NULL — Column last_updated of table category.
- `level`: integer NOT NULL — Column level of table category.
- `parent_id`: text NULL — Column parent_id of table category.
- `require_tag`: boolean NOT NULL — Column require_tag of table category.
- `sort_order`: integer NOT NULL — Column sort_order of table category.
- `url`: text NULL — Column url of table category.
- `use_evaluate`: boolean NOT NULL — Column use_evaluate of table category.
- `use_note`: boolean NOT NULL — Column use_note of table category.
- `use_opinion`: boolean NOT NULL — Column use_opinion of table category.
- `use_tag`: boolean NOT NULL — Column use_tag of table category.
- `version`: bigint NOT NULL — Column version of table category.
- `writable`: boolean NOT NULL — Column writable of table category.
- `write_by_external_link`: boolean NULL — Column write_by_external_link of table category.
- primary key: code

### change_log  (source backend: postgres)
Source table change_log.

- `article_id`: bigint NOT NULL — Column article_id of table change_log.
- `avatar_id`: bigint NULL — Column avatar_id of table change_log.
- `content_id`: bigint NULL — Column content_id of table change_log.
- `date_created`: timestamp NOT NULL — Column date_created of table change_log.
- `id`: bigint NOT NULL — Column id of table change_log.
- `md5`: text NOT NULL — Column md5 of table change_log.
- `patch`: text NOT NULL — Column patch of table change_log.
- `revision`: integer NOT NULL — Column revision of table change_log.
- `type`: text NOT NULL — Column type of table change_log.
- `version`: bigint NOT NULL — Column version of table change_log.
- primary key: id

### company  (source backend: files)
Source table company.

- `enabled`: boolean NOT NULL — Column enabled of table company.
- `id`: bigint NOT NULL — Column id of table company.
- `locked`: boolean NOT NULL — Column locked of table company.
- `logo`: text NULL — Column logo of table company.
- `manager_id`: bigint NULL — Column manager_id of table company.
- `name`: text NOT NULL — Column name of table company.
- `register_number`: text NOT NULL — Column register_number of table company.
- `version`: bigint NOT NULL — Column version of table company.
- primary key: id

### company_info  (source backend: rest)
Source table company_info.

- `company_id`: bigint NULL — Column company_id of table company_info.
- `description`: text NULL — Column description of table company_info.
- `email`: text NOT NULL — Column email of table company_info.
- `employee_number`: integer NOT NULL — Column employee_number of table company_info.
- `homepage_url`: text NULL — Column homepage_url of table company_info.
- `id`: bigint NOT NULL — Column id of table company_info.
- `tel`: text NOT NULL — Column tel of table company_info.
- `version`: bigint NOT NULL — Column version of table company_info.
- `welfare`: text NULL — Column welfare of table company_info.
- primary key: id

### confirm_email  (source backend: mongodb)
Source table confirm_email.

- `date_expired`: timestamp NOT NULL — Column date_expired of table confirm_email.
- `email`: text NOT NULL — Column email of table confirm_email.
- `id`: bigint NOT NULL — Column id of table confirm_email.
- `secured_key`: text NOT NULL — Column secured_key of table confirm_email.
- `user_id`: bigint NOT NULL — Column user_id of table confirm_email.
- `version`: bigint NOT NULL — Column version of table confirm_email.
- primary key: id

### content  (source backend: s3)
Source table content.

- `a_nick_name`: text NULL — Column a_nick_name of table content.
- `anonymity`: boolean NOT NULL — Column anonymity of table content.
- `article_id`: bigint NULL — Column article_id of table content.
- `author_id`: bigint NULL — Column author_id of table content.
- `create_ip`: text NULL — Column create_ip of table content.
- `date_created`: timestamp NOT NULL — Column date_created of table content.
- `id`: bigint NOT NULL — Column id of table content.
- `last_editor_id`: bigint NULL — Column last_editor_id of table content.
- `last_updated`: timestamp NOT NULL — Column last_updated of table content.
- `selected`: boolean NOT NULL — Column selected of table content.
- `text`: text NOT NULL — Column text of table content.
- `text_type`: integer NOT NULL — Column text_type of table content.
- `type`: integer NOT NULL — Column type of table content.
- `version`: bigint NOT NULL — Column version of table content.
- `vote_count`: integer NOT NULL — Column vote_count of table content.
- primary key: id

### content_file  (source backend: rest)
Source table content_file.

- `content_files_id`: bigint NULL — Column content_files_id of table content_file.
- `file_id`: bigint NULL — Column file_id of table content_file.

### content_vote  (source backend: files)
Source table content_vote.

- `article_id`: bigint NOT NULL — Column article_id of table content_vote.
- `content_id`: bigint NOT NULL — Column content_id of table content_vote.
- `date_created`: timestamp NOT NULL — Column date_created of table content_vote.
- `id`: bigint NOT NULL — Column id of table content_vote.
- `point`: integer NOT NULL — Column point of table content_vote.
- `voter_id`: bigint NOT NULL — Column voter_id of table content_vote.
- primary key: id

### file  (source backend: mongodb)
Source table file.

- `attach_type`: text NOT NULL — Column attach_type of table file.
- `byte_size`: integer NOT NULL — Column byte_size of table file.
- `height`: integer NOT NULL — Column height of table file.
- `id`: bigint NOT NULL — Column id of table file.
- `name`: text NOT NULL — Column name of table file.
- `org_name`: text NOT NULL — Column org_name of table file.
- `type`: text NOT NULL — Column type of table file.
- `version`: bigint NOT NULL — Column version of table file.
- `width`: integer NOT NULL — Column width of table file.
- primary key: id

### follow  (source backend: postgres)
Source table follow.

- `date_created`: timestamp NOT NULL — Column date_created of table follow.
- `follower_id`: bigint NOT NULL — Column follower_id of table follow.
- `following_id`: bigint NOT NULL — Column following_id of table follow.
- primary key: follower_id, following_id

### job_position  (source backend: rest)
Source table job_position.

- `description`: text NOT NULL — Column description of table job_position.
- `id`: bigint NOT NULL — Column id of table job_position.
- `job_pay_type`: text NOT NULL — Column job_pay_type of table job_position.
- `max_career`: integer NULL — Column max_career of table job_position.
- `min_career`: integer NOT NULL — Column min_career of table job_position.
- `recruit_id`: bigint NOT NULL — Column recruit_id of table job_position.
- `tag_string`: text NULL — Column tag_string of table job_position.
- `title`: text NOT NULL — Column title of table job_position.
- `version`: bigint NOT NULL — Column version of table job_position.
- primary key: id

### job_position_tag  (source backend: s3)
Source table job_position_tag.

- `job_position_tags_id`: bigint NULL — Column job_position_tags_id of table job_position_tag.
- `tag_id`: bigint NULL — Column tag_id of table job_position_tag.

### logged_in  (source backend: rest)
Source table logged_in.

- `date_created`: timestamp NOT NULL — Column date_created of table logged_in.
- `id`: bigint NOT NULL — Column id of table logged_in.
- `remote_addr`: text NULL — Column remote_addr of table logged_in.
- `user_id`: bigint NOT NULL — Column user_id of table logged_in.
- `version`: bigint NOT NULL — Column version of table logged_in.
- primary key: id

### managed_user  (source backend: rest)
Source table managed_user.

- `id`: bigint NOT NULL — Column id of table managed_user.
- `user_id`: bigint NOT NULL — Column user_id of table managed_user.
- `version`: bigint NOT NULL — Column version of table managed_user.
- primary key: id

### notification  (source backend: rest)
Source table notification.

- `article_id`: bigint NOT NULL — Column article_id of table notification.
- `content_id`: bigint NOT NULL — Column content_id of table notification.
- `date_created`: timestamp NOT NULL — Column date_created of table notification.
- `id`: bigint NOT NULL — Column id of table notification.
- `last_updated`: timestamp NOT NULL — Column last_updated of table notification.
- `receiver_id`: bigint NOT NULL — Column receiver_id of table notification.
- `sender_id`: bigint NOT NULL — Column sender_id of table notification.
- `type`: text NOT NULL — Column type of table notification.
- `version`: bigint NOT NULL — Column version of table notification.
- primary key: id

### notification_read  (source backend: rest)
Source table notification_read.

- `avatar_id`: bigint NOT NULL — Column avatar_id of table notification_read.
- `id`: bigint NOT NULL — Column id of table notification_read.
- `last_read`: timestamp NOT NULL — Column last_read of table notification_read.
- `version`: bigint NOT NULL — Column version of table notification_read.
- primary key: id

### oauthid  (source backend: files)
Source table oauthid.

- `access_token`: text NOT NULL — Column access_token of table oauthid.
- `id`: bigint NOT NULL — Column id of table oauthid.
- `provider`: text NOT NULL — Column provider of table oauthid.
- `user_id`: bigint NOT NULL — Column user_id of table oauthid.
- `version`: bigint NOT NULL — Column version of table oauthid.
- primary key: id

### opinion  (source backend: postgres)
Source table opinion.

- `author_id`: bigint NOT NULL — Column author_id of table opinion.
- `comment`: text NOT NULL — Column comment of table opinion.
- `content_id`: bigint NOT NULL — Column content_id of table opinion.
- `date_created`: timestamp NOT NULL — Column date_created of table opinion.
- `id`: bigint NOT NULL — Column id of table opinion.
- `last_updated`: timestamp NOT NULL — Column last_updated of table opinion.
- `version`: bigint NOT NULL — Column version of table opinion.
- `vote_count`: integer NOT NULL — Column vote_count of table opinion.
- primary key: id

### person  (source backend: rest)
Source table person.

- `company_id`: bigint NULL — Column company_id of table person.
- `date_created`: timestamp NOT NULL — Column date_created of table person.
- `dm_allowed`: boolean NOT NULL — Column dm_allowed of table person.
- `email`: text NOT NULL — Column email of table person.
- `full_name`: text NOT NULL — Column full_name of table person.
- `homepage_url`: text NULL — Column homepage_url of table person.
- `id`: bigint NOT NULL — Column id of table person.
- `last_updated`: timestamp NOT NULL — Column last_updated of table person.
- `resume_id`: bigint NULL — Column resume_id of table person.
- `version`: bigint NOT NULL — Column version of table person.
- primary key: id

### recruit  (source backend: mongodb)
Source table recruit.

- `article_id`: bigint NOT NULL — Column article_id of table recruit.
- `city`: text NOT NULL — Column city of table recruit.
- `closed`: boolean NOT NULL — Column closed of table recruit.
- `company_id`: bigint NULL — Column company_id of table recruit.
- `district`: text NOT NULL — Column district of table recruit.
- `email`: text NOT NULL — Column email of table recruit.
- `id`: bigint NOT NULL — Column id of table recruit.
- `job_type`: text NOT NULL — Column job_type of table recruit.
- `start_date`: text NULL — Column start_date of table recruit.
- `tel`: text NOT NULL — Column tel of table recruit.
- `version`: bigint NOT NULL — Column version of table recruit.
- `working_month`: integer NULL — Column working_month of table recruit.
- primary key: id

### resume  (source backend: postgres)
Source table resume.

- `id`: bigint NOT NULL — Column id of table resume.
- `version`: bigint NOT NULL — Column version of table resume.
- primary key: id

### role  (source backend: mongodb)
Source table role.

- `authority`: text NOT NULL — Column authority of table role.
- `id`: bigint NOT NULL — Column id of table role.
- `version`: bigint NOT NULL — Column version of table role.
- primary key: id

### scrap  (source backend: s3)
Source table scrap.

- `article_id`: bigint NOT NULL — Column article_id of table scrap.
- `avatar_id`: bigint NOT NULL — Column avatar_id of table scrap.
- `date_created`: timestamp NOT NULL — Column date_created of table scrap.
- `version`: bigint NOT NULL — Column version of table scrap.
- primary key: article_id, avatar_id

### spam_word  (source backend: files)
Source table spam_word.

- `id`: bigint NOT NULL — Column id of table spam_word.
- `text`: text NOT NULL — Column text of table spam_word.
- `version`: bigint NOT NULL — Column version of table spam_word.
- primary key: id

### tag  (source backend: rest)
Source table tag.

- `date_created`: timestamp NOT NULL — Column date_created of table tag.
- `description`: text NULL — Column description of table tag.
- `id`: bigint NOT NULL — Column id of table tag.
- `name`: text NOT NULL — Column name of table tag.
- `tagged_count`: integer NOT NULL — Column tagged_count of table tag.
- primary key: id

### tag_similar_text  (source backend: mongodb)
Source table tag_similar_text.

- `id`: bigint NOT NULL — Column id of table tag_similar_text.
- `tag_id`: bigint NOT NULL — Column tag_id of table tag_similar_text.
- `text`: text NOT NULL — Column text of table tag_similar_text.
- `version`: bigint NOT NULL — Column version of table tag_similar_text.
- primary key: id

### user  (source backend: mongodb)
Source table user.

- `account_expired`: boolean NOT NULL — Column account_expired of table user.
- `account_locked`: boolean NOT NULL — Column account_locked of table user.
- `avatar_id`: bigint NOT NULL — Column avatar_id of table user.
- `create_ip`: text NULL — Column create_ip of table user.
- `date_created`: timestamp NOT NULL — Column date_created of table user.
- `date_withdraw`: timestamp NULL — Column date_withdraw of table user.
- `enabled`: boolean NOT NULL — Column enabled of table user.
- `id`: bigint NOT NULL — Column id of table user.
- `last_password_changed`: timestamp NOT NULL — Column last_password_changed of table user.
- `last_update_ip`: text NULL — Column last_update_ip of table user.
- `last_updated`: timestamp NOT NULL — Column last_updated of table user.
- `password`: text NOT NULL — Column password of table user.
- `password_expired`: boolean NOT NULL — Column password_expired of table user.
- `person_id`: bigint NOT NULL — Column person_id of table user.
- `username`: text NOT NULL — Column username of table user.
- `version`: bigint NOT NULL — Column version of table user.
- `withdraw`: boolean NOT NULL — Column withdraw of table user.
- primary key: id

### user_role  (source backend: mongodb)
Source table user_role.

- `role_id`: bigint NOT NULL — Column role_id of table user_role.
- `user_id`: bigint NOT NULL — Column user_id of table user_role.
- primary key: role_id, user_id

### Relationships

- activity(article_id) -> article(id) [required]
- activity(avatar_id) -> avatar(id) [required]
- activity(content_id) -> content(id) [required]
- anonymous(article_id) -> article(id) [required]
- anonymous(content_id) -> content(id) [required]
- anonymous(user_id) -> user(id) [required]
- area_district_code(area_city_code_id) -> area_city_code(id) [required]
- article(author_id) -> avatar(id) [optional (may be NULL/dangling)]
- article(category_id) -> category(code) [required]
- article(content_id) -> content(id) [optional (may be NULL/dangling)]
- article(last_editor_id) -> avatar(id) [optional (may be NULL/dangling)]
- article(selected_note_id) -> content(id) [optional (may be NULL/dangling)]
- article_tag(article_tags_id) -> article(id) [optional (may be NULL/dangling)]
- article_tag(tag_id) -> tag(id) [optional (may be NULL/dangling)]
- avatar_tag(avatar_tags_id) -> avatar(id) [optional (may be NULL/dangling)]
- avatar_tag(tag_id) -> tag(id) [optional (may be NULL/dangling)]
- banner_click(banner_id) -> banner(id) [required]
- career(company_id) -> company(id) [required]
- career(resume_id) -> resume(id) [required]
- change_log(article_id) -> article(id) [required]
- change_log(avatar_id) -> avatar(id) [optional (may be NULL/dangling)]
- change_log(content_id) -> content(id) [optional (may be NULL/dangling)]
- company(manager_id) -> person(id) [optional (may be NULL/dangling)]
- company_info(company_id) -> company(id) [optional (may be NULL/dangling)]
- confirm_email(user_id) -> user(id) [required]
- content(article_id) -> article(id) [optional (may be NULL/dangling)]
- content(author_id) -> avatar(id) [optional (may be NULL/dangling)]
- content(last_editor_id) -> avatar(id) [optional (may be NULL/dangling)]
- content_file(content_files_id) -> content(id) [optional (may be NULL/dangling)]
- content_file(file_id) -> file(id) [optional (may be NULL/dangling)]
- content_vote(article_id) -> article(id) [required]
- content_vote(content_id) -> content(id) [required]
- content_vote(voter_id) -> avatar(id) [required]
- follow(follower_id) -> avatar(id) [required]
- follow(following_id) -> avatar(id) [required]
- job_position(recruit_id) -> recruit(id) [required]
- job_position_tag(job_position_tags_id) -> job_position(id) [optional (may be NULL/dangling)]
- job_position_tag(tag_id) -> tag(id) [optional (may be NULL/dangling)]
- logged_in(user_id) -> user(id) [required]
- managed_user(user_id) -> user(id) [required]
- notification(article_id) -> article(id) [required]
- notification(content_id) -> content(id) [required]
- notification(receiver_id) -> avatar(id) [required]
- notification(sender_id) -> avatar(id) [required]
- notification_read(avatar_id) -> avatar(id) [required]
- oauthid(user_id) -> user(id) [required]
- opinion(author_id) -> avatar(id) [required]
- opinion(content_id) -> content(id) [required]
- person(company_id) -> company(id) [optional (may be NULL/dangling)]
- person(resume_id) -> resume(id) [optional (may be NULL/dangling)]
- recruit(article_id) -> article(id) [required]
- recruit(company_id) -> company(id) [optional (may be NULL/dangling)]
- scrap(article_id) -> article(id) [required]
- scrap(avatar_id) -> avatar(id) [required]
- tag_similar_text(tag_id) -> tag(id) [required]
- user(avatar_id) -> avatar(id) [required]
- user(person_id) -> person(id) [required]
- user_role(role_id) -> role(id) [required]
- user_role(user_id) -> user(id) [required]

