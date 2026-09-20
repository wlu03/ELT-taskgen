# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# 3D Object Pose Estimation And Tracking

## Specification

PROJECT OVERVIEW: 3D Object Pose Estimation And Tracking

This project builds two analytical marts over the 3d_object_pose_estimation_and_tracking scenario. Every source table must be extracted from the backend named for it here.

Source tables and their extraction backends:
- Source table object_poses must be extracted from the rest backend. It records one pose estimation per pose_id, with timestamp, norm_posX, norm_posY, confidence, theta, phi, object_id, environment_id, sensor_id, pose_quality and pose_source.
- Source table objects must be extracted from the rest backend, keyed by object_id, with object_name, description, category_id, object_type (static or dynamic), object_size, object_color, object_material and object_weight.
- Source table datasets must be extracted from the files backend, keyed by dataset_id, with dataset_name, description, dataset_type (training, testing or validation), dataset_size, dataset_creation_date and dataset_description.
- Source table object_pose_links must be extracted from the mongodb backend, keyed by link_id, with object_id, pose_id, dataset_id, environment_id and sensor_id.
- Source table pose_estimation_methods must be extracted from the mongodb backend, keyed by method_id, with method_name, description, method_type (neural network or traditional algorithm), method_accuracy and method_complexity (low, medium or high).
- Source table pose_estimation_results must be extracted from the rest backend, keyed by result_id, with pose_id, method_id, accuracy, method_version, result_description and result_timestamp.
- Source table users must be extracted from the s3 backend, keyed by user_id, with user_name, email, role (researcher, data analyst or admin), department, position and access_level (read, write or admin).
- Source table access_logs must be extracted from the mongodb backend, keyed by access_id, with pose_id, user_id, access_date, access_type (view or download), ip_address, user_agent and access_duration.
- Source table object_categories must be extracted from the files backend, keyed by category_id, with category_name, description, category_type (indoor or outdoor) and category_description.
- Source table object_category_links must be extracted from the postgres backend, keyed by link_id, with object_id and category_id.
- Source table environmental_data must be extracted from the s3 backend, keyed by env_data_id, with pose_id, temperature, humidity, lighting, environment_type, environment_description and environment_conditions.
- Source table sensors must be extracted from the files backend, keyed by sensor_id, with sensor_name, sensor_type (camera or LIDAR), sensor_description, sensor_accuracy and sensor_version.
- Source table environments must be extracted from the s3 backend, keyed by environment_id, with environment_name, environment_type, environment_description and environment_conditions.

Relationships between the source tables (each stated with its child table and keys, its parent table and keys, and whether it is required or optional):
- Child table access_logs with key pose_id refers to parent table object_poses with key pose_id; this relationship is optional (may be NULL or dangling).
- Child table access_logs with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).
- Child table environmental_data with key pose_id refers to parent table object_poses with key pose_id; this relationship is optional (may be NULL or dangling).
- Child table object_category_links with key category_id refers to parent table object_categories with key category_id; this relationship is optional (may be NULL or dangling).
- Child table object_category_links with key object_id refers to parent table objects with key object_id; this relationship is optional (may be NULL or dangling).
- Child table object_pose_links with key environment_id refers to parent table environments with key environment_id; this relationship is optional (may be NULL or dangling).
- Child table object_pose_links with key object_id refers to parent table objects with key object_id; this relationship is optional (may be NULL or dangling).
- Child table object_pose_links with key pose_id refers to parent table object_poses with key pose_id; this relationship is optional (may be NULL or dangling).
- Child table object_pose_links with key sensor_id refers to parent table sensors with key sensor_id; this relationship is optional (may be NULL or dangling).
- Child table object_poses with key environment_id refers to parent table environments with key environment_id; this relationship is optional (may be NULL or dangling).
- Child table object_poses with key object_id refers to parent table objects with key object_id; this relationship is optional (may be NULL or dangling).
- Child table object_poses with key sensor_id refers to parent table sensors with key sensor_id; this relationship is optional (may be NULL or dangling).
- Child table pose_estimation_results with key method_id refers to parent table pose_estimation_methods with key method_id; this relationship is optional (may be NULL or dangling).

Throughout, an access_logs row is said to be linked to a users row when the access_logs row's user_id equals that users row's user_id. Rounding to 4 decimal places uses ordinary half-up decimal rounding.

=== Mart users_access_logs_rollup: per-users roll-up of linked access_logs activity in the 3d_object_pose_estimation_and_tracking scenario, including the fan-out onto object_poses ===

The mart users_access_logs_rollup is a per-users roll-up of linked access_logs activity in the 3d_object_pose_estimation_and_tracking scenario, including the fan-out onto object_poses.

Grain: one row per users row (user_id), INCLUDING users rows with no linked access_logs rows. The key column of this mart is parent_key.

Rule 1: the source table users is read in full as an input of this mart.

Rule 2: the source table access_logs is read in full as an input of this mart.

Rule 3: the source table object_poses is read in full as an input of this mart.

Rule 4: from source table users there is one row per users row, keyed by user_id, carrying parent_key and parent_name.

Rule 5 (hop 1): the linked access_logs rows are brought in against the grain, matching an access_logs row by its user_id to the parent_key of the users row; the carried column here is user_id, from source table access_logs. One users row may have many access_logs rows, and a users row with no access_logs rows at all is RETAINED — preservation is left-sided, on the users side.

Rule 6 (hop 2): from source table object_poses each linked access_logs row's pose_id is matched to the pose_id of an object_poses row, carrying pose_id. An access_logs row whose object_poses row is missing still counts as a link and is RETAINED — preservation is left-sided, on the access_logs side.

Rule 7: there is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group, and each such row reports link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 8: the mart columns are named parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount; total_amount, active_amount and max_amount report their declared defaults of 0 — never NULL — for a group with no matching rows. For total_amount, active_amount and max_amount, the default of 0 also applies to a group none of whose real rows carries an input value.

Rule 9 (guarded ratio): alongside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount, each row reports active_amount_ratio — active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0 or has no value.

Rule 10: alongside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount and active_amount_ratio, each row reports size_band — the size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band: a link_count of exactly 2 is 'small' and a link_count of exactly 5 is 'medium'.

Rule 11: alongside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio and size_band, each row reports has_links — 'yes' when this parent has at least one link, 'no' otherwise, with a link_count of exactly 0 giving 'no'. Never NULL.

Rule 12: the deterministic output order is ascending parent_key, sorted on parent_key alone.

Output columns of users_access_logs_rollup:
- parent_key (integer): identifier of the users row. One row per value.
- parent_name (text): user_name of the users row, copied unchanged.
- link_count (bigint): number of access_logs rows linked to this users row. 0 when there are none.
- distinct_child_count (bigint): the number of distinct object_poses rows reached through those links. Two links pointing at the same child count ONCE. 0 when there are no links. A link whose object_poses row is missing reaches no object_poses row and adds nothing to this count.
- active_link_count (bigint): number of linked access_logs rows whose access_type is one of ['view']. A parent whose links ALL fail that test reports 0, not a missing row.
- total_amount (float): total of access_duration over every linked row; 0 when there are no links, and 0 when none of the linked rows carries an access_duration value.
- active_amount (float): total of access_duration over links whose access_type is one of ['view']; 0 when none qualify, and 0 when every qualifying row lacks an access_duration value.
- max_amount (float): largest access_duration among the linked rows; 0 when there are no links, and 0 when none of the linked rows carries an access_duration value.
- active_amount_ratio (float): active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0.
- size_band (text): size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band.
- has_links (text): 'yes' when this parent has at least one link, 'no' otherwise. Never NULL.

=== Mart users_access_logs_cohorts: per-(users, status cohort) summary of linked access_logs rows in the 3d_object_pose_estimation_and_tracking scenario, with passing and failing cohorts kept separate ===

The mart users_access_logs_cohorts is a per-(users, status cohort) summary of linked access_logs rows in the 3d_object_pose_estimation_and_tracking scenario, with passing and failing cohorts kept separate.

Grain: one row per (user_id, status cohort) pair represented among linked access_logs rows, plus one no-activity row for a users row with no linked access_logs row at all. A users row whose linked access_logs rows all lack an access_type value is in no cohort and gets no no-activity row, so it has no row in this mart. The key columns of this mart are entity_key and cohort.

Rule 1: the source table users is read in full as an input of this mart.

Rule 2: the source table access_logs is read in full as an input of this mart.

Rule 3: from source table users, each user_id and its user_name are carried into the cohort calculation as entity_key and entity_name.

Rule 4: the linked access_logs rows from source table access_logs are brought into each users entity before assigning status cohorts, matching an access_logs row by its user_id to the entity_key of the users entity, and carrying entity_key, entity_name and user_id. Preservation is left-sided, on the users side: a users entity with no matching access_logs row is retained at this point.

Rule 5: for the passing cohort, only rows whose access_type is one of the passing cohort values ['view'] are kept, carrying entity_key and entity_name.

Rule 6: there is one row per users entity that has at least one linked access_logs row in the passing cohort, carrying entity_key and entity_name and reporting link_count as the number of those rows, distinct_status_count as how many different access_type values occur among them, total_amount as the total of their access_duration, and max_amount as their largest access_duration. The total and the largest value read only the rows that carry an access_duration value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 7: for those passing-cohort rows, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 8: these measures — entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share — are labelled with cohort 'passing'.

Rule 9: for the failing cohort, only rows whose access_type is one of the failing cohort values ['download'] are kept, carrying entity_key and entity_name.

Rule 10: there is one row per users entity that has at least one linked access_logs row in the failing cohort, carrying entity_key and entity_name and reporting link_count as the number of those rows, distinct_status_count as how many different access_type values occur among them, total_amount as the total of their access_duration, and max_amount as their largest access_duration. The total and the largest value read only the rows that carry an access_duration value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 11: for those failing-cohort rows, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 12: these measures — entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share — are labelled with cohort 'failing'.

Rule 13: the placeholder row, carrying entity_key and entity_name, is kept for a users entity with no linked access_logs row at all; a users entity that has linked access_logs rows gets no placeholder, even when every one of those rows lacks an access_type value.

Rule 14: there is one row per users entity with no linked access_logs row at all, carrying entity_key and entity_name and reporting link_count of 0 rows, distinct_status_count of 0 different access_type values, a total_amount access_duration total of 0 and a max_amount largest access_duration of 0.

Rule 15: for those placeholder rows, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 16: these measures — entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share — are labelled with cohort 'no_activity'.

Rule 17: the disjoint passing and failing cohort summaries are combined, all rows of both kept together, carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 18: the no-activity summaries are added to that combination, all such rows kept, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 19: the deterministic output order is ascending entity, then cohort — rows sorted on entity_key first and on cohort second.

Output columns of users_access_logs_cohorts:
- entity_key (integer): identifier of the users row.
- cohort (text): 'passing' for access_type values ['view']; 'failing' for values ['download']; 'no_activity' when the users row has no linked access_logs row. A linked access_logs row whose access_type has no value belongs to no cohort: it is not counted in any cell, and it does not make the users row 'no_activity'.
- entity_name (text): user_name of the users row, copied unchanged.
- link_count (bigint): number of access_logs rows in this entity/cohort cell.
- distinct_status_count (bigint): the distinct status count — the number of different access_type values represented in this cell.
- total_amount (float): total of access_duration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an access_duration value.
- max_amount (float): largest access_duration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an access_duration value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `users_access_logs_rollup`

- Grain: One row per users (user_id), INCLUDING users rows with no linked access_logs rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links

```text
Mart 'users_access_logs_rollup' has 12 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table access_logs. (public source tables: access_logs)
3. [source] Read source table object_poses. (public source tables: object_poses)
4. [derive] One row per users row, keyed by user_id. (public source tables: users | public carried/output columns: parent_key, parent_name)
5. [join] Hop 1: bring in access_logs against the grain. One users row may have many access_logs rows, and a users row with no access_logs rows at all is RETAINED. (public source tables: access_logs | public carried/output columns: user_id | join preservation: left | condition public identifiers: access_logs, user_id, parent_key)
6. [join] Hop 2: bring in object_poses, matching each linked access_logs row's pose_id to the pose_id of a object_poses row. A access_logs row whose object_poses row is missing still counts as a link and is RETAINED. (public source tables: object_poses | public carried/output columns: pose_id | join preservation: left | condition public identifiers: object_poses, pose_id)
7. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
8. [derive] Name the mart columns; total_amount, active_amount, max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount, max_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
9. [ratio] Guarded ratios: active_amount_ratio — active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [conditional] size_band — Size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band | semantic parameters: boundary=a value exactly at 2 is 'small'; a value exactly at 5 is 'medium')
11. [conditional] has_links — 'yes' when this parent has at least one link, 'no' otherwise. Never NULL. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links | semantic parameters: boundary=a value exactly at 0 is 'no')
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `users_access_logs_cohorts`

- Grain: One row per (user_id, status cohort) pair represented among linked access_logs rows, plus one no-activity row for a users row with no linked access_logs row at all. A users row whose linked access_logs rows all lack a access_type value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'users_access_logs_cohorts' has 19 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table access_logs. (public source tables: access_logs)
3. [derive] Carry each user_id and its user_name into the cohort calculation. (public source tables: users | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked access_logs rows into each users entity before assigning status cohorts. (public source tables: access_logs | public carried/output columns: entity_key, entity_name, user_id | join preservation: left | condition public identifiers: access_logs, user_id, entity_key)
5. [filter] Keep rows whose access_type belongs to the passing cohort values ['view']. (public carried/output columns: entity_key, entity_name | condition literal specification values: view)
6. [distinct] One row per users entity that has at least one linked access_logs row in the passing cohort, reporting the number of those rows, how many different access_type values occur among them, the total of their access_duration, and their largest access_duration. The total and the largest value read only the rows that carry an access_duration value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose access_type belongs to the failing cohort values ['download']. (public carried/output columns: entity_key, entity_name | condition literal specification values: download)
10. [distinct] One row per users entity that has at least one linked access_logs row in the failing cohort, reporting the number of those rows, how many different access_type values occur among them, the total of their access_duration, and their largest access_duration. The total and the largest value read only the rows that carry an access_duration value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a users entity with no linked access_logs row at all; a users entity that has linked access_logs rows gets no placeholder, even when every one of those rows lacks a access_type value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per users entity with no linked access_logs row at all, reporting 0 rows, 0 different access_type values, a access_duration total of 0 and a largest access_duration of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

## Source tables

### object_poses  (source backend: rest)
Source table object_poses of the 3d_object_pose_estimation_and_tracking scenario.

- `pose_id`: integer NOT NULL — Unique identifier for each pose estimation
- `timestamp`: float NULL — Timestamp of the pose estimation
- `norm_posX`: float NULL — Normalized X position of the object
- `norm_posY`: float NULL — Normalized Y position of the object
- `confidence`: float NULL — Confidence level of the pose estimation
- `theta`: float NULL — Euler angle theta representing the object's orientation
- `phi`: float NULL — Euler angle phi representing the object's orientation
- `object_id`: integer NULL — ID of the object
- `environment_id`: integer NULL — ID of the environment
- `sensor_id`: integer NULL — ID of the sensor
- `pose_quality`: text NULL — Quality of the pose estimation
- `pose_source`: text NULL — Source of the pose data
- primary key: pose_id

### objects  (source backend: rest)
Source table objects of the 3d_object_pose_estimation_and_tracking scenario.

- `object_id`: integer NOT NULL — Unique identifier for each object
- `object_name`: text NULL — Name of the object
- `description`: text NULL — Description of the object
- `category_id`: integer NULL — ID of the category
- `object_type`: text NULL — Type of the object (e.g., static, dynamic) one of: static, dynamic.
- `object_size`: text NULL — Size of the object
- `object_color`: text NULL — Color of the object
- `object_material`: text NULL — Material of the object
- `object_weight`: float NULL — Weight of the object
- primary key: object_id

### datasets  (source backend: files)
Source table datasets of the 3d_object_pose_estimation_and_tracking scenario.

- `dataset_id`: integer NOT NULL — Unique identifier for each dataset
- `dataset_name`: text NULL — Name of the dataset
- `description`: text NULL — Description of the dataset
- `dataset_type`: text NULL — Type of the dataset (e.g., training, testing, validation) one of: training, testing, validation.
- `dataset_size`: integer NULL — Size of the dataset (number of pose estimations)
- `dataset_creation_date`: text NULL — Date when the dataset was created
- `dataset_description`: text NULL — Detailed description of the dataset
- primary key: dataset_id

### object_pose_links  (source backend: mongodb)
Source table object_pose_links of the 3d_object_pose_estimation_and_tracking scenario.

- `link_id`: integer NOT NULL — Unique identifier for each link
- `object_id`: integer NULL — ID of the object
- `pose_id`: integer NULL — ID of the pose estimation
- `dataset_id`: integer NULL — ID of the dataset
- `environment_id`: integer NULL — ID of the environment
- `sensor_id`: integer NULL — ID of the sensor
- primary key: link_id

### pose_estimation_methods  (source backend: mongodb)
Source table pose_estimation_methods of the 3d_object_pose_estimation_and_tracking scenario.

- `method_id`: integer NOT NULL — Unique identifier for each method
- `method_name`: text NULL — Name of the pose estimation method
- `description`: text NULL — Description of the method
- `method_type`: text NULL — Type of the method (e.g., neural network, traditional algorithm) one of: neural network, traditional algorithm.
- `method_accuracy`: float NULL — Average accuracy of the method
- `method_complexity`: text NULL — Complexity of the method (e.g., low, medium, high) one of: low, medium, high.
- primary key: method_id

### pose_estimation_results  (source backend: rest)
Source table pose_estimation_results of the 3d_object_pose_estimation_and_tracking scenario.

- `result_id`: integer NOT NULL — Unique identifier for each result
- `pose_id`: integer NULL — ID of the pose estimation
- `method_id`: integer NULL — ID of the pose estimation method
- `accuracy`: float NULL — Accuracy of the pose estimation
- `method_version`: text NULL — Version of the method used
- `result_description`: text NULL — Detailed description of the result
- `result_timestamp`: text NULL — Timestamp when the result was generated
- primary key: result_id

### users  (source backend: s3)
Source table users of the 3d_object_pose_estimation_and_tracking scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `user_name`: text NULL — Full name of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., researcher, data analyst, admin) one of: researcher, data analyst, admin.
- `department`: text NULL — Department of the user
- `position`: text NULL — Position of the user
- `access_level`: text NULL — Access level of the user (e.g., read, write, admin) one of: read, write, admin.
- primary key: user_id

### access_logs  (source backend: mongodb)
Source table access_logs of the 3d_object_pose_estimation_and_tracking scenario.

- `access_id`: integer NOT NULL — Unique identifier for each access event
- `pose_id`: integer NULL — ID of the pose estimation being accessed
- `user_id`: integer NULL — ID of the user accessing the pose estimation
- `access_date`: text NULL — Date when the pose estimation was accessed
- `access_type`: text NULL — Type of access (e.g., view, download) one of: view, download.
- `ip_address`: text NULL — IP address of the user
- `user_agent`: text NULL — User agent of the user
- `access_duration`: float NULL — Duration of the access
- primary key: access_id

### object_categories  (source backend: files)
Source table object_categories of the 3d_object_pose_estimation_and_tracking scenario.

- `category_id`: integer NOT NULL — Unique identifier for each category
- `category_name`: text NULL — Name of the category
- `description`: text NULL — Description of the category
- `category_type`: text NULL — Type of the category (e.g., indoor, outdoor) one of: indoor, outdoor.
- `category_description`: text NULL — Detailed description of the category
- primary key: category_id

### object_category_links  (source backend: postgres)
Source table object_category_links of the 3d_object_pose_estimation_and_tracking scenario.

- `link_id`: integer NOT NULL — Unique identifier for each link
- `object_id`: integer NULL — ID of the object
- `category_id`: integer NULL — ID of the category
- primary key: link_id

### environmental_data  (source backend: s3)
Source table environmental_data of the 3d_object_pose_estimation_and_tracking scenario.

- `env_data_id`: integer NOT NULL — Unique identifier for each environmental data entry
- `pose_id`: integer NULL — ID of the pose estimation
- `temperature`: float NULL — Temperature of the environment
- `humidity`: float NULL — Humidity of the environment
- `lighting`: float NULL — Lighting of the environment
- `environment_type`: text NULL — Type of the environment (e.g., indoor, outdoor) one of: indoor, outdoor.
- `environment_description`: text NULL — Detailed description of the environment
- `environment_conditions`: text NULL — Additional conditions of the environment (e.g., weather, noise level) one of: weather, noise level.
- primary key: env_data_id

### sensors  (source backend: files)
Source table sensors of the 3d_object_pose_estimation_and_tracking scenario.

- `sensor_id`: integer NOT NULL — Unique identifier for each sensor
- `sensor_name`: text NULL — Name of the sensor
- `sensor_type`: text NULL — Type of the sensor (e.g., camera, LIDAR) one of: camera, LIDAR.
- `sensor_description`: text NULL — Description of the sensor
- `sensor_accuracy`: float NULL — Accuracy of the sensor
- `sensor_version`: text NULL — Version of the sensor
- primary key: sensor_id

### environments  (source backend: s3)
Source table environments of the 3d_object_pose_estimation_and_tracking scenario.

- `environment_id`: integer NOT NULL — Unique identifier for each environment
- `environment_name`: text NULL — Name of the environment
- `environment_type`: text NULL — Type of the environment (e.g., indoor, outdoor) one of: indoor, outdoor.
- `environment_description`: text NULL — Detailed description of the environment
- `environment_conditions`: text NULL — Additional conditions of the environment (e.g., weather, noise level) one of: weather, noise level.
- primary key: environment_id

### Relationships

- access_logs(pose_id) -> object_poses(pose_id) [optional (may be NULL/dangling)]
- access_logs(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- environmental_data(pose_id) -> object_poses(pose_id) [optional (may be NULL/dangling)]
- object_category_links(category_id) -> object_categories(category_id) [optional (may be NULL/dangling)]
- object_category_links(object_id) -> objects(object_id) [optional (may be NULL/dangling)]
- object_pose_links(environment_id) -> environments(environment_id) [optional (may be NULL/dangling)]
- object_pose_links(object_id) -> objects(object_id) [optional (may be NULL/dangling)]
- object_pose_links(pose_id) -> object_poses(pose_id) [optional (may be NULL/dangling)]
- object_pose_links(sensor_id) -> sensors(sensor_id) [optional (may be NULL/dangling)]
- object_poses(environment_id) -> environments(environment_id) [optional (may be NULL/dangling)]
- object_poses(object_id) -> objects(object_id) [optional (may be NULL/dangling)]
- object_poses(sensor_id) -> sensors(sensor_id) [optional (may be NULL/dangling)]
- pose_estimation_results(method_id) -> pose_estimation_methods(method_id) [optional (may be NULL/dangling)]

