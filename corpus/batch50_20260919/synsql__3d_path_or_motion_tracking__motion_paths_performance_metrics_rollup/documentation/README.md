# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# 3D Path Or Motion Tracking

## Specification

PROJECT OVERVIEW: 3D Path Or Motion Tracking

This project builds two analytical marts over the 3d_path_or_motion_tracking scenario. Every source table must be extracted from the backend named for it here, and nowhere else.

Source tables and their extraction backends:
- Source table robots must be extracted from the mongodb backend; it holds one row per robot, identified by robot_id, with robot_name, robot_type (one of robotic arm, AGV), model, serial_number, installation_date, last_maintenance_date, location, manufacturer, contact_email, ip_address and mac_address.
- Source table motion_paths must be extracted from the s3 backend; it holds one row per motion path, identified by path_id, with robot_id, path_name, description, start_date, end_date, version, status (one of active, inactive, archived), created_by and modified_by.
- Source table steps must be extracted from the s3 backend; it holds one row per step, identified by step_id, with path_id, step_number, step_name (one of start, end, intermediate points), x_coordinate, y_coordinate, z_coordinate, timestamp, velocity and acceleration.
- Source table path_versions must be extracted from the files backend; it holds one row per version, identified by version_id, with path_id, version_number, created_date, description, created_by and change_log.
- Source table maintenance_records must be extracted from the files backend; it holds one row per maintenance record, identified by record_id, with robot_id, maintenance_date, maintenance_type (one of routine, emergency), description, performed_by, next_maintenance_due and maintenance_duration.
- Source table users must be extracted from the rest backend; it holds one row per user, identified by user_id, with user_name, email, role (one of engineer, operator, admin), department, password and last_login_date.
- Source table access_logs must be extracted from the mongodb backend; it holds one row per access event, identified by access_id, with user_id, access_date, access_type (one of view, edit, delete), robot_id, path_id and access_result (one of success, failure).
- Source table performance_metrics must be extracted from the mongodb backend; it holds one row per metric, identified by metric_id, with robot_id, path_id, metric_name (one of cycle time, efficiency), metric_value, recorded_date, unit (one of seconds, percent), target_value and threshold_value.
- Source table machine_learning_models must be extracted from the mongodb backend; it holds one row per model, identified by model_id, with model_name, model_type (one of regression, classification), algorithm, training_date, accuracy and description.
- Source table optimization_results must be extracted from the mongodb backend; it holds one row per result, identified by result_id, with path_id, optimization_date, optimization_type (one of speed, efficiency) and result.
- Source table notifications must be extracted from the mongodb backend; it holds one row per notification, identified by notification_id, with robot_id, notification_date, notification_type (one of maintenance, optimization) and message.
- Source table system_settings must be extracted from the rest backend; it holds one row per setting, identified by setting_id, with setting_name, setting_value and description.

Relationships between the source tables, each labelled exactly as the source schema labels it:
- Child table access_logs with key path_id refers to parent table motion_paths with key path_id; this relationship is optional (the value may be NULL or dangling).
- Child table access_logs with key robot_id refers to parent table robots with key robot_id; this relationship is optional (the value may be NULL or dangling).
- Child table access_logs with key user_id refers to parent table users with key user_id; this relationship is optional (the value may be NULL or dangling).
- Child table maintenance_records with key robot_id refers to parent table robots with key robot_id; this relationship is optional (the value may be NULL or dangling).
- Child table motion_paths with key robot_id refers to parent table robots with key robot_id; this relationship is optional (the value may be NULL or dangling).
- Child table notifications with key robot_id refers to parent table robots with key robot_id; this relationship is optional (the value may be NULL or dangling).
- Child table optimization_results with key path_id refers to parent table motion_paths with key path_id; this relationship is optional (the value may be NULL or dangling).
- Child table path_versions with key path_id refers to parent table motion_paths with key path_id; this relationship is optional (the value may be NULL or dangling).
- Child table performance_metrics with key path_id refers to parent table motion_paths with key path_id; this relationship is optional (the value may be NULL or dangling).
- Child table performance_metrics with key robot_id refers to parent table robots with key robot_id; this relationship is optional (the value may be NULL or dangling).
- Child table steps with key path_id refers to parent table motion_paths with key path_id; this relationship is optional (the value may be NULL or dangling).

Rounding convention: wherever a value is described as rounded to 4 decimal places, report it rounded to 4 decimal places.

=== Mart motion_paths_performance_metrics_rollup: Per-motion_paths roll-up of linked performance_metrics activity in the 3d_path_or_motion_tracking scenario, including the fan-out onto robots ===

Grain: one row per motion_paths (path_id), INCLUDING motion_paths rows with no linked performance_metrics rows.

Key column: parent_key is the only key column of this mart.

How the mart is built, rule by rule:

Rule 1. The source table motion_paths is read in full as an input of this mart.

Rule 2. The source table performance_metrics is read in full as an input of this mart.

Rule 3. The source table robots is read in full as an input of this mart.

Rule 4. From source table motion_paths there is one row per motion_paths row, keyed by path_id, carrying parent_key and parent_name.

Rule 5. Hop 1: the performance_metrics rows are brought in against that grain, matching on path_id of performance_metrics equal to parent_key, carrying path_id; preservation is left-sided, so one motion_paths row may have many matching performance_metrics rows, and a motion_paths row with no performance_metrics rows at all is RETAINED.

Rule 6. Hop 2: rows of robots are brought in, matching each linked performance_metrics row's robot_id to the robot_id of a robots row and carrying robot_id; preservation is left-sided, so a performance_metrics row whose robots row is missing still counts as a link and is RETAINED.

Rule 7. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split the rows of a key, and each such row reports link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount over that row's matching rows. A parent with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 8. The mart columns are named parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount, and total_amount, active_amount and max_amount report their declared defaults — never NULL — for a parent with no matching rows; for total_amount, active_amount and max_amount the same default also applies to a parent none of whose real rows carries an input value.

Rule 9. Guarded ratio: active_amount_ratio is active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0 or has no value; it is reported beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount.

Rule 10. size_band is the size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, and 'large' above 5; every value falls in exactly one band, a value exactly at 2 is 'small' and a value exactly at 5 is 'medium'. It is reported beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount and active_amount_ratio.

Rule 11. has_links is 'yes' when this parent has at least one link and 'no' otherwise, never NULL, with a link_count value exactly at 0 giving 'no'; it is reported beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio and size_band.

Rule 12. Deterministic output order: rows appear sorted in ascending parent_key order.

Output columns of motion_paths_performance_metrics_rollup:
- parent_key (integer): the identifier of the motion_paths row; there is one row per value.
- parent_name (text): the path_name of the motion_paths row, copied unchanged.
- link_count (bigint): the number of performance_metrics rows linked to this motion_paths row; 0 when there are none.
- distinct_child_count (bigint): the number of different robots rows reached through those links. Two links pointing at the same child count ONCE; the value is 0 when there are no links, and a link whose robots row is missing reaches no robots row and adds nothing to this count.
- active_link_count (bigint): the number of linked performance_metrics rows whose metric_name is one of ['cycle time']. A parent whose links ALL fail that test reports 0, not a missing row.
- total_amount (float): the total of metric_value over every linked row; 0 when there are no links, and 0 when none of the linked rows carries a metric_value value.
- active_amount (float): the total of metric_value over links whose metric_name is one of ['cycle time']; 0 when none qualify, and 0 when every qualifying row lacks a metric_value value.
- max_amount (float): the largest metric_value among the linked rows; 0 when there are no links, and 0 when none of the linked rows carries a metric_value value.
- active_amount_ratio (float): active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0.
- size_band (text): the size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5; every value falls in exactly one band.
- has_links (text): 'yes' when this parent has at least one link, 'no' otherwise; never NULL.

=== Mart motion_paths_performance_metrics_cohorts: Per-(motion_paths, status cohort) summary of linked performance_metrics rows in the 3d_path_or_motion_tracking scenario, with passing and failing cohorts kept separate ===

Grain: one row per (path_id, status cohort) pair represented among linked performance_metrics rows, plus one no-activity row for a motion_paths row with no linked performance_metrics row at all. A motion_paths row whose linked performance_metrics rows all lack a metric_name value is in no cohort and gets no no-activity row, so it has no row in this mart.

Key columns: entity_key and cohort together are the key columns of this mart.

How the mart is built, rule by rule:

Rule 1. The source table motion_paths is read in full as an input of this mart.

Rule 2. The source table performance_metrics is read in full as an input of this mart.

Rule 3. From source table motion_paths, each path_id and its path_name are carried into the cohort calculation as entity_key and entity_name.

Rule 4. The linked performance_metrics rows are brought into each motion_paths entity before assigning status cohorts, matching path_id of performance_metrics to entity_key and carrying entity_key, entity_name and path_id; preservation is left-sided.

Rule 5. For the passing cohort, only rows whose metric_name belongs to the passing cohort values ['cycle time'] are kept, carrying entity_key and entity_name.

Rule 6. There is one row per motion_paths entity that has at least one linked performance_metrics row in the passing cohort, carrying entity_key and entity_name and reporting link_count as the number of those rows, distinct_status_count as how many different metric_name values occur among them, total_amount as the total of their metric_value, and max_amount as their largest metric_value. The total and the largest value read only the rows that carry a metric_value value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 7. For those passing-cohort rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it is reported beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.

Rule 8. These measures are labelled as the passing cohort, so cohort reads 'passing' beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 9. For the failing cohort, only rows whose metric_name belongs to the failing cohort values ['efficiency'] are kept, carrying entity_key and entity_name.

Rule 10. There is one row per motion_paths entity that has at least one linked performance_metrics row in the failing cohort, carrying entity_key and entity_name and reporting link_count as the number of those rows, distinct_status_count as how many different metric_name values occur among them, total_amount as the total of their metric_value, and max_amount as their largest metric_value. The total and the largest value read only the rows that carry a metric_value value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 11. For those failing-cohort rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it is reported beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.

Rule 12. These measures are labelled as the failing cohort, so cohort reads 'failing' beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 13. The placeholder row, carrying entity_key and entity_name, is kept for a motion_paths entity with no linked performance_metrics row at all; a motion_paths entity that has linked performance_metrics rows gets no placeholder, even when every one of those rows lacks a metric_name value.

Rule 14. There is one row per motion_paths entity with no linked performance_metrics row at all, carrying entity_key and entity_name and reporting link_count of 0 rows, distinct_status_count of 0 different metric_name values, a total_amount metric_value total of 0 and a max_amount largest metric_value of 0.

Rule 15. For those no-activity rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it is reported beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.

Rule 16. These measures are labelled as the no_activity cohort, so cohort reads 'no_activity' beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 17. The disjoint passing and failing cohort summaries are combined into one body of rows, keeping all rows of both, each carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 18. The no-activity summaries are added to that combined body, keeping all such rows, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 19. Deterministic output order: rows appear sorted by entity_key ascending, then by cohort ascending.

Output columns of motion_paths_performance_metrics_cohorts:
- entity_key (integer): the identifier of the motion_paths row.
- cohort (text): 'passing' for metric_name values ['cycle time']; 'failing' for values ['efficiency']; 'no_activity' when the motion_paths row has no linked performance_metrics row. A linked performance_metrics row whose metric_name has no value belongs to no cohort: it is not counted in any cell, and it does not make the motion_paths row 'no_activity'.
- entity_name (text): the path_name of the motion_paths row, copied unchanged.
- link_count (bigint): the number of performance_metrics rows in this entity/cohort cell.
- distinct_status_count (bigint): the number of different metric_name values represented in this cell.
- total_amount (float): the total of metric_value in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an metric_value value.
- max_amount (float): the largest metric_value in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an metric_value value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `motion_paths_performance_metrics_rollup`

- Grain: One row per motion_paths (path_id), INCLUDING motion_paths rows with no linked performance_metrics rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links

```text
Mart 'motion_paths_performance_metrics_rollup' has 12 declared semantic rules:
1. [source] Read source table motion_paths. (public source tables: motion_paths)
2. [source] Read source table performance_metrics. (public source tables: performance_metrics)
3. [source] Read source table robots. (public source tables: robots)
4. [derive] One row per motion_paths row, keyed by path_id. (public source tables: motion_paths | public carried/output columns: parent_key, parent_name)
5. [join] Hop 1: bring in performance_metrics against the grain. One motion_paths row may have many performance_metrics rows, and a motion_paths row with no performance_metrics rows at all is RETAINED. (public source tables: performance_metrics | public carried/output columns: path_id | join preservation: left | condition public identifiers: performance_metrics, path_id, parent_key)
6. [join] Hop 2: bring in robots, matching each linked performance_metrics row's robot_id to the robot_id of a robots row. A performance_metrics row whose robots row is missing still counts as a link and is RETAINED. (public source tables: robots | public carried/output columns: robot_id | join preservation: left | condition public identifiers: robots, robot_id)
7. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
8. [derive] Name the mart columns; total_amount, active_amount, max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount, max_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
9. [ratio] Guarded ratios: active_amount_ratio — active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [conditional] size_band — Size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band | semantic parameters: boundary=a value exactly at 2 is 'small'; a value exactly at 5 is 'medium')
11. [conditional] has_links — 'yes' when this parent has at least one link, 'no' otherwise. Never NULL. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links | semantic parameters: boundary=a value exactly at 0 is 'no')
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `motion_paths_performance_metrics_cohorts`

- Grain: One row per (path_id, status cohort) pair represented among linked performance_metrics rows, plus one no-activity row for a motion_paths row with no linked performance_metrics row at all. A motion_paths row whose linked performance_metrics rows all lack a metric_name value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'motion_paths_performance_metrics_cohorts' has 19 declared semantic rules:
1. [source] Read source table motion_paths. (public source tables: motion_paths)
2. [source] Read source table performance_metrics. (public source tables: performance_metrics)
3. [derive] Carry each path_id and its path_name into the cohort calculation. (public source tables: motion_paths | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked performance_metrics rows into each motion_paths entity before assigning status cohorts. (public source tables: performance_metrics | public carried/output columns: entity_key, entity_name, path_id | join preservation: left | condition public identifiers: performance_metrics, path_id, entity_key)
5. [filter] Keep rows whose metric_name belongs to the passing cohort values ['cycle time']. (public carried/output columns: entity_key, entity_name | condition literal specification values: cycle time)
6. [distinct] One row per motion_paths entity that has at least one linked performance_metrics row in the passing cohort, reporting the number of those rows, how many different metric_name values occur among them, the total of their metric_value, and their largest metric_value. The total and the largest value read only the rows that carry a metric_value value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose metric_name belongs to the failing cohort values ['efficiency']. (public carried/output columns: entity_key, entity_name | condition literal specification values: efficiency)
10. [distinct] One row per motion_paths entity that has at least one linked performance_metrics row in the failing cohort, reporting the number of those rows, how many different metric_name values occur among them, the total of their metric_value, and their largest metric_value. The total and the largest value read only the rows that carry a metric_value value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a motion_paths entity with no linked performance_metrics row at all; a motion_paths entity that has linked performance_metrics rows gets no placeholder, even when every one of those rows lacks a metric_name value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per motion_paths entity with no linked performance_metrics row at all, reporting 0 rows, 0 different metric_name values, a metric_value total of 0 and a largest metric_value of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

## Source tables

### robots  (source backend: mongodb)
Source table robots of the 3d_path_or_motion_tracking scenario.

- `robot_id`: integer NOT NULL — Unique identifier for each robot
- `robot_name`: text NULL — Name of the robot
- `robot_type`: text NULL — Type of the robot (e.g., robotic arm, AGV) one of: robotic arm, AGV.
- `model`: text NULL — Model of the robot
- `serial_number`: text NULL — Serial number of the robot
- `installation_date`: text NULL — Date the robot was installed
- `last_maintenance_date`: text NULL — Date of the last maintenance
- `location`: text NULL — Location where the robot is installed
- `manufacturer`: text NULL — Manufacturer of the robot
- `contact_email`: text NULL — Contact email for the manufacturer
- `ip_address`: text NULL — IP address of the robot
- `mac_address`: text NULL — MAC address of the robot
- primary key: robot_id

### motion_paths  (source backend: s3)
Source table motion_paths of the 3d_path_or_motion_tracking scenario.

- `path_id`: integer NOT NULL — Unique identifier for each motion path
- `robot_id`: integer NULL — ID of the robot associated with the path
- `path_name`: text NULL — Name of the motion path
- `description`: text NULL — Description of the motion path
- `start_date`: text NULL — Date the path started
- `end_date`: text NULL — Date the path ended
- `version`: text NULL — Version of the motion path
- `status`: text NULL — Status of the path (e.g., active, inactive, archived) one of: active, inactive, archived.
- `created_by`: text NULL — User who created the path
- `modified_by`: text NULL — User who last modified the path
- primary key: path_id

### steps  (source backend: s3)
Source table steps of the 3d_path_or_motion_tracking scenario.

- `step_id`: integer NOT NULL — Unique identifier for each step
- `path_id`: integer NULL — ID of the motion path the step belongs to
- `step_number`: integer NULL — Number of the step in the path
- `step_name`: text NULL — Name of the step (e.g., start, end, intermediate points) one of: start, end, intermediate points.
- `x_coordinate`: float NULL — X coordinate of the step
- `y_coordinate`: float NULL — Y coordinate of the step
- `z_coordinate`: float NULL — Z coordinate of the step
- `timestamp`: text NULL — Timestamp when the step was recorded
- `velocity`: float NULL — Velocity of the robot at the step
- `acceleration`: float NULL — Acceleration of the robot at the step
- primary key: step_id

### path_versions  (source backend: files)
Source table path_versions of the 3d_path_or_motion_tracking scenario.

- `version_id`: integer NOT NULL — Unique identifier for each version
- `path_id`: integer NULL — ID of the motion path
- `version_number`: text NULL — Version number of the path
- `created_date`: text NULL — Date the version was created
- `description`: text NULL — Description of the changes in the version
- `created_by`: text NULL — User who created the version
- `change_log`: text NULL — Detailed change log for the version
- primary key: version_id

### maintenance_records  (source backend: files)
Source table maintenance_records of the 3d_path_or_motion_tracking scenario.

- `record_id`: integer NOT NULL — Unique identifier for each maintenance record
- `robot_id`: integer NULL — ID of the robot
- `maintenance_date`: text NULL — Date the maintenance was performed
- `maintenance_type`: text NULL — Type of maintenance (e.g., routine, emergency) one of: routine, emergency.
- `description`: text NULL — Description of the maintenance performed
- `performed_by`: text NULL — User who performed the maintenance
- `next_maintenance_due`: text NULL — Date the next maintenance is due
- `maintenance_duration`: integer NULL — Duration of the maintenance in minutes
- primary key: record_id

### users  (source backend: rest)
Source table users of the 3d_path_or_motion_tracking scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `user_name`: text NULL — Full name of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., engineer, operator, admin) one of: engineer, operator, admin.
- `department`: text NULL — Department the user belongs to
- `password`: text NULL — Password for the user account
- `last_login_date`: text NULL — Date of the last login
- primary key: user_id

### access_logs  (source backend: mongodb)
Source table access_logs of the 3d_path_or_motion_tracking scenario.

- `access_id`: integer NOT NULL — Unique identifier for each access event
- `user_id`: integer NULL — ID of the user accessing the system
- `access_date`: text NULL — Date and time of the access
- `access_type`: text NULL — Type of access (e.g., view, edit, delete) one of: view, edit, delete.
- `robot_id`: integer NULL — ID of the robot being accessed
- `path_id`: integer NULL — ID of the motion path being accessed
- `access_result`: text NULL — Result of the access (e.g., success, failure) one of: success, failure.
- primary key: access_id

### performance_metrics  (source backend: mongodb)
Source table performance_metrics of the 3d_path_or_motion_tracking scenario.

- `metric_id`: integer NOT NULL — Unique identifier for each performance metric
- `robot_id`: integer NULL — ID of the robot
- `path_id`: integer NULL — ID of the motion path
- `metric_name`: text NULL — Name of the performance metric (e.g., cycle time, efficiency) one of: cycle time, efficiency.
- `metric_value`: float NULL — Value of the performance metric
- `recorded_date`: text NULL — Date the metric was recorded
- `unit`: text NULL — Unit of the metric (e.g., seconds, percent) one of: seconds, percent.
- `target_value`: float NULL — Target value for the metric
- `threshold_value`: float NULL — Threshold value for the metric
- primary key: metric_id

### machine_learning_models  (source backend: mongodb)
Source table machine_learning_models of the 3d_path_or_motion_tracking scenario.

- `model_id`: integer NOT NULL — Unique identifier for each machine learning model
- `model_name`: text NULL — Name of the machine learning model
- `model_type`: text NULL — Type of the machine learning model (e.g., regression, classification) one of: regression, classification.
- `algorithm`: text NULL — Algorithm used for training the model
- `training_date`: text NULL — Date the model was trained
- `accuracy`: float NULL — Accuracy of the model
- `description`: text NULL — Description of the model
- primary key: model_id

### optimization_results  (source backend: mongodb)
Source table optimization_results of the 3d_path_or_motion_tracking scenario.

- `result_id`: integer NOT NULL — Unique identifier for each optimization result
- `path_id`: integer NULL — ID of the motion path
- `optimization_date`: text NULL — Date the optimization was performed
- `optimization_type`: text NULL — Type of optimization (e.g., speed, efficiency) one of: speed, efficiency.
- `result`: text NULL — Result of the optimization
- primary key: result_id

### notifications  (source backend: mongodb)
Source table notifications of the 3d_path_or_motion_tracking scenario.

- `notification_id`: integer NOT NULL — Unique identifier for each notification
- `robot_id`: integer NULL — ID of the robot
- `notification_date`: text NULL — Date the notification was sent
- `notification_type`: text NULL — Type of notification (e.g., maintenance, optimization) one of: maintenance, optimization.
- `message`: text NULL — Message of the notification
- primary key: notification_id

### system_settings  (source backend: rest)
Source table system_settings of the 3d_path_or_motion_tracking scenario.

- `setting_id`: integer NOT NULL — Unique identifier for each system setting
- `setting_name`: text NULL — Name of the system setting
- `setting_value`: text NULL — Value of the system setting
- `description`: text NULL — Description of the system setting
- primary key: setting_id

### Relationships

- access_logs(path_id) -> motion_paths(path_id) [optional (may be NULL/dangling)]
- access_logs(robot_id) -> robots(robot_id) [optional (may be NULL/dangling)]
- access_logs(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- maintenance_records(robot_id) -> robots(robot_id) [optional (may be NULL/dangling)]
- motion_paths(robot_id) -> robots(robot_id) [optional (may be NULL/dangling)]
- notifications(robot_id) -> robots(robot_id) [optional (may be NULL/dangling)]
- optimization_results(path_id) -> motion_paths(path_id) [optional (may be NULL/dangling)]
- path_versions(path_id) -> motion_paths(path_id) [optional (may be NULL/dangling)]
- performance_metrics(path_id) -> motion_paths(path_id) [optional (may be NULL/dangling)]
- performance_metrics(robot_id) -> robots(robot_id) [optional (may be NULL/dangling)]
- steps(path_id) -> motion_paths(path_id) [optional (may be NULL/dangling)]

