# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# 3D Motion Tracking And Analysis

## Specification

PROJECT OVERVIEW: 3D Motion Tracking And Analysis

This project builds two analytical marts over the 3d_motion_tracking_and_analysis scenario. Seven source tables must be extracted, each from its own backend, and each backend is mandatory.

Source table motion_data must be extracted from the postgres backend. It holds one row per recorded data point, identified by data_id (its primary key), and also carries timestamp, x_position, y_position, z_position, yaw, pitch, roll, sensor_id, data_quality (which is one of good, bad or uncertain when present), sampling_rate and data_source (one of sensor, simulation or external system when present). Every column other than data_id may have no value.

Source table sensors must be extracted from the files backend. It holds one row per sensor, identified by sensor_id (its primary key), and also carries sensor_name, sensor_type (IMU or GPS), location (Robot Arm or VR Headset), description, calibration_date, status (active, inactive or maintenance), firmware_version, software_version and manufacturer. Every column other than sensor_id may have no value.

Source table users must be extracted from the postgres backend, keyed by user_id, with username, password, email, role (engineer, data analyst or administrator) and last_login.

Source table roles must be extracted from the rest backend, keyed by role_id, with role_name and description.

Source table permissions must be extracted from the mongodb backend, keyed by permission_id, with permission_name and description.

Source table user_roles must be extracted from the mongodb backend, keyed by user_id, with role_id.

Source table role_permissions must be extracted from the rest backend, keyed by role_id, with permission_id.

RELATIONSHIP: the child table motion_data through its key sensor_id refers to the parent table sensors through its key sensor_id, and this relationship is optional — a motion_data row may carry no sensor_id at all, and a sensor_id it does carry may match no sensors row. This is the only relationship declared in the source schema.

Both marts below read only the sensors table and the motion_data table. Throughout this specification, a motion_data row is said to be linked to, or attributed to, a sensors row when the motion_data row carries the same sensor_id value as that sensors row; a motion_data row with no sensor_id value, or with a sensor_id value that appears in no sensors row, is linked to no sensors row. Fractions are reported rounded to 4 decimal places.

=== Mart sensors_motion_data_cohorts — Per-(sensors, status cohort) summary of linked motion_data rows in the 3d_motion_tracking_and_analysis scenario, with passing and failing cohorts kept separate ===

This mart is the per-(sensors, status cohort) summary of linked motion_data rows in the 3d_motion_tracking_and_analysis scenario, with passing and failing cohorts kept separate.

Grain: one row per (sensor_id, status cohort) pair represented among linked motion_data rows, plus one no-activity row for a sensors row with no linked motion_data row at all. A sensors row whose linked motion_data rows all lack a data_quality value is in no cohort and gets no no-activity row, so it has no row in this mart.

The key columns of this mart are entity_key and cohort; together they identify one output row.

Output columns:

- entity_key: the identifier of the sensors row.
- cohort: 'passing' for data_quality values ['good']; 'failing' for values ['bad', 'uncertain']; 'no_activity' when the sensors row has no linked motion_data row. A linked motion_data row whose data_quality has no value belongs to no cohort: it is not counted in any cell, and it does not make the sensors row 'no_activity'.
- entity_name: the sensor_name of the sensors row, copied unchanged.
- link_count: the number of motion_data rows in this entity/cohort cell.
- distinct_status_count: the number of distinct data_quality values represented in this cell.
- total_amount: the total of x_position in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an x_position value.
- max_amount: the largest x_position in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an x_position value.
- max_amount_share: max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules for sensors_motion_data_cohorts:

1. The source table sensors is read in full as an input of this mart.

2. The source table motion_data is read in full as an input of this mart.

3. Each sensor_id of sensors and its sensor_name are carried into the cohort calculation as entity_key and entity_name respectively, taken from the sensors table.

4. The linked motion_data rows are brought into each sensors entity before assigning status cohorts, matching a motion_data row to an entity when the motion_data column sensor_id equals that entity's entity_key; preservation is left-sided, so a sensors entity with no matching motion_data row is kept at this point, carrying entity_key, entity_name and sensor_id.

5. For the passing cohort, only rows whose data_quality belongs to the passing cohort values ['good'] — that is, the value good — are kept, carrying entity_key and entity_name.

6. There is one row per sensors entity that has at least one linked motion_data row in the passing cohort, reporting as entity_key and entity_name the number of those rows in link_count, how many different data_quality values occur among them in distinct_status_count, the total of their x_position in total_amount, and their largest x_position in max_amount. The total and the largest value read only the rows that carry a x_position value; a cohort whose rows all lack one reports 0 for both, never empty.

7. For each such passing-cohort row, alongside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

8. These measures are labelled as the passing cohort: the row carries cohort with the value 'passing' beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

9. For the failing cohort, only rows whose data_quality belongs to the failing cohort values ['bad', 'uncertain'] — that is, the values bad and uncertain — are kept, carrying entity_key and entity_name.

10. There is one row per sensors entity that has at least one linked motion_data row in the failing cohort, reporting as entity_key and entity_name the number of those rows in link_count, how many different data_quality values occur among them in distinct_status_count, the total of their x_position in total_amount, and their largest x_position in max_amount. The total and the largest value read only the rows that carry a x_position value; a cohort whose rows all lack one reports 0 for both, never empty.

11. For each such failing-cohort row, alongside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

12. These measures are labelled as the failing cohort: the row carries cohort with the value 'failing' beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

13. The placeholder row, carrying entity_key and entity_name, is kept for a sensors entity with no linked motion_data row at all; a sensors entity that has linked motion_data rows gets no placeholder, even when every one of those rows lacks a data_quality value.

14. There is one row per sensors entity with no linked motion_data row at all, carrying entity_key and entity_name and reporting 0 rows in link_count, 0 different data_quality values in distinct_status_count, a x_position total of 0 in total_amount and a largest x_position of 0 in max_amount.

15. For each such no-activity row, alongside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

16. These measures are labelled as the no_activity cohort: the row carries cohort with the value no_activity beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

17. The disjoint passing and failing cohort summaries are combined into one body of rows, all rows of both kept, each contributing entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

18. The no-activity summaries are added to that combined body, all such rows kept, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

19. The deterministic output order is entity, then cohort: rows appear in ascending entity_key order, and within one entity_key in ascending cohort order.

=== Mart sensors_motion_data_bands — Per-sensors banding of linked motion_data activity in the 3d_motion_tracking_and_analysis scenario, over the value domain the source schema itself declares ===

This mart is the per-sensors banding of linked motion_data activity in the 3d_motion_tracking_and_analysis scenario, over the value domain the source schema itself declares.

Grain: one row per sensors (sensor_id), including sensors rows with no linked motion_data rows.

The key column of this mart is parent_key; each parent_key value appears on exactly one output row.

Output columns:

- parent_key: the identifier of the sensors row. One row per value.
- parent_name: the sensor_name of the sensors row, copied unchanged.
- parent_status: the status of the sensors row, copied unchanged. Declared domain: ['active', 'inactive', 'maintenance'].
- link_count: the number of motion_data rows for this sensors row; 0 when there are none. Every linked motion_data row counts, whatever its data_quality value. A sensors row kept with no motion_data row reports 0 here, never 1: its placeholder holds no motion_data row to count.
- passing_count: of those, how many have data_quality in ['good']. 0, never missing, when none do.
- failing_count: how many have data_quality in ['bad', 'uncertain']. 0 when none do.
- distinct_status_count: how many distinct data_quality values occur among them.
- passing_ratio: passing_count divided by link_count as a fraction between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links.
- adoption_band: the band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or above (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the higher band.
- status_group: parent_status mapped value by value: 'active' becomes 'active'; 'inactive' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never null.

Rules for sensors_motion_data_bands:

1. The source table sensors is read in full as an input of this mart.

2. The source table motion_data is read in full as an input of this mart.

3. There is one row per sensors row, keyed by sensor_id, carrying parent_key, parent_name and parent_status from the sensors table.

4. The motion_data rows are brought in (hop 1 of 1) by matching the motion_data column sensor_id to parent_key, carrying sensor_id; preservation is left-sided, so rows with no matching motion_data row are RETAINED and report the declared defaults.

5. There is one output row per parent_key, carrying parent_name and parent_status beside the keys: a parent_key value identifies one source row for the carried columns, so they take one value per key and never split a set of rows, reporting link_count, passing_count, failing_count and distinct_status_count for that row's matching rows. A set with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

6. The mart columns are named parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count.

7. Guarded ratios: beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count, passing_ratio is passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when there are no links — that is, 0.0 when the denominator is 0 or has no value.

8. adoption_band is carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count and passing_ratio as the band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), and otherwise 'low' below 0.5. Boundaries are inclusive of the HIGHER band, and the value is never null or blank; the declared parent_status domain remains active, inactive, maintenance.

9. status_group is carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio and adoption_band as parent_status mapped value by value: 'active' becomes 'active'; 'inactive' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name, the declared domain being active, inactive and maintenance — otherwise becomes 'unmapped'. It is never NULL; this is a categorical mapping with no numeric boundary.

10. The deterministic output order is by parent_key: rows appear sorted in ascending parent_key order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `sensors_motion_data_cohorts`

- Grain: One row per (sensor_id, status cohort) pair represented among linked motion_data rows, plus one no-activity row for a sensors row with no linked motion_data row at all. A sensors row whose linked motion_data rows all lack a data_quality value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'sensors_motion_data_cohorts' has 19 declared semantic rules:
1. [source] Read source table sensors. (public source tables: sensors)
2. [source] Read source table motion_data. (public source tables: motion_data)
3. [derive] Carry each sensor_id and its sensor_name into the cohort calculation. (public source tables: sensors | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked motion_data rows into each sensors entity before assigning status cohorts. (public source tables: motion_data | public carried/output columns: entity_key, entity_name, sensor_id | join preservation: left | condition public identifiers: motion_data, sensor_id, entity_key)
5. [filter] Keep rows whose data_quality belongs to the passing cohort values ['good']. (public carried/output columns: entity_key, entity_name | condition literal specification values: good)
6. [distinct] One row per sensors entity that has at least one linked motion_data row in the passing cohort, reporting the number of those rows, how many different data_quality values occur among them, the total of their x_position, and their largest x_position. The total and the largest value read only the rows that carry a x_position value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose data_quality belongs to the failing cohort values ['bad', 'uncertain']. (public carried/output columns: entity_key, entity_name | condition literal specification values: bad, uncertain)
10. [distinct] One row per sensors entity that has at least one linked motion_data row in the failing cohort, reporting the number of those rows, how many different data_quality values occur among them, the total of their x_position, and their largest x_position. The total and the largest value read only the rows that carry a x_position value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a sensors entity with no linked motion_data row at all; a sensors entity that has linked motion_data rows gets no placeholder, even when every one of those rows lacks a data_quality value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per sensors entity with no linked motion_data row at all, reporting 0 rows, 0 different data_quality values, a x_position total of 0 and a largest x_position of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

### `sensors_motion_data_bands`

- Grain: One row per sensors (sensor_id), INCLUDING sensors rows with no linked motion_data rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group

```text
Mart 'sensors_motion_data_bands' has 10 declared semantic rules:
1. [source] Read source table sensors. (public source tables: sensors)
2. [source] Read source table motion_data. (public source tables: motion_data)
3. [derive] One row per sensors row, keyed by sensor_id. (public source tables: sensors | public carried/output columns: parent_key, parent_name, parent_status)
4. [join] Bring in motion_data (hop 1 of 1): rows with no matching motion_data row are RETAINED and report the declared defaults. (public source tables: motion_data | public carried/output columns: sensor_id | join preservation: left | condition public identifiers: motion_data, sensor_id, parent_key)
5. [filtered_aggregate] One output row per parent_key, carrying parent_name, parent_status beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, passing_count, failing_count, distinct_status_count for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
6. [derive] Name the mart columns. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
7. [ratio] Guarded ratios: passing_ratio — passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
8. [conditional] adoption_band — Band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the HIGHER band. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band | semantic parameters: boundary=a value exactly at 0.8 is 'high'; a value exactly at 0.5 is 'medium'; domain=active, inactive, maintenance)
9. [conditional] status_group — parent_status mapped value by value: 'active' becomes 'active'; 'inactive' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never NULL. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group | semantic parameters: boundary=categorical mapping; no numeric boundary; domain=active, inactive, maintenance)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### motion_data  (source backend: postgres)
Source table motion_data of the 3d_motion_tracking_and_analysis scenario.

- `data_id`: integer NOT NULL — Unique identifier for each data point
- `timestamp`: text NULL — Timestamp of when the data point was recorded
- `x_position`: float NULL — X position in the 3D coordinate system
- `y_position`: float NULL — Y position in the 3D coordinate system
- `z_position`: float NULL — Z position in the 3D coordinate system
- `yaw`: float NULL — Yaw angle (rotation around the Z-axis)
- `pitch`: float NULL — Pitch angle (rotation around the X-axis)
- `roll`: float NULL — Roll angle (rotation around the Y-axis)
- `sensor_id`: integer NULL — ID of the sensor or device that recorded the data point
- `data_quality`: text NULL — Quality of the data point (e.g., good, bad, uncertain) one of: good, bad, uncertain.
- `sampling_rate`: float NULL — Sampling rate of the sensor or device
- `data_source`: text NULL — Source of the data (e.g., sensor, simulation, external system) one of: sensor, simulation, external system.
- primary key: data_id

### sensors  (source backend: files)
Source table sensors of the 3d_motion_tracking_and_analysis scenario.

- `sensor_id`: integer NOT NULL — Unique identifier for each sensor
- `sensor_name`: text NULL — Name of the sensor
- `sensor_type`: text NULL — Type of sensor (e.g., IMU, GPS) one of: IMU, GPS.
- `location`: text NULL — Location of the sensor (e.g., Robot Arm, VR Headset) one of: Robot Arm, VR Headset.
- `description`: text NULL — Description of the sensor
- `calibration_date`: text NULL — Date the sensor was last calibrated
- `status`: text NULL — Current status of the sensor (e.g., active, inactive, maintenance) one of: active, inactive, maintenance.
- `firmware_version`: text NULL — Firmware version of the sensor
- `software_version`: text NULL — Software version of the sensor
- `manufacturer`: text NULL — Manufacturer of the sensor
- primary key: sensor_id

### users  (source backend: postgres)
Source table users of the 3d_motion_tracking_and_analysis scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `username`: text NULL — Username chosen by the user
- `password`: text NULL — Password for the user's account
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., engineer, data analyst, administrator) one of: engineer, data analyst, administrator.
- `last_login`: text NULL — Timestamp of the user's last login
- primary key: user_id

### roles  (source backend: rest)
Source table roles of the 3d_motion_tracking_and_analysis scenario.

- `role_id`: integer NOT NULL — Unique identifier for each role
- `role_name`: text NULL — Name of the role
- `description`: text NULL — Description of the role
- primary key: role_id

### permissions  (source backend: mongodb)
Source table permissions of the 3d_motion_tracking_and_analysis scenario.

- `permission_id`: integer NOT NULL — Unique identifier for each permission
- `permission_name`: text NULL — Name of the permission
- `description`: text NULL — Description of the permission
- primary key: permission_id

### user_roles  (source backend: mongodb)
Source table user_roles of the 3d_motion_tracking_and_analysis scenario.

- `user_id`: integer NOT NULL — Foreign key referencing the users table
- `role_id`: integer NULL — Foreign key referencing the roles table
- primary key: user_id

### role_permissions  (source backend: rest)
Source table role_permissions of the 3d_motion_tracking_and_analysis scenario.

- `role_id`: integer NOT NULL — Foreign key referencing the roles table
- `permission_id`: integer NULL — Foreign key referencing the permissions table
- primary key: role_id

### Relationships

- motion_data(sensor_id) -> sensors(sensor_id) [optional (may be NULL/dangling)]

