# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# 3D Object Positioning And Animation Data

## Specification

PROJECT OVERVIEW

This project builds two analytical marts over the 3d_object_positioning_and_animation_data scenario, which records 3D scenes, the objects placed inside them, the people who work on them, and what those people do.

Six source tables feed the work, and each must be read from its own extraction backend:

- Source table scenes must be extracted from the s3 backend. It holds one row per scene, identified by scene_id, with scene_name, description, created_at, updated_at, scene_type (indoor, outdoor or action), status (draft, in progress or completed), created_by, updated_by and is_active.
- Source table scene_objects must be extracted from the postgres backend. It holds one row per scene-object relationship, identified by scene_object_id, with scene_id, object_id, object_role (main character or background), start_time, end_time, status (active or inactive), created_by, updated_by and is_active.
- Source table users must be extracted from the postgres backend. It holds one row per user, identified by user_id, with user_name, email, role (animator, modeler or admin), password_hash, created_at, updated_at, is_active and last_login.
- Source table user_actions must be extracted from the postgres backend. It holds one row per recorded user action, identified by action_id, with user_id, action_type (create, update or delete), action_details, action_time, scene_id, object_id, ip_address and user_agent.
- Source table animation_types must be extracted from the postgres backend. It holds one row per animation type, identified by type_id, with type_name, description, created_at, created_by, updated_at, updated_by and is_active.
- Source table object_versions must be extracted from the s3 backend. It holds one row per object version, identified by version_id, with object_id, version_number, version_details, versioned_at, version_type (major, minor or patch), created_by, ip_address and user_agent.

Relationships between the tables, each stated with its child side and parent side exactly as the source schema declares it:

- Child table animation_types through its created_by column relates to parent table users through its user_id column; this relationship is optional (the child value may be NULL or dangling).
- Child table animation_types through its updated_by column relates to parent table users through its user_id column; this relationship is optional (the child value may be NULL or dangling).
- Child table object_versions through its created_by column relates to parent table users through its user_id column; this relationship is optional (the child value may be NULL or dangling).
- Child table scene_objects through its created_by column relates to parent table users through its user_id column; this relationship is optional (the child value may be NULL or dangling).
- Child table scene_objects through its scene_id column relates to parent table scenes through its scene_id column; this relationship is optional (the child value may be NULL or dangling).
- Child table scene_objects through its updated_by column relates to parent table users through its user_id column; this relationship is optional (the child value may be NULL or dangling).
- Child table scenes through its created_by column relates to parent table users through its user_id column; this relationship is optional (the child value may be NULL or dangling).
- Child table scenes through its updated_by column relates to parent table users through its user_id column; this relationship is optional (the child value may be NULL or dangling).
- Child table user_actions through its scene_id column relates to parent table scenes through its scene_id column; this relationship is optional (the child value may be NULL or dangling).
- Child table user_actions through its user_id column relates to parent table users through its user_id column; this relationship is optional (the child value may be NULL or dangling).

Text columns are compared exactly as stored; no trimming or case-folding is applied to any value. Every rounding instruction below means half-up rounding to the stated number of decimal places.

MART users_user_actions_rollup — Per-users roll-up of linked user_actions activity in the 3d_object_positioning_and_animation_data scenario, including the fan-out onto scenes.

Grain: one row per users (user_id), INCLUDING users rows with no linked user_actions rows. The key column of this mart is parent_key.

Rule 1: the source table users is read in full as an input to this mart.

Rule 2: the source table user_actions is read in full as an input to this mart.

Rule 3: the source table scenes is read in full as an input to this mart.

Rule 4: from source table users there is one row per users row, keyed by user_id, carrying parent_key and parent_name.

Rule 5 (Hop 1): the user_actions rows are brought in against the grain, carrying user_id; a user_actions row belongs to the users row whose parent_key equals that user_actions row's user_id. One users row may have many user_actions rows, and a users row with no user_actions rows at all is RETAINED — preservation is left-sided, so the users side keeps its rows.

Rule 6 (Hop 2): the scenes rows are brought in, carrying scene_id, by matching each linked user_actions row's scene_id to the scene_id of a scenes row. A user_actions row whose scenes row is missing still counts as a link and is RETAINED — preservation is left-sided, so the user_actions side keeps its rows.

Rule 7: there is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 8: the mart columns are named parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount; total_amount, active_amount and max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount and max_amount, the default also applies to a group none of whose real rows carries an input value.

Rule 9 (guarded ratio): active_amount_ratio is active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0; the guarded result 0.0 also stands when the denominator is 0 or NULL. This value is carried beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount.

Rule 10: size_band is the size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5; a value exactly at 2 is 'small' and a value exactly at 5 is 'medium'. Every value falls in exactly one band, and size_band is carried beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount and active_amount_ratio.

Rule 11: has_links is 'yes' when this parent has at least one link, 'no' otherwise, and a value exactly at 0 is 'no'. It is never NULL, and it is carried beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio and size_band.

Rule 12: deterministic output order — rows appear sorted by parent_key in ascending order.

Output columns of users_user_actions_rollup:

- parent_key (integer): identifier of the users row. One row per value.
- parent_name (text): user_name of the users row, copied unchanged.
- link_count (bigint): number of user_actions rows linked to this users row. 0 when there are none.
- distinct_child_count (bigint): the number of distinct scenes rows reached through those links. Two links pointing at the same child count ONCE. 0 when there are no links. A link whose scenes row is missing reaches no scenes row and adds nothing to this count.
- active_link_count (bigint): number of linked user_actions rows whose action_type is one of ['create']. A parent whose links ALL fail that test reports 0, not a missing row.
- total_amount (integer): total of object_id over every linked row; 0 when there are no links, and 0 when none of the linked rows carries a object_id value.
- active_amount (integer): total of object_id over links whose action_type is one of ['create']; 0 when none qualify, and 0 when every qualifying row lacks a object_id value.
- max_amount (integer): largest object_id among the linked rows; 0 when there are no links, and 0 when none of the linked rows carries a object_id value.
- active_amount_ratio (float): active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0.
- size_band (text): size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band.
- has_links (text): 'yes' when this parent has at least one link, 'no' otherwise. Never NULL.

MART users_user_actions_cohorts — Per-(users, status cohort) summary of linked user_actions rows in the 3d_object_positioning_and_animation_data scenario, with passing and failing cohorts kept separate.

Grain: one row per (user_id, status cohort) pair represented among linked user_actions rows, plus one no-activity row for a users row with no linked user_actions row at all. A users row whose linked user_actions rows all lack a action_type value is in no cohort and gets no no-activity row, so it has no row in this mart. The key columns of this mart are entity_key and cohort.

Rule 1: the source table users is read in full as an input to this mart.

Rule 2: the source table user_actions is read in full as an input to this mart.

Rule 3: from source table users, each user_id and its user_name are carried into the cohort calculation as entity_key and entity_name.

Rule 4: the linked user_actions rows are brought into each users entity before status cohorts are assigned, carrying entity_key, entity_name and user_id; a user_actions row is linked to the entity whose entity_key equals that user_actions row's user_id, and preservation is left-sided, so a users entity with no such rows is kept at this stage.

Rule 5: for the passing cohort, the rows kept are those whose action_type belongs to the passing cohort values ['create'], carrying entity_key and entity_name.

Rule 6: there is one row per users entity that has at least one linked user_actions row in the passing cohort, reporting entity_key, entity_name, link_count as the number of those rows, distinct_status_count as how many different action_type values occur among them, total_amount as the total of their object_id, and max_amount as their largest object_id. The total and the largest value read only the rows that carry an object_id value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 7: for this passing cohort summary, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. It is carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.

Rule 8: these passing measures are labelled with cohort 'passing', alongside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 9: for the failing cohort, the rows kept are those whose action_type belongs to the failing cohort values ['update', 'delete'], carrying entity_key and entity_name.

Rule 10: there is one row per users entity that has at least one linked user_actions row in the failing cohort, reporting entity_key, entity_name, link_count as the number of those rows, distinct_status_count as how many different action_type values occur among them, total_amount as the total of their object_id, and max_amount as their largest object_id. The total and the largest value read only the rows that carry an object_id value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 11: for this failing cohort summary, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. It is carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.

Rule 12: these failing measures are labelled with cohort 'failing', alongside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 13: the placeholder row, carrying entity_key and entity_name, is kept for a users entity with no linked user_actions row at all; a users entity that has linked user_actions rows gets no placeholder, even when every one of those rows lacks a action_type value.

Rule 14: there is one row per users entity with no linked user_actions row at all, reporting entity_key, entity_name, link_count of 0 rows, distinct_status_count of 0 different action_type values, a total_amount object_id total of 0 and a max_amount largest object_id of 0.

Rule 15: for this no-activity summary, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. It is carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.

Rule 16: these measures are labelled with the no_activity cohort, alongside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 17: the disjoint passing and failing cohort summaries are combined into one body of rows, keeping all rows of both, each carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 18: the no-activity summaries are added to that body, keeping all of their rows, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 19: deterministic output order — rows appear sorted by entity, then cohort, that is ascending entity_key and, within one entity, ascending cohort.

Output columns of users_user_actions_cohorts:

- entity_key (integer): identifier of the users row.
- cohort (text): 'passing' for action_type values ['create']; 'failing' for values ['update', 'delete']; 'no_activity' when the users row has no linked user_actions row. A linked user_actions row whose action_type has no value belongs to no cohort: it is not counted in any cell, and it does not make the users row 'no_activity'.
- entity_name (text): user_name of the users row, copied unchanged.
- link_count (bigint): number of user_actions rows in this entity/cohort cell.
- distinct_status_count (bigint): the number of distinct action_type values represented in this cell.
- total_amount (integer): total of object_id in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an object_id value.
- max_amount (integer): largest object_id in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an object_id value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `users_user_actions_rollup`

- Grain: One row per users (user_id), INCLUDING users rows with no linked user_actions rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links

```text
Mart 'users_user_actions_rollup' has 12 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table user_actions. (public source tables: user_actions)
3. [source] Read source table scenes. (public source tables: scenes)
4. [derive] One row per users row, keyed by user_id. (public source tables: users | public carried/output columns: parent_key, parent_name)
5. [join] Hop 1: bring in user_actions against the grain. One users row may have many user_actions rows, and a users row with no user_actions rows at all is RETAINED. (public source tables: user_actions | public carried/output columns: user_id | join preservation: left | condition public identifiers: user_actions, user_id, parent_key)
6. [join] Hop 2: bring in scenes, matching each linked user_actions row's scene_id to the scene_id of a scenes row. A user_actions row whose scenes row is missing still counts as a link and is RETAINED. (public source tables: scenes | public carried/output columns: scene_id | join preservation: left | condition public identifiers: scenes, scene_id)
7. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
8. [derive] Name the mart columns; total_amount, active_amount, max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount, max_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
9. [ratio] Guarded ratios: active_amount_ratio — active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [conditional] size_band — Size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band | semantic parameters: boundary=a value exactly at 2 is 'small'; a value exactly at 5 is 'medium')
11. [conditional] has_links — 'yes' when this parent has at least one link, 'no' otherwise. Never NULL. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links | semantic parameters: boundary=a value exactly at 0 is 'no')
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `users_user_actions_cohorts`

- Grain: One row per (user_id, status cohort) pair represented among linked user_actions rows, plus one no-activity row for a users row with no linked user_actions row at all. A users row whose linked user_actions rows all lack a action_type value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'users_user_actions_cohorts' has 19 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table user_actions. (public source tables: user_actions)
3. [derive] Carry each user_id and its user_name into the cohort calculation. (public source tables: users | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked user_actions rows into each users entity before assigning status cohorts. (public source tables: user_actions | public carried/output columns: entity_key, entity_name, user_id | join preservation: left | condition public identifiers: user_actions, user_id, entity_key)
5. [filter] Keep rows whose action_type belongs to the passing cohort values ['create']. (public carried/output columns: entity_key, entity_name | condition literal specification values: create)
6. [distinct] One row per users entity that has at least one linked user_actions row in the passing cohort, reporting the number of those rows, how many different action_type values occur among them, the total of their object_id, and their largest object_id. The total and the largest value read only the rows that carry an object_id value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose action_type belongs to the failing cohort values ['update', 'delete']. (public carried/output columns: entity_key, entity_name | condition literal specification values: update, delete)
10. [distinct] One row per users entity that has at least one linked user_actions row in the failing cohort, reporting the number of those rows, how many different action_type values occur among them, the total of their object_id, and their largest object_id. The total and the largest value read only the rows that carry an object_id value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a users entity with no linked user_actions row at all; a users entity that has linked user_actions rows gets no placeholder, even when every one of those rows lacks a action_type value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per users entity with no linked user_actions row at all, reporting 0 rows, 0 different action_type values, a object_id total of 0 and a largest object_id of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

## Source tables

### scenes  (source backend: s3)
Source table scenes of the 3d_object_positioning_and_animation_data scenario.

- `scene_id`: integer NOT NULL — Unique identifier for each scene
- `scene_name`: text NULL — Name of the scene
- `description`: text NULL — Description of the scene
- `created_at`: text NULL — Date and time the scene was created
- `updated_at`: text NULL — Date and time the scene was last updated
- `scene_type`: text NULL — Type of the scene (e.g., indoor, outdoor, action) one of: indoor, outdoor, action.
- `status`: text NULL — Status of the scene (e.g., draft, in progress, completed) one of: draft, in progress, completed.
- `created_by`: integer NULL — ID of the user who created the scene
- `updated_by`: integer NULL — ID of the user who last updated the scene
- `is_active`: integer NULL — Flag to indicate if the scene is active
- primary key: scene_id

### scene_objects  (source backend: postgres)
Source table scene_objects of the 3d_object_positioning_and_animation_data scenario.

- `scene_object_id`: integer NOT NULL — Unique identifier for each scene-object relationship
- `scene_id`: integer NULL — ID of the scene the object belongs to
- `object_id`: integer NULL — ID of the object within the scene
- `object_role`: text NULL — Role of the object within the scene (e.g., main character, background) one of: main character, background.
- `start_time`: text NULL — Start time of the object's role in the scene
- `end_time`: text NULL — End time of the object's role in the scene
- `status`: text NULL — Status of the object within the scene (e.g., active, inactive) one of: active, inactive.
- `created_by`: integer NULL — ID of the user who added the object to the scene
- `updated_by`: integer NULL — ID of the user who last updated the object's role in the scene
- `is_active`: integer NULL — Flag to indicate if the object's role in the scene is active
- primary key: scene_object_id

### users  (source backend: postgres)
Source table users of the 3d_object_positioning_and_animation_data scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `user_name`: text NULL — Full name of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., animator, modeler, admin) one of: animator, modeler, admin.
- `password_hash`: text NULL — Hashed password for the user
- `created_at`: text NULL — Date and time the user account was created
- `updated_at`: text NULL — Date and time the user account was last updated
- `is_active`: integer NULL — Flag to indicate if the user account is active
- `last_login`: text NULL — Date and time of the user's last login
- primary key: user_id

### user_actions  (source backend: postgres)
Source table user_actions of the 3d_object_positioning_and_animation_data scenario.

- `action_id`: integer NOT NULL — Unique identifier for each user action
- `user_id`: integer NULL — ID of the user performing the action
- `action_type`: text NULL — Type of action (e.g., create, update, delete) one of: create, update, delete.
- `action_details`: text NULL — Details of the action performed
- `action_time`: text NULL — Date and time the action was performed
- `scene_id`: integer NULL — ID of the scene if the action is related to a scene
- `object_id`: integer NULL — ID of the object if the action is related to an object
- `ip_address`: text NULL — IP address from which the action was performed
- `user_agent`: text NULL — User agent string of the device used to perform the action
- primary key: action_id

### animation_types  (source backend: postgres)
Source table animation_types of the 3d_object_positioning_and_animation_data scenario.

- `type_id`: integer NOT NULL — Unique identifier for each animation type
- `type_name`: text NULL — Name of the animation type
- `description`: text NULL — Description of the animation type
- `created_at`: text NULL — Date and time the animation type was created
- `created_by`: integer NULL — ID of the user who created the animation type
- `updated_at`: text NULL — Date and time the animation type was last updated
- `updated_by`: integer NULL — ID of the user who last updated the animation type
- `is_active`: integer NULL — Flag to indicate if the animation type is active
- primary key: type_id

### object_versions  (source backend: s3)
Source table object_versions of the 3d_object_positioning_and_animation_data scenario.

- `version_id`: integer NOT NULL — Unique identifier for each version of the object
- `object_id`: integer NULL — ID of the object being versioned
- `version_number`: integer NULL — Version number of the object
- `version_details`: text NULL — Details of the changes made in this version
- `versioned_at`: text NULL — Date and time the version was created
- `version_type`: text NULL — Type of version (e.g., major, minor, patch) one of: major, minor, patch.
- `created_by`: integer NULL — ID of the user who created the version
- `ip_address`: text NULL — IP address from which the version was created
- `user_agent`: text NULL — User agent string of the device used to create the version
- primary key: version_id

### Relationships

- animation_types(created_by) -> users(user_id) [optional (may be NULL/dangling)]
- animation_types(updated_by) -> users(user_id) [optional (may be NULL/dangling)]
- object_versions(created_by) -> users(user_id) [optional (may be NULL/dangling)]
- scene_objects(created_by) -> users(user_id) [optional (may be NULL/dangling)]
- scene_objects(scene_id) -> scenes(scene_id) [optional (may be NULL/dangling)]
- scene_objects(updated_by) -> users(user_id) [optional (may be NULL/dangling)]
- scenes(created_by) -> users(user_id) [optional (may be NULL/dangling)]
- scenes(updated_by) -> users(user_id) [optional (may be NULL/dangling)]
- user_actions(scene_id) -> scenes(scene_id) [optional (may be NULL/dangling)]
- user_actions(user_id) -> users(user_id) [optional (may be NULL/dangling)]

