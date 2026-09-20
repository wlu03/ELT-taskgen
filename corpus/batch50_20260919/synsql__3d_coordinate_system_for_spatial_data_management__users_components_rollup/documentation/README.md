# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# 3D Coordinate System For Spatial Data Management

## Specification

PROJECT OVERVIEW: 3D Coordinate System For Spatial Data Management

This project builds two analytical marts over the spatial-data-management scenario. Six source tables feed the work, and each must be extracted from the backend named here.

The source table projects must be extracted from the s3 backend; it holds one row per project_id, with project_name, project_description, start_date, end_date, project_manager_id, project_status (one of active, completed, on hold), budget, location and client_id.

The source table components must be extracted from the rest backend; it holds one row per component_id (a UUID), with project_id, component_name, coordinate_x, coordinate_y, coordinate_z, component_type_id, installation_date, status (one of installed, pending, removed), material, manufacturer, serial_number, weight, dimensions, notes, last_modified_by and last_modified_date.

The source table component_types must be extracted from the files backend; it holds one row per component_type_id, with type_name (one of structural, electrical, mechanical), description, parent_type_id and is_active.

The source table users must be extracted from the files backend; it holds one row per user_id, with user_name, email, role (one of project manager, engineer, admin), password, phone_number, address, department and is_active.

The source table access_logs must be extracted from the files backend; it holds one row per access_id, with component_id, user_id, access_date, access_type (one of view, edit), ip_address and user_agent.

The source table component_versions must be extracted from the s3 backend; it holds one row per version_id, with component_id, version_number, coordinate_x, coordinate_y, coordinate_z, change_date, change_description, changed_by, approved_by and approval_date.

RELATIONSHIPS BETWEEN THE SOURCE TABLES

The child table access_logs relates through its column component_id to the parent table components through its column component_id; this relationship is optional (the child value may be NULL or dangling).

The child table access_logs relates through its column user_id to the parent table users through its column user_id; this relationship is optional (the child value may be NULL or dangling).

The child table component_versions relates through its column approved_by to the parent table users through its column user_id; this relationship is optional (the child value may be NULL or dangling).

The child table component_versions relates through its column changed_by to the parent table users through its column user_id; this relationship is optional (the child value may be NULL or dangling).

The child table component_versions relates through its column component_id to the parent table components through its column component_id; this relationship is optional (the child value may be NULL or dangling).

The child table components relates through its column component_type_id to the parent table component_types through its column component_type_id; this relationship is optional (the child value may be NULL or dangling).

The child table components relates through its column last_modified_by to the parent table users through its column user_id; this relationship is optional (the child value may be NULL or dangling).

The child table components relates through its column project_id to the parent table projects through its column project_id; this relationship is optional (the child value may be NULL or dangling).

The child table projects relates through its column project_manager_id to the parent table users through its column user_id; this relationship is optional (the child value may be NULL or dangling).

Throughout both marts, a components row is considered linked to a users row when the components row's last_modified_by value equals that users row's user_id.

=========================
MART users_components_rollup — a per-users roll-up of linked components activity in the 3d_coordinate_system_for_spatial_data_management scenario, including the fan-out onto component_types.
=========================

This mart is the per-users roll-up of linked components activity in the 3d_coordinate_system_for_spatial_data_management scenario, including the fan-out onto component_types.

Grain: one row per users (user_id), INCLUDING users rows with no linked components rows.

Key column: parent_key is the single key column of this mart; each parent_key value appears on exactly one row.

Rules that build this mart

Rule 1: the source table users is read in full, and every users row contributes to this mart.

Rule 2: the source table components is read in full, and its rows supply the linked activity measured here.

Rule 3: the source table component_types is read in full, and its rows supply the component type reached through each link.

Rule 4: there is one row per users row, keyed by user_id, and each such row carries parent_key and parent_name taken from that users row of the users source table.

Rule 5 (hop 1, preservation is left-sided): each users row has the components rows attributed to it brought in against the grain, matching the last_modified_by value of a components row to the user_id behind parent_key; one users row may have many components rows, and a users row with no components rows at all is RETAINED, carrying last_modified_by and user_id.

Rule 6 (hop 2, preservation is left-sided): each linked components row has its component_types row brought in by matching that components row's component_type_id to the component_type_id of a component_types row, carrying component_type_id; a components row whose component_types row is missing still counts as a link and is RETAINED.

Rule 7: there is one output row per parent_key, carrying parent_name beside the keys — a key value identifies one source row for the carried columns, so they take one value per key and never split a group — and that row reports link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount over its matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 8: the mart columns are named parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount, and total_amount, active_amount and max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount and max_amount the default also applies to a group none of whose real rows carries an input value.

Rule 9 (guarded ratio): beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount, active_amount_ratio is active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0. The same 0.0 result is reported whenever that denominator is 0 or NULL.

Rule 10: beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount and active_amount_ratio, size_band is the size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band, a value exactly at 2 is 'small', and a value exactly at 5 is 'medium'.

Rule 11: beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio and size_band, has_links is 'yes' when this parent has at least one link and 'no' otherwise, never NULL; a link_count value exactly at 0 gives 'no'.

Rule 12: the deterministic output order is ascending parent_key, sorted by parent_key.

Output columns of users_components_rollup

parent_key (integer): identifier of the users row; one row per value.

parent_name (text): user_name of the users row, copied unchanged.

link_count (bigint): number of components rows linked to this users row; 0 when there are none.

distinct_child_count (bigint): number of different component_types rows reached through those links. Two links pointing at the same child count ONCE. It is 0 when there are no links, and a link whose component_types row is missing reaches no component_types row and adds nothing to this count.

active_link_count (bigint): number of linked components rows whose status is one of ['installed']. A parent whose links ALL fail that test reports 0, not a missing row.

total_amount (float): total of coordinate_x over every linked row; 0 when there are no links, and 0 when none of the linked rows carries a coordinate_x value.

active_amount (float): total of coordinate_x over links whose status is one of ['installed']; 0 when none qualify, and 0 when every qualifying row lacks a coordinate_x value.

max_amount (float): largest coordinate_x among the linked rows; 0 when there are no links, and 0 when none of the linked rows carries a coordinate_x value.

active_amount_ratio (float): active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0.

size_band (text): size band of link_count — 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band.

has_links (text): 'yes' when this parent has at least one link, 'no' otherwise. Never NULL.

=========================
MART users_components_cohorts — a per-(users, status cohort) summary of linked components rows in the 3d_coordinate_system_for_spatial_data_management scenario, with passing and failing cohorts kept separate.
=========================

This mart is the per-(users, status cohort) summary of linked components rows in the 3d_coordinate_system_for_spatial_data_management scenario, with passing and failing cohorts kept separate.

Grain: one row per (user_id, status cohort) pair represented among linked components rows, plus one no-activity row for a users row with no linked components row at all. A users row whose linked components rows all lack a status value is in no cohort and gets no no-activity row, so it has no row in this mart.

Key columns: entity_key and cohort together key this mart; each (entity_key, cohort) pair appears on exactly one row.

Rules that build this mart

Rule 1: the source table users is read in full, and its rows are the entities summarized here.

Rule 2: the source table components is read in full, and its rows supply the linked activity summarized here.

Rule 3: each user_id and its user_name are carried into the cohort calculation as entity_key and entity_name, taken from the users source table.

Rule 4 (preservation is left-sided): the linked components rows are brought into each users entity before assigning status cohorts, matching the last_modified_by value of a components row to the user_id behind entity_key, carrying entity_key, entity_name, user_id and last_modified_by; a users entity with no such components rows is retained.

Rule 5: for the passing cohort, only the linked rows whose status belongs to the passing cohort values ['installed'] are kept, carrying entity_key and entity_name.

Rule 6: there is one row per users entity that has at least one linked components row in the passing cohort, reporting under entity_key and entity_name the number of those rows as link_count, how many different status values occur among them as distinct_status_count, the total of their coordinate_x as total_amount, and their largest coordinate_x as max_amount. The total and the largest value read only the rows that carry a coordinate_x value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 7: for those passing-cohort rows, beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 8: these measures are labelled as the passing cohort, so cohort reads 'passing' beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 9: for the failing cohort, only the linked rows whose status belongs to the failing cohort values ['pending', 'removed'] are kept, carrying entity_key and entity_name.

Rule 10: there is one row per users entity that has at least one linked components row in the failing cohort, reporting under entity_key and entity_name the number of those rows as link_count, how many different status values occur among them as distinct_status_count, the total of their coordinate_x as total_amount, and their largest coordinate_x as max_amount. The total and the largest value read only the rows that carry a coordinate_x value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 11: for those failing-cohort rows, beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 12: these measures are labelled as the failing cohort, so cohort reads 'failing' beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 13: the placeholder row, carrying entity_key and entity_name, is kept for a users entity with no linked components row at all; a users entity that has linked components rows gets no placeholder, even when every one of those rows lacks a status value.

Rule 14: there is one row per users entity with no linked components row at all, reporting under entity_key and entity_name 0 rows as link_count, 0 different status values as distinct_status_count, a coordinate_x total of 0 as total_amount and a largest coordinate_x of 0 as max_amount.

Rule 15: for those placeholder rows, beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 16: these measures are labelled as the no_activity cohort, so cohort reads 'no_activity' beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 17: the disjoint passing and failing cohort summaries are combined into one body of rows, keeping all rows of both with their entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 18: the no-activity summaries are added to that body, keeping all of those rows too, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 19: the deterministic output order is ascending entity_key first and then ascending cohort, sorted by entity then cohort.

Output columns of users_components_cohorts

entity_key (integer): identifier of the users row.

cohort (text): 'passing' for status values ['installed']; 'failing' for values ['pending', 'removed']; 'no_activity' when the users row has no linked components row. A linked components row whose status has no value belongs to no cohort: it is not counted in any cell, and it does not make the users row 'no_activity'.

entity_name (text): user_name of the users row, copied unchanged.

link_count (bigint): number of components rows in this entity/cohort cell.

distinct_status_count (bigint): the distinct status count — how many different status values are represented in this cell.

total_amount (float): total of coordinate_x in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an coordinate_x value.

max_amount (float): largest coordinate_x in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an coordinate_x value.

max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `users_components_rollup`

- Grain: One row per users (user_id), INCLUDING users rows with no linked components rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links

```text
Mart 'users_components_rollup' has 12 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table components. (public source tables: components)
3. [source] Read source table component_types. (public source tables: component_types)
4. [derive] One row per users row, keyed by user_id. (public source tables: users | public carried/output columns: parent_key, parent_name)
5. [join] Hop 1: bring in components against the grain. One users row may have many components rows, and a users row with no components rows at all is RETAINED. (public source tables: components | public carried/output columns: last_modified_by, user_id | join preservation: left | condition public identifiers: components, last_modified_by, parent_key)
6. [join] Hop 2: bring in component_types, matching each linked components row's component_type_id to the component_type_id of a component_types row. A components row whose component_types row is missing still counts as a link and is RETAINED. (public source tables: component_types | public carried/output columns: component_type_id | join preservation: left | condition public identifiers: component_types, component_type_id)
7. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
8. [derive] Name the mart columns; total_amount, active_amount, max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount, max_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
9. [ratio] Guarded ratios: active_amount_ratio — active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [conditional] size_band — Size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band | semantic parameters: boundary=a value exactly at 2 is 'small'; a value exactly at 5 is 'medium')
11. [conditional] has_links — 'yes' when this parent has at least one link, 'no' otherwise. Never NULL. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links | semantic parameters: boundary=a value exactly at 0 is 'no')
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `users_components_cohorts`

- Grain: One row per (user_id, status cohort) pair represented among linked components rows, plus one no-activity row for a users row with no linked components row at all. A users row whose linked components rows all lack a status value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'users_components_cohorts' has 19 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table components. (public source tables: components)
3. [derive] Carry each user_id and its user_name into the cohort calculation. (public source tables: users | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked components rows into each users entity before assigning status cohorts. (public source tables: components | public carried/output columns: entity_key, entity_name, user_id, last_modified_by | join preservation: left | condition public identifiers: components, last_modified_by, entity_key)
5. [filter] Keep rows whose status belongs to the passing cohort values ['installed']. (public carried/output columns: entity_key, entity_name | condition literal specification values: installed)
6. [distinct] One row per users entity that has at least one linked components row in the passing cohort, reporting the number of those rows, how many different status values occur among them, the total of their coordinate_x, and their largest coordinate_x. The total and the largest value read only the rows that carry a coordinate_x value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose status belongs to the failing cohort values ['pending', 'removed']. (public carried/output columns: entity_key, entity_name | condition literal specification values: pending, removed)
10. [distinct] One row per users entity that has at least one linked components row in the failing cohort, reporting the number of those rows, how many different status values occur among them, the total of their coordinate_x, and their largest coordinate_x. The total and the largest value read only the rows that carry a coordinate_x value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a users entity with no linked components row at all; a users entity that has linked components rows gets no placeholder, even when every one of those rows lacks a status value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per users entity with no linked components row at all, reporting 0 rows, 0 different status values, a coordinate_x total of 0 and a largest coordinate_x of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

## Source tables

### projects  (source backend: s3)
Source table projects of the 3d_coordinate_system_for_spatial_data_management scenario.

- `project_id`: integer NOT NULL — Unique identifier for each project
- `project_name`: text NULL — Name of the project
- `project_description`: text NULL — Description of the project
- `start_date`: text NULL — Start date of the project
- `end_date`: text NULL — End date of the project
- `project_manager_id`: integer NULL — Reference to the user who is the project manager
- `project_status`: text NULL — Current status of the project (e.g., active, completed, on hold) one of: active, completed, on hold.
- `budget`: text NULL — Total budget allocated for the project
- `location`: text NULL — Physical location of the project
- `client_id`: integer NULL — Reference to the client who commissioned the project
- primary key: project_id

### components  (source backend: rest)
Source table components of the 3d_coordinate_system_for_spatial_data_management scenario.

- `component_id`: text NOT NULL — Unique identifier for each component (UUID)
- `project_id`: integer NULL — Reference to the project the component belongs to
- `component_name`: text NULL — Name of the component
- `coordinate_x`: float NULL — X-coordinate of the component in meters
- `coordinate_y`: float NULL — Y-coordinate of the component in meters
- `coordinate_z`: float NULL — Z-coordinate of the component in meters
- `component_type_id`: integer NULL — Reference to the type of the component
- `installation_date`: text NULL — Date the component was installed
- `status`: text NULL — Status of the component (e.g., installed, pending, removed) one of: installed, pending, removed.
- `material`: text NULL — Material used for the component
- `manufacturer`: text NULL — Manufacturer of the component
- `serial_number`: text NULL — Unique serial number for the component
- `weight`: text NULL — Weight of the component in kilograms
- `dimensions`: text NULL — Dimensions of the component (e.g., '10x5x2')
- `notes`: text NULL — Additional notes or comments about the component
- `last_modified_by`: integer NULL — Reference to the user who last modified the component data
- `last_modified_date`: text NULL — Date when the component data was last modified
- primary key: component_id

### component_types  (source backend: files)
Source table component_types of the 3d_coordinate_system_for_spatial_data_management scenario.

- `component_type_id`: integer NOT NULL — Unique identifier for each component type
- `type_name`: text NULL — Name of the component type (e.g., structural, electrical, mechanical) one of: structural, electrical, mechanical.
- `description`: text NULL — Description of the component type
- `parent_type_id`: integer NULL — Reference to the parent component type (for hierarchical categorization)
- `is_active`: integer NULL — Indicates if the component type is active or not
- primary key: component_type_id

### users  (source backend: files)
Source table users of the 3d_coordinate_system_for_spatial_data_management scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `user_name`: text NULL — Full name of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., project manager, engineer, admin) one of: project manager, engineer, admin.
- `password`: text NULL — Hashed password for the user
- `phone_number`: text NULL — Contact phone number for the user
- `address`: text NULL — Address of the user
- `department`: text NULL — Department the user belongs to
- `is_active`: integer NULL — Indicates if the user account is active or not
- primary key: user_id

### access_logs  (source backend: files)
Source table access_logs of the 3d_coordinate_system_for_spatial_data_management scenario.

- `access_id`: integer NOT NULL — Unique identifier for each access event
- `component_id`: text NULL — ID of the component being accessed
- `user_id`: integer NULL — ID of the user accessing the component
- `access_date`: text NULL — Date when the component was accessed
- `access_type`: text NULL — Type of access (e.g., view, edit) one of: view, edit.
- `ip_address`: text NULL — IP address from which the access was made
- `user_agent`: text NULL — User agent string of the device used for access
- primary key: access_id

### component_versions  (source backend: s3)
Source table component_versions of the 3d_coordinate_system_for_spatial_data_management scenario.

- `version_id`: integer NOT NULL — Unique identifier for each version of the component
- `component_id`: text NULL — ID of the component
- `version_number`: integer NULL — Version number of the component
- `coordinate_x`: float NULL — X-coordinate of the component in meters
- `coordinate_y`: float NULL — Y-coordinate of the component in meters
- `coordinate_z`: float NULL — Z-coordinate of the component in meters
- `change_date`: text NULL — Date the component version was created
- `change_description`: text NULL — Description of the changes made in this version
- `changed_by`: integer NULL — Reference to the user who made the changes
- `approved_by`: integer NULL — Reference to the user who approved the changes (if applicable)
- `approval_date`: text NULL — Date when the changes were approved (if applicable)
- primary key: version_id

### Relationships

- access_logs(component_id) -> components(component_id) [optional (may be NULL/dangling)]
- access_logs(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- component_versions(approved_by) -> users(user_id) [optional (may be NULL/dangling)]
- component_versions(changed_by) -> users(user_id) [optional (may be NULL/dangling)]
- component_versions(component_id) -> components(component_id) [optional (may be NULL/dangling)]
- components(component_type_id) -> component_types(component_type_id) [optional (may be NULL/dangling)]
- components(last_modified_by) -> users(user_id) [optional (may be NULL/dangling)]
- components(project_id) -> projects(project_id) [optional (may be NULL/dangling)]
- projects(project_manager_id) -> users(user_id) [optional (may be NULL/dangling)]

