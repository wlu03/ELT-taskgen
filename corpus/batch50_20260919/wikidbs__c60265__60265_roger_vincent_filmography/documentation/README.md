# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Roger Vincent Filmography

## Specification

PROJECT OVERVIEW — Roger Vincent Filmography

This project builds two analytical marts from the ROGER_VINCENT_FILMOGRAPHY database. Five source tables are available, and each must be extracted from its own backend.

Source table filmography_details must be extracted from the mongodb backend. It holds one record per film credit, carrying film_title, film_description, film_type, film_director, film_screenwriter, cast_member, im_db_id, release_date, country_of_origin, film_genre, kinopoisk_film_id, of_db_film_id, film_color, csfd_film_id, movie_meter_film_id, all_movie_title_id, el_film_film_id, allo_cine_film_id, cine_ressources_film_id, film_affinity_film_id, unifrance_film_id, ldi_f_id, cinematheque_quebecoise_work_id, filmwebpl_film_id, film_duration, letterboxd_film_id, cnc_film_rating, exploitation_visa_number, tmdb_movie_id, original_language, kinobox_film_id, eidr_content_id, original_title, film_composer, freebase_id and douban_film_id. This table declares no primary key upstream.

Source table filmography_cast_members must be extracted from the postgres backend. Its business key is actor_name, and it carries a biography and many biographical and authority-identifier attributes for each actor.

Source table film_directors must be extracted from the postgres backend. Its business key is full_name.

Source table film_industry_professionals must be extracted from the files backend. Its business key is full_name.

Source table composer_biographies must be extracted from the files backend. Its business key is composer_name.

Relationships between the sources, exactly as the schema declares them:

The child table filmography_details, through its key cast_member, refers to the parent table filmography_cast_members, through its key actor_name. This relationship is required.

The child table filmography_details, through its key film_composer, refers to the parent table composer_biographies, through its key composer_name. This relationship is optional (the value may be NULL or dangling).

The child table filmography_details, through its key film_director, refers to the parent table film_directors, through its key full_name. This relationship is required.

The child table filmography_details, through its key film_screenwriter, refers to the parent table film_industry_professionals, through its key full_name. This relationship is optional (the value may be NULL or dangling).

Throughout, a filmography_details row is said to be linked to a filmography_cast_members row when its cast_member value matches that actor's actor_name.


MART filmography_cast_members_filmography_details_rollup — a per-filmography_cast_members roll-up of linked filmography_details rows in the ROGER_VINCENT_FILMOGRAPHY database, following the link on to composer_biographies.

Grain: one row per filmography_cast_members (actor_name), INCLUDING filmography_cast_members rows with no linked filmography_details rows.

Key column: parent_key.

Rule 1. The source table filmography_cast_members is read in full.

Rule 2. The source table filmography_details is read in full.

Rule 3. The source table composer_biographies is read in full.

Rule 4. Because filmography_details declares no primary key upstream, byte-identical duplicate rows can occur in it; every such row counts ONCE, however many copies arrive, where two rows are the same when they agree on csfd_film_id, cast_member, film_composer, film_color and film_duration together with the rest of their contents. Sameness is judged byte-identically across the ENTIRE filmography_details row — every column of that source table, including columns this mart never reads, such as film_title and release_date — so two filmography_details rows that agree on csfd_film_id, cast_member, film_composer, film_color and film_duration but differ in any other column are NOT duplicates of each other: they are two distinct linked rows, and each one is counted separately in link_count, active_link_count, distinct_child_count, total_amount and active_amount.

Rule 5. There is one row per filmography_cast_members row, keyed by actor_name: parent_key is that actor_name and parent_name is carried beside it.

Rule 6. Hop 1 brings in filmography_details rows against the grain, matching the cast_member of a filmography_details row to the parent_key value, so actor_name and cast_member are carried together; one filmography_cast_members row may have many filmography_details rows, and a filmography_cast_members row with no filmography_details rows at all is RETAINED. Preservation is left-sided: the filmography_cast_members side keeps its rows.

Rule 7. Hop 2 brings in composer_biographies, matching each linked filmography_details row's film_composer to the composer_name of a composer_biographies row, so film_composer and composer_name are carried together. A filmography_details row whose composer_biographies row is missing still counts as a link and is RETAINED; preservation is left-sided towards the linked filmography_details rows.

Rule 8. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, and that row reports link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 9. The mart columns are named parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount; total_amount, active_amount and max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount and max_amount, the default also applies to a group none of whose real rows carries an input value.

Rule 10. Guarded ratio: beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount, the column active_amount_ratio is active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0 or has no value.

Rule 11. Beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount and active_amount_ratio, size_band is the size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band, so a value exactly at 2 is 'small' and a value exactly at 5 is 'medium'.

Rule 12. Beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio and size_band, has_links is 'yes' when this parent has at least one link, 'no' otherwise, and is never NULL; a value exactly at 0 links is 'no'.

Rule 13. Deterministic output order: rows appear sorted by parent_key in ascending order.

Output columns of filmography_cast_members_filmography_details_rollup:

parent_key (text): identifier of the filmography_cast_members row; there is one row per value.

parent_name (text): the biography of the filmography_cast_members row, copied unchanged.

link_count (bigint): the number of DISTINCT filmography_details rows linked to this filmography_cast_members row, and 0 when there are none. Because filmography_details declares no primary key upstream and byte-identical duplicate rows occur in the source, such duplicates count ONCE.

distinct_child_count (bigint): the number of DISTINCT composer_biographies rows reached through those links. Two links pointing at the same child count ONCE, the value is 0 when there are no links, and a link whose composer_biographies row is missing reaches no composer_biographies row and adds nothing to this count.

active_link_count (bigint): the number of linked filmography_details rows whose film_color is one of ['black-and-white'], each counted once even if the row repeats. A parent whose links ALL fail that test reports 0, not a missing row.

total_amount (integer): the total of film_duration over every DISTINCT linked row; 0 when there are no links, and 0 when none of the linked rows carries a film_duration value.

active_amount (integer): the total of film_duration over links whose film_color is one of ['black-and-white'], each counted once even if the row repeats; 0 when none qualify, and 0 when every qualifying row lacks a film_duration value.

max_amount (integer): the largest film_duration among the linked rows; 0 when there are no links, and 0 when none of the linked rows carries a film_duration value.

active_amount_ratio (float): active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0.

size_band (text): the size band of link_count — 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band.

has_links (text): 'yes' when this parent has at least one link, 'no' otherwise. Never NULL.


MART filmography_cast_members_filmography_details_cohorts — a per-(filmography_cast_members, observed status cohort) summary of linked filmography_details rows in the ROGER_VINCENT_FILMOGRAPHY database.

Grain: one row per (actor_name, status cohort) pair represented among linked filmography_details rows, plus one no-activity row for a filmography_cast_members row with no linked filmography_details row at all. A filmography_cast_members row whose linked filmography_details rows all lack a film_color value is in no cohort and gets no no-activity row, so it has no row in this mart.

Key columns: entity_key and cohort.

Rule 1. The source table filmography_cast_members is read in full.

Rule 2. The source table filmography_details is read in full.

Rule 3. Each actor_name and its biography from filmography_cast_members are carried into the cohort calculation as entity_key and entity_name.

Rule 4. The linked filmography_details rows are brought into each filmography_cast_members entity before assigning status cohorts, matching the cast_member of a filmography_details row to the entity_key value, carrying entity_key, entity_name, actor_name and cast_member; preservation is left-sided, so an entity with no matching filmography_details row is kept at this stage. This mart performs no deduplication of filmography_details: the once-only counting of byte-identical duplicate filmography_details rows described for mart filmography_cast_members_filmography_details_rollup applies to that mart only, so here every linked filmography_details row is counted as it arrives — each byte-identical copy counts again in its cell's link_count, and each copy's film_duration is added again to that cell's total_amount (max_amount and max_amount_share are unaffected by repetition).

Rule 5. For the passing cohort, the rows kept are those whose film_color belongs to the passing cohort values ['black-and-white'], carrying entity_key and entity_name.

Rule 6. There is one row per filmography_cast_members entity that has at least one linked filmography_details row in the passing cohort, reporting under entity_key and entity_name the number of those rows as link_count, how many different film_color values occur among them as distinct_status_count, the total of their film_duration as total_amount, and their largest film_duration as max_amount. The total and the largest value read only the rows that carry a film_duration value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 7. Beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 8. These measures — entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share — are labelled with cohort 'passing'.

Rule 9. For the failing cohort, the rows kept are those whose film_color belongs to the failing cohort values ['color'], carrying entity_key and entity_name.

Rule 10. There is one row per filmography_cast_members entity that has at least one linked filmography_details row in the failing cohort, reporting under entity_key and entity_name the number of those rows as link_count, how many different film_color values occur among them as distinct_status_count, the total of their film_duration as total_amount, and their largest film_duration as max_amount. The total and the largest value read only the rows that carry a film_duration value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 11. Beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share for the failing cohort is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 12. These measures — entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share — are labelled with cohort 'failing'.

Rule 13. The placeholder row, carrying entity_key and entity_name, is kept for a filmography_cast_members entity with no linked filmography_details row at all; a filmography_cast_members entity that has linked filmography_details rows gets no placeholder, even when every one of those rows lacks a film_color value.

Rule 14. There is one row per filmography_cast_members entity with no linked filmography_details row at all, reporting under entity_key and entity_name 0 rows as link_count, 0 different film_color values as distinct_status_count, a film_duration total of 0 as total_amount and a largest film_duration of 0 as max_amount.

Rule 15. Beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share for the no-activity rows is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 16. These measures — entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share — are labelled with cohort 'no_activity'.

Rule 17. The disjoint passing and failing cohort summaries are combined into one result, keeping all rows of both, carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 18. The no-activity summaries are added to that result, keeping all of their rows, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 19. Deterministic output order: rows appear sorted by entity_key ascending, then by cohort ascending.

Output columns of filmography_cast_members_filmography_details_cohorts:

entity_key (text): identifier of the filmography_cast_members row.

cohort (text): 'passing' for film_color values ['black-and-white']; 'failing' for values ['color']; 'no_activity' when the filmography_cast_members row has no linked filmography_details row. A linked filmography_details row whose film_color has no value belongs to no cohort: it is not counted in any cell, and it does not make the filmography_cast_members row 'no_activity'.

entity_name (text): the biography of the filmography_cast_members row, copied unchanged.

link_count (bigint): the number of filmography_details rows in this entity/cohort cell.

distinct_status_count (bigint): the number of distinct film_color values represented in this cell.

total_amount (integer): the total of film_duration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an film_duration value.

max_amount (integer): the largest film_duration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an film_duration value.

max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `filmography_cast_members_filmography_details_rollup`

- Grain: One row per filmography_cast_members (actor_name), INCLUDING filmography_cast_members rows with no linked filmography_details rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links

```text
Mart 'filmography_cast_members_filmography_details_rollup' has 13 declared semantic rules:
1. [source] Read source table filmography_cast_members. (public source tables: filmography_cast_members)
2. [source] Read source table filmography_details. (public source tables: filmography_details)
3. [source] Read source table composer_biographies. (public source tables: composer_biographies)
4. [dedupe] filmography_details declares no primary key upstream, so byte-identical duplicate rows can occur; every such row counts ONCE, however many copies arrive. (public source tables: filmography_details | public carried/output columns: csfd_film_id, cast_member, film_composer, film_color, film_duration)
5. [derive] One row per filmography_cast_members row, keyed by actor_name. (public source tables: filmography_cast_members | public carried/output columns: parent_key, parent_name)
6. [join] Hop 1: bring in filmography_details against the grain. One filmography_cast_members row may have many filmography_details rows, and a filmography_cast_members row with no filmography_details rows at all is RETAINED. (public source tables: filmography_details | public carried/output columns: cast_member, actor_name | join preservation: left | condition public identifiers: filmography_details, cast_member, parent_key)
7. [join] Hop 2: bring in composer_biographies, matching each linked filmography_details row's film_composer to the composer_name of a composer_biographies row. A filmography_details row whose composer_biographies row is missing still counts as a link and is RETAINED. (public source tables: composer_biographies | public carried/output columns: composer_name, film_composer | join preservation: left | condition public identifiers: composer_biographies, composer_name)
8. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
9. [derive] Name the mart columns; total_amount, active_amount, max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount, max_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
10. [ratio] Guarded ratios: active_amount_ratio — active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] size_band — Size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band | semantic parameters: boundary=a value exactly at 2 is 'small'; a value exactly at 5 is 'medium')
12. [conditional] has_links — 'yes' when this parent has at least one link, 'no' otherwise. Never NULL. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links | semantic parameters: boundary=a value exactly at 0 is 'no')
13. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `filmography_cast_members_filmography_details_cohorts`

- Grain: One row per (actor_name, status cohort) pair represented among linked filmography_details rows, plus one no-activity row for a filmography_cast_members row with no linked filmography_details row at all. A filmography_cast_members row whose linked filmography_details rows all lack a film_color value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'filmography_cast_members_filmography_details_cohorts' has 19 declared semantic rules:
1. [source] Read source table filmography_cast_members. (public source tables: filmography_cast_members)
2. [source] Read source table filmography_details. (public source tables: filmography_details)
3. [derive] Carry each actor_name and its biography into the cohort calculation. (public source tables: filmography_cast_members | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked filmography_details rows into each filmography_cast_members entity before assigning status cohorts. (public source tables: filmography_details | public carried/output columns: entity_key, entity_name, actor_name, cast_member | join preservation: left | condition public identifiers: filmography_details, cast_member, entity_key)
5. [filter] Keep rows whose film_color belongs to the passing cohort values ['black-and-white']. (public carried/output columns: entity_key, entity_name | condition literal specification values: black-and-white)
6. [distinct] One row per filmography_cast_members entity that has at least one linked filmography_details row in the passing cohort, reporting the number of those rows, how many different film_color values occur among them, the total of their film_duration, and their largest film_duration. The total and the largest value read only the rows that carry a film_duration value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose film_color belongs to the failing cohort values ['color']. (public carried/output columns: entity_key, entity_name | condition literal specification values: color)
10. [distinct] One row per filmography_cast_members entity that has at least one linked filmography_details row in the failing cohort, reporting the number of those rows, how many different film_color values occur among them, the total of their film_duration, and their largest film_duration. The total and the largest value read only the rows that carry a film_duration value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a filmography_cast_members entity with no linked filmography_details row at all; a filmography_cast_members entity that has linked filmography_details rows gets no placeholder, even when every one of those rows lacks a film_color value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per filmography_cast_members entity with no linked filmography_details row at all, reporting 0 rows, 0 different film_color values, a film_duration total of 0 and a largest film_duration of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

## Source tables

### filmography_details  (source backend: mongodb)
Source table filmography_details of the ROGER_VINCENT_FILMOGRAPHY database (77 real rows).

- `film_title`: text NOT NULL — film title of filmography details (real vendored values).
- `film_description`: text NOT NULL — film description of filmography details (real vendored values).
- `film_type`: text NOT NULL — film type of filmography details (real vendored values).
- `film_director`: text NOT NULL — film director of filmography details (real vendored values).
- `film_screenwriter`: text NULL — film screenwriter of filmography details (real vendored values).
- `cast_member`: text NOT NULL — cast member of filmography details (real vendored values).
- `im_db_id`: text NOT NULL — im db id of filmography details (real vendored values).
- `release_date`: text NOT NULL — release date of filmography details (real vendored values).
- `country_of_origin`: text NOT NULL — country of origin of filmography details (real vendored values).
- `film_genre`: text NOT NULL — film genre of filmography details (real vendored values).
- `kinopoisk_film_id`: integer NULL — kinopoisk film id of filmography details (real vendored values).
- `of_db_film_id`: integer NULL — of db film id of filmography details (real vendored values).
- `film_color`: text NULL — film color of filmography details (real vendored values).
- `csfd_film_id`: integer NOT NULL — csfd film id of filmography details (real vendored values).
- `movie_meter_film_id`: integer NULL — movie meter film id of filmography details (real vendored values).
- `all_movie_title_id`: text NULL — all movie title id of filmography details (real vendored values).
- `el_film_film_id`: integer NOT NULL — el film film id of filmography details (real vendored values).
- `allo_cine_film_id`: integer NULL — allo cine film id of filmography details (real vendored values).
- `cine_ressources_film_id`: integer NULL — cine ressources film id of filmography details (real vendored values).
- `film_affinity_film_id`: integer NULL — film affinity film id of filmography details (real vendored values).
- `unifrance_film_id`: integer NULL — unifrance film id of filmography details (real vendored values).
- `ldi_f_id`: integer NULL — ldi f id of filmography details (real vendored values).
- `cinematheque_quebecoise_work_id`: integer NULL — cinematheque quebecoise work id of filmography details (real vendored values).
- `filmwebpl_film_id`: integer NULL — filmwebpl film id of filmography details (real vendored values).
- `film_duration`: integer NULL — film duration of filmography details (real vendored values).
- `letterboxd_film_id`: text NOT NULL — letterboxd film id of filmography details (real vendored values).
- `cnc_film_rating`: text NULL — cnc film rating of filmography details (real vendored values).
- `exploitation_visa_number`: integer NULL — exploitation visa number of filmography details (real vendored values).
- `tmdb_movie_id`: integer NOT NULL — tmdb movie id of filmography details (real vendored values).
- `original_language`: text NULL — original language of filmography details (real vendored values).
- `kinobox_film_id`: integer NOT NULL — kinobox film id of filmography details (real vendored values).
- `eidr_content_id`: text NULL — eidr content id of filmography details (real vendored values).
- `original_title`: text NULL — original title of filmography details (real vendored values).
- `film_composer`: text NULL — film composer of filmography details (real vendored values).
- `freebase_id`: text NULL — freebase id of filmography details (real vendored values).
- `douban_film_id`: integer NULL — douban film id of filmography details (real vendored values).

### filmography_cast_members  (source backend: postgres)
Source table filmography_cast_members of the ROGER_VINCENT_FILMOGRAPHY database (61 real rows).

- `actor_name`: text NOT NULL — actor name of filmography cast members (real vendored values).
- `biography`: text NOT NULL — biography of filmography cast members (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of filmography cast members (real vendored values).
- `birthplace`: text NULL — birthplace of filmography cast members (real vendored values).
- `deathplace`: text NULL — deathplace of filmography cast members (real vendored values).
- `nationality`: text NULL — nationality of filmography cast members (real vendored values).
- `profession`: text NOT NULL — profession of filmography cast members (real vendored values).
- `viaf_id`: text NULL — viaf id of filmography cast members (real vendored values).
- `isni_identifier`: text NULL — isni identifier of filmography cast members (real vendored values).
- `gnd_id`: text NULL — gnd id of filmography cast members (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of filmography cast members (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of filmography cast members (real vendored values).
- `id_ref_id`: text NULL — id ref id of filmography cast members (real vendored values).
- `commons_category`: text NULL — commons category of filmography cast members (real vendored values).
- `im_db_id`: text NULL — im db id of filmography cast members (real vendored values).
- `birth_date`: text NOT NULL — birth date of filmography cast members (real vendored values).
- `death_date`: text NULL — death date of filmography cast members (real vendored values).
- `profile_image`: text NULL — profile image of filmography cast members (real vendored values).
- `entity_type`: text NOT NULL — entity type of filmography cast members (real vendored values).
- `freebase_id`: text NULL — freebase id of filmography cast members (real vendored values).
- `first_name`: text NULL — first name of filmography cast members (real vendored values).
- `languages`: text NOT NULL — languages of filmography cast members (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of filmography cast members (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of filmography cast members (real vendored values).
- `full_birth_name`: text NULL — full birth name of filmography cast members (real vendored values).
- `port_person_id`: integer NULL — port person id of filmography cast members (real vendored values).
- `fast_id`: integer NULL — fast id of filmography cast members (real vendored values).
- `national_library_of_spain_id`: text NULL — national library of spain id of filmography cast members (real vendored values).
- `les_archives_du_spectacle_person_id`: integer NULL — les archives du spectacle person id of filmography cast members (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of filmography cast members (real vendored values).
- `filmportal_id`: text NULL — filmportal id of filmography cast members (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of filmography cast members (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of filmography cast members (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of filmography cast members (real vendored values).
- `snac_ark_id`: text NULL — snac ark id of filmography cast members (real vendored values).
- `career_start_year`: text NULL — career start year of filmography cast members (real vendored values).
- `cine_magia_person_id`: integer NULL — cine magia person id of filmography cast members (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of filmography cast members (real vendored values).
- `burial_site`: text NULL — burial site of filmography cast members (real vendored values).
- `surname`: text NULL — surname of filmography cast members (real vendored values).
- `gender`: text NOT NULL — gender of filmography cast members (real vendored values).
- `music_brainz_artist_id`: text NULL — music brainz artist id of filmography cast members (real vendored values).
- `cine_ressources_person_id`: integer NULL — cine ressources person id of filmography cast members (real vendored values).
- `unifrance_person_id`: integer NULL — unifrance person id of filmography cast members (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of filmography cast members (real vendored values).
- `deutsche_biographie_gnd_id`: text NULL — deutsche biographie gnd id of filmography cast members (real vendored values).
- `cinematheque_quebecoise_person_id`: integer NULL — cinematheque quebecoise person id of filmography cast members (real vendored values).
- `national_library_of_israel_j9u_id`: bigint NULL — national library of israel j9u id of filmography cast members (real vendored values).
- `fichier_des_personnes_decedees_id_match_id`: text NULL — fichier des personnes decedees id match id of filmography cast members (real vendored values).
- `m_ymovies_person_id`: integer NULL — m ymovies person id of filmography cast members (real vendored values).
- `ivi_person_id`: text NULL — ivi person id of filmography cast members (real vendored values).
- `kinobox_person_id`: integer NULL — kinobox person id of filmography cast members (real vendored values).
- `prabook_id`: integer NULL — prabook id of filmography cast members (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of filmography cast members (real vendored values).
- business key: actor_name

### film_directors  (source backend: postgres)
Source table film_directors of the ROGER_VINCENT_FILMOGRAPHY database (55 real rows).

- `full_name`: text NOT NULL — full name of film directors (real vendored values).
- `biography`: text NULL — biography of film directors (real vendored values).
- `viaf_id`: text NULL — viaf id of film directors (real vendored values).
- `isni_identifier`: text NULL — isni identifier of film directors (real vendored values).
- `primary_occupation`: text NOT NULL — primary occupation of film directors (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of film directors (real vendored values).
- `gnd_id`: text NULL — gnd id of film directors (real vendored values).
- `im_db_id`: text NOT NULL — im db id of film directors (real vendored values).
- `id_ref_id`: text NULL — id ref id of film directors (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of film directors (real vendored values).
- `birthplace`: text NULL — birthplace of film directors (real vendored values).
- `deathplace`: text NULL — deathplace of film directors (real vendored values).
- `nationality`: text NULL — nationality of film directors (real vendored values).
- `birth_date`: text NULL — birth date of film directors (real vendored values).
- `death_date`: text NULL — death date of film directors (real vendored values).
- `entity_type`: text NOT NULL — entity type of film directors (real vendored values).
- `freebase_id`: text NULL — freebase id of film directors (real vendored values).
- `first_name`: text NULL — first name of film directors (real vendored values).
- `languages`: text NULL — languages of film directors (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of film directors (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of film directors (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of film directors (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of film directors (real vendored values).
- `port_person_id`: integer NULL — port person id of film directors (real vendored values).
- `fast_id`: integer NULL — fast id of film directors (real vendored values).
- `national_library_of_spain_id`: text NULL — national library of spain id of film directors (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of film directors (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of film directors (real vendored values).
- `filmportal_id`: text NULL — filmportal id of film directors (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of film directors (real vendored values).
- `snac_ark_id`: text NULL — snac ark id of film directors (real vendored values).
- `gender`: text NULL — gender of film directors (real vendored values).
- `surname`: text NULL — surname of film directors (real vendored values).
- `related_category`: text NULL — related category of film directors (real vendored values).
- `deutsche_biographie_gnd_id`: text NULL — deutsche biographie gnd id of film directors (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of film directors (real vendored values).
- `plwabn_id`: bigint NULL — plwabn id of film directors (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of film directors (real vendored values).
- `movie_meter_person_id`: integer NULL — movie meter person id of film directors (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of film directors (real vendored values).
- `cine_ressources_person_id`: integer NULL — cine ressources person id of film directors (real vendored values).
- `kinobox_person_id`: integer NULL — kinobox person id of film directors (real vendored values).
- `native_name`: text NULL — native name of film directors (real vendored values).
- `cinematheque_quebecoise_person_id`: integer NULL — cinematheque quebecoise person id of film directors (real vendored values).
- `fichier_des_personnes_decedees_id_match_id`: text NULL — fichier des personnes decedees id match id of film directors (real vendored values).
- `birth_name`: text NULL — birth name of film directors (real vendored values).
- business key: full_name

### film_industry_professionals  (source backend: files)
Source table film_industry_professionals of the ROGER_VINCENT_FILMOGRAPHY database (51 real rows).

- `full_name`: text NOT NULL — full name of film industry professionals (real vendored values).
- `biography`: text NOT NULL — biography of film industry professionals (real vendored values).
- `id_ref_id`: text NULL — id ref id of film industry professionals (real vendored values).
- `death_location`: text NULL — death location of film industry professionals (real vendored values).
- `birth_location`: text NULL — birth location of film industry professionals (real vendored values).
- `viaf_id`: integer NULL — viaf id of film industry professionals (real vendored values).
- `international_standard_name_identifier`: text NULL — international standard name identifier of film industry professionals (real vendored values).
- `nationality`: text NOT NULL — nationality of film industry professionals (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of film industry professionals (real vendored values).
- `gnd_id`: text NULL — gnd id of film industry professionals (real vendored values).
- `im_db_id`: text NULL — im db id of film industry professionals (real vendored values).
- `birth_date`: text NOT NULL — birth date of film industry professionals (real vendored values).
- `death_date`: text NOT NULL — death date of film industry professionals (real vendored values).
- `entity_type`: text NOT NULL — entity type of film industry professionals (real vendored values).
- `profession`: text NOT NULL — profession of film industry professionals (real vendored values).
- `freebase_id`: text NULL — freebase id of film industry professionals (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of film industry professionals (real vendored values).
- `surname`: text NULL — surname of film industry professionals (real vendored values).
- `first_name`: text NOT NULL — first name of film industry professionals (real vendored values).
- `nationale_thesaurus_voor_auteursnamen_id`: text NULL — nationale thesaurus voor auteursnamen id of film industry professionals (real vendored values).
- `nl_cr_aut_id`: text NULL — nl cr aut id of film industry professionals (real vendored values).
- `nukat_id`: text NULL — nukat id of film industry professionals (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of film industry professionals (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of film industry professionals (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of film industry professionals (real vendored values).
- `port_person_id`: integer NULL — port person id of film industry professionals (real vendored values).
- `fast_id`: integer NULL — fast id of film industry professionals (real vendored values).
- `filmportal_id`: text NULL — filmportal id of film industry professionals (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of film industry professionals (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of film industry professionals (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of film industry professionals (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of film industry professionals (real vendored values).
- `snac_ark_id`: text NULL — snac ark id of film industry professionals (real vendored values).
- `native_name`: text NULL — native name of film industry professionals (real vendored values).
- `babelio_author_id`: integer NULL — babelio author id of film industry professionals (real vendored values).
- `national_library_of_spain_id`: text NULL — national library of spain id of film industry professionals (real vendored values).
- `languages`: text NULL — languages of film industry professionals (real vendored values).
- `gender`: text NOT NULL — gender of film industry professionals (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of film industry professionals (real vendored values).
- `deutsche_biographie_gnd_id`: text NULL — deutsche biographie gnd id of film industry professionals (real vendored values).
- `plwabn_id`: bigint NULL — plwabn id of film industry professionals (real vendored values).
- `cinematheque_quebecoise_person_id`: integer NULL — cinematheque quebecoise person id of film industry professionals (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of film industry professionals (real vendored values).
- `national_library_of_israel_j9u_id`: bigint NULL — national library of israel j9u id of film industry professionals (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of film industry professionals (real vendored values).
- `original_name`: text NULL — original name of film industry professionals (real vendored values).
- `kinobox_person_id`: integer NULL — kinobox person id of film industry professionals (real vendored values).
- `profile_image`: text NULL — profile image of film industry professionals (real vendored values).
- `les_archives_du_spectacle_person_id`: integer NULL — les archives du spectacle person id of film industry professionals (real vendored values).
- `fichier_des_personnes_decedees_id_match_id`: text NULL — fichier des personnes decedees id match id of film industry professionals (real vendored values).
- business key: full_name

### composer_biographies  (source backend: files)
Source table composer_biographies of the ROGER_VINCENT_FILMOGRAPHY database (28 real rows).

- `composer_name`: text NOT NULL — composer name of composer biographies (real vendored values).
- `biography`: text NOT NULL — biography of composer biographies (real vendored values).
- `burial_site`: text NULL — burial site of composer biographies (real vendored values).
- `citizenship`: text NOT NULL — citizenship of composer biographies (real vendored values).
- `birthplace`: text NOT NULL — birthplace of composer biographies (real vendored values).
- `deathplace`: text NOT NULL — deathplace of composer biographies (real vendored values).
- `profession`: text NOT NULL — profession of composer biographies (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of composer biographies (real vendored values).
- `viaf_id`: text NOT NULL — viaf id of composer biographies (real vendored values).
- `gnd_id`: text NULL — gnd id of composer biographies (real vendored values).
- `international_standard_name_identifier`: text NULL — international standard name identifier of composer biographies (real vendored values).
- `music_brainz_artist_id`: text NULL — music brainz artist id of composer biographies (real vendored values).
- `first_name`: text NULL — first name of composer biographies (real vendored values).
- `birth_date`: text NOT NULL — birth date of composer biographies (real vendored values).
- `death_date`: text NOT NULL — death date of composer biographies (real vendored values).
- `entity_type`: text NOT NULL — entity type of composer biographies (real vendored values).
- `freebase_id`: text NULL — freebase id of composer biographies (real vendored values).
- `im_db_id`: text NOT NULL — im db id of composer biographies (real vendored values).
- `nationale_thesaurus_voor_auteursnamen_id`: text NULL — nationale thesaurus voor auteursnamen id of composer biographies (real vendored values).
- `musical_genre`: text NULL — musical genre of composer biographies (real vendored values).
- `discogs_artist_id`: integer NULL — discogs artist id of composer biographies (real vendored values).
- `fast_id`: integer NULL — fast id of composer biographies (real vendored values).
- `national_library_of_spain_id`: text NULL — national library of spain id of composer biographies (real vendored values).
- `les_archives_du_spectacle_person_id`: integer NULL — les archives du spectacle person id of composer biographies (real vendored values).
- `filmportal_id`: text NULL — filmportal id of composer biographies (real vendored values).
- `snac_ark_id`: text NULL — snac ark id of composer biographies (real vendored values).
- `native_name`: text NULL — native name of composer biographies (real vendored values).
- `isidore_scholar_id`: text NULL — isidore scholar id of composer biographies (real vendored values).
- `carnegie_hall_agent_id`: integer NULL — carnegie hall agent id of composer biographies (real vendored values).
- `surname`: text NULL — surname of composer biographies (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NOT NULL — bibliotheque nationale de france id of composer biographies (real vendored values).
- `languages`: text NOT NULL — languages of composer biographies (real vendored values).
- `muziekweb_performer_id`: text NULL — muziekweb performer id of composer biographies (real vendored values).
- `id_ref_id`: text NOT NULL — id ref id of composer biographies (real vendored values).
- `gender`: text NOT NULL — gender of composer biographies (real vendored values).
- `nl_cr_aut_id`: text NULL — nl cr aut id of composer biographies (real vendored values).
- `musicalics_composer_id`: integer NULL — musicalics composer id of composer biographies (real vendored values).
- `plwabn_id`: bigint NULL — plwabn id of composer biographies (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of composer biographies (real vendored values).
- `europeana_reference`: text NULL — europeana reference of composer biographies (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of composer biographies (real vendored values).
- `deutsche_biographie_gnd_id`: text NULL — deutsche biographie gnd id of composer biographies (real vendored values).
- `dahr_artist_id`: integer NULL — dahr artist id of composer biographies (real vendored values).
- `lieder_net_composer_id`: integer NULL — lieder net composer id of composer biographies (real vendored values).
- `grove_music_online_id`: text NULL — grove music online id of composer biographies (real vendored values).
- `kanto_id`: text NULL — kanto id of composer biographies (real vendored values).
- `cinematheque_quebecoise_person_id`: integer NULL — cinematheque quebecoise person id of composer biographies (real vendored values).
- `fichier_des_personnes_decedees_id_match_id`: text NULL — fichier des personnes decedees id match id of composer biographies (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of composer biographies (real vendored values).
- `national_library_of_israel_j9u_id`: bigint NULL — national library of israel j9u id of composer biographies (real vendored values).
- `awards`: text NULL — awards of composer biographies (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of composer biographies (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of composer biographies (real vendored values).
- `sacem_museum_artist_id`: integer NULL — sacem museum artist id of composer biographies (real vendored values).
- `kbr_person_id`: integer NULL — kbr person id of composer biographies (real vendored values).
- `kinobox_person_id`: integer NOT NULL — kinobox person id of composer biographies (real vendored values).
- `portrait_image`: text NULL — portrait image of composer biographies (real vendored values).
- `port_person_id`: integer NULL — port person id of composer biographies (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of composer biographies (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of composer biographies (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of composer biographies (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of composer biographies (real vendored values).
- `related_category`: text NULL — related category of composer biographies (real vendored values).
- `birth_name`: text NULL — birth name of composer biographies (real vendored values).
- business key: composer_name

### Relationships

- filmography_details(cast_member) -> filmography_cast_members(actor_name) [required]
- filmography_details(film_composer) -> composer_biographies(composer_name) [optional (may be NULL/dangling)]
- filmography_details(film_director) -> film_directors(full_name) [required]
- filmography_details(film_screenwriter) -> film_industry_professionals(full_name) [optional (may be NULL/dangling)]

