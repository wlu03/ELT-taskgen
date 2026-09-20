# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Jacques Haitkin Filmography

## Specification

PROJECT OVERVIEW: Jacques Haitkin Filmography

This project builds one analytical mart from the JACQUES_HAITKIN_FILMOGRAPHY database. Seven source tables are available, and each must be extracted from its own backend.

Source tables and their extraction backends:
- The source table filmography_details must be extracted from the mongodb backend; it holds film-level records (film_title, film_description, im_db_id, original_language, freebase_id, country_of_origin, media_type, release_date, film_director, music_composer, director_of_photography, film_duration, cast_members, screenwriter, film_genre, film_producer and many external catalogue identifiers).
- The source table film_industry_professionals must be extracted from the mongodb backend; its business key is full_name.
- The source table film_directors must be extracted from the files backend; its business key is director_name.
- The source table producer must be extracted from the s3 backend; its business key is full_name.
- The source table film_composers must be extracted from the files backend; its business key is composer_name.
- The source table filmography_cast_details must be extracted from the rest backend; its business key is actor_name.
- The source table film_genres must be extracted from the files backend; its business key is genre_name.

Relationships between the source tables, each labelled exactly as the schema declares it:
- The child table filmography_details through its key cast_members matches the parent table filmography_cast_details through its key actor_name; this relationship is required.
- The child table filmography_details through its key film_director matches the parent table film_directors through its key director_name; this relationship is required.
- The child table filmography_details through its key film_genre matches the parent table film_genres through its key genre_name; this relationship is optional (the value may be NULL or dangling).
- The child table filmography_details through its key film_producer matches the parent table producer through its key full_name; this relationship is optional (the value may be NULL or dangling).
- The child table filmography_details through its key music_composer matches the parent table film_composers through its key composer_name; this relationship is optional (the value may be NULL or dangling).
- The child table filmography_details through its key screenwriter matches the parent table film_industry_professionals through its key full_name; this relationship is optional (the value may be NULL or dangling).

All identifiers below are given exactly as they must appear in the output.

Mart producer_filmography_details_distribution — the per-(producer, measure state) distribution of linked filmography_details rows in the JACQUES_HAITKIN_FILMOGRAPHY database.

Grain: one row per (full_name, measure state) pair represented among linked filmography_details rows; the absent state includes missing film_duration values and a no-activity row for a producer row with no links. A linked filmography_details row whose film_duration has a value belongs only to the present state and never to the absent state.

Key columns: the key columns of this mart are entity_key and measure_state; together they identify one output row.

Output columns:
- entity_key (text): the identifier of the producer row.
- measure_state (text): 'present' for a linked filmography_details row whose film_duration has a value; 'absent' when film_duration is missing, including a producer row with no linked filmography_details row. A linked filmography_details row whose film_duration has a value belongs only to the present state and never to the absent state.
- entity_name (text): the biography of the producer row, copied unchanged.
- row_count (bigint): the number of linked filmography_details rows in this entity/state cell; an absent cell holding real filmography_details rows whose film_duration is missing counts those rows, and only the placeholder cell of a producer row with no linked filmography_details row at all reports 0.
- distinct_amount_count (bigint): the number of unique non-missing film_duration values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no film_duration value at all — both for a producer row with no linked filmography_details row and for an absent cell whose rows all have a missing film_duration.
- total_amount (integer): the total of film_duration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an film_duration value.
- max_amount (integer): the largest film_duration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an film_duration value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules, each stated as the outcome it produces:

Rule 1. The source table producer is read in full, so that every producer row is available to this mart.

Rule 2. The source table filmography_details is read in full, so that every filmography_details row is available to this mart.

Rule 3. From the source table producer, each full_name is carried into the measure-state calculation as entity_key, and its biography is carried alongside it as entity_name.

Rule 4. Each producer entity, identified by entity_key and described by entity_name, has brought into it the linked filmography_details rows — those filmography_details rows whose film_producer value matches the producer full_name that entity_key came from; preservation is left-sided on the producer side, so an entity with no linked filmography_details row is retained, with entity_key, entity_name, full_name and film_producer carried, so that its absent state stays visible.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are the rows kept because they are a real filmography_details row whose film_duration has a value.

Rule 6. Among the present measure-state rows there is one row per producer entity that has at least one row in the present measure state, and no row here for an entity with none, and that row reports, for entity_key and entity_name, the row count as row_count, how many different non-missing film_duration values occur as distinct_amount_count (each different value counted once, however many rows repeat it), the total film_duration as total_amount, and the largest film_duration as max_amount.

Rule 7. For each present-state row carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and is 0.0 when total_amount is 0.

Rule 8. These measures are labelled as the present measure state: measure_state reads 'present' on every such row, carried together with entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are the rows kept because film_duration is missing, including the retained placeholder for a producer row with no filmography_details rows; a real filmography_details row whose film_duration has a value belongs only to the present state and never to this absent state.

Rule 10. Among the absent measure-state rows there is one row per producer entity that has at least one row in the absent measure state, and no row here for an entity with none, and that row reports, for entity_key and entity_name, the row count as row_count, how many different non-missing film_duration values occur as distinct_amount_count (each different value counted once, however many rows repeat it), the total film_duration as total_amount, and the largest film_duration as max_amount.

Rule 11. For each absent-state row carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and is 0.0 when total_amount is 0.

Rule 12. These measures are labelled as the absent measure state: measure_state reads 'absent' on every such row, carried together with entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list holding all rows of both, carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. Output order is deterministic: rows appear in ascending entity_key order, and within the same entity in ascending measure_state order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `producer_filmography_details_distribution`

- Grain: One row per (full_name, measure state) pair represented among linked filmography_details rows; the absent state includes missing film_duration values and a no-activity row for a producer row with no links. A linked filmography_details row whose film_duration has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'producer_filmography_details_distribution' has 14 declared semantic rules:
1. [source] Read source table producer. (public source tables: producer)
2. [source] Read source table filmography_details. (public source tables: filmography_details)
3. [derive] Carry each full_name and its biography into the measure-state calculation. (public source tables: producer | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked filmography_details rows into each producer entity; retain an entity with no linked row so its absent state is visible. (public source tables: filmography_details | public carried/output columns: entity_key, entity_name, full_name, film_producer | join preservation: left | condition public identifiers: filmography_details, film_producer, entity_key)
5. [filter] Keep the present measure-state rows: a real filmography_details row whose film_duration has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per producer entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing film_duration values occur (each different value counted once, however many rows repeat it), total film_duration, and largest film_duration. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: film_duration is missing, including the retained placeholder for a producer row with no filmography_details rows. A real filmography_details row whose film_duration has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per producer entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing film_duration values occur (each different value counted once, however many rows repeat it), total film_duration, and largest film_duration. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### filmography_details  (source backend: mongodb)
Source table filmography_details of the JACQUES_HAITKIN_FILMOGRAPHY database (44 real rows).

- `film_title`: text NOT NULL — film title of filmography details (real vendored values).
- `film_description`: text NOT NULL — film description of filmography details (real vendored values).
- `im_db_id`: text NOT NULL — im db id of filmography details (real vendored values).
- `original_language`: text NOT NULL — original language of filmography details (real vendored values).
- `freebase_id`: text NULL — freebase id of filmography details (real vendored values).
- `country_of_origin`: text NOT NULL — country of origin of filmography details (real vendored values).
- `media_type`: text NOT NULL — media type of filmography details (real vendored values).
- `release_date`: text NOT NULL — release date of filmography details (real vendored values).
- `all_movie_title_id`: text NULL — all movie title id of filmography details (real vendored values).
- `allo_cine_film_id`: integer NULL — allo cine film id of filmography details (real vendored values).
- `film_director`: text NOT NULL — film director of filmography details (real vendored values).
- `title`: text NULL — title of filmography details (real vendored values).
- `music_composer`: text NULL — music composer of filmography details (real vendored values).
- `director_of_photography`: text NOT NULL — director of photography of filmography details (real vendored values).
- `film_duration`: integer NULL — film duration of filmography details (real vendored values).
- `of_db_film_id`: integer NULL — of db film id of filmography details (real vendored values).
- `cast_members`: text NOT NULL — cast members of filmography details (real vendored values).
- `ldi_f_id`: integer NULL — ldi f id of filmography details (real vendored values).
- `kinopoisk_film_id`: integer NOT NULL — kinopoisk film id of filmography details (real vendored values).
- `elonet_movie_id`: integer NULL — elonet movie id of filmography details (real vendored values).
- `el_film_film_id`: integer NULL — el film film id of filmography details (real vendored values).
- `tcm_movie_database_film_id`: integer NULL — tcm movie database film id of filmography details (real vendored values).
- `rotten_tomatoes_id`: text NULL — rotten tomatoes id of filmography details (real vendored values).
- `film_affinity_film_id`: integer NULL — film affinity film id of filmography details (real vendored values).
- `eidr_content_id`: text NULL — eidr content id of filmography details (real vendored values).
- `csfd_film_id`: integer NULL — csfd film id of filmography details (real vendored values).
- `movie_meter_film_id`: integer NULL — movie meter film id of filmography details (real vendored values).
- `douban_film_id`: integer NULL — douban film id of filmography details (real vendored values).
- `screenwriter`: text NULL — screenwriter of filmography details (real vendored values).
- `film_genre`: text NULL — film genre of filmography details (real vendored values).
- `distribution_format`: text NULL — distribution format of filmography details (real vendored values).
- `distributor`: text NULL — distributor of filmography details (real vendored values).
- `letterboxd_film_id`: text NOT NULL — letterboxd film id of filmography details (real vendored values).
- `tmdb_movie_id`: integer NOT NULL — tmdb movie id of filmography details (real vendored values).
- `trakttv_id`: text NULL — trakttv id of filmography details (real vendored values).
- `kinobox_film_id`: integer NOT NULL — kinobox film id of filmography details (real vendored values).
- `plex_media_key`: text NULL — plex media key of filmography details (real vendored values).
- `schnittberichtecom_title_id`: integer NULL — schnittberichtecom title id of filmography details (real vendored values).
- `the_tvdb_movie_id`: integer NULL — the tvdb movie id of filmography details (real vendored values).
- `film_vandaag_id`: text NULL — film vandaag id of filmography details (real vendored values).
- `film_color`: text NULL — film color of filmography details (real vendored values).
- `port_film_id`: integer NULL — port film id of filmography details (real vendored values).
- `deutsche_synchronkartei_film_id`: integer NULL — deutsche synchronkartei film id of filmography details (real vendored values).
- `moviepilotde_film_id`: text NULL — moviepilotde film id of filmography details (real vendored values).
- `film_producer`: text NULL — film producer of filmography details (real vendored values).
- `lumiere_film_id`: integer NULL — lumiere film id of filmography details (real vendored values).
- `apple_tv_movie_id`: text NULL — apple tv movie id of filmography details (real vendored values).

### film_industry_professionals  (source backend: mongodb)
Source table film_industry_professionals of the JACQUES_HAITKIN_FILMOGRAPHY database (23 real rows).

- `full_name`: text NOT NULL — full name of film industry professionals (real vendored values).
- `biography`: text NULL — biography of film industry professionals (real vendored values).
- `gender`: text NOT NULL — gender of film industry professionals (real vendored values).
- `viaf_id`: integer NULL — viaf id of film industry professionals (real vendored values).
- `international_standard_name_identifier`: text NULL — international standard name identifier of film industry professionals (real vendored values).
- `nationality`: text NULL — nationality of film industry professionals (real vendored values).
- `birthplace`: text NULL — birthplace of film industry professionals (real vendored values).
- `im_db_id`: text NULL — im db id of film industry professionals (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of film industry professionals (real vendored values).
- `gnd_id`: text NULL — gnd id of film industry professionals (real vendored values).
- `entity_type`: text NOT NULL — entity type of film industry professionals (real vendored values).
- `birth_date`: text NULL — birth date of film industry professionals (real vendored values).
- `freebase_id`: text NULL — freebase id of film industry professionals (real vendored values).
- `first_name`: text NOT NULL — first name of film industry professionals (real vendored values).
- `profession`: text NOT NULL — profession of film industry professionals (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of film industry professionals (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of film industry professionals (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of film industry professionals (real vendored values).
- `port_person_id`: integer NULL — port person id of film industry professionals (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of film industry professionals (real vendored values).
- `national_library_of_spain_id`: text NULL — national library of spain id of film industry professionals (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of film industry professionals (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of film industry professionals (real vendored values).
- `id_ref_id`: text NULL — id ref id of film industry professionals (real vendored values).
- `languages`: text NULL — languages of film industry professionals (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of film industry professionals (real vendored values).
- `related_film_category`: text NULL — related film category of film industry professionals (real vendored values).
- `plwabn_id`: bigint NULL — plwabn id of film industry professionals (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of film industry professionals (real vendored values).
- `movie_meter_person_id`: integer NULL — movie meter person id of film industry professionals (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of film industry professionals (real vendored values).
- `nukat_id`: text NULL — nukat id of film industry professionals (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of film industry professionals (real vendored values).
- `ivi_person_id`: text NULL — ivi person id of film industry professionals (real vendored values).
- `kinobox_person_id`: integer NULL — kinobox person id of film industry professionals (real vendored values).
- `alma_mater`: text NULL — alma mater of film industry professionals (real vendored values).
- `surname`: text NULL — surname of film industry professionals (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of film industry professionals (real vendored values).
- business key: full_name

### film_directors  (source backend: files)
Source table film_directors of the JACQUES_HAITKIN_FILMOGRAPHY database (35 real rows).

- `director_name`: text NOT NULL — director name of film directors (real vendored values).
- `biography`: text NULL — biography of film directors (real vendored values).
- `gender`: text NULL — gender of film directors (real vendored values).
- `profession`: text NULL — profession of film directors (real vendored values).
- `nationality`: text NULL — nationality of film directors (real vendored values).
- `im_db_id`: text NULL — im db id of film directors (real vendored values).
- `gnd_id`: text NULL — gnd id of film directors (real vendored values).
- `birth_date`: text NULL — birth date of film directors (real vendored values).
- `entity_type`: text NOT NULL — entity type of film directors (real vendored values).
- `birth_place`: text NULL — birth place of film directors (real vendored values).
- `freebase_id`: text NULL — freebase id of film directors (real vendored values).
- `first_name`: text NULL — first name of film directors (real vendored values).
- `national_library_of_spain_id`: text NULL — national library of spain id of film directors (real vendored values).
- `isni_code`: text NULL — isni code of film directors (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of film directors (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of film directors (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of film directors (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of film directors (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of film directors (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of film directors (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of film directors (real vendored values).
- `port_person_id`: integer NULL — port person id of film directors (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of film directors (real vendored values).
- `last_name`: text NULL — last name of film directors (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of film directors (real vendored values).
- `viaf_id`: text NULL — viaf id of film directors (real vendored values).
- `related_category`: text NULL — related category of film directors (real vendored values).
- `plwabn_id`: bigint NULL — plwabn id of film directors (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of film directors (real vendored values).
- `movie_meter_person_id`: integer NULL — movie meter person id of film directors (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of film directors (real vendored values).
- `ivi_person_id`: text NULL — ivi person id of film directors (real vendored values).
- `kinobox_person_id`: integer NULL — kinobox person id of film directors (real vendored values).
- `languages`: text NULL — languages of film directors (real vendored values).
- business key: director_name

### producer  (source backend: s3)
Source table producer of the JACQUES_HAITKIN_FILMOGRAPHY database (19 real rows).

- `full_name`: text NOT NULL — full name of producer (real vendored values).
- `biography`: text NOT NULL — biography of producer (real vendored values).
- `gender`: text NOT NULL — gender of producer (real vendored values).
- `viaf_id`: integer NULL — viaf id of producer (real vendored values).
- `international_standard_name_identifier`: text NULL — international standard name identifier of producer (real vendored values).
- `nationality`: text NULL — nationality of producer (real vendored values).
- `birthplace`: text NULL — birthplace of producer (real vendored values).
- `im_db_id`: text NOT NULL — im db id of producer (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of producer (real vendored values).
- `gnd_id`: integer NULL — gnd id of producer (real vendored values).
- `entity_type`: text NOT NULL — entity type of producer (real vendored values).
- `birth_date`: text NULL — birth date of producer (real vendored values).
- `freebase_id`: text NULL — freebase id of producer (real vendored values).
- `first_name`: text NOT NULL — first name of producer (real vendored values).
- `profession`: text NOT NULL — profession of producer (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of producer (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of producer (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of producer (real vendored values).
- `port_person_id`: integer NULL — port person id of producer (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of producer (real vendored values).
- `national_library_of_spain_id`: text NULL — national library of spain id of producer (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of producer (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of producer (real vendored values).
- `id_ref_id`: text NULL — id ref id of producer (real vendored values).
- `languages`: text NULL — languages of producer (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of producer (real vendored values).
- `nl_cr_aut_id`: text NULL — nl cr aut id of producer (real vendored values).
- `related_film_category`: text NULL — related film category of producer (real vendored values).
- `plwabn_id`: bigint NULL — plwabn id of producer (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of producer (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of producer (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of producer (real vendored values).
- `ivi_person_id`: text NULL — ivi person id of producer (real vendored values).
- `kinobox_person_id`: integer NOT NULL — kinobox person id of producer (real vendored values).
- `alma_mater`: text NULL — alma mater of producer (real vendored values).
- `surname`: text NULL — surname of producer (real vendored values).
- `prabook_id`: integer NULL — prabook id of producer (real vendored values).
- business key: full_name

### film_composers  (source backend: files)
Source table film_composers of the JACQUES_HAITKIN_FILMOGRAPHY database (26 real rows).

- `composer_name`: text NOT NULL — composer name of film composers (real vendored values).
- `composer_description`: text NOT NULL — composer description of film composers (real vendored values).
- `viaf_id`: text NULL — viaf id of film composers (real vendored values).
- `composer_isni`: text NULL — composer isni of film composers (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of film composers (real vendored values).
- `gnd_id`: text NULL — gnd id of film composers (real vendored values).
- `music_brainz_artist_id`: text NULL — music brainz artist id of film composers (real vendored values).
- `im_db_id`: text NULL — im db id of film composers (real vendored values).
- `id_ref_id`: text NULL — id ref id of film composers (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of film composers (real vendored values).
- `composer_birth_date`: text NULL — composer birth date of film composers (real vendored values).
- `entity_type`: text NOT NULL — entity type of film composers (real vendored values).
- `composer_website`: text NULL — composer website of film composers (real vendored values).
- `birth_place`: text NULL — birth place of film composers (real vendored values).
- `freebase_id`: text NOT NULL — freebase id of film composers (real vendored values).
- `citizenship_country`: text NULL — citizenship country of film composers (real vendored values).
- `composer_occupation`: text NULL — composer occupation of film composers (real vendored values).
- `education_institution`: text NULL — education institution of film composers (real vendored values).
- `first_name`: text NULL — first name of film composers (real vendored values).
- `all_music_artist_id`: text NULL — all music artist id of film composers (real vendored values).
- `discogs_artist_id`: integer NULL — discogs artist id of film composers (real vendored values).
- `nl_cr_aut_id`: text NULL — nl cr aut id of film composers (real vendored values).
- `national_library_of_spain_id`: text NULL — national library of spain id of film composers (real vendored values).
- `nukat_id`: text NULL — nukat id of film composers (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of film composers (real vendored values).
- `snac_ark_id`: text NULL — snac ark id of film composers (real vendored values).
- `gender`: text NULL — gender of film composers (real vendored values).
- `languages`: text NULL — languages of film composers (real vendored values).
- `career_start_year`: text NULL — career start year of film composers (real vendored values).
- `europeana_entity_id`: text NULL — europeana entity id of film composers (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of film composers (real vendored values).
- `last_name`: text NULL — last name of film composers (real vendored values).
- `plwabn_id`: bigint NULL — plwabn id of film composers (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of film composers (real vendored values).
- `muziekweb_performer_id`: text NULL — muziekweb performer id of film composers (real vendored values).
- `prabook_id`: integer NULL — prabook id of film composers (real vendored values).
- `kinobox_person_id`: integer NULL — kinobox person id of film composers (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of film composers (real vendored values).
- business key: composer_name

### filmography_cast_details  (source backend: rest)
Source table filmography_cast_details of the JACQUES_HAITKIN_FILMOGRAPHY database (43 real rows).

- `actor_name`: text NOT NULL — actor name of filmography cast details (real vendored values).
- `biography`: text NOT NULL — biography of filmography cast details (real vendored values).
- `commons_category`: text NULL — commons category of filmography cast details (real vendored values).
- `viaf_id`: text NULL — viaf id of filmography cast details (real vendored values).
- `gnd_id`: text NULL — gnd id of filmography cast details (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of filmography cast details (real vendored values).
- `role`: text NOT NULL — role of filmography cast details (real vendored values).
- `isni_identifier`: text NULL — isni identifier of filmography cast details (real vendored values).
- `im_db_id`: text NOT NULL — im db id of filmography cast details (real vendored values).
- `profile_image`: text NULL — profile image of filmography cast details (real vendored values).
- `id_ref_id`: text NULL — id ref id of filmography cast details (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of filmography cast details (real vendored values).
- `nationality`: text NOT NULL — nationality of filmography cast details (real vendored values).
- `birth_date`: text NULL — birth date of filmography cast details (real vendored values).
- `entity_type`: text NOT NULL — entity type of filmography cast details (real vendored values).
- `birth_place`: text NULL — birth place of filmography cast details (real vendored values).
- `freebase_id`: text NULL — freebase id of filmography cast details (real vendored values).
- `first_name`: text NOT NULL — first name of filmography cast details (real vendored values).
- `nationale_thesaurus_voor_auteursnamen_id`: text NULL — nationale thesaurus voor auteursnamen id of filmography cast details (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of filmography cast details (real vendored values).
- `rotten_tomatoes_id`: text NULL — rotten tomatoes id of filmography cast details (real vendored values).
- `last_name`: text NULL — last name of filmography cast details (real vendored values).
- `languages_spoken`: text NULL — languages spoken of filmography cast details (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of filmography cast details (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of filmography cast details (real vendored values).
- `port_person_id`: integer NULL — port person id of filmography cast details (real vendored values).
- `education`: text NULL — education of filmography cast details (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of filmography cast details (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of filmography cast details (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of filmography cast details (real vendored values).
- `national_library_of_spain_id`: text NULL — national library of spain id of filmography cast details (real vendored values).
- `scopedk_person_id`: integer NULL — scopedk person id of filmography cast details (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of filmography cast details (real vendored values).
- `nndb_people_id`: text NULL — nndb people id of filmography cast details (real vendored values).
- `nukat_id`: text NULL — nukat id of filmography cast details (real vendored values).
- `tmdb_person_id`: integer NOT NULL — tmdb person id of filmography cast details (real vendored values).
- `open_media_database_person_id`: integer NULL — open media database person id of filmography cast details (real vendored values).
- `nl_cr_aut_id`: text NULL — nl cr aut id of filmography cast details (real vendored values).
- `gender`: text NOT NULL — gender of filmography cast details (real vendored values).
- `career_start_year`: text NULL — career start year of filmography cast details (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of filmography cast details (real vendored values).
- `plwabn_id`: bigint NULL — plwabn id of filmography cast details (real vendored values).
- `fandango_person_id`: integer NULL — fandango person id of filmography cast details (real vendored values).
- `cine_magia_person_id`: integer NULL — cine magia person id of filmography cast details (real vendored values).
- `m_ymovies_person_id`: integer NULL — m ymovies person id of filmography cast details (real vendored values).
- `native_language`: text NULL — native language of filmography cast details (real vendored values).
- `writing_language`: text NULL — writing language of filmography cast details (real vendored values).
- `i_sz_db_person_id`: integer NULL — i sz db person id of filmography cast details (real vendored values).
- `national_library_of_israel_j9u_id`: bigint NULL — national library of israel j9u id of filmography cast details (real vendored values).
- `douban_movie_celebrity_id`: integer NULL — douban movie celebrity id of filmography cast details (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of filmography cast details (real vendored values).
- `ivi_person_id`: text NULL — ivi person id of filmography cast details (real vendored values).
- `filmwebpl_person_id`: integer NULL — filmwebpl person id of filmography cast details (real vendored values).
- `kinobox_person_id`: integer NOT NULL — kinobox person id of filmography cast details (real vendored values).
- `listal_id`: text NULL — listal id of filmography cast details (real vendored values).
- business key: actor_name

### film_genres  (source backend: files)
Source table film_genres of the JACQUES_HAITKIN_FILMOGRAPHY database (16 real rows).

- `genre_name`: text NOT NULL — genre name of film genres (real vendored values).
- `genre_description`: text NOT NULL — genre description of film genres (real vendored values).
- `genre_subclass`: text NOT NULL — genre subclass of film genres (real vendored values).
- `genre_instance`: text NOT NULL — genre instance of film genres (real vendored values).
- `commons_category`: text NULL — commons category of film genres (real vendored values).
- `main_category`: text NOT NULL — main category of film genres (real vendored values).
- `freebase_id`: text NULL — freebase id of film genres (real vendored values).
- `representative_image`: text NULL — representative image of film genres (real vendored values).
- `related_list`: text NULL — related list of film genres (real vendored values).
- `babel_net_id`: text NULL — babel net id of film genres (real vendored values).
- `k_bpedia_id`: text NULL — k bpedia id of film genres (real vendored values).
- `yso_id`: integer NULL — yso id of film genres (real vendored values).
- `all_movie_genre_id`: text NULL — all movie genre id of film genres (real vendored values).
- `rate_your_music_film_genre_id`: text NULL — rate your music film genre id of film genres (real vendored values).
- `gnd_id`: text NULL — gnd id of film genres (real vendored values).
- `library_of_congress_genre_form_terms_id`: text NULL — library of congress genre form terms id of film genres (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of film genres (real vendored values).
- `national_library_of_israel_j9u_id`: bigint NULL — national library of israel j9u id of film genres (real vendored values).
- `nl_cr_aut_id`: text NULL — nl cr aut id of film genres (real vendored values).
- business key: genre_name

### Relationships

- filmography_details(cast_members) -> filmography_cast_details(actor_name) [required]
- filmography_details(film_director) -> film_directors(director_name) [required]
- filmography_details(film_genre) -> film_genres(genre_name) [optional (may be NULL/dangling)]
- filmography_details(film_producer) -> producer(full_name) [optional (may be NULL/dangling)]
- filmography_details(music_composer) -> film_composers(composer_name) [optional (may be NULL/dangling)]
- filmography_details(screenwriter) -> film_industry_professionals(full_name) [optional (may be NULL/dangling)]

