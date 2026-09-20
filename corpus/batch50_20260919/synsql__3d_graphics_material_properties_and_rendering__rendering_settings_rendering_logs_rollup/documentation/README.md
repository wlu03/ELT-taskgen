# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# 3D Graphics Material Properties And Rendering

## Specification

PROJECT OVERVIEW

This project builds two analytical marts for the 3d_graphics_material_properties_and_rendering scenario. Both marts describe rendering configurations and the rendering activity recorded against them.

Source data and where it comes from. The source table materials must be extracted from the files backend. The source table objects must be extracted from the files backend. The source table scenes must be extracted from the mongodb backend. The source table cameras must be extracted from the rest backend. The source table lighting must be extracted from the mongodb backend. The source table rendering_settings must be extracted from the s3 backend. The source table users must be extracted from the mongodb backend. The source table object_materials must be extracted from the s3 backend. The source table scene_objects must be extracted from the mongodb backend. The source table rendering_logs must be extracted from the s3 backend.

Relationships in the source schema. The relationship from child table cameras on key camera_used is not one of them; the declared relationships are as follows, each labelled exactly as the source schema labels it.

- Child table cameras with key created_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table cameras with key updated_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table lighting with key created_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table lighting with key updated_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table materials with key created_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table materials with key updated_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table object_materials with key created_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table object_materials with key updated_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table objects with key created_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table objects with key material_id refers to parent table materials with key material_id: optional (may be NULL or dangling).
- Child table objects with key scene_id refers to parent table scenes with key scene_id: optional (may be NULL or dangling).
- Child table objects with key updated_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table rendering_logs with key camera_used refers to parent table cameras with key camera_id: optional (may be NULL or dangling).
- Child table rendering_logs with key rendering_id refers to parent table rendering_settings with key rendering_id: optional (may be NULL or dangling).
- Child table rendering_logs with key scene_id refers to parent table scenes with key scene_id: optional (may be NULL or dangling).
- Child table rendering_settings with key created_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table rendering_settings with key updated_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table scene_objects with key created_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table scene_objects with key updated_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table scenes with key camera_id refers to parent table cameras with key camera_id: optional (may be NULL or dangling).
- Child table scenes with key created_by refers to parent table users with key user_id: optional (may be NULL or dangling).
- Child table scenes with key lighting_id refers to parent table lighting with key lighting_id: optional (may be NULL or dangling).
- Child table scenes with key updated_by refers to parent table users with key user_id: optional (may be NULL or dangling).

Throughout, "a linked rendering_logs row" of a rendering_settings row means a rendering_logs row whose rendering_id equals that rendering_settings row's rendering_id.


MART rendering_settings_rendering_logs_rollup — Per-rendering_settings roll-up of linked rendering_logs activity in the 3d_graphics_material_properties_and_rendering scenario, including the fan-out onto cameras.

Grain: one row per rendering_settings (rendering_id), INCLUDING rendering_settings rows with no linked rendering_logs rows.

Key column: parent_key.

Output columns.
- parent_key (integer): identifier of the rendering_settings row; one row per value.
- parent_name (text): name of the rendering_settings row, copied unchanged.
- link_count (bigint): number of rendering_logs rows linked to this rendering_settings row; 0 when there are none.
- distinct_child_count (bigint): the number of distinct cameras rows reached through those links. Two links pointing at the same child count ONCE; it is 0 when there are no links, and a link whose cameras row is missing reaches no cameras row and adds nothing to this count.
- active_link_count (bigint): number of linked rendering_logs rows whose performance_data is one of ['rendering time']. A parent whose links ALL fail that test reports 0, not a missing row.
- total_amount (integer): total of object_count over every linked row; 0 when there are no links, and 0 when none of the linked rows carries a object_count value.
- active_amount (integer): total of object_count over links whose performance_data is one of ['rendering time']; 0 when none qualify, and 0 when every qualifying row lacks a object_count value.
- max_amount (integer): largest object_count among the linked rows; 0 when there are no links, and 0 when none of the linked rows carries a object_count value.
- active_amount_ratio (float): active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0.
- size_band (text): size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band.
- has_links (text): 'yes' when this parent has at least one link, 'no' otherwise. Never NULL.

Rules.

1. The source table rendering_settings is read in full.

2. The source table rendering_logs is read in full.

3. The source table cameras is read in full.

4. From the source table rendering_settings there is one row per rendering_settings row, keyed by rendering_id, carrying parent_key and parent_name.

5. Hop 1: the rendering_logs rows are brought in against the grain, matching the rendering_id of a rendering_logs row to parent_key and carrying rendering_id; one rendering_settings row may have many rendering_logs rows, and a rendering_settings row with no rendering_logs rows at all is RETAINED. Preservation is left-sided, so the rendering_settings side keeps its rows.

6. Hop 2: the cameras rows are brought in, matching each linked rendering_logs row's camera_used to the camera_id of a cameras row and carrying camera_id and camera_used. A rendering_logs row whose cameras row is missing still counts as a link and is RETAINED; preservation is left-sided.

7. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

8. The mart columns are named parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount; total_amount, active_amount and max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount and max_amount, the default also applies to a group none of whose real rows carries an input value.

9. Guarded ratio, reported beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount: active_amount_ratio is active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when the denominator total_amount is 0 or has no value.

10. Beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount and active_amount_ratio, size_band is the size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5, so a value exactly at 2 is 'small' and a value exactly at 5 is 'medium'. Every value falls in exactly one band, and no other value is ever produced.

11. Beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio and size_band, has_links is 'yes' when this parent has at least one link and 'no' otherwise, a value exactly at 0 links being 'no'. It is never NULL.

12. Deterministic output order: rows appear sorted by parent_key, ascending.


MART rendering_settings_rendering_logs_cohorts — Per-(rendering_settings, status cohort) summary of linked rendering_logs rows in the 3d_graphics_material_properties_and_rendering scenario, with passing and failing cohorts kept separate.

Grain: one row per (rendering_id, status cohort) pair represented among linked rendering_logs rows, plus one no-activity row for a rendering_settings row with no linked rendering_logs row at all. A rendering_settings row whose linked rendering_logs rows all lack a performance_data value is in no cohort and gets no no-activity row, so it has no row in this mart.

Key columns: entity_key and cohort.

Output columns.
- entity_key (integer): identifier of the rendering_settings row.
- cohort (text): 'passing' for performance_data values ['rendering time']; 'failing' for values ['memory usage']; 'no_activity' when the rendering_settings row has no linked rendering_logs row. A linked rendering_logs row whose performance_data has no value belongs to no cohort: it is not counted in any cell, and it does not make the rendering_settings row 'no_activity'.
- entity_name (text): name of the rendering_settings row, copied unchanged.
- link_count (bigint): number of rendering_logs rows in this entity/cohort cell.
- distinct_status_count (bigint): the number of distinct performance_data values represented in this cell.
- total_amount (integer): total of object_count in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an object_count value.
- max_amount (integer): largest object_count in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an object_count value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules.

1. The source table rendering_settings is read in full.

2. The source table rendering_logs is read in full.

3. From the source table rendering_settings, each rendering_id and its name are carried into the cohort calculation as entity_key and entity_name.

4. The linked rendering_logs rows are brought into each rendering_settings entity before assigning status cohorts, matching the rendering_id of a rendering_logs row to entity_key and carrying entity_key, entity_name and rendering_id; preservation is left-sided, so a rendering_settings entity is retained even with no matching rendering_logs row.

5. For the passing cohort, only rows whose performance_data belongs to the passing cohort values ['rendering time'] are kept, carrying entity_key and entity_name.

6. There is one row per rendering_settings entity that has at least one linked rendering_logs row in the passing cohort, carrying entity_key and entity_name and reporting link_count as the number of those rows, distinct_status_count as how many different performance_data values occur among them, total_amount as the total of their object_count, and max_amount as their largest object_count. The total and the largest value read only the rows that carry an object_count value; a cohort whose rows all lack one reports 0 for both, never empty.

7. Beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

8. These measures are labelled as the passing cohort: the row carries entity_key, cohort set to 'passing', entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

9. For the failing cohort, only rows whose performance_data belongs to the failing cohort values ['memory usage'] are kept, carrying entity_key and entity_name.

10. There is one row per rendering_settings entity that has at least one linked rendering_logs row in the failing cohort, carrying entity_key and entity_name and reporting link_count as the number of those rows, distinct_status_count as how many different performance_data values occur among them, total_amount as the total of their object_count, and max_amount as their largest object_count. The total and the largest value read only the rows that carry an object_count value; a cohort whose rows all lack one reports 0 for both, never empty.

11. Beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share for the failing cohort is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

12. These measures are labelled as the failing cohort: the row carries entity_key, cohort set to 'failing', entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

13. The placeholder row, carrying entity_key and entity_name, is kept for a rendering_settings entity with no linked rendering_logs row at all; a rendering_settings entity that has linked rendering_logs rows gets no placeholder, even when every one of those rows lacks a performance_data value.

14. There is one row per rendering_settings entity with no linked rendering_logs row at all, carrying entity_key and entity_name and reporting link_count of 0 rows, 0 different performance_data values as distinct_status_count, a object_count total of 0 as total_amount and a largest object_count of 0 as max_amount.

15. Beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share for the no-activity row is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

16. These measures are labelled as the no_activity cohort: the row carries entity_key, cohort set to 'no_activity', entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

17. The disjoint passing and failing cohort summaries are combined, keeping all rows of both, each carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

18. The no-activity summaries are added to that combination, keeping all such rows, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

19. Deterministic output order: rows appear sorted ascending by entity, that is entity_key, then cohort.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `rendering_settings_rendering_logs_rollup`

- Grain: One row per rendering_settings (rendering_id), INCLUDING rendering_settings rows with no linked rendering_logs rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links

```text
Mart 'rendering_settings_rendering_logs_rollup' has 12 declared semantic rules:
1. [source] Read source table rendering_settings. (public source tables: rendering_settings)
2. [source] Read source table rendering_logs. (public source tables: rendering_logs)
3. [source] Read source table cameras. (public source tables: cameras)
4. [derive] One row per rendering_settings row, keyed by rendering_id. (public source tables: rendering_settings | public carried/output columns: parent_key, parent_name)
5. [join] Hop 1: bring in rendering_logs against the grain. One rendering_settings row may have many rendering_logs rows, and a rendering_settings row with no rendering_logs rows at all is RETAINED. (public source tables: rendering_logs | public carried/output columns: rendering_id | join preservation: left | condition public identifiers: rendering_logs, rendering_id, parent_key)
6. [join] Hop 2: bring in cameras, matching each linked rendering_logs row's camera_used to the camera_id of a cameras row. A rendering_logs row whose cameras row is missing still counts as a link and is RETAINED. (public source tables: cameras | public carried/output columns: camera_id, camera_used | join preservation: left | condition public identifiers: cameras, camera_id)
7. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
8. [derive] Name the mart columns; total_amount, active_amount, max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount, max_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
9. [ratio] Guarded ratios: active_amount_ratio — active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [conditional] size_band — Size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band | semantic parameters: boundary=a value exactly at 2 is 'small'; a value exactly at 5 is 'medium')
11. [conditional] has_links — 'yes' when this parent has at least one link, 'no' otherwise. Never NULL. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links | semantic parameters: boundary=a value exactly at 0 is 'no')
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `rendering_settings_rendering_logs_cohorts`

- Grain: One row per (rendering_id, status cohort) pair represented among linked rendering_logs rows, plus one no-activity row for a rendering_settings row with no linked rendering_logs row at all. A rendering_settings row whose linked rendering_logs rows all lack a performance_data value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'rendering_settings_rendering_logs_cohorts' has 19 declared semantic rules:
1. [source] Read source table rendering_settings. (public source tables: rendering_settings)
2. [source] Read source table rendering_logs. (public source tables: rendering_logs)
3. [derive] Carry each rendering_id and its name into the cohort calculation. (public source tables: rendering_settings | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked rendering_logs rows into each rendering_settings entity before assigning status cohorts. (public source tables: rendering_logs | public carried/output columns: entity_key, entity_name, rendering_id | join preservation: left | condition public identifiers: rendering_logs, rendering_id, entity_key)
5. [filter] Keep rows whose performance_data belongs to the passing cohort values ['rendering time']. (public carried/output columns: entity_key, entity_name | condition literal specification values: rendering time)
6. [distinct] One row per rendering_settings entity that has at least one linked rendering_logs row in the passing cohort, reporting the number of those rows, how many different performance_data values occur among them, the total of their object_count, and their largest object_count. The total and the largest value read only the rows that carry an object_count value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose performance_data belongs to the failing cohort values ['memory usage']. (public carried/output columns: entity_key, entity_name | condition literal specification values: memory usage)
10. [distinct] One row per rendering_settings entity that has at least one linked rendering_logs row in the failing cohort, reporting the number of those rows, how many different performance_data values occur among them, the total of their object_count, and their largest object_count. The total and the largest value read only the rows that carry an object_count value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a rendering_settings entity with no linked rendering_logs row at all; a rendering_settings entity that has linked rendering_logs rows gets no placeholder, even when every one of those rows lacks a performance_data value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per rendering_settings entity with no linked rendering_logs row at all, reporting 0 rows, 0 different performance_data values, a object_count total of 0 and a largest object_count of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

## Source tables

### materials  (source backend: files)
Source table materials of the 3d_graphics_material_properties_and_rendering scenario.

- `material_id`: integer NOT NULL — Unique identifier for each material
- `name`: text NULL — Name of the material (if available)
- `color_diffuse`: text NULL — Diffuse color of the material (RGB values)
- `color_specular`: text NULL — Specular color of the material (RGB values)
- `color_ambient`: text NULL — Ambient color of the material (RGB values)
- `color_emissive`: text NULL — Emissive color of the material (RGB values)
- `transparency`: float NULL — Transparency level of the material (0.0 to 1.0)
- `reflectivity`: float NULL — Reflectivity level of the material (0.0 to 1.0)
- `bump_map`: text NULL — Path to the bump map texture file
- `normal_map`: text NULL — Path to the normal map texture file
- `displacement_map`: text NULL — Path to the displacement map texture file
- `roughness`: float NULL — Roughness level of the material (0.0 to 1.0)
- `metallic`: float NULL — Metallic level of the material (0.0 to 1.0)
- `anisotropy`: float NULL — Anisotropy level of the material (0.0 to 1.0)
- `ior`: float NULL — Index of refraction (IOR) of the material
- `version`: integer NULL — Version of the material
- `created_at`: text NULL — Creation date and time of the material
- `updated_at`: text NULL — Last update date and time of the material
- `created_by`: integer NULL — ID of the user who created the material
- `updated_by`: integer NULL — ID of the user who last updated the material
- primary key: material_id

### objects  (source backend: files)
Source table objects of the 3d_graphics_material_properties_and_rendering scenario.

- `object_id`: integer NOT NULL — Unique identifier for each object
- `name`: text NULL — Name of the object
- `material_id`: integer NULL — ID of the material assigned to the object
- `scene_id`: integer NULL — ID of the scene the object belongs to
- `scale`: text NULL — Scale factor of the object (x, y, z)
- `rotation`: text NULL — Rotation of the object (pitch, yaw, roll)
- `position`: text NULL — Position of the object (x, y, z)
- `visible`: integer NULL — Indicates whether the object is visible in the scene
- `version`: integer NULL — Version of the object
- `created_at`: text NULL — Creation date and time of the object
- `updated_at`: text NULL — Last update date and time of the object
- `created_by`: integer NULL — ID of the user who created the object
- `updated_by`: integer NULL — ID of the user who last updated the object
- primary key: object_id

### scenes  (source backend: mongodb)
Source table scenes of the 3d_graphics_material_properties_and_rendering scenario.

- `scene_id`: integer NOT NULL — Unique identifier for each scene
- `name`: text NULL — Name of the scene
- `camera_id`: integer NULL — ID of the camera used in the scene
- `lighting_id`: integer NULL — ID of the lighting configuration used in the scene
- `description`: text NULL — Detailed description of the scene
- `version`: integer NULL — Version of the scene
- `created_at`: text NULL — Creation date and time of the scene
- `updated_at`: text NULL — Last update date and time of the scene
- `created_by`: integer NULL — ID of the user who created the scene
- `updated_by`: integer NULL — ID of the user who last updated the scene
- primary key: scene_id

### cameras  (source backend: rest)
Source table cameras of the 3d_graphics_material_properties_and_rendering scenario.

- `camera_id`: integer NOT NULL — Unique identifier for each camera
- `name`: text NULL — Name of the camera
- `position`: text NULL — Position of the camera (x, y, z coordinates)
- `orientation`: text NULL — Orientation of the camera (pitch, yaw, roll values)
- `field_of_view`: float NULL — Field of view (FOV) of the camera
- `aspect_ratio`: float NULL — Aspect ratio of the camera
- `near_plane`: float NULL — Near clipping plane of the camera
- `far_plane`: float NULL — Far clipping plane of the camera
- `version`: integer NULL — Version of the camera
- `created_at`: text NULL — Creation date and time of the camera
- `updated_at`: text NULL — Last update date and time of the camera
- `created_by`: integer NULL — ID of the user who created the camera
- `updated_by`: integer NULL — ID of the user who last updated the camera
- primary key: camera_id

### lighting  (source backend: mongodb)
Source table lighting of the 3d_graphics_material_properties_and_rendering scenario.

- `lighting_id`: integer NOT NULL — Unique identifier for each lighting configuration
- `name`: text NULL — Name of the lighting configuration
- `type`: text NULL — Type of lighting (e.g., directional, point, ambient) one of: directional, point, ambient.
- `intensity`: float NULL — Intensity of the lighting
- `color`: text NULL — Color of the lighting (RGB values)
- `position`: text NULL — Position of the light source (x, y, z) for point lights
- `direction`: text NULL — Direction of the light source (x, y, z) for directional lights
- `cone_angle`: float NULL — Cone angle for spotlight lights
- `cone_falloff`: float NULL — Falloff angle for spotlight lights
- `version`: integer NULL — Version of the lighting configuration
- `created_at`: text NULL — Creation date and time of the lighting configuration
- `updated_at`: text NULL — Last update date and time of the lighting configuration
- `created_by`: integer NULL — ID of the user who created the lighting configuration
- `updated_by`: integer NULL — ID of the user who last updated the lighting configuration
- primary key: lighting_id

### rendering_settings  (source backend: s3)
Source table rendering_settings of the 3d_graphics_material_properties_and_rendering scenario.

- `rendering_id`: integer NOT NULL — Unique identifier for each rendering configuration
- `name`: text NULL — Name of the rendering configuration
- `technique`: text NULL — Rendering technique used (e.g., raytracing, rasterization) one of: raytracing, rasterization.
- `quality`: text NULL — Quality of the rendering (e.g., low, medium, high) one of: low, medium, high.
- `resolution`: text NULL — Resolution of the rendered image (width, height)
- `samples_per_pixel`: integer NULL — Number of samples per pixel for anti-aliasing
- `max_bounces`: integer NULL — Maximum number of light bounces for global illumination
- `denoiser`: text NULL — Denoiser used (e.g., no denoiser, OpenImageDenoise) one of: no denoiser, OpenImageDenoise.
- `version`: integer NULL — Version of the rendering settings
- `created_at`: text NULL — Creation date and time of the rendering settings
- `updated_at`: text NULL — Last update date and time of the rendering settings
- `created_by`: integer NULL — ID of the user who created the rendering settings
- `updated_by`: integer NULL — ID of the user who last updated the rendering settings
- primary key: rendering_id

### users  (source backend: mongodb)
Source table users of the 3d_graphics_material_properties_and_rendering scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `name`: text NULL — Full name of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., administrator, artist, developer) one of: administrator, artist, developer.
- `password_hash`: text NULL — Hashed password of the user
- `last_login`: text NULL — Date and time of the user's last login
- `status`: text NULL — Status of the user (e.g., active, inactive, suspended) one of: active, inactive, suspended.
- primary key: user_id

### object_materials  (source backend: s3)
Source table object_materials of the 3d_graphics_material_properties_and_rendering scenario.

- `object_id`: integer NOT NULL — ID of the object
- `material_id`: integer NULL — ID of the material assigned to the object
- `version`: integer NULL — Version of the object-material relationship
- `created_at`: text NULL — Creation date and time of the relationship
- `updated_at`: text NULL — Last update date and time of the relationship
- `created_by`: integer NULL — ID of the user who created the relationship
- `updated_by`: integer NULL — ID of the user who last updated the relationship
- primary key: object_id

### scene_objects  (source backend: mongodb)
Source table scene_objects of the 3d_graphics_material_properties_and_rendering scenario.

- `scene_id`: integer NOT NULL — ID of the scene
- `object_id`: integer NULL — ID of the object in the scene
- `version`: integer NULL — Version of the scene-object relationship
- `created_at`: text NULL — Creation date and time of the relationship
- `updated_at`: text NULL — Last update date and time of the relationship
- `created_by`: integer NULL — ID of the user who created the relationship
- `updated_by`: integer NULL — ID of the user who last updated the relationship
- primary key: scene_id

### rendering_logs  (source backend: s3)
Source table rendering_logs of the 3d_graphics_material_properties_and_rendering scenario.

- `log_id`: integer NOT NULL — Unique identifier for each rendering log
- `rendering_id`: integer NULL — ID of the rendering configuration used
- `scene_id`: integer NULL — ID of the scene that was rendered
- `object_count`: integer NULL — Number of objects in the scene
- `material_count`: integer NULL — Number of materials used in the scene
- `lighting_count`: integer NULL — Number of lighting configurations used in the scene
- `camera_used`: integer NULL — ID of the camera used for rendering
- `start_time`: text NULL — Start time of the rendering process
- `end_time`: text NULL — End time of the rendering process
- `performance_data`: text NULL — Performance data (e.g., rendering time, memory usage) one of: rendering time, memory usage.
- `performance_metrics`: text NULL — Detailed performance metrics (e.g., rendering time, memory usage, CPU/GPU usage) one of: rendering time, memory usage, CPU/GPU usage.
- primary key: log_id

### Relationships

- cameras(created_by) -> users(user_id) [optional (may be NULL/dangling)]
- cameras(updated_by) -> users(user_id) [optional (may be NULL/dangling)]
- lighting(created_by) -> users(user_id) [optional (may be NULL/dangling)]
- lighting(updated_by) -> users(user_id) [optional (may be NULL/dangling)]
- materials(created_by) -> users(user_id) [optional (may be NULL/dangling)]
- materials(updated_by) -> users(user_id) [optional (may be NULL/dangling)]
- object_materials(created_by) -> users(user_id) [optional (may be NULL/dangling)]
- object_materials(updated_by) -> users(user_id) [optional (may be NULL/dangling)]
- objects(created_by) -> users(user_id) [optional (may be NULL/dangling)]
- objects(material_id) -> materials(material_id) [optional (may be NULL/dangling)]
- objects(scene_id) -> scenes(scene_id) [optional (may be NULL/dangling)]
- objects(updated_by) -> users(user_id) [optional (may be NULL/dangling)]
- rendering_logs(camera_used) -> cameras(camera_id) [optional (may be NULL/dangling)]
- rendering_logs(rendering_id) -> rendering_settings(rendering_id) [optional (may be NULL/dangling)]
- rendering_logs(scene_id) -> scenes(scene_id) [optional (may be NULL/dangling)]
- rendering_settings(created_by) -> users(user_id) [optional (may be NULL/dangling)]
- rendering_settings(updated_by) -> users(user_id) [optional (may be NULL/dangling)]
- scene_objects(created_by) -> users(user_id) [optional (may be NULL/dangling)]
- scene_objects(updated_by) -> users(user_id) [optional (may be NULL/dangling)]
- scenes(camera_id) -> cameras(camera_id) [optional (may be NULL/dangling)]
- scenes(created_by) -> users(user_id) [optional (may be NULL/dangling)]
- scenes(lighting_id) -> lighting(lighting_id) [optional (may be NULL/dangling)]
- scenes(updated_by) -> users(user_id) [optional (may be NULL/dangling)]

