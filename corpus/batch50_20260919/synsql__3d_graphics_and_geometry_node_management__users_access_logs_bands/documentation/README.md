# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# 3D Graphics And Geometry Node Management

## Specification

PROJECT OVERVIEW: 3D Graphics And Geometry Node Management

This project builds two analytical marts over the 3d_graphics_and_geometry_node_management scenario. Seven source tables feed the work, and each must be extracted from the backend named here.

Source tables and their extraction backends:
- Source table nodes must be extracted from the postgres backend. It holds one row per node, identified by node_id (its primary key), with node_type_id, name, description, version, created_at, updated_at and status (whose declared values are active and deprecated).
- Source table node_types must be extracted from the s3 backend. It holds one row per node type, identified by node_type_id (its primary key), with node_type_name (declared values vertex attribute, color, coordinate), description, created_at, updated_at and category (declared values basic, advanced).
- Source table vertex_attributes must be extracted from the mongodb backend. It holds one row per vertex attribute, identified by vertex_attribute_id (its primary key), with node_id, attrib, ccw, color, created_at, updated_at, version and metadata.
- Source table colors must be extracted from the files backend. It holds one row per color, identified by color_id (its primary key), with node_id, color, color_per_vertex, created_at, updated_at, version and metadata.
- Source table coordinates must be extracted from the s3 backend. It holds one row per coordinate, identified by coordinate_id (its primary key), with node_id, coord, created_at, updated_at, version and metadata.
- Source table users must be extracted from the s3 backend. It holds one row per user, identified by user_id (its primary key), with user_name, email, role (declared values developer, designer, administrator), password_hash, created_at, updated_at and status (declared values active, suspended).
- Source table access_logs must be extracted from the s3 backend. It holds one row per access event, identified by access_id (its primary key), with node_id, user_id, access_date, access_type (declared values view, edit), created_at, ip_address and user_agent.

Relationships between the source tables. Each is stated with its child table and child key, its parent table and parent key, and whether it is required or optional.
- Child table access_logs through its column node_id refers to parent table nodes through its column node_id; this relationship is optional, so the child value may be NULL or dangling.
- Child table access_logs through its column user_id refers to parent table users through its column user_id; this relationship is optional, so the child value may be NULL or dangling.
- Child table colors through its column node_id refers to parent table nodes through its column node_id; this relationship is optional, so the child value may be NULL or dangling.
- Child table coordinates through its column node_id refers to parent table nodes through its column node_id; this relationship is optional, so the child value may be NULL or dangling.
- Child table nodes through its column node_type_id refers to parent table node_types through its column node_type_id; this relationship is optional, so the child value may be NULL or dangling.
- Child table vertex_attributes through its column node_id refers to parent table nodes through its column node_id; this relationship is optional, so the child value may be NULL or dangling.

Conventions used throughout: text timestamps are read as they are stored; every ratio is a fraction between 0 and 1, never a percentage, and is rounded to 4 decimal places; every count described below reports 0 rather than nothing when there is nothing to count.

=== Mart users_access_logs_bands: Per-users banding of linked access_logs activity in the 3d_graphics_and_geometry_node_management scenario, over the value domain the source schema itself declares ===

Grain: one row per users row (user_id), INCLUDING users rows with no linked access_logs rows. The key column of this mart is parent_key.

Rule 1: the users source table is read in full, and every users row is available to this mart.

Rule 2: the access_logs source table is read in full, and every access_logs row is available to this mart.

Rule 3: from source table users, there is one row per users row, keyed by user_id, and that row carries parent_key, parent_name and parent_status.

Rule 4: the access_logs rows are brought in against the users rows in a single hop (hop 1 of 1), matching the access_logs column user_id to parent_key, and the carried column from access_logs is user_id; preservation is left-sided, so a users row with no matching access_logs row is RETAINED and reports the declared defaults below.

Rule 5: there is one output row per parent_key, carrying parent_name and parent_status beside the keys — a parent_key value identifies one source row for those carried columns, so they take one value per key and never split the row's group — and that row reports link_count, passing_count, failing_count and distinct_status_count over the access_logs rows matching it. A parent_key with no qualifying rows still appears, reporting 0; a retained users row with no matching access_logs row has nothing to count, so its counts are 0, never 1.

Rule 6: the mart columns are named parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count.

Column meanings for this mart:
- parent_key is the identifier of the users row; there is one row per value.
- parent_name is the user_name of the users row, copied unchanged.
- parent_status is the role of the users row, copied unchanged; its declared domain is 'developer', 'designer', 'administrator'.
- link_count is the number of access_logs rows for this users row, and 0 when there are none. Every linked access_logs row counts, whatever its access_type value. A users row kept with no access_logs row reports 0 here, never 1: its placeholder holds no access_logs row to count.
- passing_count is, of those rows, how many have access_type in ['view']; it is 0, never missing, when none do.
- failing_count is how many of those rows have access_type in ['edit']; it is 0 when none do.
- distinct_status_count is how many distinct access_type values occur among those rows.
- passing_ratio is passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when there are no links.
- adoption_band is the band of passing_ratio, described in Rule 8.
- status_group is the mapped value of parent_status, described in Rule 9.

Rule 7: the guarded ratio passing_ratio is carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count, and it is passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places; the guard substitutes the result 0.0 whenever the denominator is 0 or NULL, that is, whenever there are no links.

Rule 8: adoption_band is carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count and passing_ratio, and it is the band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), otherwise 'low' below 0.5, never null and never blank. Boundaries are inclusive of the HIGHER band, so a value exactly at 0.8 is 'high' and a value exactly at 0.5 is 'medium'; the declared domain of the underlying parent_status values is developer, designer, administrator.

Rule 9: status_group is carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio and adoption_band, and it is parent_status mapped value by value: 'developer' becomes 'active'; 'designer' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. It is never NULL; this is a categorical mapping with no numeric boundary, over the domain developer, designer, administrator.

Rule 10: the output order is deterministic — rows appear in ascending parent_key order.

=== Mart nodes_vertex_attributes_distribution: Per-(nodes, measure state) distribution of linked vertex_attributes rows in the 3d_graphics_and_geometry_node_management scenario ===

Grain: one row per (node_id, measure state) pair represented among linked vertex_attributes rows; the absent state includes missing ccw values and a no-activity row for a nodes row with no links. A linked vertex_attributes row whose ccw has a value belongs only to the present state and never to the absent state. The key columns of this mart are entity_key and measure_state.

Rule 1: the nodes source table is read in full, and every nodes row is available to this mart.

Rule 2: the vertex_attributes source table is read in full, and every vertex_attributes row is available to this mart.

Rule 3: from source table nodes, each node_id and its name are carried into the measure-state calculation as entity_key and entity_name.

Rule 4: the linked vertex_attributes rows are brought into each nodes entity, matching the vertex_attributes column node_id to entity_key, carrying entity_key, entity_name and node_id; preservation is left-sided, so an entity with no linked row is retained and its absent state stays visible.

Rule 5: the present measure-state rows kept are those where a real vertex_attributes row has a ccw that has a value; these rows carry entity_key and entity_name.

Rule 6: there is one row per nodes entity that has at least one row in the present measure state, and no row here for an entity with none; each such row carries entity_key and entity_name and reports row_count as its row count, distinct_amount_count as how many different non-missing ccw values occur (each different value counted once, however many rows repeat it), total_amount as the total ccw, and max_amount as the largest ccw.

Rule 7: for these present-state rows, max_amount_share is carried beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, and it is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, with the result 0.0 when total_amount is 0.

Rule 8: these measures are labelled as the present measure state, so measure_state reads 'present' on the row carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9: the absent measure-state rows kept are those where ccw is missing, including the retained placeholder for a nodes row with no vertex_attributes rows; these rows carry entity_key and entity_name. A real vertex_attributes row whose ccw has a value belongs only to the present state and never to this absent state.

Rule 10: there is one row per nodes entity that has at least one row in the absent measure state, and no row here for an entity with none; each such row carries entity_key and entity_name and reports row_count as its row count, distinct_amount_count as how many different non-missing ccw values occur (each different value counted once, however many rows repeat it), total_amount as the total ccw, and max_amount as the largest ccw.

Rule 11: for these absent-state rows, max_amount_share is carried beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, and it is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, with the result 0.0 when total_amount is 0.

Rule 12: these measures are labelled as the absent measure state, so measure_state reads 'absent' on the row carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13: the present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share, keeping all rows from both: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14: the output order is deterministic — rows appear in ascending entity_key order and, for one entity, in ascending measure_state order.

Column meanings for this mart:
- entity_key is the identifier of the nodes row.
- measure_state is 'present' for a linked vertex_attributes row whose ccw has a value, and 'absent' when ccw is missing, including a nodes row with no linked vertex_attributes row. A linked vertex_attributes row whose ccw has a value belongs only to the present state and never to the absent state.
- entity_name is the name of the nodes row, copied unchanged.
- row_count is the number of linked vertex_attributes rows in this entity/state cell; an absent cell holding real vertex_attributes rows whose ccw is missing COUNTS those rows, and only the placeholder cell of a nodes row with no linked vertex_attributes row at all reports 0.
- distinct_amount_count is the number of unique non-missing ccw values in this cell; each unique non-missing value is counted once, however many rows repeat it; it is 0 whenever the cell holds no ccw value at all — both for a nodes row with no linked vertex_attributes row and for an absent cell whose rows all have a missing ccw.
- total_amount is the sum of ccw in this cell; it is 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries a ccw value.
- max_amount is the largest ccw in this cell; it is 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries a ccw value.
- max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `users_access_logs_bands`

- Grain: One row per users (user_id), INCLUDING users rows with no linked access_logs rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group

```text
Mart 'users_access_logs_bands' has 10 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table access_logs. (public source tables: access_logs)
3. [derive] One row per users row, keyed by user_id. (public source tables: users | public carried/output columns: parent_key, parent_name, parent_status)
4. [join] Bring in access_logs (hop 1 of 1): rows with no matching access_logs row are RETAINED and report the declared defaults. (public source tables: access_logs | public carried/output columns: user_id | join preservation: left | condition public identifiers: access_logs, user_id, parent_key)
5. [filtered_aggregate] One output row per parent_key, carrying parent_name, parent_status beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, passing_count, failing_count, distinct_status_count for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
6. [derive] Name the mart columns. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
7. [ratio] Guarded ratios: passing_ratio — passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
8. [conditional] adoption_band — Band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the HIGHER band. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band | semantic parameters: boundary=a value exactly at 0.8 is 'high'; a value exactly at 0.5 is 'medium'; domain=developer, designer, administrator)
9. [conditional] status_group — parent_status mapped value by value: 'developer' becomes 'active'; 'designer' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never NULL. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group | semantic parameters: boundary=categorical mapping; no numeric boundary; domain=developer, designer, administrator)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `nodes_vertex_attributes_distribution`

- Grain: One row per (node_id, measure state) pair represented among linked vertex_attributes rows; the absent state includes missing ccw values and a no-activity row for a nodes row with no links. A linked vertex_attributes row whose ccw has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'nodes_vertex_attributes_distribution' has 14 declared semantic rules:
1. [source] Read source table nodes. (public source tables: nodes)
2. [source] Read source table vertex_attributes. (public source tables: vertex_attributes)
3. [derive] Carry each node_id and its name into the measure-state calculation. (public source tables: nodes | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked vertex_attributes rows into each nodes entity; retain an entity with no linked row so its absent state is visible. (public source tables: vertex_attributes | public carried/output columns: entity_key, entity_name, node_id | join preservation: left | condition public identifiers: vertex_attributes, node_id, entity_key)
5. [filter] Keep the present measure-state rows: a real vertex_attributes row whose ccw has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per nodes entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing ccw values occur (each different value counted once, however many rows repeat it), total ccw, and largest ccw. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: ccw is missing, including the retained placeholder for a nodes row with no vertex_attributes rows. A real vertex_attributes row whose ccw has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per nodes entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing ccw values occur (each different value counted once, however many rows repeat it), total ccw, and largest ccw. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### nodes  (source backend: postgres)
Source table nodes of the 3d_graphics_and_geometry_node_management scenario.

- `node_id`: integer NOT NULL — Unique identifier for each node
- `node_type_id`: integer NULL — Reference to the type of node (e.g., vertex attribute, color, coordinate)
- `name`: text NULL — Name of the node
- `description`: text NULL — Description of the node
- `version`: text NULL — Version of the node
- `created_at`: text NULL — Timestamp for when the node was created
- `updated_at`: text NULL — Timestamp for the last update to the node
- `status`: text NULL — Status of the node (e.g., active, deprecated) one of: active, deprecated.
- primary key: node_id

### node_types  (source backend: s3)
Source table node_types of the 3d_graphics_and_geometry_node_management scenario.

- `node_type_id`: integer NOT NULL — Unique identifier for each node type
- `node_type_name`: text NULL — Name of the node type (e.g., vertex attribute, color, coordinate) one of: vertex attribute, color, coordinate.
- `description`: text NULL — Description of the node type
- `created_at`: text NULL — Timestamp for when the node type was created
- `updated_at`: text NULL — Timestamp for the last update to the node type
- `category`: text NULL — Category of the node type (e.g., basic, advanced) one of: basic, advanced.
- primary key: node_type_id

### vertex_attributes  (source backend: mongodb)
Source table vertex_attributes of the 3d_graphics_and_geometry_node_management scenario.

- `vertex_attribute_id`: integer NOT NULL — Unique identifier for each vertex attribute
- `node_id`: integer NULL — Reference to the node that the vertex attribute belongs to
- `attrib`: text NULL — Vertex attribute information (e.g., X3DVertexAttributeNode)
- `ccw`: integer NULL — Clockwise (ccw) value for the vertex attribute
- `color`: text NULL — Color information for the vertex attribute
- `created_at`: text NULL — Timestamp for when the vertex attribute was created
- `updated_at`: text NULL — Timestamp for the last update to the vertex attribute
- `version`: text NULL — Version of the vertex attribute
- `metadata`: text NULL — Additional metadata for the vertex attribute (e.g., JSON format)
- primary key: vertex_attribute_id

### colors  (source backend: files)
Source table colors of the 3d_graphics_and_geometry_node_management scenario.

- `color_id`: integer NOT NULL — Unique identifier for each color
- `node_id`: integer NULL — Reference to the node that the color belongs to
- `color`: text NULL — Color information (e.g., X3DColorNode)
- `color_per_vertex`: integer NULL — Color per vertex value (true or false)
- `created_at`: text NULL — Timestamp for when the color was created
- `updated_at`: text NULL — Timestamp for the last update to the color
- `version`: text NULL — Version of the color
- `metadata`: text NULL — Additional metadata for the color (e.g., JSON format)
- primary key: color_id

### coordinates  (source backend: s3)
Source table coordinates of the 3d_graphics_and_geometry_node_management scenario.

- `coordinate_id`: integer NOT NULL — Unique identifier for each coordinate
- `node_id`: integer NULL — Reference to the node that the coordinate belongs to
- `coord`: text NULL — Coordinate information (e.g., X3DCoordinateNode)
- `created_at`: text NULL — Timestamp for when the coordinate was created
- `updated_at`: text NULL — Timestamp for the last update to the coordinate
- `version`: text NULL — Version of the coordinate
- `metadata`: text NULL — Additional metadata for the coordinate (e.g., JSON format)
- primary key: coordinate_id

### users  (source backend: s3)
Source table users of the 3d_graphics_and_geometry_node_management scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `user_name`: text NULL — Full name of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., developer, designer, administrator) one of: developer, designer, administrator.
- `password_hash`: text NULL — Hashed password for the user
- `created_at`: text NULL — Timestamp for when the user was created
- `updated_at`: text NULL — Timestamp for the last update to the user
- `status`: text NULL — Status of the user (e.g., active, suspended) one of: active, suspended.
- primary key: user_id

### access_logs  (source backend: s3)
Source table access_logs of the 3d_graphics_and_geometry_node_management scenario.

- `access_id`: integer NOT NULL — Unique identifier for each access event
- `node_id`: integer NULL — Reference to the node being accessed
- `user_id`: integer NULL — Reference to the user accessing the node
- `access_date`: text NULL — Date when the node was accessed
- `access_type`: text NULL — Type of access (e.g., view, edit) one of: view, edit.
- `created_at`: text NULL — Timestamp for when the access log entry was created
- `ip_address`: text NULL — IP address of the user accessing the node
- `user_agent`: text NULL — User agent string of the client accessing the node
- primary key: access_id

### Relationships

- access_logs(node_id) -> nodes(node_id) [optional (may be NULL/dangling)]
- access_logs(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- colors(node_id) -> nodes(node_id) [optional (may be NULL/dangling)]
- coordinates(node_id) -> nodes(node_id) [optional (may be NULL/dangling)]
- nodes(node_type_id) -> node_types(node_type_id) [optional (may be NULL/dangling)]
- vertex_attributes(node_id) -> nodes(node_id) [optional (may be NULL/dangling)]

