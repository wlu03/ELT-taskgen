# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# 3D Spatial Data Monitoring And Analysis

## Specification

PROJECT OVERVIEW

This project, 3D Spatial Data Monitoring And Analysis, builds two analytical marts over the operational records of a drone fleet. Nine source tables feed the scenario, and each must be extracted from the backend named here.

Source tables and their extraction backends:
- Source table drones must be extracted from the files backend. It carries one row per drone, keyed by drone_id, with drone_model, drone_number, status (either active or inactive), registration_number, manufacture_date, last_inspection_date, next_inspection_date, battery_capacity, maximum_payload and flight_controller_firmware_version.
- Source table drone_positions must be extracted from the files backend. It carries one row per position record, keyed by position_id, with drone_id, pos_x, pos_y, pos_z, timestamp, altitude, latitude, longitude and accuracy.
- Source table drone_velocities must be extracted from the s3 backend. It carries one row per velocity record, keyed by velocity_id, with drone_id, vel_x, vel_y, vel_z, timestamp, acceleration_x, acceleration_y and acceleration_z.
- Source table drone_speeds must be extracted from the files backend. It carries one row per speed record, keyed by speed_id, with drone_id, speed, timestamp, ground_speed and air_speed.
- Source table flight_logs must be extracted from the s3 backend. It carries one row per flight log, keyed by flight_id, with drone_id, start_time, end_time, status (either completed or aborted), starting_point, ending_point, route_id, total_distance, average_speed and battery_used.
- Source table users must be extracted from the rest backend. It carries one row per user, keyed by user_id, with username, email, role (either operations manager or data analyst), last_login, created_at, updated_at and status (either active or inactive).
- Source table user_access_logs must be extracted from the rest backend. It carries one row per access record, keyed by access_id, with user_id, access_time, access_type (either view or edit), ip_address and user_agent.
- Source table alerts must be extracted from the s3 backend. It carries one row per alert, keyed by alert_id, with drone_id, alert_message, timestamp, alert_type (system, environmental or hardware), severity (low, medium or high), resolved and resolution_time.
- Source table maintenance_records must be extracted from the rest backend. It carries one row per maintenance record, keyed by maintenance_id, with drone_id, maintenance_date, details, technician_id, parts_replaced, maintenance_cost and maintenance_notes.

Relationships between the source tables (each stated as child table with its key referring to parent table with its key, and labelled exactly as the schema labels it):
- Child table alerts with key drone_id refers to parent table drones with key drone_id; this relationship is optional (the child value may be NULL or dangling).
- Child table drone_positions with key drone_id refers to parent table drones with key drone_id; this relationship is optional (the child value may be NULL or dangling).
- Child table drone_speeds with key drone_id refers to parent table drones with key drone_id; this relationship is optional (the child value may be NULL or dangling).
- Child table drone_velocities with key drone_id refers to parent table drones with key drone_id; this relationship is optional (the child value may be NULL or dangling).
- Child table flight_logs with key drone_id refers to parent table drones with key drone_id; this relationship is optional (the child value may be NULL or dangling).
- Child table maintenance_records with key drone_id refers to parent table drones with key drone_id; this relationship is optional (the child value may be NULL or dangling).
- Child table maintenance_records with key technician_id refers to parent table users with key user_id; this relationship is optional (the child value may be NULL or dangling).
- Child table user_access_logs with key user_id refers to parent table users with key user_id; this relationship is optional (the child value may be NULL or dangling).

Both marts are built from drones and flight_logs only; the other source tables are part of the scenario's extraction surface and are not read by either mart.


MART drones_flight_logs_cohorts — a per-(drones, status cohort) summary of linked flight_logs rows in the 3d_spatial_data_monitoring_and_analysis scenario, with the passing and failing cohorts kept separate.

Grain: one row per (drone_id, status cohort) pair represented among linked flight_logs rows, plus one no-activity row for a drones row with no linked flight_logs row at all. A drones row whose linked flight_logs rows all lack a status value is in no cohort and gets no no-activity row, so it has no row in this mart.

Key columns: entity_key and cohort together identify a row of this mart.

Output columns:
- entity_key (integer): the identifier of the drones row.
- cohort (text): 'passing' for status values ['completed']; 'failing' for values ['aborted']; 'no_activity' when the drones row has no linked flight_logs row. A linked flight_logs row whose status has no value belongs to no cohort: it is not counted in any cell, and it does not make the drones row 'no_activity'.
- entity_name (text): the drone_model of the drones row, copied unchanged.
- link_count (bigint): the number of flight_logs rows in this entity/cohort cell.
- distinct_status_count (bigint): the number of unique status values represented in this cell.
- total_amount (integer): the total of route_id in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an route_id value.
- max_amount (integer): the largest route_id in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an route_id value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules that produce this mart:

Rule 1. Read source table drones; every row of source table drones is available to this mart.

Rule 2. Read source table flight_logs; every row of source table flight_logs is available to this mart.

Rule 3. From source table drones, each drone_id is carried as entity_key and its drone_model is carried as entity_name into the cohort calculation.

Rule 4. The linked flight_logs rows are brought into each drones entity before assigning status cohorts, matching a flight_logs row to an entity when its drone_id equals that entity's entity_key; preservation is left-sided, so a drones entity with no matching flight_logs row is kept, carrying entity_key, entity_name and drone_id.

Rule 5. For the passing cohort, the rows kept are those whose status is completed, carrying entity_key and entity_name.

Rule 6. There is one row per drones entity that has at least one linked flight_logs row in the passing cohort, carrying entity_key and entity_name and reporting link_count as the number of those rows, distinct_status_count as how many different status values occur among them, total_amount as the total of their route_id, and max_amount as their largest route_id. The total and the largest value read only the rows that carry a route_id value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 7. For each of these passing-cohort rows, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, the max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures are labelled as the passing cohort: each such row has cohort set to the text passing, alongside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 9. For the failing cohort, the rows kept are those whose status is aborted, carrying entity_key and entity_name.

Rule 10. There is one row per drones entity that has at least one linked flight_logs row in the failing cohort, carrying entity_key and entity_name and reporting link_count as the number of those rows, distinct_status_count as how many different status values occur among them, total_amount as the total of their route_id, and max_amount as their largest route_id. The total and the largest value read only the rows that carry a route_id value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 11. For each of these failing-cohort rows, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, the max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures are labelled as the failing cohort: each such row has cohort set to the text failing, alongside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 13. The placeholder row is kept for a drones entity with no linked flight_logs row at all, carrying entity_key and entity_name; a drones entity that has linked flight_logs rows gets no placeholder, even when every one of those rows lacks a status value.

Rule 14. There is one row per drones entity with no linked flight_logs row at all, carrying entity_key and entity_name and reporting link_count as 0 rows, distinct_status_count as 0 different status values, total_amount as a route_id total of 0 and max_amount as a largest route_id of 0.

Rule 15. For each of these no-activity rows, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, the max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 16. These measures are labelled as the no_activity cohort: each such row has cohort set to the text no_activity, alongside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 17. The disjoint passing and failing cohort summaries are combined into one list, keeping all rows of both, each carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 18. The no-activity summaries are added to that list, keeping all of their rows too, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 19. Deterministic output order: rows appear in ascending entity_key order, and within one entity in ascending cohort order.


MART drones_flight_logs_distribution — a per-(drones, measure state) distribution of linked flight_logs rows in the 3d_spatial_data_monitoring_and_analysis scenario.

Grain: one row per (drone_id, measure state) pair represented among linked flight_logs rows; the absent state includes missing route_id values and a no-activity row for a drones row with no links. A linked flight_logs row whose route_id has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state together identify a row of this mart.

Output columns:
- entity_key (integer): the identifier of the drones row.
- measure_state (text): 'present' for a linked flight_logs row whose route_id has a value; 'absent' when route_id is missing, including a drones row with no linked flight_logs row. A linked flight_logs row whose route_id has a value belongs only to the present state and never to the absent state.
- entity_name (text): the drone_model of the drones row, copied unchanged.
- row_count (bigint): the number of linked flight_logs rows in this entity/state cell; a absent cell holding real flight_logs rows whose route_id is missing COUNTS those rows, and only the placeholder cell of a drones row with no linked flight_logs row at all reports 0.
- distinct_amount_count (bigint): the number of unique non-missing route_id values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no route_id value at all — both for a drones row with no linked flight_logs row and for an absent cell whose rows all have a missing route_id.
- total_amount (integer): the total of route_id in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an route_id value.
- max_amount (integer): the largest route_id in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an route_id value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules that produce this mart:

Rule 1. Read source table drones; every row of source table drones is available to this mart.

Rule 2. Read source table flight_logs; every row of source table flight_logs is available to this mart.

Rule 3. From source table drones, each drone_id is carried as entity_key and its drone_model is carried as entity_name into the measure-state calculation.

Rule 4. The linked flight_logs rows are brought into each drones entity, matching a flight_logs row to an entity when its drone_id equals that entity's entity_key; preservation is left-sided, so an entity with no linked row is retained so its absent state is visible, carrying entity_key, entity_name and drone_id.

Rule 5. The present measure-state rows kept are those that are a real flight_logs row whose route_id has a value, carrying entity_key and entity_name.

Rule 6. There is one row per drones entity that has at least one row in the present measure state, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different non-missing route_id values occur (each different value counted once, however many rows repeat it), total_amount as the total route_id, and max_amount as the largest route_id.

Rule 7. For each of these present-state rows, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, the max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures are labelled as the present measure state: each such row has measure_state set to the text present, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows kept are those where route_id is missing, including the retained placeholder for a drones row with no flight_logs rows, carrying entity_key and entity_name; a real flight_logs row whose route_id has a value belongs only to the present state and never to this absent state.

Rule 10. There is one row per drones entity that has at least one row in the absent measure state, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different non-missing route_id values occur (each different value counted once, however many rows repeat it), total_amount as the total route_id, and max_amount as the largest route_id.

Rule 11. For each of these absent-state rows, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, the max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures are labelled as the absent measure state: each such row has measure_state set to the text absent, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other; every row carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 14. Deterministic output order: rows appear in ascending entity_key order, and within one entity in ascending measure_state order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `drones_flight_logs_cohorts`

- Grain: One row per (drone_id, status cohort) pair represented among linked flight_logs rows, plus one no-activity row for a drones row with no linked flight_logs row at all. A drones row whose linked flight_logs rows all lack a status value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'drones_flight_logs_cohorts' has 19 declared semantic rules:
1. [source] Read source table drones. (public source tables: drones)
2. [source] Read source table flight_logs. (public source tables: flight_logs)
3. [derive] Carry each drone_id and its drone_model into the cohort calculation. (public source tables: drones | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked flight_logs rows into each drones entity before assigning status cohorts. (public source tables: flight_logs | public carried/output columns: entity_key, entity_name, drone_id | join preservation: left | condition public identifiers: flight_logs, drone_id, entity_key)
5. [filter] Keep rows whose status belongs to the passing cohort values ['completed']. (public carried/output columns: entity_key, entity_name | condition literal specification values: completed)
6. [distinct] One row per drones entity that has at least one linked flight_logs row in the passing cohort, reporting the number of those rows, how many different status values occur among them, the total of their route_id, and their largest route_id. The total and the largest value read only the rows that carry a route_id value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose status belongs to the failing cohort values ['aborted']. (public carried/output columns: entity_key, entity_name | condition literal specification values: aborted)
10. [distinct] One row per drones entity that has at least one linked flight_logs row in the failing cohort, reporting the number of those rows, how many different status values occur among them, the total of their route_id, and their largest route_id. The total and the largest value read only the rows that carry a route_id value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a drones entity with no linked flight_logs row at all; a drones entity that has linked flight_logs rows gets no placeholder, even when every one of those rows lacks a status value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per drones entity with no linked flight_logs row at all, reporting 0 rows, 0 different status values, a route_id total of 0 and a largest route_id of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

### `drones_flight_logs_distribution`

- Grain: One row per (drone_id, measure state) pair represented among linked flight_logs rows; the absent state includes missing route_id values and a no-activity row for a drones row with no links. A linked flight_logs row whose route_id has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'drones_flight_logs_distribution' has 14 declared semantic rules:
1. [source] Read source table drones. (public source tables: drones)
2. [source] Read source table flight_logs. (public source tables: flight_logs)
3. [derive] Carry each drone_id and its drone_model into the measure-state calculation. (public source tables: drones | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked flight_logs rows into each drones entity; retain an entity with no linked row so its absent state is visible. (public source tables: flight_logs | public carried/output columns: entity_key, entity_name, drone_id | join preservation: left | condition public identifiers: flight_logs, drone_id, entity_key)
5. [filter] Keep the present measure-state rows: a real flight_logs row whose route_id has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per drones entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing route_id values occur (each different value counted once, however many rows repeat it), total route_id, and largest route_id. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: route_id is missing, including the retained placeholder for a drones row with no flight_logs rows. A real flight_logs row whose route_id has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per drones entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing route_id values occur (each different value counted once, however many rows repeat it), total route_id, and largest route_id. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### drones  (source backend: files)
Source table drones of the 3d_spatial_data_monitoring_and_analysis scenario.

- `drone_id`: integer NOT NULL — Unique identifier for each drone
- `drone_model`: text NULL — Model type of the drone
- `drone_number`: text NULL — Identification number of the drone
- `status`: text NULL — Current operational status of the drone (e.g., active, inactive) one of: active, inactive.
- `registration_number`: text NULL — Unique registration number for each drone
- `manufacture_date`: text NULL — Date the drone was manufactured
- `last_inspection_date`: text NULL — Date of the last inspection
- `next_inspection_date`: text NULL — Date of the next scheduled inspection
- `battery_capacity`: integer NULL — Maximum battery capacity in mAh
- `maximum_payload`: integer NULL — Maximum payload capacity in grams
- `flight_controller_firmware_version`: text NULL — Current firmware version of the flight controller
- primary key: drone_id

### drone_positions  (source backend: files)
Source table drone_positions of the 3d_spatial_data_monitoring_and_analysis scenario.

- `position_id`: integer NOT NULL — Unique identifier for each position record
- `drone_id`: integer NULL — Reference to the drone that the position data belongs to
- `pos_x`: float NULL — X coordinate of the drone's position
- `pos_y`: float NULL — Y coordinate of the drone's position
- `pos_z`: float NULL — Z coordinate of the drone's position (altitude)
- `timestamp`: text NULL — Timestamp of when the position was recorded
- `altitude`: float NULL — Altitude of the drone
- `latitude`: float NULL — Latitude of the drone
- `longitude`: float NULL — Longitude of the drone
- `accuracy`: float NULL — Accuracy of the position data
- primary key: position_id

### drone_velocities  (source backend: s3)
Source table drone_velocities of the 3d_spatial_data_monitoring_and_analysis scenario.

- `velocity_id`: integer NOT NULL — Unique identifier for each velocity record
- `drone_id`: integer NULL — Reference to the drone that the velocity data belongs to
- `vel_x`: float NULL — Velocity in the X direction
- `vel_y`: float NULL — Velocity in the Y direction
- `vel_z`: float NULL — Velocity in the Z direction
- `timestamp`: text NULL — Timestamp of when the velocity was recorded
- `acceleration_x`: float NULL — Acceleration in the X direction
- `acceleration_y`: float NULL — Acceleration in the Y direction
- `acceleration_z`: float NULL — Acceleration in the Z direction
- primary key: velocity_id

### drone_speeds  (source backend: files)
Source table drone_speeds of the 3d_spatial_data_monitoring_and_analysis scenario.

- `speed_id`: integer NOT NULL — Unique identifier for each speed record
- `drone_id`: integer NULL — Reference to the drone that the speed data belongs to
- `speed`: float NULL — Calculated speed of the drone
- `timestamp`: text NULL — Timestamp of when the speed was recorded
- `ground_speed`: float NULL — Ground speed of the drone
- `air_speed`: float NULL — Air speed of the drone
- primary key: speed_id

### flight_logs  (source backend: s3)
Source table flight_logs of the 3d_spatial_data_monitoring_and_analysis scenario.

- `flight_id`: integer NOT NULL — Unique identifier for each flight log
- `drone_id`: integer NULL — Reference to the drone that the flight log belongs to
- `start_time`: text NULL — Start time of the drone flight
- `end_time`: text NULL — End time of the drone flight
- `status`: text NULL — Status of the flight (e.g., completed, aborted) one of: completed, aborted.
- `starting_point`: text NULL — Starting point of the flight
- `ending_point`: text NULL — Ending point of the flight
- `route_id`: integer NULL — ID of the planned route
- `total_distance`: float NULL — Total distance covered in the flight
- `average_speed`: float NULL — Average speed of the flight
- `battery_used`: float NULL — Battery used during the flight
- primary key: flight_id

### users  (source backend: rest)
Source table users of the 3d_spatial_data_monitoring_and_analysis scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `username`: text NULL — Username of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., operations manager, data analyst) one of: operations manager, data analyst.
- `last_login`: text NULL — Last time the user logged in
- `created_at`: text NULL — Date and time the user account was created
- `updated_at`: text NULL — Date and time the user account was last updated
- `status`: text NULL — Status of the user account (e.g., active, inactive) one of: active, inactive.
- primary key: user_id

### user_access_logs  (source backend: rest)
Source table user_access_logs of the 3d_spatial_data_monitoring_and_analysis scenario.

- `access_id`: integer NOT NULL — Unique identifier for each access record
- `user_id`: integer NULL — Reference to the user accessing the system
- `access_time`: text NULL — Time when the user accessed the system
- `access_type`: text NULL — Type of access (e.g., view, edit) one of: view, edit.
- `ip_address`: text NULL — IP address of the user accessing the system
- `user_agent`: text NULL — User agent string of the device used to access the system
- primary key: access_id

### alerts  (source backend: s3)
Source table alerts of the 3d_spatial_data_monitoring_and_analysis scenario.

- `alert_id`: integer NOT NULL — Unique identifier for each alert
- `drone_id`: integer NULL — Reference to the drone related to the alert
- `alert_message`: text NULL — Message detailing the alert
- `timestamp`: text NULL — Timestamp of when the alert was generated
- `alert_type`: text NULL — Type of the alert (e.g., system, environmental, hardware) one of: system, environmental, hardware.
- `severity`: text NULL — Severity level of the alert (e.g., low, medium, high) one of: low, medium, high.
- `resolved`: integer NULL — Boolean indicating if the alert has been resolved
- `resolution_time`: text NULL — Time when the alert was resolved
- primary key: alert_id

### maintenance_records  (source backend: rest)
Source table maintenance_records of the 3d_spatial_data_monitoring_and_analysis scenario.

- `maintenance_id`: integer NOT NULL — Unique identifier for each maintenance record
- `drone_id`: integer NULL — Reference to the drone that underwent maintenance
- `maintenance_date`: text NULL — Date when maintenance was performed
- `details`: text NULL — Details of the maintenance performed
- `technician_id`: integer NULL — ID of the technician who performed the maintenance
- `parts_replaced`: text NULL — Parts replaced during maintenance
- `maintenance_cost`: float NULL — Cost of the maintenance
- `maintenance_notes`: text NULL — Additional notes about the maintenance
- primary key: maintenance_id

### Relationships

- alerts(drone_id) -> drones(drone_id) [optional (may be NULL/dangling)]
- drone_positions(drone_id) -> drones(drone_id) [optional (may be NULL/dangling)]
- drone_speeds(drone_id) -> drones(drone_id) [optional (may be NULL/dangling)]
- drone_velocities(drone_id) -> drones(drone_id) [optional (may be NULL/dangling)]
- flight_logs(drone_id) -> drones(drone_id) [optional (may be NULL/dangling)]
- maintenance_records(drone_id) -> drones(drone_id) [optional (may be NULL/dangling)]
- maintenance_records(technician_id) -> users(user_id) [optional (may be NULL/dangling)]
- user_access_logs(user_id) -> users(user_id) [optional (may be NULL/dangling)]

