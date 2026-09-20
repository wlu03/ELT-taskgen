# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Mattis Mathiesen Filmography

## Specification

PROJECT OVERVIEW — Mattis Mathiesen Filmography

This project builds two marts from the Mattis_Mathiesen_Filmography database. Five source tables are involved, each with the backend it must be extracted from:

- Source table filmography_details must be extracted from the mongodb backend. It describes films, with film_title, film_description, film_type, film_director, film_screenwriter, director_of_photography, lead_actor, im_db_id, country_of_origin, original_language, release_date, original_title, norwegian_filmography_id, swedish_film_database_film_id, film_duration, production_company, film_composer, film_genre, film_color, el_film_film_id, kinopoisk_film_id, eidr_content_id, letterboxd_film_id, filmfront_film_id_archived, google_knowledge_graph_id, tmdb_movie_id, lumiere_film_id, trakttv_id, kinobox_film_id and film_editor.
- Source table film_industry_professionals must be extracted from the mongodb backend. Its business key is full_name.
- Source table screenwriter must be extracted from the s3 backend. Its business key is full_name.
- Source table norwegian_filmmakers must be extracted from the mongodb backend. Its business key is full_name.
- Source table filmography_cast_details must be extracted from the mongodb backend. Its business key is actor_name.

Relationships between these tables, exactly as the source schema declares them:

- Child table filmography_details through its column film_director refers to parent table film_industry_professionals through its column full_name; this relationship is required.
- Child table filmography_details through its column film_editor refers to parent table norwegian_filmmakers through its column full_name; this relationship is optional, meaning film_editor may hold no value or a value with no matching parent row.
- Child table filmography_details through its column film_screenwriter refers to parent table screenwriter through its column full_name; this relationship is required.
- Child table filmography_details through its column lead_actor refers to parent table filmography_cast_details through its column actor_name; this relationship is optional, meaning lead_actor may hold no value or a value with no matching parent row.

Throughout, a filmography_details row is said to be attributed to a screenwriter row when its film_screenwriter equals that screenwriter row's full_name. Numbers written as fractions are plain fractions, never percentages.

================================================================
Mart screenwriter_filmography_details_rollup — a per-screenwriter roll-up of the linked filmography_details rows in the Mattis_Mathiesen_Filmography database, following the link on to film_industry_professionals.

Grain: one row per screenwriter (full_name), INCLUDING screenwriter rows with no linked filmography_details rows. The key column is parent_key.

How the mart is built, rule by rule:

1. The screenwriter source table is read in full.
2. The filmography_details source table is read in full.
3. The film_industry_professionals source table is read in full.
4. filmography_details declares no primary key upstream, so byte-identical duplicate rows can occur in it; every such row counts ONCE, however many copies arrive, where two copies are the same when they agree on all of filmfront_film_id_archived, film_screenwriter, film_director, film_color and film_duration.
5. There is one row per screenwriter row, keyed by full_name: parent_key is that full_name and parent_name is carried beside it.
6. Hop 1 brings in the filmography_details rows against the grain, matching filmography_details on film_screenwriter to parent_key: one screenwriter row may have many filmography_details rows, one screenwriter row's full_name may be repeated across them, and a screenwriter row with no filmography_details rows at all is RETAINED, so preservation here is left-sided.
7. Hop 2 brings in film_industry_professionals, matching each linked filmography_details row's film_director to the full_name of a film_industry_professionals row; a filmography_details row whose film_industry_professionals row is missing still counts as a link and is RETAINED, so preservation here is again left-sided.
8. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount for that row's matching rows. A parent with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.
9. The mart columns are named parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount; total_amount, active_amount and max_amount report their declared defaults of 0 — never NULL — for a parent with no matching rows, and that default also applies to a parent none of whose real rows carries an input value.
10. The guarded ratio active_amount_ratio is active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0 or has no value; it is carried beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount.
11. size_band is the size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, and 'large' above 5; a value exactly at 2 is 'small' and a value exactly at 5 is 'medium', so every value falls in exactly one band, and size_band is carried beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount and active_amount_ratio.
12. has_links is 'yes' when this parent has at least one link and 'no' otherwise, a value exactly at 0 links being 'no'; it is never NULL and is carried beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio and size_band.
13. Deterministic output order: rows appear sorted by parent_key ascending.

Output columns:

- parent_key (text): identifier of the screenwriter row; there is one row per value.
- parent_name (text): the biography of the screenwriter row, copied unchanged.
- link_count (bigint): the number of DISTINCT filmography_details rows linked to this screenwriter row, and 0 when there are none; filmography_details declares no primary key upstream and byte-identical duplicate rows occur in the source, and they count ONCE.
- distinct_child_count (bigint): the number of DISTINCT film_industry_professionals rows reached through those links; two links pointing at the same child count ONCE, and the value is 0 when there are no links.
- active_link_count (bigint): the number of linked filmography_details rows whose film_color is one of ['black-and-white'], each counted once even if the row repeats; a parent whose links ALL fail that test reports 0, not a missing row.
- total_amount (integer): the total of film_duration over every DISTINCT linked row; 0 when there are no links, and 0 when none of the linked rows carries a film_duration value.
- active_amount (integer): the total of film_duration over links whose film_color is one of ['black-and-white'], each counted once even if the row repeats; 0 when none qualify, and 0 when every qualifying row lacks a film_duration value.
- max_amount (integer): the largest film_duration among the linked rows; 0 when there are no links, and 0 when none of the linked rows carries a film_duration value.
- active_amount_ratio (float): active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0.
- size_band (text): the size band of link_count — 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5; every value falls in exactly one band.
- has_links (text): 'yes' when this parent has at least one link, 'no' otherwise; never NULL.

================================================================
Mart screenwriter_filmography_details_cohorts — a per-(screenwriter, observed status cohort) summary of the linked filmography_details rows in the Mattis_Mathiesen_Filmography database.

Grain: one row per (full_name, status cohort) pair represented among linked filmography_details rows, plus one no-activity row for a screenwriter row with no linked filmography_details row at all. A screenwriter row whose linked filmography_details rows all lack a film_color value is in no cohort and gets no no-activity row, so it has no row in this mart. The key columns are entity_key and cohort.

How the mart is built, rule by rule:

1. The screenwriter source table is read in full.
2. The filmography_details source table is read in full.
3. Each screenwriter full_name is carried into the cohort calculation as entity_key, and its biography is carried as entity_name. This mart applies no deduplication of its own: filmography_details declares no primary key upstream, so byte-identical duplicate rows can occur, and unlike mart screenwriter_filmography_details_rollup — where two copies agreeing on filmfront_film_id_archived, film_screenwriter, film_director, film_color and film_duration count ONCE — this mart reads the filmography_details source table as it arrives, so every copy of a duplicated linked row is counted separately: each copy adds to link_count, each copy's film_duration is added into total_amount, and every copy is considered for max_amount (and hence for max_amount_share) in the entity/cohort cell it falls in.
4. The linked filmography_details rows are brought into each screenwriter entity before status cohorts are assigned, matching filmography_details on film_screenwriter to entity_key and carrying entity_key, entity_name, full_name and film_screenwriter; preservation here is left-sided, so a screenwriter entity with no such rows is retained at this point.
5. For the passing cohort, only the rows whose film_color is one of the passing cohort values ['black-and-white'] are kept, carrying entity_key and entity_name.
6. There is one row per screenwriter entity that has at least one linked filmography_details row in the passing cohort, reporting entity_key, entity_name, link_count as the number of those rows, distinct_status_count as how many different film_color values occur among them, total_amount as the total of their film_duration, and max_amount as their largest film_duration. The total and the largest value read only the rows that carry a film_duration value; a cohort whose rows all lack one reports 0 for both, never empty.
7. For these passing rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it is carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.
8. These measures are labelled with cohort 'passing', carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
9. For the failing cohort, only the rows whose film_color is one of the failing cohort values ['color'] are kept, carrying entity_key and entity_name.
10. There is one row per screenwriter entity that has at least one linked filmography_details row in the failing cohort, reporting entity_key, entity_name, link_count as the number of those rows, distinct_status_count as how many different film_color values occur among them, total_amount as the total of their film_duration, and max_amount as their largest film_duration. The total and the largest value read only the rows that carry a film_duration value; a cohort whose rows all lack one reports 0 for both, never empty.
11. For these failing rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it is carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.
12. These measures are labelled with cohort 'failing', carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
13. The placeholder row, carrying entity_key and entity_name, is kept for a screenwriter entity with no linked filmography_details row at all; a screenwriter entity that has linked filmography_details rows gets no placeholder, even when every one of those rows lacks a film_color value.
14. There is one row per screenwriter entity with no linked filmography_details row at all, carrying entity_key and entity_name and reporting link_count of 0 rows, distinct_status_count of 0 different film_color values, a total_amount film_duration total of 0 and a max_amount largest film_duration of 0.
15. For these placeholder rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it is carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.
16. These measures are labelled with cohort 'no_activity', carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
17. The disjoint passing and failing cohort summaries are combined, all rows of both being kept, carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
18. The no-activity summaries are added to that combination, all such rows being kept, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
19. Deterministic output order: rows appear sorted ascending by entity_key first, then by cohort.

Output columns:

- entity_key (text): identifier of the screenwriter row.
- cohort (text): 'passing' for film_color values ['black-and-white']; 'failing' for values ['color']; 'no_activity' when the screenwriter row has no linked filmography_details row. A linked filmography_details row whose film_color has no value belongs to no cohort: it is not counted in any cell, and it does not make the screenwriter row 'no_activity'.
- entity_name (text): the biography of the screenwriter row, copied unchanged.
- link_count (bigint): the number of filmography_details rows in this entity/cohort cell.
- distinct_status_count (bigint): the number of different film_color values represented in this cell.
- total_amount (integer): the total of film_duration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries a film_duration value.
- max_amount (integer): the largest film_duration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries a film_duration value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `screenwriter_filmography_details_rollup`

- Grain: One row per screenwriter (full_name), INCLUDING screenwriter rows with no linked filmography_details rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links

```text
Mart 'screenwriter_filmography_details_rollup' has 13 declared semantic rules:
1. [source] Read source table screenwriter. (public source tables: screenwriter)
2. [source] Read source table filmography_details. (public source tables: filmography_details)
3. [source] Read source table film_industry_professionals. (public source tables: film_industry_professionals)
4. [dedupe] filmography_details declares no primary key upstream, so byte-identical duplicate rows can occur; every such row counts ONCE, however many copies arrive. (public source tables: filmography_details | public carried/output columns: filmfront_film_id_archived, film_screenwriter, film_director, film_color, film_duration)
5. [derive] One row per screenwriter row, keyed by full_name. (public source tables: screenwriter | public carried/output columns: parent_key, parent_name)
6. [join] Hop 1: bring in filmography_details against the grain. One screenwriter row may have many filmography_details rows, and a screenwriter row with no filmography_details rows at all is RETAINED. (public source tables: filmography_details | public carried/output columns: film_screenwriter, full_name | join preservation: left | condition public identifiers: filmography_details, film_screenwriter, parent_key)
7. [join] Hop 2: bring in film_industry_professionals, matching each linked filmography_details row's film_director to the full_name of a film_industry_professionals row. A filmography_details row whose film_industry_professionals row is missing still counts as a link and is RETAINED. (public source tables: film_industry_professionals | public carried/output columns: full_name, film_director | join preservation: left | condition public identifiers: film_industry_professionals, full_name)
8. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
9. [derive] Name the mart columns; total_amount, active_amount, max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount, max_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
10. [ratio] Guarded ratios: active_amount_ratio — active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] size_band — Size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band | semantic parameters: boundary=a value exactly at 2 is 'small'; a value exactly at 5 is 'medium')
12. [conditional] has_links — 'yes' when this parent has at least one link, 'no' otherwise. Never NULL. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links | semantic parameters: boundary=a value exactly at 0 is 'no')
13. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `screenwriter_filmography_details_cohorts`

- Grain: One row per (full_name, status cohort) pair represented among linked filmography_details rows, plus one no-activity row for a screenwriter row with no linked filmography_details row at all. A screenwriter row whose linked filmography_details rows all lack a film_color value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'screenwriter_filmography_details_cohorts' has 19 declared semantic rules:
1. [source] Read source table screenwriter. (public source tables: screenwriter)
2. [source] Read source table filmography_details. (public source tables: filmography_details)
3. [derive] Carry each full_name and its biography into the cohort calculation. (public source tables: screenwriter | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked filmography_details rows into each screenwriter entity before assigning status cohorts. (public source tables: filmography_details | public carried/output columns: entity_key, entity_name, full_name, film_screenwriter | join preservation: left | condition public identifiers: filmography_details, film_screenwriter, entity_key)
5. [filter] Keep rows whose film_color belongs to the passing cohort values ['black-and-white']. (public carried/output columns: entity_key, entity_name | condition literal specification values: black-and-white)
6. [distinct] One row per screenwriter entity that has at least one linked filmography_details row in the passing cohort, reporting the number of those rows, how many different film_color values occur among them, the total of their film_duration, and their largest film_duration. The total and the largest value read only the rows that carry a film_duration value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose film_color belongs to the failing cohort values ['color']. (public carried/output columns: entity_key, entity_name | condition literal specification values: color)
10. [distinct] One row per screenwriter entity that has at least one linked filmography_details row in the failing cohort, reporting the number of those rows, how many different film_color values occur among them, the total of their film_duration, and their largest film_duration. The total and the largest value read only the rows that carry a film_duration value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a screenwriter entity with no linked filmography_details row at all; a screenwriter entity that has linked filmography_details rows gets no placeholder, even when every one of those rows lacks a film_color value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per screenwriter entity with no linked filmography_details row at all, reporting 0 rows, 0 different film_color values, a film_duration total of 0 and a largest film_duration of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

## Source tables

### filmography_details  (source backend: mongodb)
Source table filmography_details of the Mattis_Mathiesen_Filmography database (31 real rows).

- `film_title`: text NOT NULL — film title of filmography details (real vendored values).
- `film_description`: text NOT NULL — film description of filmography details (real vendored values).
- `film_type`: text NOT NULL — film type of filmography details (real vendored values).
- `film_director`: text NOT NULL — film director of filmography details (real vendored values).
- `film_screenwriter`: text NOT NULL — film screenwriter of filmography details (real vendored values).
- `director_of_photography`: text NOT NULL — director of photography of filmography details (real vendored values).
- `lead_actor`: text NULL — lead actor of filmography details (real vendored values).
- `im_db_id`: text NOT NULL — im db id of filmography details (real vendored values).
- `country_of_origin`: text NOT NULL — country of origin of filmography details (real vendored values).
- `original_language`: text NOT NULL — original language of filmography details (real vendored values).
- `release_date`: text NOT NULL — release date of filmography details (real vendored values).
- `original_title`: text NULL — original title of filmography details (real vendored values).
- `norwegian_filmography_id`: integer NULL — norwegian filmography id of filmography details (real vendored values).
- `swedish_film_database_film_id`: integer NULL — swedish film database film id of filmography details (real vendored values).
- `film_duration`: integer NULL — film duration of filmography details (real vendored values).
- `production_company`: text NULL — production company of filmography details (real vendored values).
- `film_composer`: text NULL — film composer of filmography details (real vendored values).
- `film_genre`: text NULL — film genre of filmography details (real vendored values).
- `film_color`: text NULL — film color of filmography details (real vendored values).
- `el_film_film_id`: integer NULL — el film film id of filmography details (real vendored values).
- `kinopoisk_film_id`: integer NULL — kinopoisk film id of filmography details (real vendored values).
- `eidr_content_id`: text NULL — eidr content id of filmography details (real vendored values).
- `letterboxd_film_id`: text NOT NULL — letterboxd film id of filmography details (real vendored values).
- `filmfront_film_id_archived`: integer NOT NULL — filmfront film id archived of filmography details (real vendored values).
- `google_knowledge_graph_id`: text NULL — google knowledge graph id of filmography details (real vendored values).
- `tmdb_movie_id`: integer NOT NULL — tmdb movie id of filmography details (real vendored values).
- `lumiere_film_id`: integer NULL — lumiere film id of filmography details (real vendored values).
- `trakttv_id`: text NULL — trakttv id of filmography details (real vendored values).
- `kinobox_film_id`: integer NULL — kinobox film id of filmography details (real vendored values).
- `film_editor`: text NULL — film editor of filmography details (real vendored values).

### film_industry_professionals  (source backend: mongodb)
Source table film_industry_professionals of the Mattis_Mathiesen_Filmography database (10 real rows).

- `full_name`: text NOT NULL — full name of film industry professionals (real vendored values).
- `biography`: text NOT NULL — biography of film industry professionals (real vendored values).
- `im_db_id`: text NOT NULL — im db id of film industry professionals (real vendored values).
- `kinopoisk_person_id`: integer NOT NULL — kinopoisk person id of film industry professionals (real vendored values).
- `entity_type`: text NOT NULL — entity type of film industry professionals (real vendored values).
- `nationality`: text NOT NULL — nationality of film industry professionals (real vendored values).
- `viaf_id`: text NOT NULL — viaf id of film industry professionals (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of film industry professionals (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of film industry professionals (real vendored values).
- `international_standard_name_identifier`: text NULL — international standard name identifier of film industry professionals (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of film industry professionals (real vendored values).
- `gender`: text NOT NULL — gender of film industry professionals (real vendored values).
- `languages`: text NULL — languages of film industry professionals (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of film industry professionals (real vendored values).
- `surname`: text NOT NULL — surname of film industry professionals (real vendored values).
- `birth_date`: text NOT NULL — birth date of film industry professionals (real vendored values).
- `awards`: text NULL — awards of film industry professionals (real vendored values).
- `birth_place`: text NOT NULL — birth place of film industry professionals (real vendored values).
- `profession`: text NOT NULL — profession of film industry professionals (real vendored values).
- `first_name`: text NOT NULL — first name of film industry professionals (real vendored values).
- `store_norske_leksikon_id`: text NULL — store norske leksikon id of film industry professionals (real vendored values).
- `norsk_biografisk_leksikon_id`: text NULL — norsk biografisk leksikon id of film industry professionals (real vendored values).
- `filmography_category`: text NULL — filmography category of film industry professionals (real vendored values).
- `death_date`: text NULL — death date of film industry professionals (real vendored values).
- `death_place`: text NULL — death place of film industry professionals (real vendored values).
- `freebase_id`: text NOT NULL — freebase id of film industry professionals (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of film industry professionals (real vendored values).
- `movie_meter_person_id`: integer NULL — movie meter person id of film industry professionals (real vendored values).
- `prabook_id`: integer NULL — prabook id of film industry professionals (real vendored values).
- `kinobox_person_id`: integer NOT NULL — kinobox person id of film industry professionals (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of film industry professionals (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of film industry professionals (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of film industry professionals (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of film industry professionals (real vendored values).
- `partner`: text NULL — partner of film industry professionals (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of film industry professionals (real vendored values).
- business key: full_name

### screenwriter  (source backend: s3)
Source table screenwriter of the Mattis_Mathiesen_Filmography database (18 real rows).

- `full_name`: text NOT NULL — full name of screenwriter (real vendored values).
- `biography`: text NOT NULL — biography of screenwriter (real vendored values).
- `im_db_id`: text NOT NULL — im db id of screenwriter (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of screenwriter (real vendored values).
- `entity_type`: text NOT NULL — entity type of screenwriter (real vendored values).
- `nationality`: text NOT NULL — nationality of screenwriter (real vendored values).
- `viaf_id`: text NULL — viaf id of screenwriter (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of screenwriter (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of screenwriter (real vendored values).
- `international_standard_name_identifier`: text NULL — international standard name identifier of screenwriter (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of screenwriter (real vendored values).
- `gender`: text NOT NULL — gender of screenwriter (real vendored values).
- `languages`: text NULL — languages of screenwriter (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of screenwriter (real vendored values).
- `surname`: text NOT NULL — surname of screenwriter (real vendored values).
- `birth_date`: text NOT NULL — birth date of screenwriter (real vendored values).
- `awards`: text NULL — awards of screenwriter (real vendored values).
- `birth_place`: text NULL — birth place of screenwriter (real vendored values).
- `profession`: text NOT NULL — profession of screenwriter (real vendored values).
- `first_name`: text NULL — first name of screenwriter (real vendored values).
- `store_norske_leksikon_id`: text NULL — store norske leksikon id of screenwriter (real vendored values).
- `norsk_biografisk_leksikon_id`: text NULL — norsk biografisk leksikon id of screenwriter (real vendored values).
- `death_date`: text NULL — death date of screenwriter (real vendored values).
- `death_place`: text NULL — death place of screenwriter (real vendored values).
- `gnd_id`: text NULL — gnd id of screenwriter (real vendored values).
- `freebase_id`: text NULL — freebase id of screenwriter (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of screenwriter (real vendored values).
- `prabook_id`: integer NULL — prabook id of screenwriter (real vendored values).
- `kinobox_person_id`: integer NULL — kinobox person id of screenwriter (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of screenwriter (real vendored values).
- `noraf_id`: integer NULL — noraf id of screenwriter (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of screenwriter (real vendored values).
- `genicom_profile_id`: bigint NULL — genicom profile id of screenwriter (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of screenwriter (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of screenwriter (real vendored values).
- `sceneweb_artist_id`: integer NULL — sceneweb artist id of screenwriter (real vendored values).
- business key: full_name

### norwegian_filmmakers  (source backend: mongodb)
Source table norwegian_filmmakers of the Mattis_Mathiesen_Filmography database (12 real rows).

- `full_name`: text NOT NULL — full name of norwegian filmmakers (real vendored values).
- `biography`: text NOT NULL — biography of norwegian filmmakers (real vendored values).
- `im_db_id`: text NOT NULL — im db id of norwegian filmmakers (real vendored values).
- `kinopoisk_person_id`: integer NOT NULL — kinopoisk person id of norwegian filmmakers (real vendored values).
- `entity_type`: text NOT NULL — entity type of norwegian filmmakers (real vendored values).
- `nationality`: text NOT NULL — nationality of norwegian filmmakers (real vendored values).
- `viaf_id`: text NULL — viaf id of norwegian filmmakers (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of norwegian filmmakers (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of norwegian filmmakers (real vendored values).
- `gender`: text NOT NULL — gender of norwegian filmmakers (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of norwegian filmmakers (real vendored values).
- `surname`: text NOT NULL — surname of norwegian filmmakers (real vendored values).
- `birth_date`: text NULL — birth date of norwegian filmmakers (real vendored values).
- `awards`: text NULL — awards of norwegian filmmakers (real vendored values).
- `birth_place`: text NULL — birth place of norwegian filmmakers (real vendored values).
- `profession`: text NOT NULL — profession of norwegian filmmakers (real vendored values).
- `first_name`: text NOT NULL — first name of norwegian filmmakers (real vendored values).
- `port_person_id`: integer NULL — port person id of norwegian filmmakers (real vendored values).
- `death_date`: text NULL — death date of norwegian filmmakers (real vendored values).
- `freebase_id`: text NULL — freebase id of norwegian filmmakers (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of norwegian filmmakers (real vendored values).
- `kinobox_person_id`: integer NULL — kinobox person id of norwegian filmmakers (real vendored values).
- business key: full_name

### filmography_cast_details  (source backend: mongodb)
Source table filmography_cast_details of the Mattis_Mathiesen_Filmography database (12 real rows).

- `actor_name`: text NOT NULL — actor name of filmography cast details (real vendored values).
- `biography`: text NOT NULL — biography of filmography cast details (real vendored values).
- `gender`: text NOT NULL — gender of filmography cast details (real vendored values).
- `viaf_id`: text NULL — viaf id of filmography cast details (real vendored values).
- `international_standard_name_identifier`: text NULL — international standard name identifier of filmography cast details (real vendored values).
- `profession`: text NOT NULL — profession of filmography cast details (real vendored values).
- `profile_image`: text NULL — profile image of filmography cast details (real vendored values).
- `birthplace`: text NULL — birthplace of filmography cast details (real vendored values).
- `nationality`: text NOT NULL — nationality of filmography cast details (real vendored values).
- `entity_type`: text NOT NULL — entity type of filmography cast details (real vendored values).
- `birth_date`: text NOT NULL — birth date of filmography cast details (real vendored values).
- `death_date`: text NULL — death date of filmography cast details (real vendored values).
- `freebase_id`: text NULL — freebase id of filmography cast details (real vendored values).
- `noraf_id`: integer NULL — noraf id of filmography cast details (real vendored values).
- `im_db_id`: text NULL — im db id of filmography cast details (real vendored values).
- `awards`: text NULL — awards of filmography cast details (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of filmography cast details (real vendored values).
- `commons_category`: text NULL — commons category of filmography cast details (real vendored values).
- `store_norske_leksikon_id`: text NULL — store norske leksikon id of filmography cast details (real vendored values).
- `norsk_biografisk_leksikon_id`: text NULL — norsk biografisk leksikon id of filmography cast details (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of filmography cast details (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of filmography cast details (real vendored values).
- `prabook_id`: integer NULL — prabook id of filmography cast details (real vendored values).
- `sceneweb_artist_id`: integer NOT NULL — sceneweb artist id of filmography cast details (real vendored values).
- `digitalt_museum_id`: text NULL — digitalt museum id of filmography cast details (real vendored values).
- `first_name`: text NOT NULL — first name of filmography cast details (real vendored values).
- `last_name`: text NOT NULL — last name of filmography cast details (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of filmography cast details (real vendored values).
- `kultur_nav_id`: text NULL — kultur nav id of filmography cast details (real vendored values).
- `kinobox_person_id`: integer NULL — kinobox person id of filmography cast details (real vendored values).
- `genicom_profile_id`: bigint NULL — genicom profile id of filmography cast details (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of filmography cast details (real vendored values).
- `music_brainz_artist_id`: text NULL — music brainz artist id of filmography cast details (real vendored values).
- business key: actor_name

### Relationships

- filmography_details(film_director) -> film_industry_professionals(full_name) [required]
- filmography_details(film_editor) -> norwegian_filmmakers(full_name) [optional (may be NULL/dangling)]
- filmography_details(film_screenwriter) -> screenwriter(full_name) [required]
- filmography_details(lead_actor) -> filmography_cast_details(actor_name) [optional (may be NULL/dangling)]

