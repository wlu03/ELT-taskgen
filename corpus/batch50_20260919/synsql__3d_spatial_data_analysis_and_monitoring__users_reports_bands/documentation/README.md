# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# 3D Spatial Data Analysis And Monitoring

## Specification

PROJECT OVERVIEW

This project, "3D Spatial Data Analysis And Monitoring", builds two published marts from the source tables of the 3d_spatial_data_analysis_and_monitoring scenario. Each source table is extracted from exactly one backend, and an implementation must read it from that backend and no other.

Source tables and their extraction backends:
- Source table cells must be extracted from the mongodb backend. It holds one row per cell_id, with x_coord, y_coord, z_coord, ch1, ch2, ch3, ch4, ch2_adj, ch3_adj, ch4_adj, outside, nbrs_old, nbrs_new, nbrs_that_are_outside, median_dist_to_nbrs, nbr_id_list, created_at and updated_at.
- Source table sensors must be extracted from the s3 backend. It holds one row per sensor_id, with cell_id, sensor_type (one of air quality, temperature), installation_date, last_calibration_date, manufacturer, model, created_at and updated_at.
- Source table users must be extracted from the s3 backend. It holds one row per user_id, with user_name, email, role (one of researcher, admin, analyst), password, created_at and updated_at.
- Source table access_logs must be extracted from the rest backend. It holds one row per access_id, with cell_id, user_id, access_date, access_type (one of view, download), created_at and updated_at.
- Source table adjustments must be extracted from the files backend. It holds one row per adjustment_id, with cell_id, ch2_adj, ch3_adj, ch4_adj, adjustment_date, created_at and updated_at.
- Source table neighborhoods must be extracted from the rest backend. It holds one row per neighborhood_id, with cell_id, nbr_cell_ids, created_at and updated_at.
- Source table calibration_logs must be extracted from the s3 backend. It holds one row per calibration_id, with sensor_id, calibration_date, calibrator_name, calibrator_contact, created_at and updated_at.
- Source table reports must be extracted from the files backend. It holds one row per report_id, with user_id, report_date, report_content, report_type (one of air quality, temperature), created_at and updated_at.
- Source table alerts must be extracted from the s3 backend. It holds one row per alert_id, with cell_id, alert_type (one of high value, low value), alert_date, message, created_at and updated_at.
- Source table roles must be extracted from the s3 backend. It holds one row per role_id, with role_name, role_description, created_at and updated_at.
- Source table user_roles must be extracted from the mongodb backend. It holds one row per user_id, with role_id, created_at and updated_at.
- Source table sensor_types must be extracted from the mongodb backend. It holds one row per sensor_type_id, with sensor_type_name, sensor_type_description, created_at and updated_at.
- Source table sensor_readings must be extracted from the mongodb backend. It holds one row per sensor_reading_id, with cell_id, sensor_id, reading_date, reading_value, created_at and updated_at.

Relationships between the source tables, each labelled exactly as the source schema declares it:
- Child table access_logs with key cell_id refers to parent table cells with key cell_id; this relationship is optional (may be NULL or dangling).
- Child table access_logs with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).
- Child table adjustments with key cell_id refers to parent table cells with key cell_id; this relationship is optional (may be NULL or dangling).
- Child table alerts with key cell_id refers to parent table cells with key cell_id; this relationship is optional (may be NULL or dangling).
- Child table calibration_logs with key sensor_id refers to parent table sensors with key sensor_id; this relationship is optional (may be NULL or dangling).
- Child table neighborhoods with key cell_id refers to parent table cells with key cell_id; this relationship is optional (may be NULL or dangling).
- Child table reports with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).
- Child table sensor_readings with key cell_id refers to parent table cells with key cell_id; this relationship is optional (may be NULL or dangling).
- Child table sensor_readings with key sensor_id refers to parent table sensors with key sensor_id; this relationship is optional (may be NULL or dangling).
- Child table sensors with key cell_id refers to parent table cells with key cell_id; this relationship is optional (may be NULL or dangling).

All rounding stated below is ordinary half-up rounding to the stated number of decimal places. Every rule stated for a mart applies to that mart alone.

=== Mart users_reports_bands: Per-users banding of linked reports activity in the 3d_spatial_data_analysis_and_monitoring scenario, over the value domain the source schema itself declares ===

Grain: one row per users (user_id), INCLUDING users rows with no linked reports rows. The key column is parent_key.

Output columns.
- parent_key (integer): the identifier of the users row, with one row per value.
- parent_name (text): the user_name of the users row, copied unchanged.
- parent_status (text): the role of the users row, copied unchanged. Its declared domain is ['researcher', 'admin', 'analyst'].
- link_count (bigint): the number of reports rows for this users row; 0 when there are none. Every linked reports row counts, whatever its report_type value. A users row kept with no reports row reports 0 here, never 1: its placeholder holds no reports row to count.
- passing_count (bigint): of those, how many have report_type in ['air quality']. It is 0, never missing, when none do.
- failing_count (bigint): how many have report_type in ['temperature']. It is 0 when none do.
- distinct_status_count (bigint): how many different report_type values occur among them, each different value counted once however many rows repeat it.
- passing_ratio (float): passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links.
- adoption_band (text): the band of passing_ratio, decided on the rounded passing_ratio value this mart reports.
- status_group (text): parent_status mapped value by value, and never NULL.

Rules.
1. The source table users is read as an input of this mart.
2. The source table reports is read as an input of this mart.
3. From source table users there is one row per users row, keyed by user_id, carrying parent_key, parent_name and parent_status.
4. Bring in reports (hop 1 of 1) by matching the user_id of source table reports to parent_key, carrying user_id; preservation is left-sided, so users rows with no matching reports row are RETAINED and report the declared defaults.
5. There is one output row per parent_key, carrying parent_name and parent_status beside the keys: a parent_key value identifies one source row for the carried columns, so parent_name and parent_status take one value per key and never split a group, and each such row reports link_count, passing_count, failing_count and distinct_status_count for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.
6. The mart columns are named parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count.
7. Guarded ratios, carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count: passing_ratio is passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and the result is 0.0 when the denominator is 0 or NULL, that is 0.0 when there are no links.
8. adoption_band, carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count and passing_ratio, is the band of passing_ratio decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), otherwise 'low' below 0.5. Boundaries are inclusive of the HIGHER band, over the declared domain researcher, admin, analyst, and adoption_band always holds one of these four labels, never null or blank.
9. status_group, carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio and adoption_band, is parent_status mapped value by value: 'researcher' becomes 'active'; 'admin' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. This is a categorical mapping with no numeric boundary over the declared domain researcher, admin, analyst, and status_group is never NULL.
10. Deterministic output order: rows appear sorted in ascending parent_key order.

=== Mart sensors_sensor_readings_distribution: Per-(sensors, measure state) distribution of linked sensor_readings rows in the 3d_spatial_data_analysis_and_monitoring scenario ===

Grain: one row per (sensor_id, measure state) pair represented among linked sensor_readings rows; the absent state includes missing reading_value values and a no-activity row for a sensors row with no links. A linked sensor_readings row whose reading_value has a value belongs only to the present state and never to the absent state. The key columns are entity_key and measure_state.

Output columns.
- entity_key (integer): the identifier of the sensors row.
- measure_state (text): 'present' for a linked sensor_readings row whose reading_value has a value; 'absent' when reading_value is missing, including a sensors row with no linked sensor_readings row. A linked sensor_readings row whose reading_value has a value belongs only to the present state and never to the absent state.
- entity_name (text): the sensor_type of the sensors row, copied unchanged.
- row_count (bigint): the number of linked sensor_readings rows in this entity/state cell; an absent cell holding real sensor_readings rows whose reading_value is missing COUNTS those rows, and only the placeholder cell of a sensors row with no linked sensor_readings row at all reports 0.
- distinct_amount_count (bigint): the number of unique non-missing reading_value values in this cell; each unique non-missing value is counted once, however many rows repeat it; it is 0 whenever the cell holds no reading_value value at all — both for a sensors row with no linked sensor_readings row and for an absent cell whose rows all have a missing reading_value.
- total_amount (float): the total of reading_value in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries a reading_value value.
- max_amount (float): the largest reading_value in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries a reading_value value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules.
1. The source table sensors is read as an input of this mart.
2. The source table sensor_readings is read as an input of this mart.
3. From source table sensors, each sensor_id and its sensor_type are carried into the measure-state calculation as entity_key and entity_name.
4. The linked sensor_readings rows are brought into each sensors entity by matching the sensor_id of source table sensor_readings to entity_key, carrying entity_key, entity_name and sensor_id; preservation is left-sided, so an entity with no linked row is retained and its absent state is visible.
5. The present measure-state rows, carrying entity_key and entity_name, are the rows kept because they are a real sensor_readings row whose reading_value has a value.
6. In the present measure state there is one row per sensors entity that has at least one such row, and no row here for an entity with none, reporting row_count as the row count, distinct_amount_count as how many different non-missing reading_value values occur (each different value counted once, however many rows repeat it), total_amount as the total reading_value, and max_amount as the largest reading_value, beside entity_key and entity_name.
7. Beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.
8. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state 'present', the present measure state.
9. The absent measure-state rows, carrying entity_key and entity_name, are the rows kept because reading_value is missing, including the retained placeholder for a sensors row with no sensor_readings rows. A real sensor_readings row whose reading_value has a value belongs only to the present state and never to this absent state.
10. In the absent measure state there is one row per sensors entity that has at least one such row, and no row here for an entity with none, reporting row_count as the row count, distinct_amount_count as how many different non-missing reading_value values occur (each different value counted once, however many rows repeat it), total_amount as the total reading_value, and max_amount as the largest reading_value, beside entity_key and entity_name.
11. Beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is again max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.
12. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state 'absent', the absent measure state.
13. The present-state summary and the absent-state summary are stacked into one output list of entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, all such rows are kept, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.
14. Deterministic output order: rows appear sorted in ascending entity_key order, and within one entity in ascending measure_state order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `users_reports_bands`

- Grain: One row per users (user_id), INCLUDING users rows with no linked reports rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group

```text
Mart 'users_reports_bands' has 10 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table reports. (public source tables: reports)
3. [derive] One row per users row, keyed by user_id. (public source tables: users | public carried/output columns: parent_key, parent_name, parent_status)
4. [join] Bring in reports (hop 1 of 1): rows with no matching reports row are RETAINED and report the declared defaults. (public source tables: reports | public carried/output columns: user_id | join preservation: left | condition public identifiers: reports, user_id, parent_key)
5. [filtered_aggregate] One output row per parent_key, carrying parent_name, parent_status beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, passing_count, failing_count, distinct_status_count for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
6. [derive] Name the mart columns. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
7. [ratio] Guarded ratios: passing_ratio — passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
8. [conditional] adoption_band — Band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the HIGHER band. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band | semantic parameters: boundary=a value exactly at 0.8 is 'high'; a value exactly at 0.5 is 'medium'; domain=researcher, admin, analyst)
9. [conditional] status_group — parent_status mapped value by value: 'researcher' becomes 'active'; 'admin' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never NULL. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group | semantic parameters: boundary=categorical mapping; no numeric boundary; domain=researcher, admin, analyst)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `sensors_sensor_readings_distribution`

- Grain: One row per (sensor_id, measure state) pair represented among linked sensor_readings rows; the absent state includes missing reading_value values and a no-activity row for a sensors row with no links. A linked sensor_readings row whose reading_value has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'sensors_sensor_readings_distribution' has 14 declared semantic rules:
1. [source] Read source table sensors. (public source tables: sensors)
2. [source] Read source table sensor_readings. (public source tables: sensor_readings)
3. [derive] Carry each sensor_id and its sensor_type into the measure-state calculation. (public source tables: sensors | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked sensor_readings rows into each sensors entity; retain an entity with no linked row so its absent state is visible. (public source tables: sensor_readings | public carried/output columns: entity_key, entity_name, sensor_id | join preservation: left | condition public identifiers: sensor_readings, sensor_id, entity_key)
5. [filter] Keep the present measure-state rows: a real sensor_readings row whose reading_value has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per sensors entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing reading_value values occur (each different value counted once, however many rows repeat it), total reading_value, and largest reading_value. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: reading_value is missing, including the retained placeholder for a sensors row with no sensor_readings rows. A real sensor_readings row whose reading_value has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per sensors entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing reading_value values occur (each different value counted once, however many rows repeat it), total reading_value, and largest reading_value. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### cells  (source backend: mongodb)
Source table cells of the 3d_spatial_data_analysis_and_monitoring scenario.

- `cell_id`: integer NOT NULL — Unique identifier for each cell
- `x_coord`: float NULL — X-coordinate of the cell in 3D space
- `y_coord`: float NULL — Y-coordinate of the cell in 3D space
- `z_coord`: float NULL — Z-coordinate of the cell in 3D space
- `ch1`: float NULL — Sensor reading for channel 1
- `ch2`: float NULL — Sensor reading for channel 2
- `ch3`: float NULL — Sensor reading for channel 3
- `ch4`: float NULL — Sensor reading for channel 4
- `ch2_adj`: float NULL — Adjusted value for channel 2
- `ch3_adj`: float NULL — Adjusted value for channel 3
- `ch4_adj`: float NULL — Adjusted value for channel 4
- `outside`: integer NULL — Indicator if the measurement is outside the target area
- `nbrs_old`: integer NULL — Number of old neighbors
- `nbrs_new`: integer NULL — Number of new neighbors
- `nbrs_that_are_outside`: integer NULL — Number of neighbors outside the target area
- `median_dist_to_nbrs`: float NULL — Median distance to neighbors
- `nbr_id_list`: text NULL — List of neighboring cell IDs
- `created_at`: text NULL — Timestamp when the cell was created
- `updated_at`: text NULL — Timestamp when the cell was last updated
- primary key: cell_id

### sensors  (source backend: s3)
Source table sensors of the 3d_spatial_data_analysis_and_monitoring scenario.

- `sensor_id`: integer NOT NULL — Unique identifier for each sensor
- `cell_id`: integer NULL — Reference to the associated measurement cell
- `sensor_type`: text NULL — Type of sensor (e.g., air quality, temperature) one of: air quality, temperature.
- `installation_date`: text NULL — Date when the sensor was installed
- `last_calibration_date`: text NULL — Date when the sensor was last calibrated
- `manufacturer`: text NULL — Manufacturer of the sensor
- `model`: text NULL — Model of the sensor
- `created_at`: text NULL — Timestamp when the sensor was created
- `updated_at`: text NULL — Timestamp when the sensor was last updated
- primary key: sensor_id

### users  (source backend: s3)
Source table users of the 3d_spatial_data_analysis_and_monitoring scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `user_name`: text NULL — Full name of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., researcher, admin, analyst) one of: researcher, admin, analyst.
- `password`: text NULL — Password for the user account
- `created_at`: text NULL — Timestamp when the user was created
- `updated_at`: text NULL — Timestamp when the user was last updated
- primary key: user_id

### access_logs  (source backend: rest)
Source table access_logs of the 3d_spatial_data_analysis_and_monitoring scenario.

- `access_id`: integer NOT NULL — Unique identifier for each access event
- `cell_id`: integer NULL — ID of the cell being accessed
- `user_id`: integer NULL — ID of the user accessing the cell
- `access_date`: text NULL — Date when the cell data was accessed
- `access_type`: text NULL — Type of access (e.g., view, download) one of: view, download.
- `created_at`: text NULL — Timestamp when the access log was created
- `updated_at`: text NULL — Timestamp when the access log was last updated
- primary key: access_id

### adjustments  (source backend: files)
Source table adjustments of the 3d_spatial_data_analysis_and_monitoring scenario.

- `adjustment_id`: integer NOT NULL — Unique identifier for each adjustment record
- `cell_id`: integer NULL — Reference to the associated measurement cell
- `ch2_adj`: float NULL — Adjusted value for channel 2
- `ch3_adj`: float NULL — Adjusted value for channel 3
- `ch4_adj`: float NULL — Adjusted value for channel 4
- `adjustment_date`: text NULL — Date when the adjustment was made
- `created_at`: text NULL — Timestamp when the adjustment was created
- `updated_at`: text NULL — Timestamp when the adjustment was last updated
- primary key: adjustment_id

### neighborhoods  (source backend: rest)
Source table neighborhoods of the 3d_spatial_data_analysis_and_monitoring scenario.

- `neighborhood_id`: integer NOT NULL — Unique identifier for the neighborhood record
- `cell_id`: integer NULL — Reference to the main cell
- `nbr_cell_ids`: text NULL — List of neighboring cell IDs
- `created_at`: text NULL — Timestamp when the neighborhood was created
- `updated_at`: text NULL — Timestamp when the neighborhood was last updated
- primary key: neighborhood_id

### calibration_logs  (source backend: s3)
Source table calibration_logs of the 3d_spatial_data_analysis_and_monitoring scenario.

- `calibration_id`: integer NOT NULL — Unique identifier for each calibration record
- `sensor_id`: integer NULL — ID of the sensor being calibrated
- `calibration_date`: text NULL — Date when the calibration took place
- `calibrator_name`: text NULL — Name of the person or entity that performed the calibration
- `calibrator_contact`: text NULL — Contact information for the calibrator
- `created_at`: text NULL — Timestamp when the calibration log was created
- `updated_at`: text NULL — Timestamp when the calibration log was last updated
- primary key: calibration_id

### reports  (source backend: files)
Source table reports of the 3d_spatial_data_analysis_and_monitoring scenario.

- `report_id`: integer NOT NULL — Unique identifier for each report
- `user_id`: integer NULL — ID of the user who created the report
- `report_date`: text NULL — Date when the report was generated
- `report_content`: text NULL — Content of the report
- `report_type`: text NULL — Type of report (e.g., air quality, temperature) one of: air quality, temperature.
- `created_at`: text NULL — Timestamp when the report was created
- `updated_at`: text NULL — Timestamp when the report was last updated
- primary key: report_id

### alerts  (source backend: s3)
Source table alerts of the 3d_spatial_data_analysis_and_monitoring scenario.

- `alert_id`: integer NOT NULL — Unique identifier for each alert
- `cell_id`: integer NULL — ID of the cell that triggered the alert
- `alert_type`: text NULL — Type of alert (e.g., high value, low value) one of: high value, low value.
- `alert_date`: text NULL — Date when the alert was generated
- `message`: text NULL — Message providing details about the alert
- `created_at`: text NULL — Timestamp when the alert was created
- `updated_at`: text NULL — Timestamp when the alert was last updated
- primary key: alert_id

### roles  (source backend: s3)
Source table roles of the 3d_spatial_data_analysis_and_monitoring scenario.

- `role_id`: integer NOT NULL — Unique identifier for each role
- `role_name`: text NULL — Name of the role
- `role_description`: text NULL — Description of the role
- `created_at`: text NULL — Timestamp when the role was created
- `updated_at`: text NULL — Timestamp when the role was last updated
- primary key: role_id

### user_roles  (source backend: mongodb)
Source table user_roles of the 3d_spatial_data_analysis_and_monitoring scenario.

- `user_id`: integer NOT NULL — ID of the user being assigned the role
- `role_id`: integer NULL — ID of the role being assigned
- `created_at`: text NULL — Timestamp when the role was assigned
- `updated_at`: text NULL — Timestamp when the role was last updated
- primary key: user_id

### sensor_types  (source backend: mongodb)
Source table sensor_types of the 3d_spatial_data_analysis_and_monitoring scenario.

- `sensor_type_id`: integer NOT NULL — Unique identifier for each sensor type
- `sensor_type_name`: text NULL — Name of the sensor type
- `sensor_type_description`: text NULL — Description of the sensor type
- `created_at`: text NULL — Timestamp when the sensor type was created
- `updated_at`: text NULL — Timestamp when the sensor type was last updated
- primary key: sensor_type_id

### sensor_readings  (source backend: mongodb)
Source table sensor_readings of the 3d_spatial_data_analysis_and_monitoring scenario.

- `sensor_reading_id`: integer NOT NULL — Unique identifier for each sensor reading
- `cell_id`: integer NULL — ID of the measurement cell
- `sensor_id`: integer NULL — ID of the sensor that took the reading
- `reading_date`: text NULL — Date when the reading was taken
- `reading_value`: float NULL — Value of the reading
- `created_at`: text NULL — Timestamp when the reading was created
- `updated_at`: text NULL — Timestamp when the reading was last updated
- primary key: sensor_reading_id

### Relationships

- access_logs(cell_id) -> cells(cell_id) [optional (may be NULL/dangling)]
- access_logs(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- adjustments(cell_id) -> cells(cell_id) [optional (may be NULL/dangling)]
- alerts(cell_id) -> cells(cell_id) [optional (may be NULL/dangling)]
- calibration_logs(sensor_id) -> sensors(sensor_id) [optional (may be NULL/dangling)]
- neighborhoods(cell_id) -> cells(cell_id) [optional (may be NULL/dangling)]
- reports(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- sensor_readings(cell_id) -> cells(cell_id) [optional (may be NULL/dangling)]
- sensor_readings(sensor_id) -> sensors(sensor_id) [optional (may be NULL/dangling)]
- sensors(cell_id) -> cells(cell_id) [optional (may be NULL/dangling)]

