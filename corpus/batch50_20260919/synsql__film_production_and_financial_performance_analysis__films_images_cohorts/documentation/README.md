# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Film Production And Financial Performance Analysis

## Specification

PROJECT OVERVIEW: Film Production And Financial Performance Analysis

This project builds two analytical marts over the film production data set. Every source table must be extracted from its own backend, and the extraction backend for each table is fixed as follows.

The source table films is extracted from the postgres backend. The source table directors is extracted from the mongodb backend. The source table producers is extracted from the rest backend. The source table writers is extracted from the mongodb backend. The source table composers is extracted from the files backend. The source table actors is extracted from the mongodb backend. The source table financials is extracted from the mongodb backend. The source table images is extracted from the rest backend. The source table users is extracted from the files backend. The source table access_logs is extracted from the s3 backend.

Relationships between the tables, each labelled exactly as the source schema declares it:

- The child table access_logs through its film_id refers to the parent table films through its film_id; this relationship is optional (the value may be NULL or dangling).
- The child table access_logs through its user_id refers to the parent table users through its user_id; this relationship is optional (the value may be NULL or dangling).
- The child table films through its bond_actor_id refers to the parent table actors through its actor_id; this relationship is optional (the value may be NULL or dangling).
- The child table films through its composer_id refers to the parent table composers through its composer_id; this relationship is optional (the value may be NULL or dangling).
- The child table films through its director_id refers to the parent table directors through its director_id; this relationship is optional (the value may be NULL or dangling).
- The child table films through its producer_id refers to the parent table producers through its producer_id; this relationship is optional (the value may be NULL or dangling).
- The child table films through its writer_id refers to the parent table writers through its writer_id; this relationship is optional (the value may be NULL or dangling).
- The child table financials through its film_id refers to the parent table films through its film_id; this relationship is optional (the value may be NULL or dangling).
- The child table images through its film_id refers to the parent table films through its film_id; this relationship is optional (the value may be NULL or dangling).

All rounding stated below is ordinary half-up rounding to the stated number of decimal places.

=== Mart films_images_cohorts: Per-(films, status cohort) summary of linked images rows in the __film_production_and_financial_performance_analysis__ scenario, with passing and failing cohorts kept separate. ===

Grain: one row per (film_id, status cohort) pair represented among linked images rows, plus one no-activity row for a films row with no linked images row at all. A films row whose linked images rows all lack a image_type value is in no cohort and gets no no-activity row, so it has no row in this mart.

Key columns: entity_key and cohort together identify a row of this mart.

Output columns:
- entity_key (integer): identifier of the films row.
- cohort (text): 'passing' for image_type values ['poster']; 'failing' for values ['still', 'behind-the-scenes']; 'no_activity' when the films row has no linked images row. A linked images row whose image_type has no value belongs to no cohort: it is not counted in any cell, and it does not make the films row 'no_activity'.
- entity_name (text): title of the films row, copied unchanged.
- link_count (bigint): number of images rows in this entity/cohort cell.
- distinct_status_count (bigint): number of distinct image_type values represented in this cell.
- total_amount (float): total of file_size in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an file_size value.
- max_amount (float): largest file_size in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an file_size value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules, in order:

Rule 1. The source table films is read in full as an input to this mart.

Rule 2. The source table images is read in full as an input to this mart.

Rule 3. From films, each film_id is carried as entity_key and its title is carried as entity_name into the cohort calculation.

Rule 4. The linked images rows are brought into each films entity before assigning status cohorts, matching an images row to an entity when its film_id equals that entity's entity_key; preservation is left-sided, so every films entity is retained even when no images row matches it, and entity_key, entity_name and film_id are carried forward.

Rule 5. For the passing cohort, only the rows whose image_type is one of the cohort values ['poster'] are kept, carrying entity_key and entity_name.

Rule 6. One row per films entity that has at least one linked images row in the passing cohort, reporting the number of those rows as link_count, how many different image_type values occur among them as distinct_status_count, the total of their file_size as total_amount, and their largest file_size as max_amount, beside entity_key and entity_name. The total and the largest value read only the rows that carry a file_size value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 7. For each such passing row, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 8. These measures are labelled as the passing cohort: cohort takes the value 'passing' beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 9. For the failing cohort, only the rows whose image_type is one of the cohort values ['still', 'behind-the-scenes'] are kept, carrying entity_key and entity_name.

Rule 10. One row per films entity that has at least one linked images row in the failing cohort, reporting the number of those rows as link_count, how many different image_type values occur among them as distinct_status_count, the total of their file_size as total_amount, and their largest file_size as max_amount, beside entity_key and entity_name. The total and the largest value read only the rows that carry a file_size value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 11. For each such failing row, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 12. These measures are labelled as the failing cohort: cohort takes the value 'failing' beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 13. The placeholder row, carrying entity_key and entity_name, is kept for a films entity with no linked images row at all; a films entity that has linked images rows gets no placeholder, even when every one of those rows lacks a image_type value.

Rule 14. One row per films entity with no linked images row at all, carrying entity_key and entity_name and reporting 0 rows as link_count, 0 different image_type values as distinct_status_count, a file_size total of 0 as total_amount and a largest file_size of 0 as max_amount.

Rule 15. For each such placeholder row, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 16. These measures are labelled as the no_activity cohort: cohort takes the value 'no_activity' beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 17. The disjoint passing and failing cohort summaries are combined, all rows of both kept, carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 18. The no-activity summaries are added to that combination, all rows kept, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 19. Deterministic output order: rows appear sorted by entity, then cohort — that is, ascending entity_key, and within one entity_key ascending cohort.

=== Mart users_access_logs_bands: Per-users banding of linked access_logs activity in the __film_production_and_financial_performance_analysis__ scenario, over the value domain the source schema itself declares. ===

Grain: one row per users (user_id), including users rows with no linked access_logs rows.

Key column: parent_key identifies a row of this mart.

Output columns:
- parent_key (integer): identifier of the users row. One row per value.
- parent_name (text): user_name of the users row, copied unchanged.
- parent_status (text): role of the users row, copied unchanged. Declared domain: ['producer', 'director', 'analyst'].
- link_count (bigint): number of access_logs rows for this users row; 0 when there are none. Every linked access_logs row counts, whatever its access_type value. An users row kept with no access_logs row reports 0 here, never 1: its placeholder holds no access_logs row to count.
- passing_count (bigint): of those, how many have access_type in ['view']. 0, never missing, when none do.
- failing_count (bigint): how many have access_type in ['edit']. 0 when none do.
- distinct_status_count (bigint): how many distinct access_type values occur among them.
- passing_ratio (float): passing_count divided by link_count as a fraction between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links.
- adoption_band (text): band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the HIGHER band.
- status_group (text): parent_status mapped value by value: 'producer' becomes 'active'; 'director' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never NULL.

Rules, in order:

Rule 1. The source table users is read in full as an input to this mart.

Rule 2. The source table access_logs is read in full as an input to this mart.

Rule 3. From users, there is one row per users row, keyed by user_id, carrying parent_key, parent_name and parent_status.

Rule 4. The access_logs rows are brought in (hop 1 of 1), matching an access_logs row by its user_id to parent_key; preservation is left-sided, so rows with no matching access_logs row are RETAINED and report the declared defaults, and user_id is carried.

Rule 5. One output row per parent_key, carrying parent_name, parent_status beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, passing_count, failing_count, distinct_status_count for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 6. The mart columns are named parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count.

Rule 7. Guarded ratios, carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count: passing_ratio — passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links, that is 0.0 when the denominator is 0 or NULL.

Rule 8. adoption_band — band of passing_ratio, decided on the rounded passing_ratio value this mart reports, carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count and passing_ratio: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5; boundaries are inclusive of the HIGHER band, so a value exactly at 0.8 is 'high' and a value exactly at 0.5 is 'medium'.

Rule 9. status_group — parent_status mapped value by value, carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio and adoption_band: 'producer' becomes 'active'; 'director' becomes 'other_1'; any other value — including legal values of the column (producer, director, analyst) that the mapping does not name — becomes 'unmapped'. Never NULL.

Rule 10. Deterministic output order: rows appear sorted ascending by parent_key.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `films_images_cohorts`

- Grain: One row per (film_id, status cohort) pair represented among linked images rows, plus one no-activity row for a films row with no linked images row at all. A films row whose linked images rows all lack a image_type value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'films_images_cohorts' has 19 declared semantic rules:
1. [source] Read source table films. (public source tables: films)
2. [source] Read source table images. (public source tables: images)
3. [derive] Carry each film_id and its title into the cohort calculation. (public source tables: films | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked images rows into each films entity before assigning status cohorts. (public source tables: images | public carried/output columns: entity_key, entity_name, film_id | join preservation: left | condition public identifiers: images, film_id, entity_key)
5. [filter] Keep rows whose image_type belongs to the passing cohort values ['poster']. (public carried/output columns: entity_key, entity_name | condition literal specification values: poster)
6. [distinct] One row per films entity that has at least one linked images row in the passing cohort, reporting the number of those rows, how many different image_type values occur among them, the total of their file_size, and their largest file_size. The total and the largest value read only the rows that carry a file_size value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose image_type belongs to the failing cohort values ['still', 'behind-the-scenes']. (public carried/output columns: entity_key, entity_name | condition literal specification values: still, behind-the-scenes)
10. [distinct] One row per films entity that has at least one linked images row in the failing cohort, reporting the number of those rows, how many different image_type values occur among them, the total of their file_size, and their largest file_size. The total and the largest value read only the rows that carry a file_size value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a films entity with no linked images row at all; a films entity that has linked images rows gets no placeholder, even when every one of those rows lacks a image_type value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per films entity with no linked images row at all, reporting 0 rows, 0 different image_type values, a file_size total of 0 and a largest file_size of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

### `users_access_logs_bands`

- Grain: One row per users (user_id), INCLUDING users rows with no linked access_logs rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group

```text
Mart 'users_access_logs_bands' has 10 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table access_logs. (public source tables: access_logs)
3. [derive] One row per users row, keyed by user_id. (public source tables: users | public carried/output columns: parent_key, parent_name, parent_status)
4. [join] Bring in access_logs (hop 1 of 1): rows with no matching access_logs row are RETAINED and report the declared defaults. (public source tables: access_logs | public carried/output columns: user_id | join preservation: left | condition public identifiers: access_logs, user_id, parent_key)
5. [filtered_aggregate] One output row per parent_key, carrying parent_name, parent_status beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, passing_count, failing_count, distinct_status_count for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
6. [derive] Name the mart columns. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
7. [ratio] Guarded ratios: passing_ratio — passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
8. [conditional] adoption_band — Band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the HIGHER band. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band | semantic parameters: boundary=a value exactly at 0.8 is 'high'; a value exactly at 0.5 is 'medium'; domain=producer, director, analyst)
9. [conditional] status_group — parent_status mapped value by value: 'producer' becomes 'active'; 'director' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never NULL. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group | semantic parameters: boundary=categorical mapping; no numeric boundary; domain=producer, director, analyst)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### films  (source backend: postgres)
Source table films of the __film_production_and_financial_performance_analysis__ scenario.

- `film_id`: integer NOT NULL — Unique identifier for each film
- `title`: text NULL — Title of the film
- `year`: integer NULL — Year of release
- `director_id`: integer NULL — Reference to the director
- `producer_id`: integer NULL — Reference to the producer(s)
- `writer_id`: integer NULL — Reference to the writer(s)
- `composer_id`: integer NULL — Reference to the composer
- `bond_actor_id`: integer NULL — Reference to the actor who played Bond
- `budget`: float NULL — Budget for the film
- `box_office`: float NULL — Box office earnings
- `image_file`: text NULL — File path to the film's promotional image
- `runtime`: integer NULL — Duration of the film in minutes
- `genre`: text NULL — Genre of the film
- `country`: text NULL — Country of production
- `language`: text NULL — Primary language of the film
- `rating`: text NULL — MPAA rating
- `release_date`: text NULL — Exact release date of the film
- `tagline`: text NULL — Tagline or slogan of the film
- `production_company`: text NULL — Name of the production company
- `distributor`: text NULL — Name of the distributor
- `trailer_url`: text NULL — URL to the film's trailer
- primary key: film_id

### directors  (source backend: mongodb)
Source table directors of the __film_production_and_financial_performance_analysis__ scenario.

- `director_id`: integer NOT NULL — Unique identifier for each director
- `director_name`: text NULL — Full name of the director
- `email`: text NULL — Email address of the director
- `nationality`: text NULL — Director's nationality
- `date_of_birth`: text NULL — Director's date of birth
- `biography`: text NULL — Brief biography of the director
- primary key: director_id

### producers  (source backend: rest)
Source table producers of the __film_production_and_financial_performance_analysis__ scenario.

- `producer_id`: integer NOT NULL — Unique identifier for each producer
- `producer_name`: text NULL — Full name of the producer
- `email`: text NULL — Email address of the producer
- `nationality`: text NULL — Producer's nationality
- `date_of_birth`: text NULL — Producer's date of birth
- `biography`: text NULL — Brief biography of the producer
- primary key: producer_id

### writers  (source backend: mongodb)
Source table writers of the __film_production_and_financial_performance_analysis__ scenario.

- `writer_id`: integer NOT NULL — Unique identifier for each writer
- `writer_name`: text NULL — Full name of the writer
- `email`: text NULL — Email address of the writer
- `nationality`: text NULL — Writer's nationality
- `date_of_birth`: text NULL — Writer's date of birth
- `biography`: text NULL — Brief biography of the writer
- primary key: writer_id

### composers  (source backend: files)
Source table composers of the __film_production_and_financial_performance_analysis__ scenario.

- `composer_id`: integer NOT NULL — Unique identifier for each composer
- `composer_name`: text NULL — Full name of the composer
- `email`: text NULL — Email address of the composer
- `nationality`: text NULL — Composer's nationality
- `date_of_birth`: text NULL — Composer's date of birth
- `biography`: text NULL — Brief biography of the composer
- primary key: composer_id

### actors  (source backend: mongodb)
Source table actors of the __film_production_and_financial_performance_analysis__ scenario.

- `actor_id`: integer NOT NULL — Unique identifier for each actor
- `actor_name`: text NULL — Full name of the actor
- `email`: text NULL — Email address of the actor
- `nationality`: text NULL — Actor's nationality
- `date_of_birth`: text NULL — Actor's date of birth
- `biography`: text NULL — Brief biography of the actor
- `character_name`: text NULL — Name of the character played by the actor in the film
- primary key: actor_id

### financials  (source backend: mongodb)
Source table financials of the __film_production_and_financial_performance_analysis__ scenario.

- `financial_id`: integer NOT NULL — Unique identifier for each financial record
- `film_id`: integer NULL — Reference to the film
- `budget`: float NULL — Budget for the film
- `box_office`: float NULL — Box office earnings
- `domestic_box_office`: float NULL — Domestic box office earnings
- `international_box_office`: float NULL — International box office earnings
- `marketing_budget`: float NULL — Budget allocated for marketing
- `profit`: float NULL — Calculated profit (box office - budget - marketing budget)
- `return_on_investment`: float NULL — Return on investment (profit / budget)
- primary key: financial_id

### images  (source backend: rest)
Source table images of the __film_production_and_financial_performance_analysis__ scenario.

- `image_id`: integer NOT NULL — Unique identifier for each image
- `film_id`: integer NULL — Reference to the film
- `file_path`: text NULL — File path to the image
- `file_size`: float NULL — Size of the image file in MB
- `upload_date`: text NULL — Date the file was uploaded
- `description`: text NULL — Description of the image
- `is_promotional`: integer NULL — Boolean indicating if the image is promotional
- `image_type`: text NULL — Type of image (e.g., poster, still, behind-the-scenes) one of: poster, still, behind-the-scenes.
- primary key: image_id

### users  (source backend: files)
Source table users of the __film_production_and_financial_performance_analysis__ scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `user_name`: text NULL — Full name of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., producer, director, analyst) one of: producer, director, analyst.
- `date_joined`: text NULL — Date when the user joined the platform
- `last_login`: text NULL — Date of the user's last login
- `status`: text NULL — User status (e.g., active, inactive) one of: active, inactive.
- primary key: user_id

### access_logs  (source backend: s3)
Source table access_logs of the __film_production_and_financial_performance_analysis__ scenario.

- `access_id`: integer NOT NULL — Unique identifier for each access event
- `film_id`: integer NULL — Reference to the film
- `user_id`: integer NULL — Reference to the user accessing the data
- `access_date`: text NULL — Date when the data was accessed
- `access_type`: text NULL — Type of access (e.g., view, edit) one of: view, edit.
- `ip_address`: text NULL — IP address of the user accessing the data
- `user_agent`: text NULL — User agent string of the browser used
- `device_type`: text NULL — Type of device used (e.g., desktop, mobile) one of: desktop, mobile.
- primary key: access_id

### Relationships

- access_logs(film_id) -> films(film_id) [optional (may be NULL/dangling)]
- access_logs(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- films(bond_actor_id) -> actors(actor_id) [optional (may be NULL/dangling)]
- films(composer_id) -> composers(composer_id) [optional (may be NULL/dangling)]
- films(director_id) -> directors(director_id) [optional (may be NULL/dangling)]
- films(producer_id) -> producers(producer_id) [optional (may be NULL/dangling)]
- films(writer_id) -> writers(writer_id) [optional (may be NULL/dangling)]
- financials(film_id) -> films(film_id) [optional (may be NULL/dangling)]
- images(film_id) -> films(film_id) [optional (may be NULL/dangling)]

