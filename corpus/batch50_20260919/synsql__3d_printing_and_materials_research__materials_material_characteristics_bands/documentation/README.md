# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# 3D Printing And Materials Research

## Specification

PROJECT OVERVIEW: 3D Printing And Materials Research

This project builds two published marts from the 3d_printing_and_materials_research scenario. A competent engineer should be able to reproduce every output row and every value from this document plus the published source schemas alone.

Source tables and where each one must be extracted from:
- Source table materials must be extracted from the postgres backend. It holds material_id (unique identifier for each material), material_name, material_type (one of ABS, PLA, PETG), composition, manufacturer, supplier, cost, density and melting_point, and material_id is its primary key.
- Source table printing_settings must be extracted from the postgres backend. It holds setting_id (its primary key), layer_height, wall_thickness, infill_density, infill_pattern, nozzle_temperature, bed_temperature, print_speed, adhesion_sheet, raft and support_material.
- Source table mechanical_properties must be extracted from the postgres backend. It holds property_id (its primary key), material_id, setting_id, tension_strength, elongation, youngs_modulus, yield_strength and impact_resistance.
- Source table users must be extracted from the rest backend. It holds user_id (its primary key), user_name, email, role (one of researcher, engineer, administrator), password_hash and last_login_date.
- Source table data_uploads must be extracted from the mongodb backend. It holds upload_id (its primary key), user_id, material_id, setting_id, property_id, upload_date and upload_time.
- Source table data_versions must be extracted from the mongodb backend. It holds version_id (its primary key), upload_id, version_number, version_date, version_time and version_description.
- Source table material_characteristics must be extracted from the s3 backend. It holds characteristic_id (its primary key), material_id, characteristic_name (one of melting point, density), characteristic_value and unit.
- Source table printing_statistics must be extracted from the postgres backend. It holds statistic_id (its primary key), setting_id, statistic_name (one of print time, print volume), statistic_value and unit.
- Source table roughness_values must be extracted from the mongodb backend. It holds roughness_id (its primary key), setting_id, roughness_value and unit.
- Source table material_reviews must be extracted from the files backend. It holds review_id (its primary key), material_id, user_id, review_date, review_time, rating and review_text.
- Source table setting_reviews must be extracted from the files backend. It holds review_id (its primary key), setting_id, user_id, review_date, review_time, rating and review_text.
- Source table system_logs must be extracted from the s3 backend. It holds log_id (its primary key), log_date, log_time, log_level (one of debug, info, warning, error) and log_message.

Declared relationships between the source tables, each with the label the source schema gives it:
- Child table data_uploads with key material_id refers to parent table materials with key material_id; this relationship is optional (may be NULL or dangling).
- Child table data_uploads with key property_id refers to parent table mechanical_properties with key property_id; this relationship is optional (may be NULL or dangling).
- Child table data_uploads with key setting_id refers to parent table printing_settings with key setting_id; this relationship is optional (may be NULL or dangling).
- Child table data_uploads with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).
- Child table data_versions with key upload_id refers to parent table data_uploads with key upload_id; this relationship is optional (may be NULL or dangling).
- Child table material_characteristics with key material_id refers to parent table materials with key material_id; this relationship is optional (may be NULL or dangling).
- Child table material_reviews with key material_id refers to parent table materials with key material_id; this relationship is optional (may be NULL or dangling).
- Child table material_reviews with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).
- Child table mechanical_properties with key material_id refers to parent table materials with key material_id; this relationship is optional (may be NULL or dangling).
- Child table mechanical_properties with key setting_id refers to parent table printing_settings with key setting_id; this relationship is optional (may be NULL or dangling).
- Child table printing_statistics with key setting_id refers to parent table printing_settings with key setting_id; this relationship is optional (may be NULL or dangling).
- Child table roughness_values with key setting_id refers to parent table printing_settings with key setting_id; this relationship is optional (may be NULL or dangling).
- Child table setting_reviews with key setting_id refers to parent table printing_settings with key setting_id; this relationship is optional (may be NULL or dangling).
- Child table setting_reviews with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).

============================================================
Mart materials_material_characteristics_bands — per-materials banding of linked material_characteristics activity in the 3d_printing_and_materials_research scenario, over the value domain the source schema itself declares.
============================================================

Grain: one row per materials (material_id), INCLUDING materials rows with no linked material_characteristics rows.

Key column: parent_key is the single key column of this mart.

Output columns:
- parent_key (integer): identifier of the materials row. One row per value.
- parent_name (text): material_name of the materials row, copied unchanged.
- parent_status (text): material_type of the materials row, copied unchanged. Declared domain: 'ABS', 'PLA', 'PETG'.
- link_count (bigint): number of material_characteristics rows for this materials row; 0 when there are none. Every linked material_characteristics row counts, whatever its characteristic_name value. A materials row kept with no material_characteristics row reports 0 here, never 1: its placeholder holds no material_characteristics row to count.
- passing_count (bigint): of those, how many have characteristic_name in ['melting point']. 0, never missing, when none do.
- failing_count (bigint): how many have characteristic_name in ['density']. 0 when none do.
- distinct_status_count (bigint): how many distinct characteristic_name values occur among them.
- passing_ratio (float): passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links.
- adoption_band (text): band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the HIGHER band.
- status_group (text): parent_status mapped value by value: 'ABS' becomes 'active'; 'PLA' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never NULL.

How the rows and values of materials_material_characteristics_bands come about, rule by rule:

Rule 1. The source table materials is read in full, and everything below draws on those rows.

Rule 2. The source table material_characteristics is read in full, and everything below draws on those rows.

Rule 3. From the source table materials there is exactly one row per materials row, keyed by material_id; that row carries parent_key (the material_id value), parent_name and parent_status.

Rule 4. The material_characteristics rows are brought in for each materials row by matching material_characteristics on material_id to parent_key — this is hop 1 of 1 and the carried column from material_characteristics is material_id; preservation is left-sided, meaning a materials row with no matching material_characteristics row is RETAINED and reports the declared defaults.

Rule 5. There is one output row per parent_key, carrying parent_name and parent_status beside the keys: a key value identifies one source row for the carried columns, so parent_name and parent_status take one value per key and never split a group, and each such row reports link_count, passing_count, failing_count and distinct_status_count over that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 6. The mart columns are named: parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count are the published names carried forward.

Rule 7. Guarded ratios beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count: passing_ratio is passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and it is 0.0 when there are no links, that is 0.0 whenever the denominator is 0 or NULL.

Rule 8. Beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count and passing_ratio, adoption_band is the band of passing_ratio decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (a value exactly at 0.8 is 'high'), 'medium' from 0.5 up to but not including 0.8 (a value exactly at 0.5 is 'medium'), otherwise 'low' below 0.5; boundaries are inclusive of the HIGHER band, and the declared value domain of the underlying status column is ABS, PLA, PETG.

Rule 9. Beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio and adoption_band, status_group is parent_status mapped value by value: 'ABS' becomes 'active'; 'PLA' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name, the declared domain being ABS, PLA, PETG — otherwise becomes 'unmapped'. It is a categorical mapping with no numeric boundary, and status_group is never NULL.

Rule 10. Deterministic output order: rows appear sorted by parent_key, ascending.

============================================================
Mart users_setting_reviews_distribution — per-(users, measure state) distribution of linked setting_reviews rows in the 3d_printing_and_materials_research scenario.
============================================================

Grain: one row per (user_id, measure state) pair represented among linked setting_reviews rows; the absent state includes missing rating values and a no-activity row for a users row with no links. A linked setting_reviews row whose rating has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state together are the key columns of this mart.

Output columns:
- entity_key (integer): identifier of the users row.
- measure_state (text): 'present' for a linked setting_reviews row whose rating has a value; 'absent' when rating is missing, including a users row with no linked setting_reviews row. A linked setting_reviews row whose rating has a value belongs only to the present state and never to the absent state.
- entity_name (text): user_name of the users row, copied unchanged.
- row_count (bigint): number of linked setting_reviews rows in this entity/state cell; a absent cell holding real setting_reviews rows whose rating is missing COUNTS those rows, and only the placeholder cell of an users row with no linked setting_reviews row at all reports 0.
- distinct_amount_count (bigint): number of unique non-missing rating values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no rating value at all — both for an users row with no linked setting_reviews row and for an absent cell whose rows all have a missing rating.
- total_amount (integer): total of rating in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an rating value.
- max_amount (integer): largest rating in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an rating value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

How the rows and values of users_setting_reviews_distribution come about, rule by rule:

Rule 1. The source table users is read in full, and everything below draws on those rows.

Rule 2. The source table setting_reviews is read in full, and everything below draws on those rows.

Rule 3. From the source table users, each user_id and its user_name are carried into the measure-state calculation as entity_key and entity_name.

Rule 4. The linked setting_reviews rows are brought into each users entity by matching setting_reviews on user_id to entity_key, carrying entity_key, entity_name and user_id; preservation is left-sided, so an entity with no linked row is retained and its absent state stays visible.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are kept: a real setting_reviews row whose rating has a value.

Rule 6. In the present measure state there is one row per users entity that has at least one such row, and no row here for an entity with none; that row carries entity_key and entity_name and reports row_count as the row count, distinct_amount_count as how many different non-missing rating values occur (each different value counted once, however many rows repeat it), total_amount as the total rating, and max_amount as the largest rating.

Rule 7. Beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount for these present-state rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state as the present measure state, that is the value 'present'.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are kept: rating is missing, including the retained placeholder for a users row with no setting_reviews rows. A real setting_reviews row whose rating has a value belongs only to the present state and never to this absent state.

Rule 10. In the absent measure state there is one row per users entity that has at least one such row, and no row here for an entity with none; that row carries entity_key and entity_name and reports row_count as the row count, distinct_amount_count as how many different non-missing rating values occur (each different value counted once, however many rows repeat it), total_amount as the total rating, and max_amount as the largest rating.

Rule 11. Beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount for these absent-state rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state as the absent measure state, that is the value 'absent'.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, all rows kept, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. Deterministic output order: rows appear sorted ascending by entity_key first, then by measure_state.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `materials_material_characteristics_bands`

- Grain: One row per materials (material_id), INCLUDING materials rows with no linked material_characteristics rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group

```text
Mart 'materials_material_characteristics_bands' has 10 declared semantic rules:
1. [source] Read source table materials. (public source tables: materials)
2. [source] Read source table material_characteristics. (public source tables: material_characteristics)
3. [derive] One row per materials row, keyed by material_id. (public source tables: materials | public carried/output columns: parent_key, parent_name, parent_status)
4. [join] Bring in material_characteristics (hop 1 of 1): rows with no matching material_characteristics row are RETAINED and report the declared defaults. (public source tables: material_characteristics | public carried/output columns: material_id | join preservation: left | condition public identifiers: material_characteristics, material_id, parent_key)
5. [filtered_aggregate] One output row per parent_key, carrying parent_name, parent_status beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, passing_count, failing_count, distinct_status_count for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
6. [derive] Name the mart columns. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
7. [ratio] Guarded ratios: passing_ratio — passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
8. [conditional] adoption_band — Band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the HIGHER band. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band | semantic parameters: boundary=a value exactly at 0.8 is 'high'; a value exactly at 0.5 is 'medium'; domain=ABS, PLA, PETG)
9. [conditional] status_group — parent_status mapped value by value: 'ABS' becomes 'active'; 'PLA' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never NULL. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group | semantic parameters: boundary=categorical mapping; no numeric boundary; domain=ABS, PLA, PETG)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `users_setting_reviews_distribution`

- Grain: One row per (user_id, measure state) pair represented among linked setting_reviews rows; the absent state includes missing rating values and a no-activity row for a users row with no links. A linked setting_reviews row whose rating has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'users_setting_reviews_distribution' has 14 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table setting_reviews. (public source tables: setting_reviews)
3. [derive] Carry each user_id and its user_name into the measure-state calculation. (public source tables: users | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked setting_reviews rows into each users entity; retain an entity with no linked row so its absent state is visible. (public source tables: setting_reviews | public carried/output columns: entity_key, entity_name, user_id | join preservation: left | condition public identifiers: setting_reviews, user_id, entity_key)
5. [filter] Keep the present measure-state rows: a real setting_reviews row whose rating has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per users entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing rating values occur (each different value counted once, however many rows repeat it), total rating, and largest rating. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: rating is missing, including the retained placeholder for a users row with no setting_reviews rows. A real setting_reviews row whose rating has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per users entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing rating values occur (each different value counted once, however many rows repeat it), total rating, and largest rating. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### materials  (source backend: postgres)
Source table materials of the 3d_printing_and_materials_research scenario.

- `material_id`: integer NOT NULL — Unique identifier for each material
- `material_name`: text NULL — Name of the material
- `material_type`: text NULL — Type of material (e.g., ABS, PLA, PETG) one of: ABS, PLA, PETG.
- `composition`: text NULL — Composition of the material (e.g., percentage of additives)
- `manufacturer`: text NULL — Manufacturer of the material
- `supplier`: text NULL — Supplier of the material
- `cost`: float NULL — Cost of the material per unit
- `density`: float NULL — Density of the material
- `melting_point`: float NULL — Melting point of the material
- primary key: material_id

### printing_settings  (source backend: postgres)
Source table printing_settings of the 3d_printing_and_materials_research scenario.

- `setting_id`: integer NOT NULL — Unique identifier for each printing setting
- `layer_height`: float NULL — Layer height of the print
- `wall_thickness`: integer NULL — Wall thickness of the print
- `infill_density`: integer NULL — Infill density of the print
- `infill_pattern`: text NULL — Infill pattern of the print
- `nozzle_temperature`: integer NULL — Nozzle temperature during printing
- `bed_temperature`: integer NULL — Bed temperature during printing
- `print_speed`: integer NULL — Print speed during printing
- `adhesion_sheet`: integer NULL — Use of adhesion sheet
- `raft`: integer NULL — Use of raft
- `support_material`: integer NULL — Use of support material
- primary key: setting_id

### mechanical_properties  (source backend: postgres)
Source table mechanical_properties of the 3d_printing_and_materials_research scenario.

- `property_id`: integer NOT NULL — Unique identifier for each mechanical property
- `material_id`: integer NULL — ID of the material used
- `setting_id`: integer NULL — ID of the printing setting used
- `tension_strength`: float NULL — Tension strength of the printed object
- `elongation`: float NULL — Elongation of the printed object
- `youngs_modulus`: float NULL — Young's modulus of the printed object
- `yield_strength`: float NULL — Yield strength of the printed object
- `impact_resistance`: float NULL — Impact resistance of the printed object
- primary key: property_id

### users  (source backend: rest)
Source table users of the 3d_printing_and_materials_research scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `user_name`: text NULL — Full name of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., researcher, engineer, administrator) one of: researcher, engineer, administrator.
- `password_hash`: text NULL — Hash of the user's password
- `last_login_date`: text NULL — Date of the user's last login
- primary key: user_id

### data_uploads  (source backend: mongodb)
Source table data_uploads of the 3d_printing_and_materials_research scenario.

- `upload_id`: integer NOT NULL — Unique identifier for each data upload
- `user_id`: integer NULL — ID of the user who uploaded the data
- `material_id`: integer NULL — ID of the material used
- `setting_id`: integer NULL — ID of the printing setting used
- `property_id`: integer NULL — ID of the mechanical property measured
- `upload_date`: text NULL — Date the data was uploaded
- `upload_time`: text NULL — Time the data was uploaded
- primary key: upload_id

### data_versions  (source backend: mongodb)
Source table data_versions of the 3d_printing_and_materials_research scenario.

- `version_id`: integer NOT NULL — Unique identifier for each data version
- `upload_id`: integer NULL — ID of the data upload
- `version_number`: integer NULL — Version number of the data
- `version_date`: text NULL — Date the data was versioned
- `version_time`: text NULL — Time the data was versioned
- `version_description`: text NULL — Description of the version
- primary key: version_id

### material_characteristics  (source backend: s3)
Source table material_characteristics of the 3d_printing_and_materials_research scenario.

- `characteristic_id`: integer NOT NULL — Unique identifier for each material characteristic
- `material_id`: integer NULL — ID of the material
- `characteristic_name`: text NULL — Name of the characteristic (e.g., melting point, density) one of: melting point, density.
- `characteristic_value`: text NULL — Value of the characteristic
- `unit`: text NULL — Unit of the characteristic value
- primary key: characteristic_id

### printing_statistics  (source backend: postgres)
Source table printing_statistics of the 3d_printing_and_materials_research scenario.

- `statistic_id`: integer NOT NULL — Unique identifier for each printing statistic
- `setting_id`: integer NULL — ID of the printing setting
- `statistic_name`: text NULL — Name of the statistic (e.g., print time, print volume) one of: print time, print volume.
- `statistic_value`: text NULL — Value of the statistic
- `unit`: text NULL — Unit of the statistic value
- primary key: statistic_id

### roughness_values  (source backend: mongodb)
Source table roughness_values of the 3d_printing_and_materials_research scenario.

- `roughness_id`: integer NOT NULL — Unique identifier for each roughness value
- `setting_id`: integer NULL — ID of the printing setting
- `roughness_value`: float NULL — Roughness value
- `unit`: text NULL — Unit of the roughness value
- primary key: roughness_id

### material_reviews  (source backend: files)
Source table material_reviews of the 3d_printing_and_materials_research scenario.

- `review_id`: integer NOT NULL — Unique identifier for each material review
- `material_id`: integer NULL — ID of the material reviewed
- `user_id`: integer NULL — ID of the user who wrote the review
- `review_date`: text NULL — Date the review was written
- `review_time`: text NULL — Time the review was written
- `rating`: integer NULL — Rating given by the user (e.g., 1-5)
- `review_text`: text NULL — Text of the review
- primary key: review_id

### setting_reviews  (source backend: files)
Source table setting_reviews of the 3d_printing_and_materials_research scenario.

- `review_id`: integer NOT NULL — Unique identifier for each setting review
- `setting_id`: integer NULL — ID of the printing setting reviewed
- `user_id`: integer NULL — ID of the user who wrote the review
- `review_date`: text NULL — Date the review was written
- `review_time`: text NULL — Time the review was written
- `rating`: integer NULL — Rating given by the user (e.g., 1-5)
- `review_text`: text NULL — Text of the review
- primary key: review_id

### system_logs  (source backend: s3)
Source table system_logs of the 3d_printing_and_materials_research scenario.

- `log_id`: integer NOT NULL — Unique identifier for each log entry
- `log_date`: text NULL — Date the log entry was created
- `log_time`: text NULL — Time the log entry was created
- `log_level`: text NULL — Level of the log entry (e.g., debug, info, warning, error) one of: debug, info, warning, error.
- `log_message`: text NULL — Message of the log entry
- primary key: log_id

### Relationships

- data_uploads(material_id) -> materials(material_id) [optional (may be NULL/dangling)]
- data_uploads(property_id) -> mechanical_properties(property_id) [optional (may be NULL/dangling)]
- data_uploads(setting_id) -> printing_settings(setting_id) [optional (may be NULL/dangling)]
- data_uploads(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- data_versions(upload_id) -> data_uploads(upload_id) [optional (may be NULL/dangling)]
- material_characteristics(material_id) -> materials(material_id) [optional (may be NULL/dangling)]
- material_reviews(material_id) -> materials(material_id) [optional (may be NULL/dangling)]
- material_reviews(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- mechanical_properties(material_id) -> materials(material_id) [optional (may be NULL/dangling)]
- mechanical_properties(setting_id) -> printing_settings(setting_id) [optional (may be NULL/dangling)]
- printing_statistics(setting_id) -> printing_settings(setting_id) [optional (may be NULL/dangling)]
- roughness_values(setting_id) -> printing_settings(setting_id) [optional (may be NULL/dangling)]
- setting_reviews(setting_id) -> printing_settings(setting_id) [optional (may be NULL/dangling)]
- setting_reviews(user_id) -> users(user_id) [optional (may be NULL/dangling)]

