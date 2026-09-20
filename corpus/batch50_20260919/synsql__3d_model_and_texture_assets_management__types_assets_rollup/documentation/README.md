# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# 3D Model And Texture Assets Management

## Specification

PROJECT OVERVIEW

This project builds two analytical marts for the 3d_model_and_texture_assets_management scenario, describing how asset records roll up to their asset types. The prose below is the complete statement of intended semantics; nothing else about the pipeline is implied.

Source tables and their extraction backends. The table assets must be extracted from the postgres backend. The table types must be extracted from the mongodb backend. The table categories must be extracted from the rest backend. The table subcategories must be extracted from the postgres backend. The table tags must be extracted from the files backend. The table asset_tags must be extracted from the files backend. The table users must be extracted from the rest backend. The table uploads must be extracted from the mongodb backend. The table versions must be extracted from the s3 backend. The table access_logs must be extracted from the rest backend.

Relationships in the source schema, each stated with its child table and keys, its parent table and keys, and whether it is required or optional.
- The child table access_logs by its asset_id refers to the parent table assets by its asset_id; this relationship is optional (may be NULL or dangling).
- The child table access_logs by its user_id refers to the parent table users by its user_id; this relationship is optional (may be NULL or dangling).
- The child table assets by its category_id refers to the parent table categories by its category_id; this relationship is optional (may be NULL or dangling).
- The child table assets by its subcategory_id refers to the parent table subcategories by its subcategory_id; this relationship is optional (may be NULL or dangling).
- The child table assets by its type_id refers to the parent table types by its type_id; this relationship is optional (may be NULL or dangling).
- The child table uploads by its asset_id refers to the parent table assets by its asset_id; this relationship is optional (may be NULL or dangling).
- The child table uploads by its user_id refers to the parent table users by its user_id; this relationship is optional (may be NULL or dangling).
- The child table versions by its asset_id refers to the parent table assets by its asset_id; this relationship is optional (may be NULL or dangling).

Throughout, an assets row is "linked" to a types row when the assets row's type_id equals that types row's type_id. A rounding to 4 decimal places always means the usual half-up rounding of the fractional value.

=== Mart types_assets_rollup: per-types roll-up of linked assets activity in the 3d_model_and_texture_assets_management scenario, including the fan-out onto categories ===

Grain: one row per types row (type_id), INCLUDING types rows with no linked assets rows. The key column of this mart is parent_key.

Rule 1. The source table types is read in full; every types row takes part in this mart.
Rule 2. The source table assets is read in full; every assets row takes part in this mart.
Rule 3. The source table categories is read in full; every categories row takes part in this mart.
Rule 4. From the source table types, one row exists per types row, keyed by type_id: parent_key and parent_name are the carried columns of that row.
Rule 5. Hop 1 brings in the assets rows against the grain, matching the type_id carried column of assets to parent_key; one types row may have many matching assets rows, and a types row with no assets rows at all is RETAINED (preservation is left-sided, the types side being preserved).
Rule 6. Hop 2 brings in the categories rows, matching each linked assets row's category_id to the category_id of a categories row and carrying category_id; an assets row whose categories row is missing still counts as a link and is RETAINED (preservation is left-sided, the assets side being preserved).
Rule 7. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, and that row reports link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.
Rule 8. The mart columns are named parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount; total_amount, active_amount and max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount and max_amount, the default also applies to a group none of whose real rows carries an input value.
Rule 9. Guarded ratio: active_amount_ratio is active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0 or has no value; it appears beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount.
Rule 10. size_band is the size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5, so a value exactly at 2 is 'small' and a value exactly at 5 is 'medium'; every value falls in exactly one band, and size_band appears beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount and active_amount_ratio.
Rule 11. has_links is 'yes' when this parent has at least one link and 'no' otherwise, a value exactly at 0 links being 'no'; it is never NULL and appears beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio and size_band.
Rule 12. Deterministic output order: rows are sorted by parent_key, ascending.

Output columns of types_assets_rollup.
- parent_key (integer): identifier of the types row. One row per value.
- parent_name (text): type_name of the types row, copied unchanged.
- link_count (bigint): number of assets rows linked to this types row. 0 when there are none.
- distinct_child_count (bigint): the number of distinct categories rows reached through those links. Two links pointing at the same child count ONCE. 0 when there are no links. A link whose categories row is missing reaches no categories row and adds nothing to this count.
- active_link_count (bigint): number of linked assets rows whose tags is one of ['cyber']. A parent whose links ALL fail that test reports 0, not a missing row.
- total_amount (integer): total of class over every linked row; 0 when there are no links, and 0 when none of the linked rows carries a class value.
- active_amount (integer): total of class over links whose tags is one of ['cyber']; 0 when none qualify, and 0 when every qualifying row lacks a class value.
- max_amount (integer): largest class among the linked rows; 0 when there are no links, and 0 when none of the linked rows carries a class value.
- active_amount_ratio (float): active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0.
- size_band (text): size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band.
- has_links (text): 'yes' when this parent has at least one link, 'no' otherwise. Never NULL.

=== Mart types_assets_cohorts: per-(types, status cohort) summary of linked assets rows in the 3d_model_and_texture_assets_management scenario, with passing and failing cohorts kept separate ===

Grain: one row per (type_id, status cohort) pair represented among linked assets rows, plus one no-activity row for a types row with no linked assets row at all. A types row whose linked assets rows all lack a tags value is in no cohort and gets no no-activity row, so it has no row in this mart. The key columns of this mart are entity_key and cohort.

Rule 1. The source table types is read in full; every types row takes part in this mart.
Rule 2. The source table assets is read in full; every assets row takes part in this mart.
Rule 3. From the source table types, each type_id and its type_name are carried into the cohort calculation as entity_key and entity_name.
Rule 4. The linked assets rows are brought into each types entity before status cohorts are assigned, matching the type_id of assets to entity_key and carrying entity_key, entity_name and type_id; a types entity with no assets rows is retained (preservation is left-sided).
Rule 5. For the passing cohort, the rows kept are those whose tags belongs to the passing cohort values ['cyber'], carrying entity_key and entity_name.
Rule 6. There is one row per types entity that has at least one linked assets row in the passing cohort, reporting as entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount the number of those rows, how many different tags values occur among them, the total of their class, and their largest class. The total and the largest value read only the rows that carry a class value; a cohort whose rows all lack one reports 0 for both, never empty.
Rule 7. For that passing summary, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it appears beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.
Rule 8. These measures are labelled with cohort 'passing', so each such row carries entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
Rule 9. For the failing cohort, the rows kept are those whose tags belongs to the failing cohort values ['neon', 'pixel'], carrying entity_key and entity_name.
Rule 10. There is one row per types entity that has at least one linked assets row in the failing cohort, reporting as entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount the number of those rows, how many different tags values occur among them, the total of their class, and their largest class. The total and the largest value read only the rows that carry a class value; a cohort whose rows all lack one reports 0 for both, never empty.
Rule 11. For that failing summary, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it appears beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.
Rule 12. These measures are labelled with cohort 'failing', so each such row carries entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
Rule 13. The placeholder row, carrying entity_key and entity_name, is kept for a types entity with no linked assets row at all; a types entity that has linked assets rows gets no placeholder, even when every one of those rows lacks a tags value.
Rule 14. There is one row per types entity with no linked assets row at all, reporting as entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount: 0 rows, 0 different tags values, a class total of 0 and a largest class of 0.
Rule 15. For that no-activity summary, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it appears beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.
Rule 16. These measures are labelled with cohort 'no_activity', so each such row carries entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
Rule 17. The disjoint passing and failing cohort summaries are combined, every row of both being kept, carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
Rule 18. The no-activity summaries are added to that combination, every one of their rows being kept, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
Rule 19. Deterministic output order: rows are sorted by entity, then cohort — ascending entity_key, and within an entity_key ascending cohort.

Output columns of types_assets_cohorts.
- entity_key (integer): identifier of the types row.
- cohort (text): 'passing' for tags values ['cyber']; 'failing' for values ['neon', 'pixel']; 'no_activity' when the types row has no linked assets row. A linked assets row whose tags has no value belongs to no cohort: it is not counted in any cell, and it does not make the types row 'no_activity'.
- entity_name (text): type_name of the types row, copied unchanged.
- link_count (bigint): number of assets rows in this entity/cohort cell.
- distinct_status_count (bigint): number of distinct tags values represented in this cell.
- total_amount (integer): total of class in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an class value.
- max_amount (integer): largest class in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an class value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `types_assets_rollup`

- Grain: One row per types (type_id), INCLUDING types rows with no linked assets rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links

```text
Mart 'types_assets_rollup' has 12 declared semantic rules:
1. [source] Read source table types. (public source tables: types)
2. [source] Read source table assets. (public source tables: assets)
3. [source] Read source table categories. (public source tables: categories)
4. [derive] One row per types row, keyed by type_id. (public source tables: types | public carried/output columns: parent_key, parent_name)
5. [join] Hop 1: bring in assets against the grain. One types row may have many assets rows, and a types row with no assets rows at all is RETAINED. (public source tables: assets | public carried/output columns: type_id | join preservation: left | condition public identifiers: assets, type_id, parent_key)
6. [join] Hop 2: bring in categories, matching each linked assets row's category_id to the category_id of a categories row. A assets row whose categories row is missing still counts as a link and is RETAINED. (public source tables: categories | public carried/output columns: category_id | join preservation: left | condition public identifiers: categories, category_id)
7. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
8. [derive] Name the mart columns; total_amount, active_amount, max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount, max_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
9. [ratio] Guarded ratios: active_amount_ratio — active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [conditional] size_band — Size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band | semantic parameters: boundary=a value exactly at 2 is 'small'; a value exactly at 5 is 'medium')
11. [conditional] has_links — 'yes' when this parent has at least one link, 'no' otherwise. Never NULL. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links | semantic parameters: boundary=a value exactly at 0 is 'no')
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `types_assets_cohorts`

- Grain: One row per (type_id, status cohort) pair represented among linked assets rows, plus one no-activity row for a types row with no linked assets row at all. A types row whose linked assets rows all lack a tags value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'types_assets_cohorts' has 19 declared semantic rules:
1. [source] Read source table types. (public source tables: types)
2. [source] Read source table assets. (public source tables: assets)
3. [derive] Carry each type_id and its type_name into the cohort calculation. (public source tables: types | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked assets rows into each types entity before assigning status cohorts. (public source tables: assets | public carried/output columns: entity_key, entity_name, type_id | join preservation: left | condition public identifiers: assets, type_id, entity_key)
5. [filter] Keep rows whose tags belongs to the passing cohort values ['cyber']. (public carried/output columns: entity_key, entity_name | condition literal specification values: cyber)
6. [distinct] One row per types entity that has at least one linked assets row in the passing cohort, reporting the number of those rows, how many different tags values occur among them, the total of their class, and their largest class. The total and the largest value read only the rows that carry a class value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose tags belongs to the failing cohort values ['neon', 'pixel']. (public carried/output columns: entity_key, entity_name | condition literal specification values: neon, pixel)
10. [distinct] One row per types entity that has at least one linked assets row in the failing cohort, reporting the number of those rows, how many different tags values occur among them, the total of their class, and their largest class. The total and the largest value read only the rows that carry a class value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a types entity with no linked assets row at all; a types entity that has linked assets rows gets no placeholder, even when every one of those rows lacks a tags value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per types entity with no linked assets row at all, reporting 0 rows, 0 different tags values, a class total of 0 and a largest class of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

## Source tables

### assets  (source backend: postgres)
Source table assets of the 3d_model_and_texture_assets_management scenario.

- `asset_id`: integer NOT NULL — Unique identifier for each asset
- `type_id`: integer NULL — ID of the type of asset (e.g., 3D Models, Textures, HDRI)
- `category_id`: integer NULL — ID of the category of asset (e.g., Installations, Buildings)
- `subcategory_id`: integer NULL — ID of the subcategory of asset (e.g., Destroyed, Lamps)
- `tags`: text NULL — Tags associated with the asset (e.g., cyber, neon, pixel) one of: cyber, neon, pixel.
- `file_path`: text NULL — File path to the asset
- `class`: integer NULL — Class of the asset
- `created_at`: text NULL — Date and time when the asset was created
- `updated_at`: text NULL — Date and time when the asset was last updated
- `description`: text NULL — Detailed description of the asset
- `status`: text NULL — Status of the asset (e.g., active, archived, draft) one of: active, archived, draft.
- `thumbnail_path`: text NULL — Path to the thumbnail image of the asset
- `license`: text NULL — License information for the asset
- `author_id`: integer NULL — ID of the user who created the asset
- `file_size`: text NULL — Size of the asset file in bytes
- `resolution`: text NULL — Resolution of the asset (if applicable, e.g., 1920x1080)
- primary key: asset_id

### types  (source backend: mongodb)
Source table types of the 3d_model_and_texture_assets_management scenario.

- `type_id`: integer NOT NULL — Unique identifier for each type
- `type_name`: text NULL — Name of the type
- `created_at`: text NULL — Date and time when the type was created
- `updated_at`: text NULL — Date and time when the type was last updated
- `description`: text NULL — Detailed description of the type
- primary key: type_id

### categories  (source backend: rest)
Source table categories of the 3d_model_and_texture_assets_management scenario.

- `category_id`: integer NOT NULL — Unique identifier for each category
- `category_name`: text NULL — Name of the category
- `type_id`: integer NULL — ID of the type of asset
- `created_at`: text NULL — Date and time when the category was created
- `updated_at`: text NULL — Date and time when the category was last updated
- `description`: text NULL — Detailed description of the category
- primary key: category_id

### subcategories  (source backend: postgres)
Source table subcategories of the 3d_model_and_texture_assets_management scenario.

- `subcategory_id`: integer NOT NULL — Unique identifier for each subcategory
- `subcategory_name`: text NULL — Name of the subcategory
- `category_id`: integer NULL — ID of the category of asset
- `created_at`: text NULL — Date and time when the subcategory was created
- `updated_at`: text NULL — Date and time when the subcategory was last updated
- `description`: text NULL — Detailed description of the subcategory
- primary key: subcategory_id

### tags  (source backend: files)
Source table tags of the 3d_model_and_texture_assets_management scenario.

- `tag_id`: integer NOT NULL — Unique identifier for each tag
- `tag_name`: text NULL — Name of the tag
- `created_at`: text NULL — Date and time when the tag was created
- `updated_at`: text NULL — Date and time when the tag was last updated
- `description`: text NULL — Detailed description of the tag
- primary key: tag_id

### asset_tags  (source backend: files)
Source table asset_tags of the 3d_model_and_texture_assets_management scenario.

- `asset_id`: integer NOT NULL — ID of the asset
- `tag_id`: integer NULL — ID of the tag
- primary key: asset_id

### users  (source backend: rest)
Source table users of the 3d_model_and_texture_assets_management scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `user_name`: text NULL — Full name of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., admin, uploader, viewer) one of: admin, uploader, viewer.
- `created_at`: text NULL — Date and time when the user account was created
- `updated_at`: text NULL — Date and time when the user account was last updated
- `last_login`: text NULL — Date and time of the user's last login
- `status`: text NULL — Status of the user account (e.g., active, suspended, deleted) one of: active, suspended, deleted.
- primary key: user_id

### uploads  (source backend: mongodb)
Source table uploads of the 3d_model_and_texture_assets_management scenario.

- `upload_id`: integer NOT NULL — Unique identifier for each upload
- `asset_id`: integer NULL — ID of the asset uploaded
- `user_id`: integer NULL — ID of the user who uploaded the asset
- `upload_date`: text NULL — Date the asset was uploaded
- `description`: text NULL — Description of the upload (e.g., reason for upload)
- `file_size`: text NULL — Size of the uploaded file in bytes
- `resolution`: text NULL — Resolution of the uploaded file (if applicable, e.g., 1920x1080)
- primary key: upload_id

### versions  (source backend: s3)
Source table versions of the 3d_model_and_texture_assets_management scenario.

- `version_id`: integer NOT NULL — Unique identifier for each version
- `asset_id`: integer NULL — ID of the asset
- `version_number`: integer NULL — Version number of the asset
- `upload_date`: text NULL — Date the version was uploaded
- `description`: text NULL — Description of the changes made in this version
- `file_size`: text NULL — Size of the file in this version
- `resolution`: text NULL — Resolution of the file in this version (if applicable, e.g., 1920x1080)
- primary key: version_id

### access_logs  (source backend: rest)
Source table access_logs of the 3d_model_and_texture_assets_management scenario.

- `log_id`: integer NOT NULL — Unique identifier for each access log
- `asset_id`: integer NULL — ID of the asset accessed
- `user_id`: integer NULL — ID of the user who accessed the asset
- `access_date`: text NULL — Date the asset was accessed
- `access_type`: text NULL — Type of access (e.g., view, download) one of: view, download.
- `ip_address`: text NULL — IP address of the user who accessed the asset
- `user_agent`: text NULL — User agent string of the device used to access the asset
- `description`: text NULL — Description of the access event
- primary key: log_id

### Relationships

- access_logs(asset_id) -> assets(asset_id) [optional (may be NULL/dangling)]
- access_logs(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- assets(category_id) -> categories(category_id) [optional (may be NULL/dangling)]
- assets(subcategory_id) -> subcategories(subcategory_id) [optional (may be NULL/dangling)]
- assets(type_id) -> types(type_id) [optional (may be NULL/dangling)]
- uploads(asset_id) -> assets(asset_id) [optional (may be NULL/dangling)]
- uploads(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- versions(asset_id) -> assets(asset_id) [optional (may be NULL/dangling)]

