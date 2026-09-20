# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Common Coolteam Ranger

## Specification

PROJECT OVERVIEW

This project builds three analytical marts over a Ranger-style access-governance schema (the 167589_xa_core_db_postgres.sql schema). Every source table below must be extracted from the backend named beside it, and no other backend may be used for it.

Source tables and their extraction backends:
- x_access_type_def is extracted from the postgres backend.
- x_access_type_def_grants is extracted from the postgres backend.
- x_asset is extracted from the files backend.
- x_audit_map is extracted from the postgres backend.
- x_auth_sess is extracted from the rest backend.
- x_context_enricher_def is extracted from the s3 backend.
- x_cred_store is extracted from the postgres backend.
- x_data_hist is extracted from the files backend.
- x_db_base is extracted from the mongodb backend.
- x_enum_def is extracted from the s3 backend.
- x_enum_element_def is extracted from the rest backend.
- x_group is extracted from the files backend.
- x_group_groups is extracted from the postgres backend.
- x_group_module_perm is extracted from the mongodb backend.
- x_group_users is extracted from the postgres backend.
- x_modules_master is extracted from the postgres backend.
- x_perm_map is extracted from the s3 backend.
- x_policy is extracted from the mongodb backend.
- x_policy_condition_def is extracted from the rest backend.
- x_policy_export_audit is extracted from the files backend.
- x_policy_item is extracted from the files backend.
- x_policy_item_access is extracted from the postgres backend.
- x_policy_item_condition is extracted from the mongodb backend.
- x_policy_item_group_perm is extracted from the mongodb backend.
- x_policy_item_user_perm is extracted from the s3 backend.
- x_policy_resource is extracted from the files backend.
- x_policy_resource_map is extracted from the files backend.
- x_portal_user is extracted from the files backend.
- x_portal_user_role is extracted from the files backend.
- x_resource is extracted from the mongodb backend.
- x_resource_def is extracted from the rest backend.
- x_service is extracted from the s3 backend.
- x_service_config_def is extracted from the mongodb backend.
- x_service_config_map is extracted from the files backend.
- x_service_def is extracted from the postgres backend.
- x_trx_log is extracted from the mongodb backend.
- x_user is extracted from the files backend.
- x_user_module_perm is extracted from the rest backend.
- xa_access_audit is extracted from the s3 backend.

RELATIONSHIPS

Each statement below names the child table with its key column, the parent table with its key column, and whether the relationship is required or optional exactly as the source schema declares it.

- Child table x_access_type_def with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_access_type_def with key def_id references parent table x_service_def with key id; required.
- Child table x_access_type_def with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_access_type_def_grants with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_access_type_def_grants with key atd_id references parent table x_access_type_def with key id; required.
- Child table x_access_type_def_grants with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_asset with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_asset with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_audit_map with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_audit_map with key group_id references parent table x_group with key id; optional (may be NULL or dangling).
- Child table x_audit_map with key res_id references parent table x_resource with key id; optional (may be NULL or dangling).
- Child table x_audit_map with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_audit_map with key user_id references parent table x_user with key id; optional (may be NULL or dangling).
- Child table x_auth_sess with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_auth_sess with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_auth_sess with key user_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_context_enricher_def with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_context_enricher_def with key def_id references parent table x_service_def with key id; required.
- Child table x_context_enricher_def with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_cred_store with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_cred_store with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_db_base with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_db_base with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_enum_def with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_enum_def with key def_id references parent table x_service_def with key id; required.
- Child table x_enum_def with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_enum_element_def with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_enum_element_def with key enum_def_id references parent table x_enum_def with key id; required.
- Child table x_enum_element_def with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_group with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_group with key cred_store_id references parent table x_cred_store with key id; optional (may be NULL or dangling).
- Child table x_group with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_group_groups with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_group_groups with key group_id references parent table x_group with key id; optional (may be NULL or dangling).
- Child table x_group_groups with key p_group_id references parent table x_group with key id; optional (may be NULL or dangling).
- Child table x_group_groups with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_group_module_perm with key group_id references parent table x_group with key id; optional (may be NULL or dangling).
- Child table x_group_module_perm with key module_id references parent table x_modules_master with key id; optional (may be NULL or dangling).
- Child table x_group_users with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_group_users with key p_group_id references parent table x_group with key id; optional (may be NULL or dangling).
- Child table x_group_users with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_group_users with key user_id references parent table x_user with key id; optional (may be NULL or dangling).
- Child table x_perm_map with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_perm_map with key group_id references parent table x_group with key id; optional (may be NULL or dangling).
- Child table x_perm_map with key res_id references parent table x_resource with key id; optional (may be NULL or dangling).
- Child table x_perm_map with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_perm_map with key user_id references parent table x_user with key id; optional (may be NULL or dangling).
- Child table x_policy with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy with key service references parent table x_service with key id; optional (may be NULL or dangling).
- Child table x_policy with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_condition_def with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_condition_def with key def_id references parent table x_service_def with key id; required.
- Child table x_policy_condition_def with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_export_audit with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_export_audit with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_item with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_item with key policy_id references parent table x_policy with key id; required.
- Child table x_policy_item with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_item_access with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_item_access with key policy_item_id references parent table x_policy_item with key id; required.
- Child table x_policy_item_access with key type references parent table x_access_type_def with key id; required.
- Child table x_policy_item_access with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_item_condition with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_item_condition with key policy_item_id references parent table x_policy_item with key id; required.
- Child table x_policy_item_condition with key type references parent table x_policy_condition_def with key id; required.
- Child table x_policy_item_condition with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_item_group_perm with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_item_group_perm with key group_id references parent table x_group with key id; optional (may be NULL or dangling).
- Child table x_policy_item_group_perm with key policy_item_id references parent table x_policy_item with key id; required.
- Child table x_policy_item_group_perm with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_item_user_perm with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_item_user_perm with key policy_item_id references parent table x_policy_item with key id; required.
- Child table x_policy_item_user_perm with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_item_user_perm with key user_id references parent table x_user with key id; optional (may be NULL or dangling).
- Child table x_policy_resource with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_resource with key policy_id references parent table x_policy with key id; required.
- Child table x_policy_resource with key res_def_id references parent table x_resource_def with key id; required.
- Child table x_policy_resource with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_resource_map with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_policy_resource_map with key resource_id references parent table x_policy_resource with key id; required.
- Child table x_policy_resource_map with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_portal_user_role with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_portal_user_role with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_portal_user_role with key user_id references parent table x_portal_user with key id; required.
- Child table x_resource with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_resource with key asset_id references parent table x_asset with key id; required.
- Child table x_resource with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_resource_def with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_resource_def with key def_id references parent table x_service_def with key id; required.
- Child table x_resource_def with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_service with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_service with key type references parent table x_service_def with key id; optional (may be NULL or dangling).
- Child table x_service with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_service_config_def with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_service_config_def with key def_id references parent table x_service_def with key id; required.
- Child table x_service_config_def with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_service_config_map with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_service_config_map with key service references parent table x_service with key id; required.
- Child table x_service_config_map with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_service_def with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_service_def with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_trx_log with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_trx_log with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_user with key added_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_user with key cred_store_id references parent table x_cred_store with key id; optional (may be NULL or dangling).
- Child table x_user with key upd_by_id references parent table x_portal_user with key id; optional (may be NULL or dangling).
- Child table x_user_module_perm with key module_id references parent table x_modules_master with key id; optional (may be NULL or dangling).
- Child table x_user_module_perm with key user_id references parent table x_portal_user with key id; optional (may be NULL or dangling).

General conventions: all rounding of fractional values is to 4 decimal places; text defaults are written exactly as the literal shown, including the parentheses of '(none)'.

=== Mart x_user_x_policy_item_user_perm_snapshot: Per-x_user latest-row snapshot over linked x_policy_item_user_perm activity in the 167589_xa_core_db_postgres.sql schema ===

Grain: one row per x_user (id), INCLUDING x_user rows with no linked x_policy_item_user_perm rows.

Key column: parent_key is the single key column of this mart.

Output columns.
- parent_key: the identifier of the x_user row; one row per value.
- parent_name: the descr of the x_user row, copied unchanged.
- event_count: the number of x_policy_item_user_perm rows for this x_user row; 0 when there are none. Every linked x_policy_item_user_perm row counts, whether or not it carries a sort_order value. A x_user row kept with no x_policy_item_user_perm row reports 0 here, never 1: its placeholder holds no x_policy_item_user_perm row to count.
- lifetime_amount: the total of sort_order over all matching x_policy_item_user_perm rows; 0 when there are no rows and when none of those rows carries an sort_order value; a row with no sort_order value adds nothing, so a group with some values totals the values it has.
- latest_row_id: the id of the row with the latest create_time; ties take the smallest id. It is 0 when there are no rows. Every x_policy_item_user_perm row of the x_user row ranks, whether or not it carries a sort_order value: the latest create_time wins even when that row's sort_order is missing.
- latest_amount: the sort_order from that same latest row; 0 when there are no rows or when the winning value is missing.
- latest_label: the guid from that same latest row; '(none)' when there are no rows or when the winning value is missing.
- latest_amount_share: latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose sort_order is missing, so such a row gives 0.0.

Rules.
1. Source table x_user is read in full from its files backend and supplies this mart's parent rows.
2. Source table x_policy_item_user_perm is read in full from its s3 backend and supplies this mart's activity rows.
3. There is one row per x_user row, keyed by id, and that row carries parent_key and parent_name.
4. The x_policy_item_user_perm rows are brought in by matching their user_id to parent_key, carrying user_id and id from x_policy_item_user_perm; preservation is left-sided towards x_user, so a x_user row with no matching x_policy_item_user_perm row is retained and receives the stated empty snapshot values.
5. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group, and each such row reports event_count and lifetime_amount for that row's matching rows.
6. For each parent_key the single row at which the ordering measure — create_time — is largest survives, ties broken by the smallest id, and latest_row_id, latest_amount and latest_label are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.
7. The extremal row's attributes are attributed to the grouped measures by matching parent_key; preservation is left-sided on the measures, so a group with no rows at all keeps its measures.
8. The mart columns are named parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label; lifetime_amount reports its declared default of 0 — never NULL — for a group with no matching rows, and for lifetime_amount that default also applies to a group none of whose real rows carries an input value.
9. Guarded ratio, carried beside parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label: latest_amount_share is latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places, and is 0.0 when the denominator lifetime_amount is 0 or has no value. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose sort_order is missing, so such a row gives 0.0.
10. Deterministic output order: rows appear sorted by parent_key ascending.

=== Mart x_service_def_x_service_config_def_distribution: Per-(x_service_def, measure state) distribution of linked x_service_config_def activity in the 167589_xa_core_db_postgres.sql schema ===

Grain: one row per (id, measure state) pair represented by linked x_service_config_def rows, plus one absent no-activity row for a x_service_def row with no links. Because item_id is required, no linked x_service_config_def row belongs to the absent state.

Key columns: entity_key and measure_state together are the key columns of this mart.

Output columns.
- entity_key: the identifier of the x_service_def row.
- measure_state: 'present' for a linked x_service_config_def row; 'absent' only for a x_service_def row with no linked x_service_config_def row. item_id is required on every real x_service_config_def row.
- entity_name: the description of the x_service_def row, copied unchanged.
- row_count: the number of linked x_service_config_def rows in this entity/state cell; 0 for a no-activity absent cell.
- distinct_amount_count: the number of unique item_id values in this cell; each unique value is counted once, however many rows repeat it; 0 for a no-activity absent cell.
- total_amount: the total of item_id in this cell; 0 for a no-activity absent cell.
- max_amount: the largest item_id in this cell; 0 for a no-activity absent cell.
- max_amount_share: max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules.
1. Source table x_service_def is read in full from its postgres backend and supplies this mart's entities.
2. Source table x_service_config_def is read in full from its mongodb backend and supplies this mart's linked activity rows.
3. Each id of x_service_def and its description are carried into the measure-state calculation as entity_key and entity_name.
4. The linked x_service_config_def rows are brought into each x_service_def entity by matching x_service_config_def def_id to entity_key, carrying entity_key, entity_name, id and def_id; preservation is left-sided towards x_service_def, so an entity with no linked row is retained and its absent state is visible.
5. The present measure-state rows are those kept because they are a real x_service_config_def row, carrying entity_key and entity_name; item_id is required on every such row.
6. In the present measure state there is one row per x_service_def entity that has at least one row there, and no row here for an entity with none, and that row reports entity_key, entity_name, row_count as the row count, distinct_amount_count as how many different item_id values occur (each different value counted once, however many rows repeat it), total_amount as the total item_id and max_amount as the largest item_id.
7. Alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.
8. These measures are labelled as the present measure state, so measure_state reads 'present' on a row carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.
9. The absent measure-state rows are the retained placeholder for a x_service_def row with no x_service_config_def rows, carrying entity_key and entity_name; no real row can enter this state because item_id is required.
10. In the absent measure state there is one row per x_service_def entity with no linked x_service_config_def row at all, whose retained placeholder is its one row in that state, and no row here for an entity that has a linked x_service_config_def row; that row reports entity_key, entity_name, a row_count of 0, distinct_amount_count of 0 different item_id values, a total_amount item_id of 0 and a max_amount largest item_id of 0.
11. Alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount for the absent state, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.
12. These measures are labelled as the absent measure state, so measure_state reads 'absent' on a row carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.
13. The present-state summary and the absent-state summary are stacked into one output list of entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, all rows are kept, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.
14. Deterministic output order: rows appear sorted ascending by entity_key, the entity, then by measure_state, the measure state.

=== Mart x_service_x_policy_top: Per-x_service extremes over linked x_policy rows in the 167589_xa_core_db_postgres.sql schema — WHICH row is largest, not how large it is ===

Grain: one row per x_service (id), INCLUDING x_service rows with no linked x_policy rows.

Key column: parent_key is the single key column of this mart.

Output columns.
- parent_key: the identifier of the x_service row; one row per value.
- parent_name: the description of the x_service row, copied unchanged.
- top_measure: the largest policy_type itself; 0 when the parent has no x_policy rows, and 0 when none of its rows carries a policy_type value.
- tied_count: how many x_policy rows are tied at that largest policy_type. It is 1 when exactly one row carries that largest policy_type; 0 when there are no rows or when none of the rows carries a policy_type value; a row with no policy_type value never ties: only a row whose policy_type value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.
- child_count: the number of x_policy rows for this x_service row; 0 when there are none. Every linked x_policy row counts, whether or not it carries a policy_type value. A x_service row kept with no x_policy row reports 0 here, never 1: its placeholder holds no x_policy row to count.
- total_measure: the total of policy_type over all of them; 0 when the parent has no x_policy rows, and 0 when none of its rows carries a policy_type value (rows with no policy_type value add nothing).
- top_label: the description of the x_policy row with the LARGEST policy_type for this x_service row. Ties in policy_type are broken by taking the SMALLEST description under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no description value sorts after every labelled row; rows tied on both are resolved by the smallest id. A row with no policy_type value still ranks, after every row that has one, so a parent holding at least one x_policy row always has a winning row — when NONE of its rows carries a policy_type value the winner is the one the tie-break alone selects, not the no-rows default. It is the literal '(none)' when the parent has no x_policy rows at all, and '(none)' when the winning row has no description value.
- top_row_id: the id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real x_policy row whenever the parent has any. This includes when none of them carries a policy_type value. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share: top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state: 'empty' when no row holds a maximum at all — the parent has no x_policy rows, or none of its rows carries a policy_type value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do.

Rules.
1. Source table x_service is read in full from its s3 backend and supplies this mart's parent rows.
2. Source table x_policy is read in full from its mongodb backend and supplies this mart's linked rows.
3. There is one row per x_service row, keyed by id, and that row carries parent_key and parent_name.
4. The x_policy rows are brought in by matching x_policy service to parent_key, carrying service and id; preservation is left-sided towards x_service, so a x_service row with no x_policy rows still appears, with the declared defaults.
5. Within each parent_key the matched rows are ranked under an explicit total order — the measure first, then the declared tie-break — so the extremal row is a function of the input and not of row order.
6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group, and each row reports top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.
7. For each parent_key the single row at which the ordering measure policy_type is largest survives, ties broken by the smallest description under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no description value sorts after every row that has one), then the smallest id, and top_label and top_row_id are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.
8. The extremal row's attributes are attributed to the grouped measures by matching parent_key; preservation is left-sided on the measures, so a group with no rows at all keeps its measures.
9. The mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults of 0 — never NULL — for a group with no matching rows, and for top_measure and total_measure the default also applies to a group none of whose real rows carries an input value.
10. Guarded ratio, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when the denominator total_measure is 0 or has no value.
11. Alongside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, tie_state is 'empty' when no row holds a maximum at all — the parent has no x_policy rows, or none of its rows carries a policy_type value — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; equivalently tie_state follows tied_count, 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more, this being a categorical mapping with no numeric boundary and never null or blank. A row holds the maximum only when it carries a policy_type value equal to the largest policy_type value among the parent's rows; a row with no policy_type value never holds the maximum, so a parent whose x_policy rows all lack a policy_type value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.
12. Deterministic output order: rows appear sorted by parent_key ascending.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `x_user_x_policy_item_user_perm_snapshot`

- Grain: One row per x_user (id), INCLUDING x_user rows with no linked x_policy_item_user_perm rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share

```text
Mart 'x_user_x_policy_item_user_perm_snapshot' has 10 declared semantic rules:
1. [source] Read source table x_user. (public source tables: x_user)
2. [source] Read source table x_policy_item_user_perm. (public source tables: x_policy_item_user_perm)
3. [derive] One row per x_user row, keyed by id. (public source tables: x_user | public carried/output columns: parent_key, parent_name)
4. [join] Bring in x_policy_item_user_perm; a x_user row with no matching x_policy_item_user_perm row is retained and receives the stated empty snapshot values. (public source tables: x_policy_item_user_perm | public carried/output columns: user_id, id | join preservation: left | condition public identifiers: x_policy_item_user_perm, user_id, parent_key)
5. [aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count, lifetime_amount for that row's matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount)
6. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest id, and take latest_row_id, latest_amount, latest_label from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, latest_row_id, latest_amount, latest_label)
7. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
8. [derive] Name the mart columns; lifetime_amount reports its declared default — never NULL — for a group with no matching rows. For lifetime_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label)
9. [ratio] Guarded ratios: latest_amount_share — latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose sort_order is missing, so such a row gives 0.0. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `x_service_def_x_service_config_def_distribution`

- Grain: One row per (id, measure state) pair represented by linked x_service_config_def rows, plus one absent no-activity row for a x_service_def row with no links. Because item_id is required, no linked x_service_config_def row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'x_service_def_x_service_config_def_distribution' has 14 declared semantic rules:
1. [source] Read source table x_service_def. (public source tables: x_service_def)
2. [source] Read source table x_service_config_def. (public source tables: x_service_config_def)
3. [derive] Carry each id and its description into the measure-state calculation. (public source tables: x_service_def | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked x_service_config_def rows into each x_service_def entity; retain an entity with no linked row so its absent state is visible. (public source tables: x_service_config_def | public carried/output columns: entity_key, entity_name, id, def_id | join preservation: left | condition public identifiers: x_service_config_def, def_id, entity_key)
5. [filter] Keep the present measure-state rows: a real x_service_config_def row; item_id is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per x_service_def entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different item_id values occur (each different value counted once, however many rows repeat it), total item_id, and largest item_id. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a x_service_def row with no x_service_config_def rows; no real row can enter this state because item_id is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per x_service_def entity with no linked x_service_config_def row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked x_service_config_def row, reporting a row count of 0, 0 different item_id values, a total item_id of 0 and a largest item_id of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `x_service_x_policy_top`

- Grain: One row per x_service (id), INCLUDING x_service rows with no linked x_policy rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'x_service_x_policy_top' has 12 declared semantic rules:
1. [source] Read source table x_service. (public source tables: x_service)
2. [source] Read source table x_policy. (public source tables: x_policy)
3. [derive] One row per x_service row, keyed by id. (public source tables: x_service | public carried/output columns: parent_key, parent_name)
4. [join] Bring in x_policy: a x_service row with no x_policy rows still appears, with the declared defaults. (public source tables: x_policy | public carried/output columns: service, id | join preservation: left | condition public identifiers: x_policy, service, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest description under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no description value sorts after every row that has one), then the smallest id, and take top_label, top_row_id from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no x_policy rows, or none of its rows carries a policy_type value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a policy_type value equal to the largest policy_type value among the parent's rows; a row with no policy_type value never holds the maximum. So a parent whose x_policy rows all lack a policy_type value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### x_access_type_def  (source backend: postgres)
Source table x_access_type_def.

- `added_by_id`: bigint NULL — Column added_by_id of table x_access_type_def.
- `create_time`: timestamp NULL — Column create_time of table x_access_type_def.
- `def_id`: bigint NOT NULL — Column def_id of table x_access_type_def.
- `guid`: text NULL — Column guid of table x_access_type_def.
- `id`: bigint NOT NULL — Column id of table x_access_type_def.
- `item_id`: bigint NOT NULL — Column item_id of table x_access_type_def.
- `label`: text NULL — Column label of table x_access_type_def.
- `name`: text NULL — Column name of table x_access_type_def.
- `rb_key_label`: text NULL — Column rb_key_label of table x_access_type_def.
- `sort_order`: integer NULL — Column sort_order of table x_access_type_def.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_access_type_def.
- `update_time`: timestamp NULL — Column update_time of table x_access_type_def.
- primary key: id

### x_access_type_def_grants  (source backend: postgres)
Source table x_access_type_def_grants.

- `added_by_id`: bigint NULL — Column added_by_id of table x_access_type_def_grants.
- `atd_id`: bigint NOT NULL — Column atd_id of table x_access_type_def_grants.
- `create_time`: timestamp NULL — Column create_time of table x_access_type_def_grants.
- `guid`: text NULL — Column guid of table x_access_type_def_grants.
- `id`: bigint NOT NULL — Column id of table x_access_type_def_grants.
- `implied_grant`: text NULL — Column implied_grant of table x_access_type_def_grants.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_access_type_def_grants.
- `update_time`: timestamp NULL — Column update_time of table x_access_type_def_grants.
- primary key: id

### x_asset  (source backend: files)
Source table x_asset.

- `act_status`: integer NOT NULL — Column act_status of table x_asset.
- `added_by_id`: bigint NULL — Column added_by_id of table x_asset.
- `asset_name`: text NOT NULL — Column asset_name of table x_asset.
- `asset_type`: integer NOT NULL — Column asset_type of table x_asset.
- `config`: text NULL — Column config of table x_asset.
- `create_time`: timestamp NULL — Column create_time of table x_asset.
- `descr`: text NULL — Column descr of table x_asset.
- `id`: bigint NOT NULL — Column id of table x_asset.
- `sup_native`: boolean NOT NULL — Column sup_native of table x_asset.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_asset.
- `update_time`: timestamp NULL — Column update_time of table x_asset.
- primary key: id

### x_audit_map  (source backend: postgres)
Source table x_audit_map.

- `added_by_id`: bigint NULL — Column ADDED_BY_ID of table x_audit_map.
- `audit_type`: bigint NOT NULL — Column AUDIT_TYPE of table x_audit_map.
- `create_time`: timestamp NULL — Column CREATE_TIME of table x_audit_map.
- `group_id`: bigint NULL — Column GROUP_ID of table x_audit_map.
- `res_id`: bigint NULL — Column RES_ID of table x_audit_map.
- `update_time`: timestamp NULL — Column UPDATE_TIME of table x_audit_map.
- `upd_by_id`: bigint NULL — Column UPD_BY_ID of table x_audit_map.
- `user_id`: bigint NULL — Column USER_ID of table x_audit_map.
- `id`: bigint NOT NULL — Column id of table x_audit_map.
- primary key: id

### x_auth_sess  (source backend: rest)
Source table x_auth_sess.

- `added_by_id`: bigint NULL — Column added_by_id of table x_auth_sess.
- `auth_provider`: integer NOT NULL — Column auth_provider of table x_auth_sess.
- `auth_status`: integer NOT NULL — Column auth_status of table x_auth_sess.
- `auth_time`: timestamp NOT NULL — Column auth_time of table x_auth_sess.
- `auth_type`: integer NOT NULL — Column auth_type of table x_auth_sess.
- `create_time`: timestamp NULL — Column create_time of table x_auth_sess.
- `device_type`: integer NOT NULL — Column device_type of table x_auth_sess.
- `ext_sess_id`: text NULL — Column ext_sess_id of table x_auth_sess.
- `id`: bigint NOT NULL — Column id of table x_auth_sess.
- `login_id`: text NOT NULL — Column login_id of table x_auth_sess.
- `req_ip`: text NOT NULL — Column req_ip of table x_auth_sess.
- `req_ua`: text NULL — Column req_ua of table x_auth_sess.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_auth_sess.
- `update_time`: timestamp NULL — Column update_time of table x_auth_sess.
- `user_id`: bigint NULL — Column user_id of table x_auth_sess.
- primary key: id

### x_context_enricher_def  (source backend: s3)
Source table x_context_enricher_def.

- `added_by_id`: bigint NULL — Column added_by_id of table x_context_enricher_def.
- `create_time`: timestamp NULL — Column create_time of table x_context_enricher_def.
- `def_id`: bigint NOT NULL — Column def_id of table x_context_enricher_def.
- `enricher`: text NULL — Column enricher of table x_context_enricher_def.
- `enricher_options`: text NULL — Column enricher_options of table x_context_enricher_def.
- `guid`: text NULL — Column guid of table x_context_enricher_def.
- `id`: bigint NOT NULL — Column id of table x_context_enricher_def.
- `item_id`: bigint NOT NULL — Column item_id of table x_context_enricher_def.
- `name`: text NULL — Column name of table x_context_enricher_def.
- `sort_order`: integer NULL — Column sort_order of table x_context_enricher_def.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_context_enricher_def.
- `update_time`: timestamp NULL — Column update_time of table x_context_enricher_def.
- primary key: id

### x_cred_store  (source backend: postgres)
Source table x_cred_store.

- `added_by_id`: bigint NULL — Column added_by_id of table x_cred_store.
- `create_time`: timestamp NULL — Column create_time of table x_cred_store.
- `descr`: text NOT NULL — Column descr of table x_cred_store.
- `id`: bigint NOT NULL — Column id of table x_cred_store.
- `store_name`: text NOT NULL — Column store_name of table x_cred_store.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_cred_store.
- `update_time`: timestamp NULL — Column update_time of table x_cred_store.
- primary key: id

### x_data_hist  (source backend: files)
Source table x_data_hist.

- `action`: text NOT NULL — Column action of table x_data_hist.
- `content`: text NOT NULL — Column content of table x_data_hist.
- `create_time`: timestamp NULL — Column create_time of table x_data_hist.
- `from_time`: timestamp NOT NULL — Column from_time of table x_data_hist.
- `id`: bigint NOT NULL — Column id of table x_data_hist.
- `obj_class_type`: integer NOT NULL — Column obj_class_type of table x_data_hist.
- `obj_guid`: text NOT NULL — Column obj_guid of table x_data_hist.
- `obj_id`: bigint NOT NULL — Column obj_id of table x_data_hist.
- `obj_name`: text NOT NULL — Column obj_name of table x_data_hist.
- `to_time`: timestamp NULL — Column to_time of table x_data_hist.
- `update_time`: timestamp NULL — Column update_time of table x_data_hist.
- `version`: bigint NULL — Column version of table x_data_hist.
- primary key: id

### x_db_base  (source backend: mongodb)
Source table x_db_base.

- `added_by_id`: bigint NULL — Column added_by_id of table x_db_base.
- `create_time`: timestamp NULL — Column create_time of table x_db_base.
- `id`: bigint NOT NULL — Column id of table x_db_base.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_db_base.
- `update_time`: timestamp NULL — Column update_time of table x_db_base.
- primary key: id

### x_enum_def  (source backend: s3)
Source table x_enum_def.

- `added_by_id`: bigint NULL — Column added_by_id of table x_enum_def.
- `create_time`: timestamp NULL — Column create_time of table x_enum_def.
- `def_id`: bigint NOT NULL — Column def_id of table x_enum_def.
- `default_index`: bigint NULL — Column default_index of table x_enum_def.
- `guid`: text NULL — Column guid of table x_enum_def.
- `id`: bigint NOT NULL — Column id of table x_enum_def.
- `item_id`: bigint NOT NULL — Column item_id of table x_enum_def.
- `name`: text NULL — Column name of table x_enum_def.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_enum_def.
- `update_time`: timestamp NULL — Column update_time of table x_enum_def.
- primary key: id

### x_enum_element_def  (source backend: rest)
Source table x_enum_element_def.

- `added_by_id`: bigint NULL — Column added_by_id of table x_enum_element_def.
- `create_time`: timestamp NULL — Column create_time of table x_enum_element_def.
- `enum_def_id`: bigint NOT NULL — Column enum_def_id of table x_enum_element_def.
- `guid`: text NULL — Column guid of table x_enum_element_def.
- `id`: bigint NOT NULL — Column id of table x_enum_element_def.
- `item_id`: bigint NOT NULL — Column item_id of table x_enum_element_def.
- `label`: text NULL — Column label of table x_enum_element_def.
- `name`: text NULL — Column name of table x_enum_element_def.
- `rb_key_label`: text NULL — Column rb_key_label of table x_enum_element_def.
- `sort_order`: integer NULL — Column sort_order of table x_enum_element_def.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_enum_element_def.
- `update_time`: timestamp NULL — Column update_time of table x_enum_element_def.
- primary key: id

### x_group  (source backend: files)
Source table x_group.

- `added_by_id`: bigint NULL — Column ADDED_BY_ID of table x_group.
- `create_time`: timestamp NULL — Column CREATE_TIME of table x_group.
- `cred_store_id`: bigint NULL — Column CRED_STORE_ID of table x_group.
- `descr`: text NULL — Column DESCR of table x_group.
- `group_name`: text NOT NULL — Column GROUP_NAME of table x_group.
- `group_src`: integer NOT NULL — Column GROUP_SRC of table x_group.
- `group_type`: integer NOT NULL — Column GROUP_TYPE of table x_group.
- `is_visible`: integer NOT NULL — Column IS_VISIBLE of table x_group.
- `status`: integer NOT NULL — Column STATUS of table x_group.
- `update_time`: timestamp NULL — Column UPDATE_TIME of table x_group.
- `upd_by_id`: bigint NULL — Column UPD_BY_ID of table x_group.
- `id`: bigint NOT NULL — Column id of table x_group.
- primary key: id

### x_group_groups  (source backend: postgres)
Source table x_group_groups.

- `added_by_id`: bigint NULL — Column added_by_id of table x_group_groups.
- `create_time`: timestamp NULL — Column create_time of table x_group_groups.
- `group_id`: bigint NULL — Column group_id of table x_group_groups.
- `group_name`: text NOT NULL — Column group_name of table x_group_groups.
- `id`: bigint NOT NULL — Column id of table x_group_groups.
- `p_group_id`: bigint NULL — Column p_group_id of table x_group_groups.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_group_groups.
- `update_time`: timestamp NULL — Column update_time of table x_group_groups.
- primary key: id

### x_group_module_perm  (source backend: mongodb)
Source table x_group_module_perm.

- `added_by_id`: bigint NULL — Column added_by_id of table x_group_module_perm.
- `create_time`: timestamp NULL — Column create_time of table x_group_module_perm.
- `group_id`: bigint NULL — Column group_id of table x_group_module_perm.
- `id`: bigint NOT NULL — Column id of table x_group_module_perm.
- `is_allowed`: integer NOT NULL — Column is_allowed of table x_group_module_perm.
- `module_id`: bigint NULL — Column module_id of table x_group_module_perm.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_group_module_perm.
- `update_time`: timestamp NULL — Column update_time of table x_group_module_perm.
- primary key: id

### x_group_users  (source backend: postgres)
Source table x_group_users.

- `added_by_id`: bigint NULL — Column added_by_id of table x_group_users.
- `create_time`: timestamp NULL — Column create_time of table x_group_users.
- `group_name`: text NOT NULL — Column group_name of table x_group_users.
- `id`: bigint NOT NULL — Column id of table x_group_users.
- `p_group_id`: bigint NULL — Column p_group_id of table x_group_users.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_group_users.
- `update_time`: timestamp NULL — Column update_time of table x_group_users.
- `user_id`: bigint NULL — Column user_id of table x_group_users.
- primary key: id

### x_modules_master  (source backend: postgres)
Source table x_modules_master.

- `added_by_id`: bigint NULL — Column added_by_id of table x_modules_master.
- `create_time`: timestamp NULL — Column create_time of table x_modules_master.
- `id`: bigint NOT NULL — Column id of table x_modules_master.
- `module`: text NOT NULL — Column module of table x_modules_master.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_modules_master.
- `update_time`: timestamp NULL — Column update_time of table x_modules_master.
- `url`: text NULL — Column url of table x_modules_master.
- primary key: id

### x_perm_map  (source backend: s3)
Source table x_perm_map.

- `added_by_id`: bigint NULL — Column added_by_id of table x_perm_map.
- `create_time`: timestamp NULL — Column create_time of table x_perm_map.
- `grant_revoke`: boolean NOT NULL — Column grant_revoke of table x_perm_map.
- `group_id`: bigint NULL — Column group_id of table x_perm_map.
- `id`: bigint NOT NULL — Column id of table x_perm_map.
- `ip_address`: text NULL — Column ip_address of table x_perm_map.
- `is_recursive`: integer NOT NULL — Column is_recursive of table x_perm_map.
- `is_wild_card`: boolean NOT NULL — Column is_wild_card of table x_perm_map.
- `perm_for`: integer NOT NULL — Column perm_for of table x_perm_map.
- `perm_group`: text NULL — Column perm_group of table x_perm_map.
- `perm_type`: integer NOT NULL — Column perm_type of table x_perm_map.
- `res_id`: bigint NULL — Column res_id of table x_perm_map.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_perm_map.
- `update_time`: timestamp NULL — Column update_time of table x_perm_map.
- `user_id`: bigint NULL — Column user_id of table x_perm_map.
- primary key: id

### x_policy  (source backend: mongodb)
Source table x_policy.

- `added_by_id`: bigint NULL — Column added_by_id of table x_policy.
- `create_time`: timestamp NULL — Column create_time of table x_policy.
- `description`: text NULL — Column description of table x_policy.
- `guid`: text NULL — Column guid of table x_policy.
- `id`: bigint NOT NULL — Column id of table x_policy.
- `is_audit_enabled`: boolean NOT NULL — Column is_audit_enabled of table x_policy.
- `is_enabled`: boolean NOT NULL — Column is_enabled of table x_policy.
- `name`: text NULL — Column name of table x_policy.
- `policy_type`: integer NULL — Column policy_type of table x_policy.
- `resource_signature`: text NULL — Column resource_signature of table x_policy.
- `service`: bigint NULL — Column service of table x_policy.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_policy.
- `update_time`: timestamp NULL — Column update_time of table x_policy.
- `version`: bigint NULL — Column version of table x_policy.
- primary key: id

### x_policy_condition_def  (source backend: rest)
Source table x_policy_condition_def.

- `added_by_id`: bigint NULL — Column added_by_id of table x_policy_condition_def.
- `create_time`: timestamp NULL — Column create_time of table x_policy_condition_def.
- `def_id`: bigint NOT NULL — Column def_id of table x_policy_condition_def.
- `description`: text NULL — Column description of table x_policy_condition_def.
- `evaluator`: text NULL — Column evaluator of table x_policy_condition_def.
- `evaluator_options`: text NULL — Column evaluator_options of table x_policy_condition_def.
- `guid`: text NULL — Column guid of table x_policy_condition_def.
- `id`: bigint NOT NULL — Column id of table x_policy_condition_def.
- `item_id`: bigint NOT NULL — Column item_id of table x_policy_condition_def.
- `label`: text NULL — Column label of table x_policy_condition_def.
- `name`: text NULL — Column name of table x_policy_condition_def.
- `rb_key_description`: text NULL — Column rb_key_description of table x_policy_condition_def.
- `rb_key_label`: text NULL — Column rb_key_label of table x_policy_condition_def.
- `rb_key_validation_message`: text NULL — Column rb_key_validation_message of table x_policy_condition_def.
- `sort_order`: integer NULL — Column sort_order of table x_policy_condition_def.
- `ui_hint`: text NULL — Column ui_hint of table x_policy_condition_def.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_policy_condition_def.
- `update_time`: timestamp NULL — Column update_time of table x_policy_condition_def.
- `validation_message`: text NULL — Column validation_message of table x_policy_condition_def.
- `validation_reg_ex`: text NULL — Column validation_reg_ex of table x_policy_condition_def.
- primary key: id

### x_policy_export_audit  (source backend: files)
Source table x_policy_export_audit.

- `added_by_id`: bigint NULL — Column added_by_id of table x_policy_export_audit.
- `agent_id`: text NULL — Column agent_id of table x_policy_export_audit.
- `client_ip`: text NOT NULL — Column client_ip of table x_policy_export_audit.
- `create_time`: timestamp NULL — Column create_time of table x_policy_export_audit.
- `exported_json`: text NULL — Column exported_json of table x_policy_export_audit.
- `http_ret_code`: integer NOT NULL — Column http_ret_code of table x_policy_export_audit.
- `id`: bigint NOT NULL — Column id of table x_policy_export_audit.
- `last_updated`: timestamp NULL — Column last_updated of table x_policy_export_audit.
- `repository_name`: text NULL — Column repository_name of table x_policy_export_audit.
- `req_epoch`: bigint NOT NULL — Column req_epoch of table x_policy_export_audit.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_policy_export_audit.
- `update_time`: timestamp NULL — Column update_time of table x_policy_export_audit.
- primary key: id

### x_policy_item  (source backend: files)
Source table x_policy_item.

- `added_by_id`: bigint NULL — Column added_by_id of table x_policy_item.
- `create_time`: timestamp NULL — Column create_time of table x_policy_item.
- `delegate_admin`: boolean NOT NULL — Column delegate_admin of table x_policy_item.
- `guid`: text NULL — Column guid of table x_policy_item.
- `id`: bigint NOT NULL — Column id of table x_policy_item.
- `policy_id`: bigint NOT NULL — Column policy_id of table x_policy_item.
- `sort_order`: integer NULL — Column sort_order of table x_policy_item.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_policy_item.
- `update_time`: timestamp NULL — Column update_time of table x_policy_item.
- primary key: id

### x_policy_item_access  (source backend: postgres)
Source table x_policy_item_access.

- `added_by_id`: bigint NULL — Column added_by_id of table x_policy_item_access.
- `create_time`: timestamp NULL — Column create_time of table x_policy_item_access.
- `guid`: text NULL — Column guid of table x_policy_item_access.
- `id`: bigint NOT NULL — Column id of table x_policy_item_access.
- `is_allowed`: boolean NOT NULL — Column is_allowed of table x_policy_item_access.
- `policy_item_id`: bigint NOT NULL — Column policy_item_id of table x_policy_item_access.
- `sort_order`: integer NULL — Column sort_order of table x_policy_item_access.
- `type`: bigint NOT NULL — Column type of table x_policy_item_access.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_policy_item_access.
- `update_time`: timestamp NULL — Column update_time of table x_policy_item_access.
- primary key: id

### x_policy_item_condition  (source backend: mongodb)
Source table x_policy_item_condition.

- `added_by_id`: bigint NULL — Column added_by_id of table x_policy_item_condition.
- `create_time`: timestamp NULL — Column create_time of table x_policy_item_condition.
- `guid`: text NULL — Column guid of table x_policy_item_condition.
- `id`: bigint NOT NULL — Column id of table x_policy_item_condition.
- `policy_item_id`: bigint NOT NULL — Column policy_item_id of table x_policy_item_condition.
- `sort_order`: integer NULL — Column sort_order of table x_policy_item_condition.
- `type`: bigint NOT NULL — Column type of table x_policy_item_condition.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_policy_item_condition.
- `update_time`: timestamp NULL — Column update_time of table x_policy_item_condition.
- `value`: text NULL — Column value of table x_policy_item_condition.
- primary key: id

### x_policy_item_group_perm  (source backend: mongodb)
Source table x_policy_item_group_perm.

- `added_by_id`: bigint NULL — Column added_by_id of table x_policy_item_group_perm.
- `create_time`: timestamp NULL — Column create_time of table x_policy_item_group_perm.
- `group_id`: bigint NULL — Column group_id of table x_policy_item_group_perm.
- `guid`: text NULL — Column guid of table x_policy_item_group_perm.
- `id`: bigint NOT NULL — Column id of table x_policy_item_group_perm.
- `policy_item_id`: bigint NOT NULL — Column policy_item_id of table x_policy_item_group_perm.
- `sort_order`: integer NULL — Column sort_order of table x_policy_item_group_perm.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_policy_item_group_perm.
- `update_time`: timestamp NULL — Column update_time of table x_policy_item_group_perm.
- primary key: id

### x_policy_item_user_perm  (source backend: s3)
Source table x_policy_item_user_perm.

- `added_by_id`: bigint NULL — Column added_by_id of table x_policy_item_user_perm.
- `create_time`: timestamp NULL — Column create_time of table x_policy_item_user_perm.
- `guid`: text NULL — Column guid of table x_policy_item_user_perm.
- `id`: bigint NOT NULL — Column id of table x_policy_item_user_perm.
- `policy_item_id`: bigint NOT NULL — Column policy_item_id of table x_policy_item_user_perm.
- `sort_order`: integer NULL — Column sort_order of table x_policy_item_user_perm.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_policy_item_user_perm.
- `update_time`: timestamp NULL — Column update_time of table x_policy_item_user_perm.
- `user_id`: bigint NULL — Column user_id of table x_policy_item_user_perm.
- primary key: id

### x_policy_resource  (source backend: files)
Source table x_policy_resource.

- `added_by_id`: bigint NULL — Column added_by_id of table x_policy_resource.
- `create_time`: timestamp NULL — Column create_time of table x_policy_resource.
- `guid`: text NULL — Column guid of table x_policy_resource.
- `id`: bigint NOT NULL — Column id of table x_policy_resource.
- `is_excludes`: boolean NOT NULL — Column is_excludes of table x_policy_resource.
- `is_recursive`: boolean NOT NULL — Column is_recursive of table x_policy_resource.
- `policy_id`: bigint NOT NULL — Column policy_id of table x_policy_resource.
- `res_def_id`: bigint NOT NULL — Column res_def_id of table x_policy_resource.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_policy_resource.
- `update_time`: timestamp NULL — Column update_time of table x_policy_resource.
- primary key: id

### x_policy_resource_map  (source backend: files)
Source table x_policy_resource_map.

- `added_by_id`: bigint NULL — Column added_by_id of table x_policy_resource_map.
- `create_time`: timestamp NULL — Column create_time of table x_policy_resource_map.
- `guid`: text NULL — Column guid of table x_policy_resource_map.
- `id`: bigint NOT NULL — Column id of table x_policy_resource_map.
- `resource_id`: bigint NOT NULL — Column resource_id of table x_policy_resource_map.
- `sort_order`: integer NULL — Column sort_order of table x_policy_resource_map.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_policy_resource_map.
- `update_time`: timestamp NULL — Column update_time of table x_policy_resource_map.
- `value`: text NULL — Column value of table x_policy_resource_map.
- primary key: id

### x_portal_user  (source backend: files)
Source table x_portal_user.

- `added_by_id`: bigint NULL — Column added_by_id of table x_portal_user.
- `create_time`: timestamp NULL — Column create_time of table x_portal_user.
- `email`: text NULL — Column email of table x_portal_user.
- `first_name`: text NULL — Column first_name of table x_portal_user.
- `id`: bigint NOT NULL — Column id of table x_portal_user.
- `last_name`: text NULL — Column last_name of table x_portal_user.
- `login_id`: text NULL — Column login_id of table x_portal_user.
- `notes`: text NULL — Column notes of table x_portal_user.
- `password`: text NOT NULL — Column password of table x_portal_user.
- `pub_scr_name`: text NULL — Column pub_scr_name of table x_portal_user.
- `status`: integer NOT NULL — Column status of table x_portal_user.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_portal_user.
- `update_time`: timestamp NULL — Column update_time of table x_portal_user.
- `user_src`: integer NOT NULL — Column user_src of table x_portal_user.
- primary key: id

### x_portal_user_role  (source backend: files)
Source table x_portal_user_role.

- `added_by_id`: bigint NULL — Column added_by_id of table x_portal_user_role.
- `create_time`: timestamp NULL — Column create_time of table x_portal_user_role.
- `id`: bigint NOT NULL — Column id of table x_portal_user_role.
- `status`: integer NOT NULL — Column status of table x_portal_user_role.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_portal_user_role.
- `update_time`: timestamp NULL — Column update_time of table x_portal_user_role.
- `user_id`: bigint NOT NULL — Column user_id of table x_portal_user_role.
- `user_role`: text NULL — Column user_role of table x_portal_user_role.
- primary key: id

### x_resource  (source backend: mongodb)
Source table x_resource.

- `added_by_id`: bigint NULL — Column added_by_id of table x_resource.
- `asset_id`: bigint NOT NULL — Column asset_id of table x_resource.
- `col_type`: integer NOT NULL — Column col_type of table x_resource.
- `create_time`: timestamp NULL — Column create_time of table x_resource.
- `descr`: text NULL — Column descr of table x_resource.
- `id`: bigint NOT NULL — Column id of table x_resource.
- `is_encrypt`: integer NOT NULL — Column is_encrypt of table x_resource.
- `is_recursive`: integer NOT NULL — Column is_recursive of table x_resource.
- `parent_id`: bigint NULL — Column parent_id of table x_resource.
- `parent_path`: text NULL — Column parent_path of table x_resource.
- `policy_name`: text NULL — Column policy_name of table x_resource.
- `res_col_fams`: text NULL — Column res_col_fams of table x_resource.
- `res_cols`: text NULL — Column res_cols of table x_resource.
- `res_dbs`: text NULL — Column res_dbs of table x_resource.
- `res_group`: text NULL — Column res_group of table x_resource.
- `res_name`: text NULL — Column res_name of table x_resource.
- `res_services`: text NULL — Column res_services of table x_resource.
- `res_status`: integer NOT NULL — Column res_status of table x_resource.
- `res_tables`: text NULL — Column res_tables of table x_resource.
- `res_topologies`: text NULL — Column res_topologies of table x_resource.
- `res_type`: integer NOT NULL — Column res_type of table x_resource.
- `res_udfs`: text NULL — Column res_udfs of table x_resource.
- `table_type`: integer NOT NULL — Column table_type of table x_resource.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_resource.
- `update_time`: timestamp NULL — Column update_time of table x_resource.
- primary key: id

### x_resource_def  (source backend: rest)
Source table x_resource_def.

- `added_by_id`: bigint NULL — Column added_by_id of table x_resource_def.
- `create_time`: timestamp NULL — Column create_time of table x_resource_def.
- `def_id`: bigint NOT NULL — Column def_id of table x_resource_def.
- `description`: text NULL — Column description of table x_resource_def.
- `excludes_supported`: boolean NOT NULL — Column excludes_supported of table x_resource_def.
- `guid`: text NULL — Column guid of table x_resource_def.
- `id`: bigint NOT NULL — Column id of table x_resource_def.
- `item_id`: bigint NOT NULL — Column item_id of table x_resource_def.
- `label`: text NULL — Column label of table x_resource_def.
- `look_up_supported`: boolean NOT NULL — Column look_up_supported of table x_resource_def.
- `mandatory`: boolean NOT NULL — Column mandatory of table x_resource_def.
- `matcher`: text NULL — Column matcher of table x_resource_def.
- `matcher_options`: text NULL — Column matcher_options of table x_resource_def.
- `name`: text NULL — Column name of table x_resource_def.
- `parent`: bigint NULL — Column parent of table x_resource_def.
- `rb_key_description`: text NULL — Column rb_key_description of table x_resource_def.
- `rb_key_label`: text NULL — Column rb_key_label of table x_resource_def.
- `rb_key_validation_message`: text NULL — Column rb_key_validation_message of table x_resource_def.
- `recursive_supported`: boolean NOT NULL — Column recursive_supported of table x_resource_def.
- `res_level`: bigint NULL — Column res_level of table x_resource_def.
- `sort_order`: integer NULL — Column sort_order of table x_resource_def.
- `type`: text NULL — Column type of table x_resource_def.
- `ui_hint`: text NULL — Column ui_hint of table x_resource_def.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_resource_def.
- `update_time`: timestamp NULL — Column update_time of table x_resource_def.
- `validation_message`: text NULL — Column validation_message of table x_resource_def.
- `validation_reg_ex`: text NULL — Column validation_reg_ex of table x_resource_def.
- primary key: id

### x_service  (source backend: s3)
Source table x_service.

- `added_by_id`: bigint NULL — Column added_by_id of table x_service.
- `create_time`: timestamp NULL — Column create_time of table x_service.
- `description`: text NULL — Column description of table x_service.
- `guid`: text NULL — Column guid of table x_service.
- `id`: bigint NOT NULL — Column id of table x_service.
- `is_enabled`: boolean NOT NULL — Column is_enabled of table x_service.
- `name`: text NULL — Column name of table x_service.
- `policy_update_time`: timestamp NULL — Column policy_update_time of table x_service.
- `policy_version`: bigint NULL — Column policy_version of table x_service.
- `type`: bigint NULL — Column type of table x_service.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_service.
- `update_time`: timestamp NULL — Column update_time of table x_service.
- `version`: bigint NULL — Column version of table x_service.
- primary key: id

### x_service_config_def  (source backend: mongodb)
Source table x_service_config_def.

- `added_by_id`: bigint NULL — Column added_by_id of table x_service_config_def.
- `create_time`: timestamp NULL — Column create_time of table x_service_config_def.
- `def_id`: bigint NOT NULL — Column def_id of table x_service_config_def.
- `default_value`: text NULL — Column default_value of table x_service_config_def.
- `description`: text NULL — Column description of table x_service_config_def.
- `guid`: text NULL — Column guid of table x_service_config_def.
- `id`: bigint NOT NULL — Column id of table x_service_config_def.
- `is_mandatory`: boolean NOT NULL — Column is_mandatory of table x_service_config_def.
- `item_id`: bigint NOT NULL — Column item_id of table x_service_config_def.
- `label`: text NULL — Column label of table x_service_config_def.
- `name`: text NULL — Column name of table x_service_config_def.
- `rb_key_description`: text NULL — Column rb_key_description of table x_service_config_def.
- `rb_key_label`: text NULL — Column rb_key_label of table x_service_config_def.
- `rb_key_validation_message`: text NULL — Column rb_key_validation_message of table x_service_config_def.
- `sort_order`: integer NULL — Column sort_order of table x_service_config_def.
- `sub_type`: text NULL — Column sub_type of table x_service_config_def.
- `type`: text NULL — Column type of table x_service_config_def.
- `ui_hint`: text NULL — Column ui_hint of table x_service_config_def.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_service_config_def.
- `update_time`: timestamp NULL — Column update_time of table x_service_config_def.
- `validation_message`: text NULL — Column validation_message of table x_service_config_def.
- `validation_reg_ex`: text NULL — Column validation_reg_ex of table x_service_config_def.
- primary key: id

### x_service_config_map  (source backend: files)
Source table x_service_config_map.

- `added_by_id`: bigint NULL — Column added_by_id of table x_service_config_map.
- `config_key`: text NULL — Column config_key of table x_service_config_map.
- `config_value`: text NULL — Column config_value of table x_service_config_map.
- `create_time`: timestamp NULL — Column create_time of table x_service_config_map.
- `guid`: text NULL — Column guid of table x_service_config_map.
- `id`: bigint NOT NULL — Column id of table x_service_config_map.
- `service`: bigint NOT NULL — Column service of table x_service_config_map.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_service_config_map.
- `update_time`: timestamp NULL — Column update_time of table x_service_config_map.
- primary key: id

### x_service_def  (source backend: postgres)
Source table x_service_def.

- `added_by_id`: bigint NULL — Column added_by_id of table x_service_def.
- `create_time`: timestamp NULL — Column create_time of table x_service_def.
- `description`: text NULL — Column description of table x_service_def.
- `guid`: text NULL — Column guid of table x_service_def.
- `id`: bigint NOT NULL — Column id of table x_service_def.
- `impl_class_name`: text NULL — Column impl_class_name of table x_service_def.
- `is_enabled`: boolean NULL — Column is_enabled of table x_service_def.
- `label`: text NULL — Column label of table x_service_def.
- `name`: text NULL — Column name of table x_service_def.
- `rb_key_description`: text NULL — Column rb_key_description of table x_service_def.
- `rb_key_label`: text NULL — Column rb_key_label of table x_service_def.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_service_def.
- `update_time`: timestamp NULL — Column update_time of table x_service_def.
- `version`: bigint NULL — Column version of table x_service_def.
- primary key: id

### x_trx_log  (source backend: mongodb)
Source table x_trx_log.

- `action`: text NULL — Column action of table x_trx_log.
- `added_by_id`: bigint NULL — Column added_by_id of table x_trx_log.
- `attr_name`: text NULL — Column attr_name of table x_trx_log.
- `class_type`: integer NOT NULL — Column class_type of table x_trx_log.
- `create_time`: timestamp NULL — Column create_time of table x_trx_log.
- `id`: bigint NOT NULL — Column id of table x_trx_log.
- `new_val`: text NULL — Column new_val of table x_trx_log.
- `object_id`: bigint NULL — Column object_id of table x_trx_log.
- `object_name`: text NULL — Column object_name of table x_trx_log.
- `parent_object_class_type`: integer NOT NULL — Column parent_object_class_type of table x_trx_log.
- `parent_object_id`: bigint NULL — Column parent_object_id of table x_trx_log.
- `parent_object_name`: text NULL — Column parent_object_name of table x_trx_log.
- `prev_val`: text NULL — Column prev_val of table x_trx_log.
- `req_id`: text NULL — Column req_id of table x_trx_log.
- `sess_id`: text NULL — Column sess_id of table x_trx_log.
- `sess_type`: text NULL — Column sess_type of table x_trx_log.
- `trx_id`: text NULL — Column trx_id of table x_trx_log.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_trx_log.
- `update_time`: timestamp NULL — Column update_time of table x_trx_log.
- primary key: id

### x_user  (source backend: files)
Source table x_user.

- `added_by_id`: bigint NULL — Column added_by_id of table x_user.
- `create_time`: timestamp NULL — Column create_time of table x_user.
- `cred_store_id`: bigint NULL — Column cred_store_id of table x_user.
- `descr`: text NULL — Column descr of table x_user.
- `id`: bigint NOT NULL — Column id of table x_user.
- `is_visible`: integer NOT NULL — Column is_visible of table x_user.
- `status`: integer NOT NULL — Column status of table x_user.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_user.
- `update_time`: timestamp NULL — Column update_time of table x_user.
- `user_name`: text NOT NULL — Column user_name of table x_user.
- primary key: id

### x_user_module_perm  (source backend: rest)
Source table x_user_module_perm.

- `added_by_id`: bigint NULL — Column added_by_id of table x_user_module_perm.
- `create_time`: timestamp NULL — Column create_time of table x_user_module_perm.
- `id`: bigint NOT NULL — Column id of table x_user_module_perm.
- `is_allowed`: integer NOT NULL — Column is_allowed of table x_user_module_perm.
- `module_id`: bigint NULL — Column module_id of table x_user_module_perm.
- `upd_by_id`: bigint NULL — Column upd_by_id of table x_user_module_perm.
- `update_time`: timestamp NULL — Column update_time of table x_user_module_perm.
- `user_id`: bigint NULL — Column user_id of table x_user_module_perm.
- primary key: id

### xa_access_audit  (source backend: s3)
Source table xa_access_audit.

- `access_result`: integer NULL — Column access_result of table xa_access_audit.
- `access_type`: text NULL — Column access_type of table xa_access_audit.
- `acl_enforcer`: text NULL — Column acl_enforcer of table xa_access_audit.
- `action`: text NULL — Column action of table xa_access_audit.
- `added_by_id`: bigint NULL — Column added_by_id of table xa_access_audit.
- `agent_id`: text NULL — Column agent_id of table xa_access_audit.
- `audit_type`: integer NOT NULL — Column audit_type of table xa_access_audit.
- `client_ip`: text NULL — Column client_ip of table xa_access_audit.
- `client_type`: text NULL — Column client_type of table xa_access_audit.
- `create_time`: timestamp NULL — Column create_time of table xa_access_audit.
- `event_time`: timestamp NULL — Column event_time of table xa_access_audit.
- `id`: bigint NOT NULL — Column id of table xa_access_audit.
- `policy_id`: bigint NULL — Column policy_id of table xa_access_audit.
- `repo_name`: text NULL — Column repo_name of table xa_access_audit.
- `repo_type`: bigint NULL — Column repo_type of table xa_access_audit.
- `request_data`: text NULL — Column request_data of table xa_access_audit.
- `request_user`: text NULL — Column request_user of table xa_access_audit.
- `resource_path`: text NULL — Column resource_path of table xa_access_audit.
- `resource_type`: text NULL — Column resource_type of table xa_access_audit.
- `result_reason`: text NULL — Column result_reason of table xa_access_audit.
- `session_id`: text NULL — Column session_id of table xa_access_audit.
- `upd_by_id`: bigint NULL — Column upd_by_id of table xa_access_audit.
- `update_time`: timestamp NULL — Column update_time of table xa_access_audit.
- primary key: id

### Relationships

- x_access_type_def(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_access_type_def(def_id) -> x_service_def(id) [required]
- x_access_type_def(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_access_type_def_grants(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_access_type_def_grants(atd_id) -> x_access_type_def(id) [required]
- x_access_type_def_grants(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_asset(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_asset(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_audit_map(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_audit_map(group_id) -> x_group(id) [optional (may be NULL/dangling)]
- x_audit_map(res_id) -> x_resource(id) [optional (may be NULL/dangling)]
- x_audit_map(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_audit_map(user_id) -> x_user(id) [optional (may be NULL/dangling)]
- x_auth_sess(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_auth_sess(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_auth_sess(user_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_context_enricher_def(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_context_enricher_def(def_id) -> x_service_def(id) [required]
- x_context_enricher_def(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_cred_store(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_cred_store(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_db_base(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_db_base(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_enum_def(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_enum_def(def_id) -> x_service_def(id) [required]
- x_enum_def(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_enum_element_def(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_enum_element_def(enum_def_id) -> x_enum_def(id) [required]
- x_enum_element_def(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_group(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_group(cred_store_id) -> x_cred_store(id) [optional (may be NULL/dangling)]
- x_group(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_group_groups(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_group_groups(group_id) -> x_group(id) [optional (may be NULL/dangling)]
- x_group_groups(p_group_id) -> x_group(id) [optional (may be NULL/dangling)]
- x_group_groups(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_group_module_perm(group_id) -> x_group(id) [optional (may be NULL/dangling)]
- x_group_module_perm(module_id) -> x_modules_master(id) [optional (may be NULL/dangling)]
- x_group_users(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_group_users(p_group_id) -> x_group(id) [optional (may be NULL/dangling)]
- x_group_users(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_group_users(user_id) -> x_user(id) [optional (may be NULL/dangling)]
- x_perm_map(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_perm_map(group_id) -> x_group(id) [optional (may be NULL/dangling)]
- x_perm_map(res_id) -> x_resource(id) [optional (may be NULL/dangling)]
- x_perm_map(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_perm_map(user_id) -> x_user(id) [optional (may be NULL/dangling)]
- x_policy(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy(service) -> x_service(id) [optional (may be NULL/dangling)]
- x_policy(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_condition_def(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_condition_def(def_id) -> x_service_def(id) [required]
- x_policy_condition_def(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_export_audit(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_export_audit(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_item(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_item(policy_id) -> x_policy(id) [required]
- x_policy_item(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_item_access(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_item_access(policy_item_id) -> x_policy_item(id) [required]
- x_policy_item_access(type) -> x_access_type_def(id) [required]
- x_policy_item_access(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_item_condition(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_item_condition(policy_item_id) -> x_policy_item(id) [required]
- x_policy_item_condition(type) -> x_policy_condition_def(id) [required]
- x_policy_item_condition(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_item_group_perm(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_item_group_perm(group_id) -> x_group(id) [optional (may be NULL/dangling)]
- x_policy_item_group_perm(policy_item_id) -> x_policy_item(id) [required]
- x_policy_item_group_perm(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_item_user_perm(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_item_user_perm(policy_item_id) -> x_policy_item(id) [required]
- x_policy_item_user_perm(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_item_user_perm(user_id) -> x_user(id) [optional (may be NULL/dangling)]
- x_policy_resource(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_resource(policy_id) -> x_policy(id) [required]
- x_policy_resource(res_def_id) -> x_resource_def(id) [required]
- x_policy_resource(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_resource_map(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_policy_resource_map(resource_id) -> x_policy_resource(id) [required]
- x_policy_resource_map(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_portal_user_role(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_portal_user_role(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_portal_user_role(user_id) -> x_portal_user(id) [required]
- x_resource(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_resource(asset_id) -> x_asset(id) [required]
- x_resource(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_resource_def(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_resource_def(def_id) -> x_service_def(id) [required]
- x_resource_def(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_service(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_service(type) -> x_service_def(id) [optional (may be NULL/dangling)]
- x_service(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_service_config_def(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_service_config_def(def_id) -> x_service_def(id) [required]
- x_service_config_def(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_service_config_map(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_service_config_map(service) -> x_service(id) [required]
- x_service_config_map(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_service_def(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_service_def(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_trx_log(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_trx_log(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_user(added_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_user(cred_store_id) -> x_cred_store(id) [optional (may be NULL/dangling)]
- x_user(upd_by_id) -> x_portal_user(id) [optional (may be NULL/dangling)]
- x_user_module_perm(module_id) -> x_modules_master(id) [optional (may be NULL/dangling)]
- x_user_module_perm(user_id) -> x_portal_user(id) [optional (may be NULL/dangling)]

