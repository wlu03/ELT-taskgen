# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# 3D Object Modeling And Rotation Analysis

## Specification

PROJECT OVERVIEW: 3D Object Modeling And Rotation Analysis

This project builds two analytical marts from the 3d_object_modeling_and_rotation_analysis scenario. The prose below is the complete statement of the intended semantics; a competent data engineer with these sentences and the public schemas has everything needed to produce exactly the intended output.

SOURCE TABLES AND THEIR EXTRACTION BACKENDS

Each source table must be extracted from the backend named beside it here.

- Source table objects must be extracted from the postgres backend. It holds one row per object_id, with x, y, z, rotations, copies, type_id, created_at and updated_at.
- Source table object_types must be extracted from the files backend. It holds one row per type_id, with type_name and description.
- Source table object_versions must be extracted from the s3 backend. It holds one row per version_id, with object_id, version_name, description and created_at.
- Source table users must be extracted from the mongodb backend. It holds one row per user_id, with user_name, email, role (one of designer, analyst, admin), password, created_at and updated_at.
- Source table object_access must be extracted from the rest backend. It holds one row per access_id, with object_id, user_id, access_date and access_type (one of view, edit).
- Source table design_teams must be extracted from the s3 backend. It holds one row per team_id, with team_name, description, leader_id, created_at and updated_at.
- Source table team_members must be extracted from the mongodb backend. It holds one row per member_id, with team_id, user_id, role (one of lead designer, designer), joined_at and left_at.
- Source table object_collaborations must be extracted from the rest backend. It holds one row per collaboration_id, with object_id, team_id, collaboration_date and status (one of active, inactive).
- Source table reports must be extracted from the s3 backend. It holds one row per report_id, with report_name, description and generated_at.
- Source table report_details must be extracted from the postgres backend. It holds one row per detail_id, with report_id, object_id, user_id and access_date.
- Source table object_tags must be extracted from the rest backend. It holds one row per tag_id, with object_id, tag_name and created_at.
- Source table object_comments must be extracted from the files backend. It holds one row per comment_id, with object_id, user_id, comment and created_at.
- Source table object_likes must be extracted from the s3 backend. It holds one row per like_id, with object_id, user_id and created_at.
- Source table object_downloads must be extracted from the files backend. It holds one row per download_id, with object_id, user_id and download_date.

RELATIONSHIPS BETWEEN THE SOURCE TABLES

Every relationship below is optional, meaning the child value may be NULL or may point at a parent row that does not exist.

- Child table design_teams with key leader_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).
- Child table object_access with key object_id refers to parent table objects with key object_id; this relationship is optional (may be NULL or dangling).
- Child table object_access with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).
- Child table object_collaborations with key object_id refers to parent table objects with key object_id; this relationship is optional (may be NULL or dangling).
- Child table object_collaborations with key team_id refers to parent table design_teams with key team_id; this relationship is optional (may be NULL or dangling).
- Child table object_comments with key object_id refers to parent table objects with key object_id; this relationship is optional (may be NULL or dangling).
- Child table object_comments with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).
- Child table object_downloads with key object_id refers to parent table objects with key object_id; this relationship is optional (may be NULL or dangling).
- Child table object_downloads with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).
- Child table object_likes with key object_id refers to parent table objects with key object_id; this relationship is optional (may be NULL or dangling).
- Child table object_likes with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).
- Child table object_tags with key object_id refers to parent table objects with key object_id; this relationship is optional (may be NULL or dangling).
- Child table object_versions with key object_id refers to parent table objects with key object_id; this relationship is optional (may be NULL or dangling).
- Child table objects with key type_id refers to parent table object_types with key type_id; this relationship is optional (may be NULL or dangling).
- Child table report_details with key object_id refers to parent table objects with key object_id; this relationship is optional (may be NULL or dangling).
- Child table report_details with key report_id refers to parent table reports with key report_id; this relationship is optional (may be NULL or dangling).
- Child table report_details with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).
- Child table team_members with key team_id refers to parent table design_teams with key team_id; this relationship is optional (may be NULL or dangling).
- Child table team_members with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).

==================================================================
MART 1 — users_team_members_bands: Per-users banding of linked team_members activity in the 3d_object_modeling_and_rotation_analysis scenario, over the value domain the source schema itself declares.
==================================================================

Grain: one row per users (user_id), INCLUDING users rows with no linked team_members rows.

Key column: parent_key.

Output columns of users_team_members_bands

- parent_key (integer): identifier of the users row. One row per value.
- parent_name (text): user_name of the users row, copied unchanged.
- parent_status (text): role of the users row, copied unchanged. Declared domain: 'designer', 'analyst', 'admin'.
- link_count (bigint): number of team_members rows for this users row; 0 when there are none. Every linked team_members row counts, whatever its role value. An users row kept with no team_members row reports 0 here, never 1: its placeholder holds no team_members row to count.
- passing_count (bigint): of those, how many have role in ['lead designer']. 0, never missing, when none do.
- failing_count (bigint): how many have role in ['designer']. 0 when none do.
- distinct_status_count (bigint): how many different role values occur among them, each different value counted once.
- passing_ratio (float): passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links.
- adoption_band (text): band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the HIGHER band.
- status_group (text): parent_status mapped value by value: 'designer' becomes 'active'; 'analyst' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never NULL.

How users_team_members_bands is built

Rule 1. Read source table users, the mongodb-backed table of user_id rows, as an input of this mart.

Rule 2. Read source table team_members, the mongodb-backed table of member_id rows, as the other input of this mart.

Rule 3. There is one row per users row, keyed by user_id, and that row carries parent_key, parent_name and parent_status: parent_key is the users user_id, parent_name is the users user_name, parent_status is the users role.

Rule 4. Bring in team_members (hop 1 of 1) by matching the team_members user_id to parent_key, carrying the team_members user_id alongside: preservation is left-sided, so users rows with no matching team_members row are RETAINED and report the declared defaults.

Rule 5. There is one output row per parent_key, carrying parent_name and parent_status beside the keys: a key value identifies one source row for those carried columns, so they take one value per key and never split the rows of a key, and that row reports link_count, passing_count, failing_count and distinct_status_count over that row's matching team_members rows. A parent_key with no qualifying rows still appears, reporting 0; a retained users row with no matching team_members row has nothing to count, so its counts are 0, never 1.

Rule 6. The mart columns are named parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count, carrying those seven values unchanged from the previous step.

Rule 7. Guarded ratios, carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count: passing_ratio is passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and it is 0.0 when there are no links, that is when the denominator is 0 or NULL.

Rule 8. adoption_band is carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count and passing_ratio, and it is the band of passing_ratio decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (a value exactly at 0.8 is 'high'), 'medium' from 0.5 up to but not including 0.8 (a value exactly at 0.5 is 'medium'), otherwise 'low' below 0.5; boundaries are inclusive of the HIGHER band, and the value is never null or blank. The declared domain of the underlying role values is designer, analyst, admin.

Rule 9. status_group is carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio and adoption_band, and it is parent_status mapped value by value: 'designer' becomes 'active'; 'analyst' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — otherwise becomes 'unmapped'. It is never NULL; this is a categorical mapping with no numeric boundary, over the declared domain designer, analyst, admin.

Rule 10. Deterministic output order: rows appear sorted in ascending parent_key order.

==================================================================
MART 2 — object_types_objects_distribution: Per-(object_types, measure state) distribution of linked objects rows in the 3d_object_modeling_and_rotation_analysis scenario.
==================================================================

Grain: one row per (type_id, measure state) pair represented among linked objects rows; the absent state includes missing x values and a no-activity row for a object_types row with no links. A linked objects row whose x has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state.

Output columns of object_types_objects_distribution

- entity_key (integer): identifier of the object_types row.
- measure_state (text): 'present' for a linked objects row whose x has a value; 'absent' when x is missing, including a object_types row with no linked objects row. A linked objects row whose x has a value belongs only to the present state and never to the absent state.
- entity_name (text): type_name of the object_types row, copied unchanged.
- row_count (bigint): number of linked objects rows in this entity/state cell; an absent cell holding real objects rows whose x is missing COUNTS those rows, and only the placeholder cell of an object_types row with no linked objects row at all reports 0.
- distinct_amount_count (bigint): number of unique non-missing x values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no x value at all — both for an object_types row with no linked objects row and for an absent cell whose rows all have a missing x.
- total_amount (integer): total of x in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an x value.
- max_amount (integer): largest x in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an x value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

How object_types_objects_distribution is built

Rule 1. Read source table object_types, the files-backed table of type_id rows, as an input of this mart.

Rule 2. Read source table objects, the postgres-backed table of object_id rows, as the other input of this mart.

Rule 3. Each object_types type_id and its type_name are carried into the measure-state calculation as entity_key and entity_name respectively.

Rule 4. The linked objects rows are brought into each object_types entity by matching the objects type_id to entity_key, carrying entity_key, entity_name and the objects type_id: preservation is left-sided, so an entity with no linked objects row is retained and its absent state stays visible.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are the kept rows that are a real objects row whose x has a value.

Rule 6. For the present measure state there is one row per object_types entity that has at least one row in that state, and no row here for an entity with none, and it reports row_count as the number of those rows, distinct_amount_count as how many different non-missing x values occur among them (each different value counted once, however many rows repeat it), total_amount as the total x, and max_amount as the largest x, beside entity_key and entity_name.

Rule 7. For those present-state rows, carried beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state 'present', the present measure state.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are the kept rows whose x is missing, including the retained placeholder for a object_types row with no objects rows. A real objects row whose x has a value belongs only to the present state and never to this absent state.

Rule 10. For the absent measure state there is one row per object_types entity that has at least one row in that state, and no row here for an entity with none, and it reports row_count as the number of those rows, distinct_amount_count as how many different non-missing x values occur among them (each different value counted once, however many rows repeat it), total_amount as the total x, and max_amount as the largest x, beside entity_key and entity_name.

Rule 11. For those absent-state rows, carried beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state 'absent', the absent measure state.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, all rows kept, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. Deterministic output order: rows appear sorted in ascending entity_key order, then in ascending measure_state order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `users_team_members_bands`

- Grain: One row per users (user_id), INCLUDING users rows with no linked team_members rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group

```text
Mart 'users_team_members_bands' has 10 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table team_members. (public source tables: team_members)
3. [derive] One row per users row, keyed by user_id. (public source tables: users | public carried/output columns: parent_key, parent_name, parent_status)
4. [join] Bring in team_members (hop 1 of 1): rows with no matching team_members row are RETAINED and report the declared defaults. (public source tables: team_members | public carried/output columns: user_id | join preservation: left | condition public identifiers: team_members, user_id, parent_key)
5. [filtered_aggregate] One output row per parent_key, carrying parent_name, parent_status beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, passing_count, failing_count, distinct_status_count for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
6. [derive] Name the mart columns. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
7. [ratio] Guarded ratios: passing_ratio — passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
8. [conditional] adoption_band — Band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the HIGHER band. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band | semantic parameters: boundary=a value exactly at 0.8 is 'high'; a value exactly at 0.5 is 'medium'; domain=designer, analyst, admin)
9. [conditional] status_group — parent_status mapped value by value: 'designer' becomes 'active'; 'analyst' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never NULL. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group | semantic parameters: boundary=categorical mapping; no numeric boundary; domain=designer, analyst, admin)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `object_types_objects_distribution`

- Grain: One row per (type_id, measure state) pair represented among linked objects rows; the absent state includes missing x values and a no-activity row for a object_types row with no links. A linked objects row whose x has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'object_types_objects_distribution' has 14 declared semantic rules:
1. [source] Read source table object_types. (public source tables: object_types)
2. [source] Read source table objects. (public source tables: objects)
3. [derive] Carry each type_id and its type_name into the measure-state calculation. (public source tables: object_types | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked objects rows into each object_types entity; retain an entity with no linked row so its absent state is visible. (public source tables: objects | public carried/output columns: entity_key, entity_name, type_id | join preservation: left | condition public identifiers: objects, type_id, entity_key)
5. [filter] Keep the present measure-state rows: a real objects row whose x has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per object_types entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing x values occur (each different value counted once, however many rows repeat it), total x, and largest x. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: x is missing, including the retained placeholder for a object_types row with no objects rows. A real objects row whose x has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per object_types entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing x values occur (each different value counted once, however many rows repeat it), total x, and largest x. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### objects  (source backend: postgres)
Source table objects of the 3d_object_modeling_and_rotation_analysis scenario.

- `object_id`: integer NOT NULL — Unique identifier for each object
- `x`: integer NULL — X-coordinate of the object
- `y`: integer NULL — Y-coordinate of the object
- `z`: integer NULL — Z-coordinate of the object
- `rotations`: integer NULL — Rotation angle of the object
- `copies`: integer NULL — Number of copies of the object
- `type_id`: integer NULL — ID of the object type
- `created_at`: text NULL — Date and time the object was created
- `updated_at`: text NULL — Date and time the object was last updated
- primary key: object_id

### object_types  (source backend: files)
Source table object_types of the 3d_object_modeling_and_rotation_analysis scenario.

- `type_id`: integer NOT NULL — Unique identifier for each object type
- `type_name`: text NULL — Name of the object type
- `description`: text NULL — Description of the object type
- primary key: type_id

### object_versions  (source backend: s3)
Source table object_versions of the 3d_object_modeling_and_rotation_analysis scenario.

- `version_id`: integer NOT NULL — Unique identifier for each object version
- `object_id`: integer NULL — ID of the object the version belongs to
- `version_name`: text NULL — Name of the version
- `description`: text NULL — Description of the version
- `created_at`: text NULL — Date and time the version was created
- primary key: version_id

### users  (source backend: mongodb)
Source table users of the 3d_object_modeling_and_rotation_analysis scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `user_name`: text NULL — Full name of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., designer, analyst, admin) one of: designer, analyst, admin.
- `password`: text NULL — Password for the user
- `created_at`: text NULL — Date and time the user was created
- `updated_at`: text NULL — Date and time the user was last updated
- primary key: user_id

### object_access  (source backend: rest)
Source table object_access of the 3d_object_modeling_and_rotation_analysis scenario.

- `access_id`: integer NOT NULL — Unique identifier for each access event
- `object_id`: integer NULL — ID of the object being accessed
- `user_id`: integer NULL — ID of the user accessing the object
- `access_date`: text NULL — Date when the object was accessed
- `access_type`: text NULL — Type of access (e.g., view, edit) one of: view, edit.
- primary key: access_id

### design_teams  (source backend: s3)
Source table design_teams of the 3d_object_modeling_and_rotation_analysis scenario.

- `team_id`: integer NOT NULL — Unique identifier for each design team
- `team_name`: text NULL — Name of the design team
- `description`: text NULL — Description of the design team
- `leader_id`: integer NULL — ID of the team leader
- `created_at`: text NULL — Date and time the team was created
- `updated_at`: text NULL — Date and time the team was last updated
- primary key: team_id

### team_members  (source backend: mongodb)
Source table team_members of the 3d_object_modeling_and_rotation_analysis scenario.

- `member_id`: integer NOT NULL — Unique identifier for each team member
- `team_id`: integer NULL — ID of the team the member belongs to
- `user_id`: integer NULL — ID of the user who is a team member
- `role`: text NULL — Role of the team member (e.g., lead designer, designer) one of: lead designer, designer.
- `joined_at`: text NULL — Date and time the member joined the team
- `left_at`: text NULL — Date and time the member left the team
- primary key: member_id

### object_collaborations  (source backend: rest)
Source table object_collaborations of the 3d_object_modeling_and_rotation_analysis scenario.

- `collaboration_id`: integer NOT NULL — Unique identifier for each collaboration
- `object_id`: integer NULL — ID of the object being collaborated on
- `team_id`: integer NULL — ID of the team collaborating on the object
- `collaboration_date`: text NULL — Date when the collaboration started
- `status`: text NULL — Status of the collaboration (e.g., active, inactive) one of: active, inactive.
- primary key: collaboration_id

### reports  (source backend: s3)
Source table reports of the 3d_object_modeling_and_rotation_analysis scenario.

- `report_id`: integer NOT NULL — Unique identifier for each report
- `report_name`: text NULL — Name of the report
- `description`: text NULL — Description of the report
- `generated_at`: text NULL — Date and time the report was generated
- primary key: report_id

### report_details  (source backend: postgres)
Source table report_details of the 3d_object_modeling_and_rotation_analysis scenario.

- `detail_id`: integer NOT NULL — Unique identifier for each report detail
- `report_id`: integer NULL — ID of the report the detail belongs to
- `object_id`: integer NULL — ID of the object being reported on
- `user_id`: integer NULL — ID of the user who accessed the object
- `access_date`: text NULL — Date when the object was accessed
- primary key: detail_id

### object_tags  (source backend: rest)
Source table object_tags of the 3d_object_modeling_and_rotation_analysis scenario.

- `tag_id`: integer NOT NULL — Unique identifier for each tag
- `object_id`: integer NULL — ID of the object the tag is assigned to
- `tag_name`: text NULL — Name of the tag
- `created_at`: text NULL — Date and time the tag was created
- primary key: tag_id

### object_comments  (source backend: files)
Source table object_comments of the 3d_object_modeling_and_rotation_analysis scenario.

- `comment_id`: integer NOT NULL — Unique identifier for each comment
- `object_id`: integer NULL — ID of the object the comment is made on
- `user_id`: integer NULL — ID of the user who made the comment
- `comment`: text NULL — Text of the comment
- `created_at`: text NULL — Date and time the comment was made
- primary key: comment_id

### object_likes  (source backend: s3)
Source table object_likes of the 3d_object_modeling_and_rotation_analysis scenario.

- `like_id`: integer NOT NULL — Unique identifier for each like
- `object_id`: integer NULL — ID of the object the like is given to
- `user_id`: integer NULL — ID of the user who gave the like
- `created_at`: text NULL — Date and time the like was given
- primary key: like_id

### object_downloads  (source backend: files)
Source table object_downloads of the 3d_object_modeling_and_rotation_analysis scenario.

- `download_id`: integer NOT NULL — Unique identifier for each download
- `object_id`: integer NULL — ID of the object being downloaded
- `user_id`: integer NULL — ID of the user who downloaded the object
- `download_date`: text NULL — Date when the object was downloaded
- primary key: download_id

### Relationships

- design_teams(leader_id) -> users(user_id) [optional (may be NULL/dangling)]
- object_access(object_id) -> objects(object_id) [optional (may be NULL/dangling)]
- object_access(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- object_collaborations(object_id) -> objects(object_id) [optional (may be NULL/dangling)]
- object_collaborations(team_id) -> design_teams(team_id) [optional (may be NULL/dangling)]
- object_comments(object_id) -> objects(object_id) [optional (may be NULL/dangling)]
- object_comments(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- object_downloads(object_id) -> objects(object_id) [optional (may be NULL/dangling)]
- object_downloads(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- object_likes(object_id) -> objects(object_id) [optional (may be NULL/dangling)]
- object_likes(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- object_tags(object_id) -> objects(object_id) [optional (may be NULL/dangling)]
- object_versions(object_id) -> objects(object_id) [optional (may be NULL/dangling)]
- objects(type_id) -> object_types(type_id) [optional (may be NULL/dangling)]
- report_details(object_id) -> objects(object_id) [optional (may be NULL/dangling)]
- report_details(report_id) -> reports(report_id) [optional (may be NULL/dangling)]
- report_details(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- team_members(team_id) -> design_teams(team_id) [optional (may be NULL/dangling)]
- team_members(user_id) -> users(user_id) [optional (may be NULL/dangling)]

