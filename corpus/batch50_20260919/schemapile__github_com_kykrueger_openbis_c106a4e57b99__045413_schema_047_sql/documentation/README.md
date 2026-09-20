# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Kykrueger Openbis

## Specification

PROJECT OVERVIEW

This project builds three analytical marts from an openBIS-style metadata schema. All source data is extracted from heterogeneous backends, and every table below must be read from the backend named beside it.

Source tables and their extraction backends. The table attachments is extracted from the postgres backend. The table attachment_contents is extracted from the postgres backend. The table authorization_groups is extracted from the mongodb backend. The table authorization_group_persons is extracted from the rest backend. The table controlled_vocabularies is extracted from the rest backend. The table controlled_vocabulary_terms is extracted from the rest backend. The table data is extracted from the mongodb backend. The table database_instances is extracted from the s3 backend. The table data_set_properties is extracted from the rest backend. The table data_set_relationships is extracted from the rest backend. The table data_set_types is extracted from the rest backend. The table data_set_type_property_types is extracted from the mongodb backend. The table data_stores is extracted from the rest backend. The table data_store_services is extracted from the postgres backend. The table data_store_service_data_set_types is extracted from the mongodb backend. The table data_types is extracted from the rest backend. The table events is extracted from the postgres backend. The table experiments is extracted from the files backend. The table experiment_properties is extracted from the postgres backend. The table experiment_types is extracted from the files backend. The table experiment_type_property_types is extracted from the rest backend. The table external_data is extracted from the postgres backend. The table file_format_types is extracted from the postgres backend. The table filters is extracted from the rest backend. The table grid_custom_columns is extracted from the rest backend. The table groups is extracted from the mongodb backend. The table invalidations is extracted from the files backend. The table locator_types is extracted from the files backend. The table materials is extracted from the postgres backend. The table material_properties is extracted from the files backend. The table material_types is extracted from the rest backend. The table material_type_property_types is extracted from the s3 backend. The table persons is extracted from the mongodb backend. The table projects is extracted from the s3 backend. The table property_types is extracted from the mongodb backend. The table role_assignments is extracted from the s3 backend. The table samples is extracted from the mongodb backend. The table sample_properties is extracted from the files backend. The table sample_types is extracted from the mongodb backend. The table sample_type_property_types is extracted from the rest backend.

RELATIONSHIPS. Each line below names the child table with its child key columns, the parent table with its parent key columns, and whether the relationship is required or optional.

relationship: child table attachments (child key exac_id) -> parent table attachment_contents (parent key id), required.
relationship: child table attachments (child key expe_id) -> parent table experiments (parent key id), optional (may be NULL or dangling).
relationship: child table attachments (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table attachments (child key proj_id) -> parent table projects (parent key id), optional (may be NULL or dangling).
relationship: child table attachments (child key samp_id) -> parent table samples (parent key id), optional (may be NULL or dangling).
relationship: child table authorization_group_persons (child key ag_id) -> parent table authorization_groups (parent key id), required.
relationship: child table authorization_group_persons (child key pers_id) -> parent table persons (parent key id), required.
relationship: child table authorization_groups (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table authorization_groups (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table controlled_vocabularies (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table controlled_vocabularies (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table controlled_vocabulary_terms (child key covo_id) -> parent table controlled_vocabularies (parent key id), required.
relationship: child table controlled_vocabulary_terms (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table data (child key dast_id) -> parent table data_stores (parent key id), required.
relationship: child table data (child key dsty_id) -> parent table data_set_types (parent key id), required.
relationship: child table data (child key expe_id) -> parent table experiments (parent key id), required.
relationship: child table data (child key samp_id) -> parent table samples (parent key id), optional (may be NULL or dangling).
relationship: child table data_set_properties (child key cvte_id) -> parent table controlled_vocabulary_terms (parent key id), optional (may be NULL or dangling).
relationship: child table data_set_properties (child key ds_id) -> parent table data (parent key id), required.
relationship: child table data_set_properties (child key dstpt_id) -> parent table data_set_type_property_types (parent key id), required.
relationship: child table data_set_properties (child key mate_prop_id) -> parent table materials (parent key id), optional (may be NULL or dangling).
relationship: child table data_set_properties (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table data_set_relationships (child key data_id_child) -> parent table data (parent key id), required.
relationship: child table data_set_relationships (child key data_id_parent) -> parent table data (parent key id), required.
relationship: child table data_set_type_property_types (child key dsty_id) -> parent table data_set_types (parent key id), required.
relationship: child table data_set_type_property_types (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table data_set_type_property_types (child key prty_id) -> parent table property_types (parent key id), required.
relationship: child table data_set_types (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table data_store_service_data_set_types (child key data_set_type_id) -> parent table data_set_types (parent key id), required.
relationship: child table data_store_service_data_set_types (child key data_store_service_id) -> parent table data_store_services (parent key id), required.
relationship: child table data_store_services (child key data_store_id) -> parent table data_stores (parent key id), required.
relationship: child table data_stores (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table events (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table experiment_properties (child key cvte_id) -> parent table controlled_vocabulary_terms (parent key id), optional (may be NULL or dangling).
relationship: child table experiment_properties (child key etpt_id) -> parent table experiment_type_property_types (parent key id), required.
relationship: child table experiment_properties (child key expe_id) -> parent table experiments (parent key id), required.
relationship: child table experiment_properties (child key mate_prop_id) -> parent table materials (parent key id), optional (may be NULL or dangling).
relationship: child table experiment_properties (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table experiment_type_property_types (child key exty_id) -> parent table experiment_types (parent key id), required.
relationship: child table experiment_type_property_types (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table experiment_type_property_types (child key prty_id) -> parent table property_types (parent key id), required.
relationship: child table experiment_types (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table experiments (child key exty_id) -> parent table experiment_types (parent key id), required.
relationship: child table experiments (child key inva_id) -> parent table invalidations (parent key id), optional (may be NULL or dangling).
relationship: child table experiments (child key mate_id_study_object) -> parent table materials (parent key id), optional (may be NULL or dangling).
relationship: child table experiments (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table experiments (child key proj_id) -> parent table projects (parent key id), required.
relationship: child table external_data (child key cvte_id_stor_fmt) -> parent table controlled_vocabulary_terms (parent key id), required.
relationship: child table external_data (child key cvte_id_store) -> parent table controlled_vocabulary_terms (parent key id), optional (may be NULL or dangling).
relationship: child table external_data (child key data_id) -> parent table data (parent key id), required.
relationship: child table external_data (child key ffty_id) -> parent table file_format_types (parent key id), required.
relationship: child table external_data (child key loty_id) -> parent table locator_types (parent key id), required.
relationship: child table file_format_types (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table filters (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table filters (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table grid_custom_columns (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table grid_custom_columns (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table groups (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table groups (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table invalidations (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table material_properties (child key cvte_id) -> parent table controlled_vocabulary_terms (parent key id), optional (may be NULL or dangling).
relationship: child table material_properties (child key mate_id) -> parent table materials (parent key id), required.
relationship: child table material_properties (child key mate_prop_id) -> parent table materials (parent key id), optional (may be NULL or dangling).
relationship: child table material_properties (child key mtpt_id) -> parent table material_type_property_types (parent key id), required.
relationship: child table material_properties (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table material_type_property_types (child key maty_id) -> parent table material_types (parent key id), required.
relationship: child table material_type_property_types (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table material_type_property_types (child key prty_id) -> parent table property_types (parent key id), required.
relationship: child table material_types (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table materials (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table materials (child key maty_id) -> parent table material_types (parent key id), required.
relationship: child table materials (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table persons (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table persons (child key grou_id) -> parent table groups (parent key id), optional (may be NULL or dangling).
relationship: child table projects (child key grou_id) -> parent table groups (parent key id), required.
relationship: child table projects (child key pers_id_leader) -> parent table persons (parent key id), optional (may be NULL or dangling).
relationship: child table projects (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table property_types (child key covo_id) -> parent table controlled_vocabularies (parent key id), optional (may be NULL or dangling).
relationship: child table property_types (child key daty_id) -> parent table data_types (parent key id), required.
relationship: child table property_types (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table property_types (child key maty_prop_id) -> parent table material_types (parent key id), optional (may be NULL or dangling).
relationship: child table property_types (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table role_assignments (child key ag_id_grantee) -> parent table authorization_groups (parent key id), optional (may be NULL or dangling).
relationship: child table role_assignments (child key dbin_id) -> parent table database_instances (parent key id), optional (may be NULL or dangling).
relationship: child table role_assignments (child key grou_id) -> parent table groups (parent key id), optional (may be NULL or dangling).
relationship: child table role_assignments (child key pers_id_grantee) -> parent table persons (parent key id), optional (may be NULL or dangling).
relationship: child table role_assignments (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table sample_properties (child key cvte_id) -> parent table controlled_vocabulary_terms (parent key id), optional (may be NULL or dangling).
relationship: child table sample_properties (child key mate_prop_id) -> parent table materials (parent key id), optional (may be NULL or dangling).
relationship: child table sample_properties (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table sample_properties (child key samp_id) -> parent table samples (parent key id), required.
relationship: child table sample_properties (child key stpt_id) -> parent table sample_type_property_types (parent key id), required.
relationship: child table sample_type_property_types (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table sample_type_property_types (child key prty_id) -> parent table property_types (parent key id), required.
relationship: child table sample_type_property_types (child key saty_id) -> parent table sample_types (parent key id), required.
relationship: child table sample_types (child key dbin_id) -> parent table database_instances (parent key id), required.
relationship: child table samples (child key dbin_id) -> parent table database_instances (parent key id), optional (may be NULL or dangling).
relationship: child table samples (child key expe_id) -> parent table experiments (parent key id), optional (may be NULL or dangling).
relationship: child table samples (child key grou_id) -> parent table groups (parent key id), optional (may be NULL or dangling).
relationship: child table samples (child key inva_id) -> parent table invalidations (parent key id), optional (may be NULL or dangling).
relationship: child table samples (child key pers_id_registerer) -> parent table persons (parent key id), required.
relationship: child table samples (child key saty_id) -> parent table sample_types (parent key id), required.

A note on text comparisons used throughout: wherever a rule speaks of the smallest text value, the comparison is a plain case-sensitive comparison of the stored text — the warehouse default order, in which every uppercase letter sorts before every lowercase one.

=== Mart samples_attachments_snapshot — per-samples latest-row snapshot over linked attachments activity in the 045413_schema-047.sql schema ===

Grain: one row per samples (id), INCLUDING samples rows with no linked attachments rows. The key column of this mart is parent_key.

Rule 1: the samples source table is read in full; every samples row is available to this mart.

Rule 2: the attachments source table is read in full; every attachments row is available to this mart.

Rule 3: from samples, there is one row per samples row, keyed by id, and that row carries parent_key and parent_name.

Rule 4: attachments rows are brought in so that an attachments row belongs to the samples row whose parent_key equals the attachments column samp_id, carrying samp_id and id from attachments; preservation is left-sided from the samples side, so a samples row with no matching attachments row is retained and receives the stated empty snapshot values.

Rule 5: there is one output row per parent_key, carrying parent_name beside the keys — a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group — and each such row reports event_count and lifetime_amount over that row's matching attachments rows.

Rule 6: for each parent_key, the single row at which the ordering measure registration_timestamp is largest survives, with ties broken by the smallest id under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), and latest_row_id, latest_amount and latest_label are taken from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows.

Rule 7: the extremal row's attributes are attached to the grouped measures by matching on parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

Rule 8: the mart columns are named parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label; lifetime_amount reports its declared default — never NULL — for a group with no matching rows.

Rule 9: the guarded ratio latest_amount_share is carried beside parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label, and is latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; it is 0.0 when lifetime_amount is 0, and 0.0 when the denominator is 0 or NULL.

Rule 10: the deterministic output order is ascending parent_key.

Output columns of samples_attachments_snapshot:

parent_key (text): identifier of the samples row; there is one row per value.

parent_name (text): code of the samples row, copied unchanged.

event_count (bigint): number of attachments rows for this samples row; 0 when there are none. A samples row kept with no attachments row reports 0 here, never 1: its placeholder holds no attachments row to count.

lifetime_amount (integer): the total of version over all matching attachments rows; 0 when there are no rows.

latest_row_id (text): id of the attachments row with the latest registration_timestamp; ties take the smallest id under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one). It is the literal '(none)' when there are no rows.

latest_amount (integer): version from that same latest row; 0 when there are no rows.

latest_label (text): file_name from that same latest row; '(none)' when there are no rows.

latest_amount_share (float): latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0.

=== Mart sample_types_sample_type_property_types_distribution — per-(sample_types, measure state) distribution of linked sample_type_property_types activity in the 045413_schema-047.sql schema ===

Grain: one row per (id, measure state) pair represented by linked sample_type_property_types rows, plus one absent no-activity row for a sample_types row with no links. Because ordinal is required, no linked sample_type_property_types row belongs to the absent state. The key columns of this mart are entity_key and measure_state.

Rule 1: the sample_types source table is read in full; every sample_types row is available to this mart.

Rule 2: the sample_type_property_types source table is read in full; every sample_type_property_types row is available to this mart.

Rule 3: from sample_types, each id and its code are carried into the measure-state calculation as entity_key and entity_name.

Rule 4: the linked sample_type_property_types rows are brought into each sample_types entity, a sample_type_property_types row belonging to the entity whose entity_key equals its saty_id, carrying entity_key, entity_name, id and saty_id; preservation is left-sided, so an entity with no linked row is retained and its absent state is visible.

Rule 5: the present measure-state rows, carrying entity_key and entity_name, are the rows that are a real sample_type_property_types row; ordinal is required on every such row.

Rule 6: in the present measure state there is one row per sample_types entity that has at least one row in that state, and no row here for an entity with none, and it reports entity_key, entity_name, row_count as the row count, distinct_amount_count as how many different ordinal values occur (each different value counted once, however many rows repeat it), total_amount as the total ordinal and max_amount as the largest ordinal.

Rule 7: beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8: these measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state as the present measure state, that is the value 'present'.

Rule 9: the absent measure-state rows, carrying entity_key and entity_name, are the retained placeholder for a sample_types row with no sample_type_property_types rows; no real row can enter this state because ordinal is required.

Rule 10: in the absent measure state there is one row per sample_types entity with no linked sample_type_property_types row at all, whose retained placeholder is its one row in that state, and no row here for an entity that has a linked sample_type_property_types row, and it reports entity_key, entity_name, a row_count of 0, 0 different ordinal values as distinct_amount_count, a total ordinal of 0 as total_amount and a largest ordinal of 0 as max_amount.

Rule 11: beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share for these absent-state rows is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12: these measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state as the absent measure state, that is the value 'absent'.

Rule 13: the present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share, keeping all rows: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14: the deterministic output order is ascending entity_key, then ascending measure_state.

Output columns of sample_types_sample_type_property_types_distribution:

entity_key (text): identifier of the sample_types row.

measure_state (text): 'present' for a linked sample_type_property_types row; 'absent' only for a sample_types row with no linked sample_type_property_types row. ordinal is required on every real sample_type_property_types row.

entity_name (text): code of the sample_types row, copied unchanged.

row_count (bigint): number of linked sample_type_property_types rows in this entity/state cell; 0 for a no_activity absent cell.

distinct_amount_count (bigint): number of unique ordinal values in this cell; each unique value is counted once, however many rows repeat it; 0 for a no_activity absent cell.

total_amount (integer): the total of ordinal in this cell; 0 for a no_activity absent cell.

max_amount (integer): largest ordinal in this cell; 0 for a no_activity absent cell.

max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

=== Mart property_types_sample_type_property_types_top — per-property_types extremes over linked sample_type_property_types rows in the 045413_schema-047.sql schema: WHICH row is largest, not how large it is ===

Grain: one row per property_types (id), INCLUDING property_types rows with no linked sample_type_property_types rows. The key column of this mart is parent_key.

Rule 1: the property_types source table is read in full; every property_types row is available to this mart.

Rule 2: the sample_type_property_types source table is read in full; every sample_type_property_types row is available to this mart.

Rule 3: from property_types, there is one row per property_types row, keyed by id, and that row carries parent_key and parent_name.

Rule 4: sample_type_property_types rows are brought in so that a sample_type_property_types row belongs to the property_types row whose parent_key equals its prty_id, carrying prty_id and id; preservation is left-sided, so a property_types row with no sample_type_property_types rows still appears, with the declared defaults.

Rule 5: within each parent_key the matched rows are ranked under an explicit total order — the measure ordinal first, then the declared tie-break — so that the extremal row is a function of the input and not of row order.

Rule 6: there is one output row per parent_key, carrying parent_name beside the keys — a key value identifies one source row for the carried columns, so they take one value per key and never split a group — reporting top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7: for each parent_key, the single row at which the ordering measure ordinal is largest survives, with ties broken by the smallest section under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no section value sorts after every row that has one — then by the smallest id under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), and top_label and top_row_id are taken from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows.

Rule 8: the extremal row's attributes are attached to the grouped measures by matching on parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

Rule 9: the mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults — never NULL — for a group with no matching rows.

Rule 10: the guarded ratio top_measure_share is carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id, and is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0, and 0.0 when the denominator is 0 or NULL.

Rule 11: tie_state, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, is 'empty' when no row holds a maximum at all — the parent has no sample_type_property_types rows — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; this is a categorical mapping with no numeric boundary. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, and otherwise, when tied_count is 2 or more, 'tied'; it is never null or blank.

Rule 12: the deterministic output order is ascending parent_key.

Output columns of property_types_sample_type_property_types_top:

parent_key (text): identifier of the property_types row; there is one row per value.

parent_name (text): code of the property_types row, copied unchanged.

top_measure (integer): the largest ordinal itself; 0 when the parent has no sample_type_property_types rows.

tied_count (bigint): how many sample_type_property_types rows are tied at that largest ordinal; 1 when exactly one row carries that largest ordinal; 0 when there are no rows.

child_count (bigint): number of sample_type_property_types rows for this property_types row; 0 when there are none. A property_types row kept with no sample_type_property_types row reports 0 here, never 1: its placeholder holds no sample_type_property_types row to count.

total_measure (integer): the total of ordinal over all of them; 0 when the parent has no sample_type_property_types rows.

top_label (text): the section of the sample_type_property_types row with the LARGEST ordinal for this property_types row. Ties in ordinal are broken by taking the SMALLEST section under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no section value sorts after every labelled row; rows tied on both are resolved by the smallest id under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one). It is the literal '(none)' when the parent has no sample_type_property_types rows at all, and '(none)' when the winning row has no section value.

top_row_id (text): the id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real sample_type_property_types row whenever the parent has any. It is the literal '(none)' when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.

top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.

tie_state (text): 'empty' when no row holds a maximum at all — the parent has no sample_type_property_types rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `samples_attachments_snapshot`

- Grain: One row per samples (id), INCLUDING samples rows with no linked attachments rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share

```text
Mart 'samples_attachments_snapshot' has 10 declared semantic rules:
1. [source] Read source table samples. (public source tables: samples)
2. [source] Read source table attachments. (public source tables: attachments)
3. [derive] One row per samples row, keyed by id. (public source tables: samples | public carried/output columns: parent_key, parent_name)
4. [join] Bring in attachments; a samples row with no matching attachments row is retained and receives the stated empty snapshot values. (public source tables: attachments | public carried/output columns: samp_id, id | join preservation: left | condition public identifiers: attachments, samp_id, parent_key)
5. [aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count, lifetime_amount for that row's matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount)
6. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest id under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), and take latest_row_id, latest_amount, latest_label from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, latest_row_id, latest_amount, latest_label)
7. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
8. [derive] Name the mart columns; lifetime_amount reports its declared default — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label)
9. [ratio] Guarded ratios: latest_amount_share — latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `sample_types_sample_type_property_types_distribution`

- Grain: One row per (id, measure state) pair represented by linked sample_type_property_types rows, plus one absent no-activity row for a sample_types row with no links. Because ordinal is required, no linked sample_type_property_types row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'sample_types_sample_type_property_types_distribution' has 14 declared semantic rules:
1. [source] Read source table sample_types. (public source tables: sample_types)
2. [source] Read source table sample_type_property_types. (public source tables: sample_type_property_types)
3. [derive] Carry each id and its code into the measure-state calculation. (public source tables: sample_types | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked sample_type_property_types rows into each sample_types entity; retain an entity with no linked row so its absent state is visible. (public source tables: sample_type_property_types | public carried/output columns: entity_key, entity_name, id, saty_id | join preservation: left | condition public identifiers: sample_type_property_types, saty_id, entity_key)
5. [filter] Keep the present measure-state rows: a real sample_type_property_types row; ordinal is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per sample_types entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different ordinal values occur (each different value counted once, however many rows repeat it), total ordinal, and largest ordinal. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a sample_types row with no sample_type_property_types rows; no real row can enter this state because ordinal is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per sample_types entity with no linked sample_type_property_types row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked sample_type_property_types row, reporting a row count of 0, 0 different ordinal values, a total ordinal of 0 and a largest ordinal of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `property_types_sample_type_property_types_top`

- Grain: One row per property_types (id), INCLUDING property_types rows with no linked sample_type_property_types rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'property_types_sample_type_property_types_top' has 12 declared semantic rules:
1. [source] Read source table property_types. (public source tables: property_types)
2. [source] Read source table sample_type_property_types. (public source tables: sample_type_property_types)
3. [derive] One row per property_types row, keyed by id. (public source tables: property_types | public carried/output columns: parent_key, parent_name)
4. [join] Bring in sample_type_property_types: a property_types row with no sample_type_property_types rows still appears, with the declared defaults. (public source tables: sample_type_property_types | public carried/output columns: prty_id, id | join preservation: left | condition public identifiers: sample_type_property_types, prty_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest section under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no section value sorts after every row that has one), then the smallest id under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), and take top_label, top_row_id from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no sample_type_property_types rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### attachments  (source backend: postgres)
Source table ATTACHMENTS.

- `exac_id`: text NOT NULL — Column EXAC_ID of table ATTACHMENTS.
- `expe_id`: text NULL — Column EXPE_ID of table ATTACHMENTS.
- `file_name`: text NOT NULL — Column FILE_NAME of table ATTACHMENTS.
- `id`: text NOT NULL — Column ID of table ATTACHMENTS.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table ATTACHMENTS.
- `proj_id`: text NULL — Column PROJ_ID of table ATTACHMENTS.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table ATTACHMENTS.
- `samp_id`: text NULL — Column SAMP_ID of table ATTACHMENTS.
- `version`: integer NOT NULL — Column VERSION of table ATTACHMENTS.
- `description`: text NULL — Column description of table ATTACHMENTS.
- `title`: text NULL — Column title of table ATTACHMENTS.
- primary key: id

### attachment_contents  (source backend: postgres)
Source table ATTACHMENT_CONTENTS.

- `id`: text NOT NULL — Column ID of table ATTACHMENT_CONTENTS.
- `value`: text NOT NULL — Column VALUE of table ATTACHMENT_CONTENTS.
- primary key: id

### authorization_groups  (source backend: mongodb)
Source table AUTHORIZATION_GROUPS.

- `code`: text NOT NULL — Column CODE of table AUTHORIZATION_GROUPS.
- `dbin_id`: text NOT NULL — Column DBIN_ID of table AUTHORIZATION_GROUPS.
- `description`: text NULL — Column DESCRIPTION of table AUTHORIZATION_GROUPS.
- `id`: text NOT NULL — Column ID of table AUTHORIZATION_GROUPS.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table AUTHORIZATION_GROUPS.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table AUTHORIZATION_GROUPS.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table AUTHORIZATION_GROUPS.
- primary key: id

### authorization_group_persons  (source backend: rest)
Source table AUTHORIZATION_GROUP_PERSONS.

- `ag_id`: text NOT NULL — Column AG_ID of table AUTHORIZATION_GROUP_PERSONS.
- `pers_id`: text NOT NULL — Column PERS_ID of table AUTHORIZATION_GROUP_PERSONS.
- primary key: ag_id, pers_id

### controlled_vocabularies  (source backend: rest)
Source table CONTROLLED_VOCABULARIES.

- `code`: text NOT NULL — Column CODE of table CONTROLLED_VOCABULARIES.
- `dbin_id`: text NOT NULL — Column DBIN_ID of table CONTROLLED_VOCABULARIES.
- `description`: text NULL — Column DESCRIPTION of table CONTROLLED_VOCABULARIES.
- `id`: text NOT NULL — Column ID of table CONTROLLED_VOCABULARIES.
- `is_chosen_from_list`: boolean NOT NULL — Column IS_CHOSEN_FROM_LIST of table CONTROLLED_VOCABULARIES.
- `is_internal_namespace`: boolean NOT NULL — Column IS_INTERNAL_NAMESPACE of table CONTROLLED_VOCABULARIES.
- `is_managed_internally`: boolean NOT NULL — Column IS_MANAGED_INTERNALLY of table CONTROLLED_VOCABULARIES.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table CONTROLLED_VOCABULARIES.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table CONTROLLED_VOCABULARIES.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table CONTROLLED_VOCABULARIES.
- `source_uri`: text NULL — Column SOURCE_URI of table CONTROLLED_VOCABULARIES.
- primary key: id

### controlled_vocabulary_terms  (source backend: rest)
Source table CONTROLLED_VOCABULARY_TERMS.

- `code`: text NOT NULL — Column CODE of table CONTROLLED_VOCABULARY_TERMS.
- `covo_id`: text NOT NULL — Column COVO_ID of table CONTROLLED_VOCABULARY_TERMS.
- `description`: text NULL — Column DESCRIPTION of table CONTROLLED_VOCABULARY_TERMS.
- `id`: text NOT NULL — Column ID of table CONTROLLED_VOCABULARY_TERMS.
- `label`: text NULL — Column LABEL of table CONTROLLED_VOCABULARY_TERMS.
- `ordinal`: integer NOT NULL — Column ORDINAL of table CONTROLLED_VOCABULARY_TERMS.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table CONTROLLED_VOCABULARY_TERMS.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table CONTROLLED_VOCABULARY_TERMS.
- primary key: id

### data  (source backend: mongodb)
Source table DATA.

- `code`: text NULL — Column CODE of table DATA.
- `dast_id`: text NOT NULL — Column DAST_ID of table DATA.
- `data_producer_code`: text NULL — Column DATA_PRODUCER_CODE of table DATA.
- `dsty_id`: text NOT NULL — Column DSTY_ID of table DATA.
- `expe_id`: text NOT NULL — Column EXPE_ID of table DATA.
- `id`: text NOT NULL — Column ID of table DATA.
- `is_derived`: boolean NOT NULL — Column IS_DERIVED of table DATA.
- `is_placeholder`: boolean NULL — Column IS_PLACEHOLDER of table DATA.
- `is_valid`: boolean NULL — Column IS_VALID of table DATA.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table DATA.
- `production_timestamp`: timestamp NULL — Column PRODUCTION_TIMESTAMP of table DATA.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table DATA.
- `samp_id`: text NULL — Column SAMP_ID of table DATA.
- primary key: id

### database_instances  (source backend: s3)
Source table DATABASE_INSTANCES.

- `code`: text NOT NULL — Column CODE of table DATABASE_INSTANCES.
- `id`: text NOT NULL — Column ID of table DATABASE_INSTANCES.
- `is_original_source`: boolean NOT NULL — Column IS_ORIGINAL_SOURCE of table DATABASE_INSTANCES.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table DATABASE_INSTANCES.
- `uuid`: text NOT NULL — Column UUID of table DATABASE_INSTANCES.
- primary key: id

### data_set_properties  (source backend: rest)
Source table DATA_SET_PROPERTIES.

- `cvte_id`: text NULL — Column CVTE_ID of table DATA_SET_PROPERTIES.
- `dstpt_id`: text NOT NULL — Column DSTPT_ID of table DATA_SET_PROPERTIES.
- `ds_id`: text NOT NULL — Column DS_ID of table DATA_SET_PROPERTIES.
- `id`: text NOT NULL — Column ID of table DATA_SET_PROPERTIES.
- `mate_prop_id`: text NULL — Column MATE_PROP_ID of table DATA_SET_PROPERTIES.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table DATA_SET_PROPERTIES.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table DATA_SET_PROPERTIES.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table DATA_SET_PROPERTIES.
- `value`: text NULL — Column VALUE of table DATA_SET_PROPERTIES.
- primary key: id

### data_set_relationships  (source backend: rest)
Source table DATA_SET_RELATIONSHIPS.

- `data_id_child`: text NOT NULL — Column DATA_ID_CHILD of table DATA_SET_RELATIONSHIPS.
- `data_id_parent`: text NOT NULL — Column DATA_ID_PARENT of table DATA_SET_RELATIONSHIPS.

### data_set_types  (source backend: rest)
Source table DATA_SET_TYPES.

- `code`: text NOT NULL — Column CODE of table DATA_SET_TYPES.
- `dbin_id`: text NOT NULL — Column DBIN_ID of table DATA_SET_TYPES.
- `description`: text NULL — Column DESCRIPTION of table DATA_SET_TYPES.
- `id`: text NOT NULL — Column ID of table DATA_SET_TYPES.
- `main_ds_path`: text NULL — Column MAIN_DS_PATH of table DATA_SET_TYPES.
- `main_ds_pattern`: text NULL — Column MAIN_DS_PATTERN of table DATA_SET_TYPES.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table DATA_SET_TYPES.
- primary key: id

### data_set_type_property_types  (source backend: mongodb)
Source table DATA_SET_TYPE_PROPERTY_TYPES.

- `dsty_id`: text NOT NULL — Column DSTY_ID of table DATA_SET_TYPE_PROPERTY_TYPES.
- `id`: text NOT NULL — Column ID of table DATA_SET_TYPE_PROPERTY_TYPES.
- `is_managed_internally`: boolean NOT NULL — Column IS_MANAGED_INTERNALLY of table DATA_SET_TYPE_PROPERTY_TYPES.
- `is_mandatory`: boolean NOT NULL — Column IS_MANDATORY of table DATA_SET_TYPE_PROPERTY_TYPES.
- `ordinal`: integer NOT NULL — Column ORDINAL of table DATA_SET_TYPE_PROPERTY_TYPES.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table DATA_SET_TYPE_PROPERTY_TYPES.
- `prty_id`: text NOT NULL — Column PRTY_ID of table DATA_SET_TYPE_PROPERTY_TYPES.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table DATA_SET_TYPE_PROPERTY_TYPES.
- `section`: text NULL — Column SECTION of table DATA_SET_TYPE_PROPERTY_TYPES.
- primary key: id

### data_stores  (source backend: rest)
Source table DATA_STORES.

- `code`: text NOT NULL — Column CODE of table DATA_STORES.
- `dbin_id`: text NOT NULL — Column DBIN_ID of table DATA_STORES.
- `download_url`: text NOT NULL — Column DOWNLOAD_URL of table DATA_STORES.
- `id`: text NOT NULL — Column ID of table DATA_STORES.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table DATA_STORES.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table DATA_STORES.
- `remote_url`: text NOT NULL — Column REMOTE_URL of table DATA_STORES.
- `session_token`: text NOT NULL — Column SESSION_TOKEN of table DATA_STORES.
- primary key: id

### data_store_services  (source backend: postgres)
Source table DATA_STORE_SERVICES.

- `data_store_id`: text NOT NULL — Column DATA_STORE_ID of table DATA_STORE_SERVICES.
- `id`: text NOT NULL — Column ID of table DATA_STORE_SERVICES.
- `kind`: text NOT NULL — Column KIND of table DATA_STORE_SERVICES.
- `label`: text NOT NULL — Column LABEL of table DATA_STORE_SERVICES.
- primary key: id

### data_store_service_data_set_types  (source backend: mongodb)
Source table DATA_STORE_SERVICE_DATA_SET_TYPES.

- `data_set_type_id`: text NOT NULL — Column DATA_SET_TYPE_ID of table DATA_STORE_SERVICE_DATA_SET_TYPES.
- `data_store_service_id`: text NOT NULL — Column DATA_STORE_SERVICE_ID of table DATA_STORE_SERVICE_DATA_SET_TYPES.

### data_types  (source backend: rest)
Source table DATA_TYPES.

- `code`: text NOT NULL — Column CODE of table DATA_TYPES.
- `description`: text NOT NULL — Column DESCRIPTION of table DATA_TYPES.
- `id`: text NOT NULL — Column ID of table DATA_TYPES.
- primary key: id

### events  (source backend: postgres)
Source table EVENTS.

- `description`: text NULL — Column DESCRIPTION of table EVENTS.
- `event_type`: text NOT NULL — Column EVENT_TYPE of table EVENTS.
- `id`: text NOT NULL — Column ID of table EVENTS.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table EVENTS.
- `reason`: text NULL — Column REASON of table EVENTS.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table EVENTS.
- `entity_type`: text NOT NULL — Column entity_type of table EVENTS. one of: ATTACHMENT, DATASET, EXPERIMENT, GROUP, MATERIAL, PROJECT, PROPERTY_TYPE, SAMPLE, VOCABULARY, AUTHORIZATION_GROUP.
- `identifier`: text NOT NULL — Column identifier of table EVENTS.
- primary key: id

### experiments  (source backend: files)
Source table EXPERIMENTS.

- `code`: text NOT NULL — Column CODE of table EXPERIMENTS.
- `exty_id`: text NOT NULL — Column EXTY_ID of table EXPERIMENTS.
- `id`: text NOT NULL — Column ID of table EXPERIMENTS.
- `inva_id`: text NULL — Column INVA_ID of table EXPERIMENTS.
- `is_public`: boolean NOT NULL — Column IS_PUBLIC of table EXPERIMENTS.
- `mate_id_study_object`: text NULL — Column MATE_ID_STUDY_OBJECT of table EXPERIMENTS.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table EXPERIMENTS.
- `perm_id`: text NOT NULL — Column PERM_ID of table EXPERIMENTS.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table EXPERIMENTS.
- `proj_id`: text NOT NULL — Column PROJ_ID of table EXPERIMENTS.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table EXPERIMENTS.
- primary key: id

### experiment_properties  (source backend: postgres)
Source table EXPERIMENT_PROPERTIES.

- `cvte_id`: text NULL — Column CVTE_ID of table EXPERIMENT_PROPERTIES.
- `etpt_id`: text NOT NULL — Column ETPT_ID of table EXPERIMENT_PROPERTIES.
- `expe_id`: text NOT NULL — Column EXPE_ID of table EXPERIMENT_PROPERTIES.
- `id`: text NOT NULL — Column ID of table EXPERIMENT_PROPERTIES.
- `mate_prop_id`: text NULL — Column MATE_PROP_ID of table EXPERIMENT_PROPERTIES.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table EXPERIMENT_PROPERTIES.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table EXPERIMENT_PROPERTIES.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table EXPERIMENT_PROPERTIES.
- `value`: text NULL — Column VALUE of table EXPERIMENT_PROPERTIES.
- primary key: id

### experiment_types  (source backend: files)
Source table EXPERIMENT_TYPES.

- `code`: text NOT NULL — Column CODE of table EXPERIMENT_TYPES.
- `dbin_id`: text NOT NULL — Column DBIN_ID of table EXPERIMENT_TYPES.
- `description`: text NULL — Column DESCRIPTION of table EXPERIMENT_TYPES.
- `id`: text NOT NULL — Column ID of table EXPERIMENT_TYPES.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table EXPERIMENT_TYPES.
- primary key: id

### experiment_type_property_types  (source backend: rest)
Source table EXPERIMENT_TYPE_PROPERTY_TYPES.

- `exty_id`: text NOT NULL — Column EXTY_ID of table EXPERIMENT_TYPE_PROPERTY_TYPES.
- `id`: text NOT NULL — Column ID of table EXPERIMENT_TYPE_PROPERTY_TYPES.
- `is_managed_internally`: boolean NOT NULL — Column IS_MANAGED_INTERNALLY of table EXPERIMENT_TYPE_PROPERTY_TYPES.
- `is_mandatory`: boolean NOT NULL — Column IS_MANDATORY of table EXPERIMENT_TYPE_PROPERTY_TYPES.
- `ordinal`: integer NOT NULL — Column ORDINAL of table EXPERIMENT_TYPE_PROPERTY_TYPES.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table EXPERIMENT_TYPE_PROPERTY_TYPES.
- `prty_id`: text NOT NULL — Column PRTY_ID of table EXPERIMENT_TYPE_PROPERTY_TYPES.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table EXPERIMENT_TYPE_PROPERTY_TYPES.
- `section`: text NULL — Column SECTION of table EXPERIMENT_TYPE_PROPERTY_TYPES.
- primary key: id

### external_data  (source backend: postgres)
Source table EXTERNAL_DATA.

- `cvte_id_store`: text NULL — Column CVTE_ID_STORE of table EXTERNAL_DATA.
- `cvte_id_stor_fmt`: text NOT NULL — Column CVTE_ID_STOR_FMT of table EXTERNAL_DATA.
- `data_id`: text NOT NULL — Column DATA_ID of table EXTERNAL_DATA.
- `ffty_id`: text NOT NULL — Column FFTY_ID of table EXTERNAL_DATA.
- `is_complete`: boolean NOT NULL — Column IS_COMPLETE of table EXTERNAL_DATA.
- `location`: text NOT NULL — Column LOCATION of table EXTERNAL_DATA.
- `loty_id`: text NOT NULL — Column LOTY_ID of table EXTERNAL_DATA.
- primary key: data_id

### file_format_types  (source backend: postgres)
Source table FILE_FORMAT_TYPES.

- `code`: text NOT NULL — Column CODE of table FILE_FORMAT_TYPES.
- `dbin_id`: text NOT NULL — Column DBIN_ID of table FILE_FORMAT_TYPES.
- `description`: text NULL — Column DESCRIPTION of table FILE_FORMAT_TYPES.
- `id`: text NOT NULL — Column ID of table FILE_FORMAT_TYPES.
- primary key: id

### filters  (source backend: rest)
Source table FILTERS.

- `dbin_id`: text NOT NULL — Column DBIN_ID of table FILTERS.
- `description`: text NULL — Column DESCRIPTION of table FILTERS.
- `expression`: text NOT NULL — Column EXPRESSION of table FILTERS.
- `grid_id`: text NOT NULL — Column GRID_ID of table FILTERS.
- `id`: text NOT NULL — Column ID of table FILTERS.
- `is_public`: boolean NOT NULL — Column IS_PUBLIC of table FILTERS.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table FILTERS.
- `name`: text NOT NULL — Column NAME of table FILTERS.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table FILTERS.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table FILTERS.
- primary key: id

### grid_custom_columns  (source backend: rest)
Source table GRID_CUSTOM_COLUMNS.

- `code`: text NOT NULL — Column CODE of table GRID_CUSTOM_COLUMNS.
- `dbin_id`: text NOT NULL — Column DBIN_ID of table GRID_CUSTOM_COLUMNS.
- `description`: text NULL — Column DESCRIPTION of table GRID_CUSTOM_COLUMNS.
- `expression`: text NOT NULL — Column EXPRESSION of table GRID_CUSTOM_COLUMNS.
- `grid_id`: text NOT NULL — Column GRID_ID of table GRID_CUSTOM_COLUMNS.
- `id`: text NOT NULL — Column ID of table GRID_CUSTOM_COLUMNS.
- `is_public`: boolean NOT NULL — Column IS_PUBLIC of table GRID_CUSTOM_COLUMNS.
- `label`: text NOT NULL — Column LABEL of table GRID_CUSTOM_COLUMNS.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table GRID_CUSTOM_COLUMNS.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table GRID_CUSTOM_COLUMNS.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table GRID_CUSTOM_COLUMNS.
- primary key: id

### groups  (source backend: mongodb)
Source table GROUPS.

- `code`: text NOT NULL — Column CODE of table GROUPS.
- `dbin_id`: text NOT NULL — Column DBIN_ID of table GROUPS.
- `description`: text NULL — Column DESCRIPTION of table GROUPS.
- `id`: text NOT NULL — Column ID of table GROUPS.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table GROUPS.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table GROUPS.
- primary key: id

### invalidations  (source backend: files)
Source table INVALIDATIONS.

- `id`: text NOT NULL — Column ID of table INVALIDATIONS.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table INVALIDATIONS.
- `reason`: text NULL — Column REASON of table INVALIDATIONS.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table INVALIDATIONS.
- primary key: id

### locator_types  (source backend: files)
Source table LOCATOR_TYPES.

- `code`: text NOT NULL — Column CODE of table LOCATOR_TYPES.
- `description`: text NULL — Column DESCRIPTION of table LOCATOR_TYPES.
- `id`: text NOT NULL — Column ID of table LOCATOR_TYPES.
- primary key: id

### materials  (source backend: postgres)
Source table MATERIALS.

- `code`: text NOT NULL — Column CODE of table MATERIALS.
- `dbin_id`: text NOT NULL — Column DBIN_ID of table MATERIALS.
- `id`: text NOT NULL — Column ID of table MATERIALS.
- `maty_id`: text NOT NULL — Column MATY_ID of table MATERIALS.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table MATERIALS.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table MATERIALS.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table MATERIALS.
- primary key: id

### material_properties  (source backend: files)
Source table MATERIAL_PROPERTIES.

- `cvte_id`: text NULL — Column CVTE_ID of table MATERIAL_PROPERTIES.
- `id`: text NOT NULL — Column ID of table MATERIAL_PROPERTIES.
- `mate_id`: text NOT NULL — Column MATE_ID of table MATERIAL_PROPERTIES.
- `mate_prop_id`: text NULL — Column MATE_PROP_ID of table MATERIAL_PROPERTIES.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table MATERIAL_PROPERTIES.
- `mtpt_id`: text NOT NULL — Column MTPT_ID of table MATERIAL_PROPERTIES.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table MATERIAL_PROPERTIES.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table MATERIAL_PROPERTIES.
- `value`: text NULL — Column VALUE of table MATERIAL_PROPERTIES.
- primary key: id

### material_types  (source backend: rest)
Source table MATERIAL_TYPES.

- `code`: text NOT NULL — Column CODE of table MATERIAL_TYPES.
- `dbin_id`: text NOT NULL — Column DBIN_ID of table MATERIAL_TYPES.
- `description`: text NULL — Column DESCRIPTION of table MATERIAL_TYPES.
- `id`: text NOT NULL — Column ID of table MATERIAL_TYPES.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table MATERIAL_TYPES.
- primary key: id

### material_type_property_types  (source backend: s3)
Source table MATERIAL_TYPE_PROPERTY_TYPES.

- `id`: text NOT NULL — Column ID of table MATERIAL_TYPE_PROPERTY_TYPES.
- `is_managed_internally`: boolean NOT NULL — Column IS_MANAGED_INTERNALLY of table MATERIAL_TYPE_PROPERTY_TYPES.
- `is_mandatory`: boolean NOT NULL — Column IS_MANDATORY of table MATERIAL_TYPE_PROPERTY_TYPES.
- `maty_id`: text NOT NULL — Column MATY_ID of table MATERIAL_TYPE_PROPERTY_TYPES.
- `ordinal`: integer NOT NULL — Column ORDINAL of table MATERIAL_TYPE_PROPERTY_TYPES.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table MATERIAL_TYPE_PROPERTY_TYPES.
- `prty_id`: text NOT NULL — Column PRTY_ID of table MATERIAL_TYPE_PROPERTY_TYPES.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table MATERIAL_TYPE_PROPERTY_TYPES.
- `section`: text NULL — Column SECTION of table MATERIAL_TYPE_PROPERTY_TYPES.
- primary key: id

### persons  (source backend: mongodb)
Source table PERSONS.

- `dbin_id`: text NOT NULL — Column DBIN_ID of table PERSONS.
- `display_settings`: text NULL — Column DISPLAY_SETTINGS of table PERSONS.
- `email`: text NULL — Column EMAIL of table PERSONS.
- `first_name`: text NULL — Column FIRST_NAME of table PERSONS.
- `grou_id`: text NULL — Column GROU_ID of table PERSONS.
- `id`: text NOT NULL — Column ID of table PERSONS.
- `last_name`: text NULL — Column LAST_NAME of table PERSONS.
- `pers_id_registerer`: text NULL — Column PERS_ID_REGISTERER of table PERSONS.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table PERSONS.
- `user_id`: text NOT NULL — Column USER_ID of table PERSONS.
- primary key: id

### projects  (source backend: s3)
Source table PROJECTS.

- `code`: text NOT NULL — Column CODE of table PROJECTS.
- `description`: text NULL — Column DESCRIPTION of table PROJECTS.
- `grou_id`: text NOT NULL — Column GROU_ID of table PROJECTS.
- `id`: text NOT NULL — Column ID of table PROJECTS.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table PROJECTS.
- `pers_id_leader`: text NULL — Column PERS_ID_LEADER of table PROJECTS.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table PROJECTS.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table PROJECTS.
- primary key: id

### property_types  (source backend: mongodb)
Source table PROPERTY_TYPES.

- `code`: text NOT NULL — Column CODE of table PROPERTY_TYPES.
- `covo_id`: text NULL — Column COVO_ID of table PROPERTY_TYPES.
- `daty_id`: text NOT NULL — Column DATY_ID of table PROPERTY_TYPES.
- `dbin_id`: text NOT NULL — Column DBIN_ID of table PROPERTY_TYPES.
- `description`: text NOT NULL — Column DESCRIPTION of table PROPERTY_TYPES.
- `id`: text NOT NULL — Column ID of table PROPERTY_TYPES.
- `is_internal_namespace`: boolean NOT NULL — Column IS_INTERNAL_NAMESPACE of table PROPERTY_TYPES.
- `is_managed_internally`: boolean NOT NULL — Column IS_MANAGED_INTERNALLY of table PROPERTY_TYPES.
- `label`: text NOT NULL — Column LABEL of table PROPERTY_TYPES.
- `maty_prop_id`: text NULL — Column MATY_PROP_ID of table PROPERTY_TYPES.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table PROPERTY_TYPES.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table PROPERTY_TYPES.
- primary key: id

### role_assignments  (source backend: s3)
Source table ROLE_ASSIGNMENTS.

- `ag_id_grantee`: text NULL — Column AG_ID_GRANTEE of table ROLE_ASSIGNMENTS.
- `dbin_id`: text NULL — Column DBIN_ID of table ROLE_ASSIGNMENTS.
- `grou_id`: text NULL — Column GROU_ID of table ROLE_ASSIGNMENTS.
- `id`: text NOT NULL — Column ID of table ROLE_ASSIGNMENTS.
- `pers_id_grantee`: text NULL — Column PERS_ID_GRANTEE of table ROLE_ASSIGNMENTS.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table ROLE_ASSIGNMENTS.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table ROLE_ASSIGNMENTS.
- `role_code`: text NOT NULL — Column ROLE_CODE of table ROLE_ASSIGNMENTS.
- primary key: id

### samples  (source backend: mongodb)
Source table SAMPLES.

- `code`: text NOT NULL — Column CODE of table SAMPLES.
- `dbin_id`: text NULL — Column DBIN_ID of table SAMPLES.
- `expe_id`: text NULL — Column EXPE_ID of table SAMPLES.
- `grou_id`: text NULL — Column GROU_ID of table SAMPLES.
- `id`: text NOT NULL — Column ID of table SAMPLES.
- `inva_id`: text NULL — Column INVA_ID of table SAMPLES.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table SAMPLES.
- `perm_id`: text NOT NULL — Column PERM_ID of table SAMPLES.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table SAMPLES.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table SAMPLES.
- `samp_id_control_layout`: text NULL — Column SAMP_ID_CONTROL_LAYOUT of table SAMPLES.
- `samp_id_generated_from`: text NULL — Column SAMP_ID_GENERATED_FROM of table SAMPLES.
- `samp_id_part_of`: text NULL — Column SAMP_ID_PART_OF of table SAMPLES.
- `samp_id_top`: text NULL — Column SAMP_ID_TOP of table SAMPLES.
- `saty_id`: text NOT NULL — Column SATY_ID of table SAMPLES.
- primary key: id

### sample_properties  (source backend: files)
Source table SAMPLE_PROPERTIES.

- `cvte_id`: text NULL — Column CVTE_ID of table SAMPLE_PROPERTIES.
- `id`: text NOT NULL — Column ID of table SAMPLE_PROPERTIES.
- `mate_prop_id`: text NULL — Column MATE_PROP_ID of table SAMPLE_PROPERTIES.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table SAMPLE_PROPERTIES.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table SAMPLE_PROPERTIES.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table SAMPLE_PROPERTIES.
- `samp_id`: text NOT NULL — Column SAMP_ID of table SAMPLE_PROPERTIES.
- `stpt_id`: text NOT NULL — Column STPT_ID of table SAMPLE_PROPERTIES.
- `value`: text NULL — Column VALUE of table SAMPLE_PROPERTIES.
- primary key: id

### sample_types  (source backend: mongodb)
Source table SAMPLE_TYPES.

- `code`: text NOT NULL — Column CODE of table SAMPLE_TYPES.
- `dbin_id`: text NOT NULL — Column DBIN_ID of table SAMPLE_TYPES.
- `description`: text NULL — Column DESCRIPTION of table SAMPLE_TYPES.
- `generated_from_depth`: integer NOT NULL — Column GENERATED_FROM_DEPTH of table SAMPLE_TYPES.
- `id`: text NOT NULL — Column ID of table SAMPLE_TYPES.
- `is_listable`: boolean NOT NULL — Column IS_LISTABLE of table SAMPLE_TYPES.
- `modification_timestamp`: timestamp NULL — Column MODIFICATION_TIMESTAMP of table SAMPLE_TYPES.
- `part_of_depth`: integer NOT NULL — Column PART_OF_DEPTH of table SAMPLE_TYPES.
- `generated_code_prefix`: text NOT NULL — Column generated_code_prefix of table SAMPLE_TYPES.
- `is_auto_generated_code`: boolean NOT NULL — Column is_auto_generated_code of table SAMPLE_TYPES.
- primary key: id

### sample_type_property_types  (source backend: rest)
Source table SAMPLE_TYPE_PROPERTY_TYPES.

- `id`: text NOT NULL — Column ID of table SAMPLE_TYPE_PROPERTY_TYPES.
- `is_displayed`: boolean NOT NULL — Column IS_DISPLAYED of table SAMPLE_TYPE_PROPERTY_TYPES.
- `is_managed_internally`: boolean NOT NULL — Column IS_MANAGED_INTERNALLY of table SAMPLE_TYPE_PROPERTY_TYPES.
- `is_mandatory`: boolean NOT NULL — Column IS_MANDATORY of table SAMPLE_TYPE_PROPERTY_TYPES.
- `ordinal`: integer NOT NULL — Column ORDINAL of table SAMPLE_TYPE_PROPERTY_TYPES.
- `pers_id_registerer`: text NOT NULL — Column PERS_ID_REGISTERER of table SAMPLE_TYPE_PROPERTY_TYPES.
- `prty_id`: text NOT NULL — Column PRTY_ID of table SAMPLE_TYPE_PROPERTY_TYPES.
- `registration_timestamp`: timestamp NOT NULL — Column REGISTRATION_TIMESTAMP of table SAMPLE_TYPE_PROPERTY_TYPES.
- `saty_id`: text NOT NULL — Column SATY_ID of table SAMPLE_TYPE_PROPERTY_TYPES.
- `section`: text NULL — Column SECTION of table SAMPLE_TYPE_PROPERTY_TYPES.
- primary key: id

### Relationships

- attachments(exac_id) -> attachment_contents(id) [required]
- attachments(expe_id) -> experiments(id) [optional (may be NULL/dangling)]
- attachments(pers_id_registerer) -> persons(id) [required]
- attachments(proj_id) -> projects(id) [optional (may be NULL/dangling)]
- attachments(samp_id) -> samples(id) [optional (may be NULL/dangling)]
- authorization_group_persons(ag_id) -> authorization_groups(id) [required]
- authorization_group_persons(pers_id) -> persons(id) [required]
- authorization_groups(dbin_id) -> database_instances(id) [required]
- authorization_groups(pers_id_registerer) -> persons(id) [required]
- controlled_vocabularies(dbin_id) -> database_instances(id) [required]
- controlled_vocabularies(pers_id_registerer) -> persons(id) [required]
- controlled_vocabulary_terms(covo_id) -> controlled_vocabularies(id) [required]
- controlled_vocabulary_terms(pers_id_registerer) -> persons(id) [required]
- data(dast_id) -> data_stores(id) [required]
- data(dsty_id) -> data_set_types(id) [required]
- data(expe_id) -> experiments(id) [required]
- data(samp_id) -> samples(id) [optional (may be NULL/dangling)]
- data_set_properties(cvte_id) -> controlled_vocabulary_terms(id) [optional (may be NULL/dangling)]
- data_set_properties(ds_id) -> data(id) [required]
- data_set_properties(dstpt_id) -> data_set_type_property_types(id) [required]
- data_set_properties(mate_prop_id) -> materials(id) [optional (may be NULL/dangling)]
- data_set_properties(pers_id_registerer) -> persons(id) [required]
- data_set_relationships(data_id_child) -> data(id) [required]
- data_set_relationships(data_id_parent) -> data(id) [required]
- data_set_type_property_types(dsty_id) -> data_set_types(id) [required]
- data_set_type_property_types(pers_id_registerer) -> persons(id) [required]
- data_set_type_property_types(prty_id) -> property_types(id) [required]
- data_set_types(dbin_id) -> database_instances(id) [required]
- data_store_service_data_set_types(data_set_type_id) -> data_set_types(id) [required]
- data_store_service_data_set_types(data_store_service_id) -> data_store_services(id) [required]
- data_store_services(data_store_id) -> data_stores(id) [required]
- data_stores(dbin_id) -> database_instances(id) [required]
- events(pers_id_registerer) -> persons(id) [required]
- experiment_properties(cvte_id) -> controlled_vocabulary_terms(id) [optional (may be NULL/dangling)]
- experiment_properties(etpt_id) -> experiment_type_property_types(id) [required]
- experiment_properties(expe_id) -> experiments(id) [required]
- experiment_properties(mate_prop_id) -> materials(id) [optional (may be NULL/dangling)]
- experiment_properties(pers_id_registerer) -> persons(id) [required]
- experiment_type_property_types(exty_id) -> experiment_types(id) [required]
- experiment_type_property_types(pers_id_registerer) -> persons(id) [required]
- experiment_type_property_types(prty_id) -> property_types(id) [required]
- experiment_types(dbin_id) -> database_instances(id) [required]
- experiments(exty_id) -> experiment_types(id) [required]
- experiments(inva_id) -> invalidations(id) [optional (may be NULL/dangling)]
- experiments(mate_id_study_object) -> materials(id) [optional (may be NULL/dangling)]
- experiments(pers_id_registerer) -> persons(id) [required]
- experiments(proj_id) -> projects(id) [required]
- external_data(cvte_id_stor_fmt) -> controlled_vocabulary_terms(id) [required]
- external_data(cvte_id_store) -> controlled_vocabulary_terms(id) [optional (may be NULL/dangling)]
- external_data(data_id) -> data(id) [required]
- external_data(ffty_id) -> file_format_types(id) [required]
- external_data(loty_id) -> locator_types(id) [required]
- file_format_types(dbin_id) -> database_instances(id) [required]
- filters(dbin_id) -> database_instances(id) [required]
- filters(pers_id_registerer) -> persons(id) [required]
- grid_custom_columns(dbin_id) -> database_instances(id) [required]
- grid_custom_columns(pers_id_registerer) -> persons(id) [required]
- groups(dbin_id) -> database_instances(id) [required]
- groups(pers_id_registerer) -> persons(id) [required]
- invalidations(pers_id_registerer) -> persons(id) [required]
- material_properties(cvte_id) -> controlled_vocabulary_terms(id) [optional (may be NULL/dangling)]
- material_properties(mate_id) -> materials(id) [required]
- material_properties(mate_prop_id) -> materials(id) [optional (may be NULL/dangling)]
- material_properties(mtpt_id) -> material_type_property_types(id) [required]
- material_properties(pers_id_registerer) -> persons(id) [required]
- material_type_property_types(maty_id) -> material_types(id) [required]
- material_type_property_types(pers_id_registerer) -> persons(id) [required]
- material_type_property_types(prty_id) -> property_types(id) [required]
- material_types(dbin_id) -> database_instances(id) [required]
- materials(dbin_id) -> database_instances(id) [required]
- materials(maty_id) -> material_types(id) [required]
- materials(pers_id_registerer) -> persons(id) [required]
- persons(dbin_id) -> database_instances(id) [required]
- persons(grou_id) -> groups(id) [optional (may be NULL/dangling)]
- projects(grou_id) -> groups(id) [required]
- projects(pers_id_leader) -> persons(id) [optional (may be NULL/dangling)]
- projects(pers_id_registerer) -> persons(id) [required]
- property_types(covo_id) -> controlled_vocabularies(id) [optional (may be NULL/dangling)]
- property_types(daty_id) -> data_types(id) [required]
- property_types(dbin_id) -> database_instances(id) [required]
- property_types(maty_prop_id) -> material_types(id) [optional (may be NULL/dangling)]
- property_types(pers_id_registerer) -> persons(id) [required]
- role_assignments(ag_id_grantee) -> authorization_groups(id) [optional (may be NULL/dangling)]
- role_assignments(dbin_id) -> database_instances(id) [optional (may be NULL/dangling)]
- role_assignments(grou_id) -> groups(id) [optional (may be NULL/dangling)]
- role_assignments(pers_id_grantee) -> persons(id) [optional (may be NULL/dangling)]
- role_assignments(pers_id_registerer) -> persons(id) [required]
- sample_properties(cvte_id) -> controlled_vocabulary_terms(id) [optional (may be NULL/dangling)]
- sample_properties(mate_prop_id) -> materials(id) [optional (may be NULL/dangling)]
- sample_properties(pers_id_registerer) -> persons(id) [required]
- sample_properties(samp_id) -> samples(id) [required]
- sample_properties(stpt_id) -> sample_type_property_types(id) [required]
- sample_type_property_types(pers_id_registerer) -> persons(id) [required]
- sample_type_property_types(prty_id) -> property_types(id) [required]
- sample_type_property_types(saty_id) -> sample_types(id) [required]
- sample_types(dbin_id) -> database_instances(id) [required]
- samples(dbin_id) -> database_instances(id) [optional (may be NULL/dangling)]
- samples(expe_id) -> experiments(id) [optional (may be NULL/dangling)]
- samples(grou_id) -> groups(id) [optional (may be NULL/dangling)]
- samples(inva_id) -> invalidations(id) [optional (may be NULL/dangling)]
- samples(pers_id_registerer) -> persons(id) [required]
- samples(saty_id) -> sample_types(id) [required]

