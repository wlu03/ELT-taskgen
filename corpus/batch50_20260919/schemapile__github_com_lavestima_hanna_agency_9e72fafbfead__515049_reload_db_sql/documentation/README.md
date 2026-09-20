# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Lavestima Hanna Agency

## Specification

PROJECT OVERVIEW

This project builds three analytical marts over the 515049_reload-db.sql agency schema. Each source table must be extracted from its own declared backend, and a table's backend is fixed: the source table addresses is extracted from the postgres backend; the source table categories is extracted from the files backend; the source table categories_parameters is extracted from the rest backend; the source table cities is extracted from the files backend; the source table countries is extracted from the s3 backend; the source table currencies is extracted from the rest backend; the source table currencies_rates is extracted from the s3 backend; the source table customers is extracted from the mongodb backend; the source table history_customers is extracted from the s3 backend; the source table history_invoices is extracted from the files backend; the source table history_orders is extracted from the mongodb backend; the source table history_products is extracted from the mongodb backend; the source table history_users is extracted from the mongodb backend; the source table invoices is extracted from the files backend; the source table invoices_products is extracted from the files backend; the source table login_attempts is extracted from the s3 backend; the source table orders is extracted from the s3 backend; the source table orders_products is extracted from the rest backend; the source table orders_statuses is extracted from the postgres backend; the source table parameters is extracted from the rest backend; the source table producers is extracted from the files backend; the source table product_images is extracted from the postgres backend; the source table products is extracted from the rest backend; the source table products_parameters is extracted from the mongodb backend; the source table products_shipment_options is extracted from the mongodb backend; the source table products_sizes is extracted from the files backend; the source table roles is extracted from the postgres backend; the source table shipment_options is extracted from the postgres backend; the source table sizes is extracted from the mongodb backend; the source table tokens is extracted from the mongodb backend; the source table users is extracted from the mongodb backend; the source table users_settings is extracted from the mongodb backend.

RELATIONSHIPS BETWEEN SOURCE TABLES

Each statement below is a complete relationship declaration: it names the child table with its key column and the parent table with its key column, and labels the relationship required or optional exactly as the source schema declares it.

Relationship: child table categories_parameters, child key id_categories, parent table categories, parent key id — required.

Relationship: child table categories_parameters, child key id_parameters, parent table parameters, parent key id — required.

Relationship: child table cities, child key id_countries, parent table countries, parent key id — required.

Relationship: child table currencies_rates, child key id_currencies, parent table currencies, parent key id — required.

Relationship: child table customers, child key id_cities, parent table cities, parent key id — required.

Relationship: child table customers, child key id_countries, parent table countries, parent key id — required.

Relationship: child table customers, child key id_currencies, parent table currencies, parent key id — required.

Relationship: child table customers, child key id_users, parent table users, parent key id — optional (may be NULL or dangling).

Relationship: child table history_customers, child key id_cities, parent table cities, parent key id — optional (may be NULL or dangling).

Relationship: child table history_customers, child key id_countries, parent table countries, parent key id — optional (may be NULL or dangling).

Relationship: child table history_customers, child key id_currencies, parent table currencies, parent key id — optional (may be NULL or dangling).

Relationship: child table history_customers, child key id_customers, parent table customers, parent key id — required.

Relationship: child table history_customers, child key id_users, parent table users, parent key id — optional (may be NULL or dangling).

Relationship: child table history_invoices, child key id_customers, parent table customers, parent key id — optional (may be NULL or dangling).

Relationship: child table history_invoices, child key id_invoices, parent table invoices, parent key id — required.

Relationship: child table history_invoices, child key user_edited, parent table users, parent key id — required.

Relationship: child table history_orders, child key id_customers, parent table customers, parent key id — optional (may be NULL or dangling).

Relationship: child table history_orders, child key id_orders, parent table orders, parent key id — required.

Relationship: child table history_orders, child key user_edited, parent table users, parent key id — required.

Relationship: child table history_products, child key id_categories, parent table categories, parent key id — optional (may be NULL or dangling).

Relationship: child table history_products, child key id_producers, parent table producers, parent key id — optional (may be NULL or dangling).

Relationship: child table history_products, child key id_products, parent table products, parent key id — required.

Relationship: child table history_users, child key id_roles, parent table roles, parent key id — optional (may be NULL or dangling).

Relationship: child table history_users, child key id_users, parent table users, parent key id — required.

Relationship: child table invoices, child key id_customers, parent table customers, parent key id — required.

Relationship: child table invoices, child key user_created, parent table users, parent key id — required.

Relationship: child table invoices, child key user_deleted, parent table users, parent key id — optional (may be NULL or dangling).

Relationship: child table invoices_products, child key id_invoices, parent table invoices, parent key id — required.

Relationship: child table invoices_products, child key id_products_sizes, parent table products_sizes, parent key id — required.

Relationship: child table login_attempts, child key id_users, parent table users, parent key id — optional (may be NULL or dangling).

Relationship: child table orders, child key id_customers, parent table customers, parent key id — required.

Relationship: child table orders, child key user_created, parent table users, parent key id — required.

Relationship: child table orders, child key user_deleted, parent table users, parent key id — optional (may be NULL or dangling).

Relationship: child table orders_products, child key id_orders, parent table orders, parent key id — required.

Relationship: child table orders_products, child key id_products_sizes, parent table products_sizes, parent key id — required.

Relationship: child table orders_products, child key id_statuses, parent table orders_statuses, parent key id — required.

Relationship: child table producers, child key id_cities, parent table cities, parent key id — required.

Relationship: child table producers, child key id_countries, parent table countries, parent key id — required.

Relationship: child table producers, child key id_users, parent table users, parent key id — optional (may be NULL or dangling).

Relationship: child table product_images, child key id_products, parent table products, parent key id — required.

Relationship: child table products, child key id_categories, parent table categories, parent key id — required.

Relationship: child table products, child key id_producers, parent table producers, parent key id — required.

Relationship: child table products_parameters, child key id_parameters, parent table parameters, parent key id — required.

Relationship: child table products_parameters, child key id_products, parent table products, parent key id — required.

Relationship: child table products_shipment_options, child key id_products, parent table products, parent key id — required.

Relationship: child table products_shipment_options, child key id_shipment_options, parent table shipment_options, parent key id — required.

Relationship: child table products_sizes, child key id_products, parent table products, parent key id — required.

Relationship: child table products_sizes, child key id_sizes, parent table sizes, parent key id — required.

Relationship: child table tokens, child key id_users, parent table users, parent key id — required.

Relationship: child table users, child key id_roles, parent table roles, parent key id — required.

Relationship: child table users_settings, child key id_users, parent table users, parent key id — required.

Three marts follow, each in its own labelled section: users_producers_snapshot, roles_history_users_distribution and products_history_products_top.

=== Mart users_producers_snapshot: a per-users latest-row snapshot over linked producers activity in the 515049_reload-db.sql schema ===

Grain: one row per users (id), INCLUDING users rows with no linked producers rows. The single key column is parent_key.

Rule 1: the source table users is read in full as an input of this mart.

Rule 2: the source table producers is read in full as an input of this mart.

Rule 3: from the source table users there is one row per users row, keyed by id, carrying parent_key and parent_name.

Rule 4: the producers rows are brought in from the source table producers, matching a producers row by its id_users against parent_key and carrying id_users and id; preservation is left-sided on the users side, so a users row with no matching producers row is retained and receives the stated empty snapshot values.

Rule 5: there is one output row per parent_key, carrying parent_name beside the keys — a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group — and each such row reports event_count and lifetime_amount for that row's matching rows.

Rule 6: for each parent_key the single row at which the ordering measure — date_created — is largest survives, ties broken by the smallest id, and latest_row_id, latest_amount and latest_label are taken from that winning row; the ordering measure is required on every real input row, so a non-empty group always has a winning row, and the declared defaults belong only to a group with NO rows.

Rule 7: the extremal row's attributes are attached to the grouped measures, matching on parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

Rule 8: the mart columns are named parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label, and lifetime_amount reports its declared default of 0 — never NULL — for a group with no matching rows.

Rule 9: a guarded ratio is added beside parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label: latest_amount_share is latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places, and it is 0.0 when the denominator lifetime_amount is 0 or has no value.

Rule 10: the deterministic output order is ascending parent_key.

Output columns of users_producers_snapshot:

parent_key (integer): the identifier of the users row; there is one row per value.

parent_name (text): the email of the users row, copied unchanged.

event_count (bigint): the number of producers rows for this users row, and 0 when there are none; a users row kept with no producers row reports 0 here, never 1, because its placeholder holds no producers row to count.

lifetime_amount (integer): the total of user_created over all matching producers rows, and 0 when there are no rows.

latest_row_id (integer): the id of the row with the latest date_created, with ties taking the smallest id; it is 0 when there are no rows.

latest_amount (integer): the user_created from that same latest row, and 0 when there are no rows.

latest_label (text): the email from that same latest row, and the literal '(none)' when there are no rows.

latest_amount_share (float): latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places, and 0.0 when lifetime_amount is 0.

=== Mart roles_history_users_distribution: a per-(roles, measure state) distribution of linked history_users activity in the 515049_reload-db.sql schema ===

Grain: one row per (id, measure state) pair represented by linked history_users rows, plus one absent no-activity row for a roles row with no links. Because user_edited is required, no linked history_users row belongs to the absent state. The key columns are entity_key and measure_state.

Rule 1: the source table roles is read in full as an input of this mart.

Rule 2: the source table history_users is read in full as an input of this mart.

Rule 3: each id of roles and its code are carried into the measure-state calculation as entity_key and entity_name.

Rule 4: the linked history_users rows are brought into each roles entity, matching a history_users row by its id_roles against entity_key and carrying entity_key, entity_name, id and id_roles; preservation is left-sided on the roles side, so an entity with no linked row is retained and its absent state is visible.

Rule 5: the present measure-state rows are kept, carrying entity_key and entity_name: a present row is a real history_users row, and user_edited is required on every such row.

Rule 6: there is one row per roles entity that has at least one row in the present measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count as the row count, distinct_amount_count as how many different user_edited values occur (each different value counted once, however many rows repeat it), total_amount as the total user_edited, and max_amount as the largest user_edited.

Rule 7: beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and it is 0.0 when total_amount is 0.

Rule 8: these measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state holding the present measure state, the literal 'present'.

Rule 9: the absent measure-state rows are kept, carrying entity_key and entity_name: an absent row is the retained placeholder for a roles row with no history_users rows, and no real row can enter this state because user_edited is required.

Rule 10: there is one row per roles entity with no linked history_users row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked history_users row, reporting entity_key, entity_name, a row_count of 0, 0 different user_edited values in distinct_amount_count, a total user_edited of 0 in total_amount and a largest user_edited of 0 in max_amount.

Rule 11: beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and it is 0.0 when total_amount is 0.

Rule 12: these measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state holding the absent measure state, the literal 'absent'.

Rule 13: the present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14: the deterministic output order is ascending entity_key, then ascending measure_state.

Output columns of roles_history_users_distribution:

entity_key (integer): the identifier of the roles row.

measure_state (text): 'present' for a linked history_users row, and 'absent' only for a roles row with no linked history_users row; user_edited is required on every real history_users row.

entity_name (text): the code of the roles row, copied unchanged.

row_count (bigint): the number of linked history_users rows in this entity/state cell, and 0 for a no-activity absent cell.

distinct_amount_count (bigint): the number of unique user_edited values in this cell, where each unique value is counted once however many rows repeat it, and 0 for a no-activity absent cell.

total_amount (integer): the total of user_edited in this cell, and 0 for a no-activity absent cell.

max_amount (integer): the largest user_edited in this cell, and 0 for a no-activity absent cell.

max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0.

=== Mart products_history_products_top: per-products extremes over linked history_products rows in the 515049_reload-db.sql schema — WHICH row is largest, not how large it is ===

Grain: one row per products (id), INCLUDING products rows with no linked history_products rows. The single key column is parent_key.

Rule 1: the source table products is read in full as an input of this mart.

Rule 2: the source table history_products is read in full as an input of this mart.

Rule 3: from the source table products there is one row per products row, keyed by id, carrying parent_key and parent_name.

Rule 4: the history_products rows are brought in from the source table history_products, matching a history_products row by its id_products against parent_key and carrying id_products and id; preservation is left-sided on the products side, so a products row with no history_products rows still appears, with the declared defaults.

Rule 5: within each parent_key group the matched rows are ranked under an explicit total order — the measure active first, then the declared tie-break — so the extremal row for parent_key is a function of the input and not of row order.

Rule 6: there is one output row per parent_key, carrying parent_name beside the keys — a key value identifies one source row for the carried columns, so they take one value per key and never split a group — reporting top_measure, tied_count, child_count and total_measure for that row's matching rows; a group with no qualifying rows still appears, reporting 0, and a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7: for each parent_key the single row at which the ordering measure active is largest survives, ties broken by the smallest name under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one, and a row with no name value sorts after every row that has one), then the smallest id, and top_label and top_row_id are taken from that winning row; the ordering measure is required on every real input row, so a non-empty group always has a winning row, and the declared defaults belong only to a group with NO rows.

Rule 8: the extremal row's attributes are attached to the grouped measures, matching on parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

Rule 9: the mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id, and top_measure and total_measure report their declared defaults of 0 — never NULL — for a group with no matching rows.

Rule 10: a guarded ratio is added beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when the denominator total_measure is 0 or has no value.

Rule 11: beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, tie_state is 'empty' when no row holds a maximum at all — the parent has no history_products rows — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more, and it is never null or blank.

Rule 12: the deterministic output order is ascending parent_key.

Output columns of products_history_products_top:

parent_key (integer): the identifier of the products row; there is one row per value.

parent_name (text): the name of the products row, copied unchanged.

top_measure (integer): the largest active itself, and 0 when the parent has no history_products rows.

tied_count (bigint): how many history_products rows are tied at that largest active; it is 1 when exactly one row carries that largest active, and 0 when there are no rows.

child_count (bigint): the number of history_products rows for this products row, and 0 when there are none; a products row kept with no history_products row reports 0 here, never 1, because its placeholder holds no history_products row to count.

total_measure (integer): the total of active over all of them, and 0 when the parent has no history_products rows.

top_label (text): the name of the history_products row with the LARGEST active for this products row; ties in active are broken by taking the SMALLEST name under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no name value sorts after every labelled row — and rows tied on both are resolved by the smallest id; it is the literal '(none)' when the parent has no history_products rows at all, and '(none)' when the winning row has no name value.

top_row_id (integer): the id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real history_products row whenever the parent has any; it is 0 when there are no rows, and only then; it identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.

top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when total_measure is 0.

tie_state (text): 'empty' when no row holds a maximum at all — the parent has no history_products rows — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, and 'tied' when it is 2 or more.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `users_producers_snapshot`

- Grain: One row per users (id), INCLUDING users rows with no linked producers rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share

```text
Mart 'users_producers_snapshot' has 10 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table producers. (public source tables: producers)
3. [derive] One row per users row, keyed by id. (public source tables: users | public carried/output columns: parent_key, parent_name)
4. [join] Bring in producers; a users row with no matching producers row is retained and receives the stated empty snapshot values. (public source tables: producers | public carried/output columns: id_users, id | join preservation: left | condition public identifiers: producers, id_users, parent_key)
5. [aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count, lifetime_amount for that row's matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount)
6. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest id, and take latest_row_id, latest_amount, latest_label from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, latest_row_id, latest_amount, latest_label)
7. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
8. [derive] Name the mart columns; lifetime_amount reports its declared default — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label)
9. [ratio] Guarded ratios: latest_amount_share — latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `roles_history_users_distribution`

- Grain: One row per (id, measure state) pair represented by linked history_users rows, plus one absent no-activity row for a roles row with no links. Because user_edited is required, no linked history_users row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'roles_history_users_distribution' has 14 declared semantic rules:
1. [source] Read source table roles. (public source tables: roles)
2. [source] Read source table history_users. (public source tables: history_users)
3. [derive] Carry each id and its code into the measure-state calculation. (public source tables: roles | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked history_users rows into each roles entity; retain an entity with no linked row so its absent state is visible. (public source tables: history_users | public carried/output columns: entity_key, entity_name, id, id_roles | join preservation: left | condition public identifiers: history_users, id_roles, entity_key)
5. [filter] Keep the present measure-state rows: a real history_users row; user_edited is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per roles entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different user_edited values occur (each different value counted once, however many rows repeat it), total user_edited, and largest user_edited. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a roles row with no history_users rows; no real row can enter this state because user_edited is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per roles entity with no linked history_users row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked history_users row, reporting a row count of 0, 0 different user_edited values, a total user_edited of 0 and a largest user_edited of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `products_history_products_top`

- Grain: One row per products (id), INCLUDING products rows with no linked history_products rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'products_history_products_top' has 12 declared semantic rules:
1. [source] Read source table products. (public source tables: products)
2. [source] Read source table history_products. (public source tables: history_products)
3. [derive] One row per products row, keyed by id. (public source tables: products | public carried/output columns: parent_key, parent_name)
4. [join] Bring in history_products: a products row with no history_products rows still appears, with the declared defaults. (public source tables: history_products | public carried/output columns: id_products, id | join preservation: left | condition public identifiers: history_products, id_products, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest name under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no name value sorts after every row that has one), then the smallest id, and take top_label, top_row_id from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no history_products rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### addresses  (source backend: postgres)
Source table Addresses.

- `id`: integer NOT NULL — Column ID of table Addresses.

### categories  (source backend: files)
Source table Categories.

- `id`: integer NOT NULL — Column ID of table Categories.
- `id_parent`: integer NULL — Column ID_PARENT of table Categories.
- `name`: text NOT NULL — Column Name of table Categories.
- `note`: text NULL — Column Note of table Categories.
- primary key: id

### categories_parameters  (source backend: rest)
Source table Categories_Parameters.

- `id`: integer NOT NULL — Column ID of table Categories_Parameters.
- `id_categories`: integer NOT NULL — Column ID_CATEGORIES of table Categories_Parameters.
- `id_parameters`: integer NOT NULL — Column ID_PARAMETERS of table Categories_Parameters.
- primary key: id

### cities  (source backend: files)
Source table Cities.

- `id`: integer NOT NULL — Column ID of table Cities.
- `id_countries`: integer NOT NULL — Column ID_COUNTRIES of table Cities.
- `name`: text NOT NULL — Column Name of table Cities.
- `note`: text NULL — Column Note of table Cities.
- primary key: id

### countries  (source backend: s3)
Source table Countries.

- `id`: integer NOT NULL — Column ID of table Countries.
- `name`: text NOT NULL — Column Name of table Countries.
- `note`: text NULL — Column Note of table Countries.
- `symbol`: text NOT NULL — Column Symbol of table Countries.
- primary key: id

### currencies  (source backend: rest)
Source table Currencies.

- `id`: integer NOT NULL — Column ID of table Currencies.
- `name`: text NOT NULL — Column Name of table Currencies.
- `symbol`: text NOT NULL — Column Symbol of table Currencies.
- primary key: id

### currencies_rates  (source backend: s3)
Source table Currencies_Rates.

- `conversion_rate`: decimal NOT NULL — Column Conversion_Rate of table Currencies_Rates.
- `date_rate`: timestamp NOT NULL — Column Date_Rate of table Currencies_Rates.
- `id`: integer NOT NULL — Column ID of table Currencies_Rates.
- `id_currencies`: integer NOT NULL — Column ID_CURRENCIES of table Currencies_Rates.
- primary key: id

### customers  (source backend: mongodb)
Source table Customers.

- `company_name`: text NULL — Column Company_Name of table Customers.
- `date_created`: timestamp NOT NULL — Column Date_Created of table Customers.
- `date_deleted`: timestamp NULL — Column Date_Deleted of table Customers.
- `default_discount`: integer NULL — Column Default_Discount of table Customers.
- `email`: text NOT NULL — Column Email of table Customers.
- `first_name`: text NOT NULL — Column First_Name of table Customers.
- `gender`: text NOT NULL — Column Gender of table Customers.
- `id`: integer NOT NULL — Column ID of table Customers.
- `id_cities`: integer NOT NULL — Column ID_CITIES of table Customers.
- `id_countries`: integer NOT NULL — Column ID_COUNTRIES of table Customers.
- `id_currencies`: integer NOT NULL — Column ID_CURRENCIES of table Customers.
- `id_users`: integer NULL — Column ID_USERS of table Customers.
- `identification_number`: text NOT NULL — Column Identification_Number of table Customers.
- `last_name`: text NOT NULL — Column Last_Name of table Customers.
- `path_slug`: text NOT NULL — Column Path_Slug of table Customers.
- `phone`: text NOT NULL — Column Phone of table Customers.
- `postal_code`: text NOT NULL — Column Postal_Code of table Customers.
- `street`: text NOT NULL — Column Street of table Customers.
- `user_created`: integer NOT NULL — Column User_Created of table Customers.
- `user_deleted`: integer NULL — Column User_Deleted of table Customers.
- `vat`: text NULL — Column VAT of table Customers.
- primary key: id

### history_customers  (source backend: s3)
Source table History_Customers.

- `company_name`: text NULL — Column Company_Name of table History_Customers.
- `date_edited`: timestamp NOT NULL — Column Date_Edited of table History_Customers.
- `default_discount`: integer NULL — Column Default_Discount of table History_Customers.
- `email`: text NULL — Column Email of table History_Customers.
- `first_name`: text NULL — Column First_Name of table History_Customers.
- `gender`: text NULL — Column Gender of table History_Customers.
- `id`: integer NOT NULL — Column ID of table History_Customers.
- `id_cities`: integer NULL — Column ID_CITIES of table History_Customers.
- `id_countries`: integer NULL — Column ID_COUNTRIES of table History_Customers.
- `id_currencies`: integer NULL — Column ID_CURRENCIES of table History_Customers.
- `id_customers`: integer NOT NULL — Column ID_CUSTOMERS of table History_Customers.
- `id_users`: integer NULL — Column ID_USERS of table History_Customers.
- `identification_number`: text NULL — Column Identification_Number of table History_Customers.
- `last_name`: text NULL — Column Last_Name of table History_Customers.
- `phone`: text NULL — Column Phone of table History_Customers.
- `postal_code`: text NULL — Column Postal_Code of table History_Customers.
- `street`: text NULL — Column Street of table History_Customers.
- `user_edited`: integer NOT NULL — Column User_Edited of table History_Customers.
- `vat`: text NULL — Column VAT of table History_Customers.
- primary key: id

### history_invoices  (source backend: files)
Source table History_Invoices.

- `date_edited`: timestamp NOT NULL — Column Date_Edited of table History_Invoices.
- `date_issued`: timestamp NULL — Column Date_Issued of table History_Invoices.
- `id`: integer NOT NULL — Column ID of table History_Invoices.
- `id_customers`: integer NULL — Column ID_CUSTOMERS of table History_Invoices.
- `id_invoices`: integer NOT NULL — Column ID_INVOICES of table History_Invoices.
- `name`: text NULL — Column Name of table History_Invoices.
- `user_edited`: integer NOT NULL — Column User_Edited of table History_Invoices.
- primary key: id

### history_orders  (source backend: mongodb)
Source table History_Orders.

- `date_edited`: timestamp NOT NULL — Column Date_Edited of table History_Orders.
- `id`: integer NOT NULL — Column ID of table History_Orders.
- `id_customers`: integer NULL — Column ID_CUSTOMERS of table History_Orders.
- `id_orders`: integer NOT NULL — Column ID_ORDERS of table History_Orders.
- `user_edited`: integer NOT NULL — Column User_Edited of table History_Orders.
- primary key: id

### history_products  (source backend: mongodb)
Source table History_Products.

- `active`: integer NOT NULL — Column Active of table History_Products.
- `date_edited`: timestamp NOT NULL — Column Date_Edited of table History_Products.
- `id`: integer NOT NULL — Column ID of table History_Products.
- `id_categories`: integer NULL — Column ID_CATEGORIES of table History_Products.
- `id_producers`: integer NULL — Column ID_PRODUCERS of table History_Products.
- `id_products`: integer NOT NULL — Column ID_PRODUCTS of table History_Products.
- `name`: text NULL — Column Name of table History_Products.
- `price_customer`: decimal NULL — Column Price_Customer of table History_Products.
- `price_producer`: decimal NULL — Column Price_Producer of table History_Products.
- `qr_code_path`: text NULL — Column QR_Code_Path of table History_Products.
- `user_edited`: integer NOT NULL — Column User_Edited of table History_Products.
- primary key: id

### history_users  (source backend: mongodb)
Source table History_Users.

- `date_edited`: timestamp NOT NULL — Column Date_Edited of table History_Users.
- `email`: text NULL — Column Email of table History_Users.
- `id`: integer NOT NULL — Column ID of table History_Users.
- `id_roles`: integer NULL — Column ID_ROLES of table History_Users.
- `id_users`: integer NOT NULL — Column ID_USERS of table History_Users.
- `login`: text NULL — Column Login of table History_Users.
- `password_hash`: text NULL — Column Password_Hash of table History_Users.
- `user_edited`: integer NOT NULL — Column User_Edited of table History_Users.
- primary key: id

### invoices  (source backend: files)
Source table Invoices.

- `date_created`: timestamp NOT NULL — Column Date_Created of table Invoices.
- `date_deleted`: timestamp NULL — Column Date_Deleted of table Invoices.
- `date_issued`: timestamp NOT NULL — Column Date_Issued of table Invoices.
- `id`: integer NOT NULL — Column ID of table Invoices.
- `id_customers`: integer NOT NULL — Column ID_CUSTOMERS of table Invoices.
- `name`: text NOT NULL — Column Name of table Invoices.
- `path_slug`: text NOT NULL — Column Path_Slug of table Invoices.
- `user_created`: integer NOT NULL — Column User_Created of table Invoices.
- `user_deleted`: integer NULL — Column User_Deleted of table Invoices.
- primary key: id

### invoices_products  (source backend: files)
Source table Invoices_Products.

- `discount`: integer NOT NULL — Column Discount of table Invoices_Products.
- `id`: integer NOT NULL — Column ID of table Invoices_Products.
- `id_invoices`: integer NOT NULL — Column ID_INVOICES of table Invoices_Products.
- `id_products_sizes`: integer NOT NULL — Column ID_PRODUCTS_SIZES of table Invoices_Products.
- `note`: text NULL — Column Note of table Invoices_Products.
- `price_final`: decimal NOT NULL — Column Price_Final of table Invoices_Products.
- `quantity`: integer NOT NULL — Column Quantity of table Invoices_Products.
- primary key: id

### login_attempts  (source backend: s3)
Source table Login_Attempts.

- `date_created`: timestamp NOT NULL — Column Date_Created of table Login_Attempts.
- `id`: integer NOT NULL — Column ID of table Login_Attempts.
- `id_users`: integer NULL — Column ID_USERS of table Login_Attempts.
- `ip_address`: text NOT NULL — Column Ip_Address of table Login_Attempts.
- `is_failed`: integer NOT NULL — Column Is_Failed of table Login_Attempts.
- primary key: id

### orders  (source backend: s3)
Source table Orders.

- `date_created`: timestamp NOT NULL — Column Date_Created of table Orders.
- `date_deleted`: timestamp NULL — Column Date_Deleted of table Orders.
- `id`: integer NOT NULL — Column ID of table Orders.
- `id_customers`: integer NOT NULL — Column ID_CUSTOMERS of table Orders.
- `identifier`: text NOT NULL — Column Identifier of table Orders.
- `path_slug`: text NOT NULL — Column Path_Slug of table Orders.
- `user_created`: integer NOT NULL — Column User_Created of table Orders.
- `user_deleted`: integer NULL — Column User_Deleted of table Orders.
- primary key: id

### orders_products  (source backend: rest)
Source table Orders_Products.

- `discount`: integer NOT NULL — Column Discount of table Orders_Products.
- `id`: integer NOT NULL — Column ID of table Orders_Products.
- `id_orders`: integer NOT NULL — Column ID_ORDERS of table Orders_Products.
- `id_products_sizes`: integer NOT NULL — Column ID_PRODUCTS_SIZES of table Orders_Products.
- `id_statuses`: integer NOT NULL — Column ID_STATUSES of table Orders_Products.
- `note`: text NULL — Column Note of table Orders_Products.
- `quantity`: integer NOT NULL — Column Quantity of table Orders_Products.
- primary key: id

### orders_statuses  (source backend: postgres)
Source table Orders_Statuses.

- `id`: integer NOT NULL — Column ID of table Orders_Statuses.
- `name`: text NOT NULL — Column Name of table Orders_Statuses.
- primary key: id

### parameters  (source backend: rest)
Source table Parameters.

- `id`: integer NOT NULL — Column ID of table Parameters.
- `name`: text NOT NULL — Column Name of table Parameters.
- `unit`: text NULL — Column Unit of table Parameters.
- primary key: id

### producers  (source backend: files)
Source table Producers.

- `date_created`: timestamp NOT NULL — Column Date_Created of table Producers.
- `date_deleted`: timestamp NULL — Column Date_Deleted of table Producers.
- `email`: text NOT NULL — Column Email of table Producers.
- `first_name`: text NULL — Column First_Name of table Producers.
- `full_name`: text NOT NULL — Column Full_Name of table Producers.
- `id`: integer NOT NULL — Column ID of table Producers.
- `id_cities`: integer NOT NULL — Column ID_CITIES of table Producers.
- `id_countries`: integer NOT NULL — Column ID_COUNTRIES of table Producers.
- `id_users`: integer NULL — Column ID_USERS of table Producers.
- `last_name`: text NULL — Column Last_Name of table Producers.
- `path_slug`: text NOT NULL — Column Path_Slug of table Producers.
- `phone`: text NOT NULL — Column Phone of table Producers.
- `postal_code`: text NOT NULL — Column Postal_Code of table Producers.
- `short_name`: text NOT NULL — Column Short_Name of table Producers.
- `street`: text NOT NULL — Column Street of table Producers.
- `user_created`: integer NOT NULL — Column User_Created of table Producers.
- `user_deleted`: integer NULL — Column User_Deleted of table Producers.
- `vat`: text NULL — Column VAT of table Producers.
- primary key: id

### product_images  (source backend: postgres)
Source table Product_Images.

- `file_path`: text NOT NULL — Column File_Path of table Product_Images.
- `id`: integer NOT NULL — Column ID of table Product_Images.
- `id_products`: integer NOT NULL — Column ID_PRODUCTS of table Product_Images.
- `sequence_position`: integer NOT NULL — Column Sequence_Position of table Product_Images.
- primary key: id

### products  (source backend: rest)
Source table Products.

- `active`: integer NOT NULL — Column Active of table Products.
- `date_created`: timestamp NOT NULL — Column Date_Created of table Products.
- `date_deleted`: timestamp NULL — Column Date_Deleted of table Products.
- `id`: integer NOT NULL — Column ID of table Products.
- `id_categories`: integer NOT NULL — Column ID_CATEGORIES of table Products.
- `id_producers`: integer NOT NULL — Column ID_PRODUCERS of table Products.
- `name`: text NOT NULL — Column Name of table Products.
- `path_slug`: text NOT NULL — Column Path_Slug of table Products.
- `price_customer`: decimal NOT NULL — Column Price_Customer of table Products.
- `price_producer`: decimal NOT NULL — Column Price_Producer of table Products.
- `user_created`: integer NOT NULL — Column User_Created of table Products.
- `user_deleted`: integer NULL — Column User_Deleted of table Products.
- primary key: id

### products_parameters  (source backend: mongodb)
Source table Products_Parameters.

- `id`: integer NOT NULL — Column ID of table Products_Parameters.
- `id_parameters`: integer NOT NULL — Column ID_PARAMETERS of table Products_Parameters.
- `id_products`: integer NOT NULL — Column ID_PRODUCTS of table Products_Parameters.
- `value`: text NOT NULL — Column Value of table Products_Parameters.
- primary key: id

### products_shipment_options  (source backend: mongodb)
Source table Products_Shipment_Options.

- `cost`: decimal NOT NULL — Column Cost of table Products_Shipment_Options.
- `id`: integer NOT NULL — Column ID of table Products_Shipment_Options.
- `id_products`: integer NOT NULL — Column ID_PRODUCTS of table Products_Shipment_Options.
- `id_shipment_options`: integer NOT NULL — Column ID_SHIPMENT_OPTIONS of table Products_Shipment_Options.
- primary key: id

### products_sizes  (source backend: files)
Source table Products_Sizes.

- `availability`: integer NOT NULL — Column Availability of table Products_Sizes.
- `id`: integer NOT NULL — Column ID of table Products_Sizes.
- `id_products`: integer NOT NULL — Column ID_PRODUCTS of table Products_Sizes.
- `id_sizes`: integer NOT NULL — Column ID_SIZES of table Products_Sizes.
- primary key: id

### roles  (source backend: postgres)
Source table Roles.

- `code`: text NOT NULL — Column Code of table Roles.
- `id`: integer NOT NULL — Column ID of table Roles.
- `name`: text NOT NULL — Column Name of table Roles.
- primary key: id

### shipment_options  (source backend: postgres)
Source table Shipment_Options.

- `cost`: decimal NOT NULL — Column Cost of table Shipment_Options.
- `id`: integer NOT NULL — Column ID of table Shipment_Options.
- `name`: text NOT NULL — Column Name of table Shipment_Options.
- primary key: id

### sizes  (source backend: mongodb)
Source table Sizes.

- `id`: integer NOT NULL — Column ID of table Sizes.
- `name`: text NOT NULL — Column Name of table Sizes.
- `note`: text NULL — Column Note of table Sizes.
- primary key: id

### tokens  (source backend: mongodb)
Source table Tokens.

- `date_created`: timestamp NOT NULL — Column Date_Created of table Tokens.
- `date_expired`: timestamp NOT NULL — Column Date_Expired of table Tokens.
- `id`: integer NOT NULL — Column ID of table Tokens.
- `id_users`: integer NOT NULL — Column ID_USERS of table Tokens.
- `token`: text NOT NULL — Column Token of table Tokens.
- primary key: id

### users  (source backend: mongodb)
Source table Users.

- `date_created`: timestamp NOT NULL — Column Date_Created of table Users.
- `date_deleted`: timestamp NULL — Column Date_Deleted of table Users.
- `email`: text NOT NULL — Column Email of table Users.
- `id`: integer NOT NULL — Column ID of table Users.
- `id_roles`: integer NOT NULL — Column ID_ROLES of table Users.
- `login`: text NOT NULL — Column Login of table Users.
- `password_hash`: text NOT NULL — Column Password_Hash of table Users.
- `path_slug`: text NOT NULL — Column Path_Slug of table Users.
- primary key: id

### users_settings  (source backend: mongodb)
Source table Users_Settings.

- `id`: integer NOT NULL — Column ID of table Users_Settings.
- `id_users`: integer NOT NULL — Column ID_USERS of table Users_Settings.
- `locale`: text NOT NULL — Column Locale of table Users_Settings.
- `newsletter`: integer NOT NULL — Column Newsletter of table Users_Settings.
- primary key: id

### Relationships

- categories_parameters(id_categories) -> categories(id) [required]
- categories_parameters(id_parameters) -> parameters(id) [required]
- cities(id_countries) -> countries(id) [required]
- currencies_rates(id_currencies) -> currencies(id) [required]
- customers(id_cities) -> cities(id) [required]
- customers(id_countries) -> countries(id) [required]
- customers(id_currencies) -> currencies(id) [required]
- customers(id_users) -> users(id) [optional (may be NULL/dangling)]
- history_customers(id_cities) -> cities(id) [optional (may be NULL/dangling)]
- history_customers(id_countries) -> countries(id) [optional (may be NULL/dangling)]
- history_customers(id_currencies) -> currencies(id) [optional (may be NULL/dangling)]
- history_customers(id_customers) -> customers(id) [required]
- history_customers(id_users) -> users(id) [optional (may be NULL/dangling)]
- history_invoices(id_customers) -> customers(id) [optional (may be NULL/dangling)]
- history_invoices(id_invoices) -> invoices(id) [required]
- history_invoices(user_edited) -> users(id) [required]
- history_orders(id_customers) -> customers(id) [optional (may be NULL/dangling)]
- history_orders(id_orders) -> orders(id) [required]
- history_orders(user_edited) -> users(id) [required]
- history_products(id_categories) -> categories(id) [optional (may be NULL/dangling)]
- history_products(id_producers) -> producers(id) [optional (may be NULL/dangling)]
- history_products(id_products) -> products(id) [required]
- history_users(id_roles) -> roles(id) [optional (may be NULL/dangling)]
- history_users(id_users) -> users(id) [required]
- invoices(id_customers) -> customers(id) [required]
- invoices(user_created) -> users(id) [required]
- invoices(user_deleted) -> users(id) [optional (may be NULL/dangling)]
- invoices_products(id_invoices) -> invoices(id) [required]
- invoices_products(id_products_sizes) -> products_sizes(id) [required]
- login_attempts(id_users) -> users(id) [optional (may be NULL/dangling)]
- orders(id_customers) -> customers(id) [required]
- orders(user_created) -> users(id) [required]
- orders(user_deleted) -> users(id) [optional (may be NULL/dangling)]
- orders_products(id_orders) -> orders(id) [required]
- orders_products(id_products_sizes) -> products_sizes(id) [required]
- orders_products(id_statuses) -> orders_statuses(id) [required]
- producers(id_cities) -> cities(id) [required]
- producers(id_countries) -> countries(id) [required]
- producers(id_users) -> users(id) [optional (may be NULL/dangling)]
- product_images(id_products) -> products(id) [required]
- products(id_categories) -> categories(id) [required]
- products(id_producers) -> producers(id) [required]
- products_parameters(id_parameters) -> parameters(id) [required]
- products_parameters(id_products) -> products(id) [required]
- products_shipment_options(id_products) -> products(id) [required]
- products_shipment_options(id_shipment_options) -> shipment_options(id) [required]
- products_sizes(id_products) -> products(id) [required]
- products_sizes(id_sizes) -> sizes(id) [required]
- tokens(id_users) -> users(id) [required]
- users(id_roles) -> roles(id) [required]
- users_settings(id_users) -> users(id) [required]

