# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Gloria Swanson Filmography

## Specification

PROJECT OVERVIEW: Gloria Swanson Filmography

This project builds two marts from the Gloria_Swanson_Filmography database. The source tables and the extraction backend each must be extracted from are as follows.

Source table film_appearances must be extracted from the s3 backend; it holds film title, film description, im db id, film type, director name, co star, commons category, screenwriter name, producer name, production company, cinematographer, release date, country of origin, film genre, film color, film duration, distributor, kinopoisk film id and related vendored attributes of the films.

Source table filmography_cast_members must be extracted from the s3 backend; its business key is full_name, and it holds biography, primary occupation, birth and death information and many external identifiers for cast members.

Source table screenwriter must be extracted from the s3 backend; it holds full_name, biography, role, birth and death information and external identifiers for screenwriters.

Source table film_directors must be extracted from the s3 backend; its business key is director_name, and it holds biography, primary occupation, birth and death information and external identifiers for directors.

Source table gloria_swanson_cinematographers must be extracted from the files backend; its business key is name, and it holds biography, role, nationality, birth and death information and external identifiers for cinematographers.

Source table film_industry_figures must be extracted from the files backend; its business key is full_name, and it holds biography, citizenship, profession, birth and death information and external identifiers for producers and other industry figures.

Source table film_studios must be extracted from the rest backend; its business key is studio_name, and it holds studio description, studio type, country of origin, founding date and external identifiers for studios.

Source table surname_metadata must be extracted from the s3 backend; it holds surname, surname description, entity type, writing system, native surname and phonetic codes.

RELATIONSHIPS BETWEEN THE SOURCE TABLES

Child table film_appearances with key cinematographer refers to parent table gloria_swanson_cinematographers with key name; this relationship is optional (the child value may be NULL or dangling).

Child table film_appearances with key co_star refers to parent table filmography_cast_members with key full_name; this relationship is required.

Child table film_appearances with key director_name refers to parent table film_directors with key director_name; this relationship is optional (the child value may be NULL or dangling).

Child table film_appearances with key producer_name refers to parent table film_industry_figures with key full_name; this relationship is optional (the child value may be NULL or dangling).

Child table film_appearances with key production_company refers to parent table film_studios with key studio_name; this relationship is optional (the child value may be NULL or dangling).

Throughout both marts, a film_appearances row is attributed to a gloria_swanson_cinematographers row when the film_appearances value of cinematographer is the same as the gloria_swanson_cinematographers value of name.

=======================================================================
MART gloria_swanson_cinematographers_film_appearances_distribution — the per-(gloria_swanson_cinematographers, measure state) distribution of linked film_appearances rows in the Gloria_Swanson_Filmography database.

Grain: one row per (name, measure state) pair represented among linked film_appearances rows; the absent state includes missing film_duration values and a no-activity row for a gloria_swanson_cinematographers row with no links. A linked film_appearances row whose film_duration has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state together identify one output row.

Output columns.

entity_key (text): the identifier of the gloria_swanson_cinematographers row.

measure_state (text): 'present' for a linked film_appearances row whose film_duration has a value; 'absent' when film_duration is missing, including a gloria_swanson_cinematographers row with no linked film_appearances row. A linked film_appearances row whose film_duration has a value belongs only to the present state and never to the absent state.

entity_name (text): the biography of the gloria_swanson_cinematographers row, copied unchanged.

row_count (bigint): the number of linked film_appearances rows in this entity/state cell; an absent cell holding real film_appearances rows whose film_duration is missing counts those rows, and only the placeholder cell of a gloria_swanson_cinematographers row with no linked film_appearances row at all reports 0.

distinct_amount_count (bigint): the number of unique non-missing film_duration values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no film_duration value at all — both for a gloria_swanson_cinematographers row with no linked film_appearances row and for an absent cell whose rows all have a missing film_duration.

total_amount (integer): the total of film_duration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries a film_duration value.

max_amount (integer): the largest film_duration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries a film_duration value.

max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules.

Rule 1. The source table gloria_swanson_cinematographers is read in full and supplies the entities of this mart.

Rule 2. The source table film_appearances is read in full and supplies the film rows measured in this mart.

Rule 3. From gloria_swanson_cinematographers, each name is carried as entity_key and its biography is carried as entity_name into the measure-state calculation.

Rule 4. The film_appearances rows attributed to each gloria_swanson_cinematographers entity are brought in through the film_appearances column cinematographer matching the gloria_swanson_cinematographers column name, which is the entity_key, carrying entity_key, entity_name, name and cinematographer; preservation is left-sided on the entity side, so an entity with no linked film_appearances row is retained and its absent state is visible.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are the rows kept because each is a real film_appearances row whose film_duration has a value.

Rule 6. The present measure state reports one row per gloria_swanson_cinematographers entity that has at least one row in the present measure state, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different non-missing film_duration values occur (each different value counted once, however many rows repeat it), total_amount as the total film_duration, and max_amount as the largest film_duration.

Rule 7. For each present-state row, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, the value max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These present measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state holding the present measure state, the literal value 'present'.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are the rows kept because film_duration is missing, including the retained placeholder for a gloria_swanson_cinematographers row with no film_appearances rows. A real film_appearances row whose film_duration has a value belongs only to the present state and never to this absent state.

Rule 10. The absent measure state reports one row per gloria_swanson_cinematographers entity that has at least one row in the absent measure state, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different non-missing film_duration values occur (each different value counted once, however many rows repeat it), total_amount as the total film_duration, and max_amount as the largest film_duration.

Rule 11. For each absent-state row, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, the value max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These absent measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state holding the absent measure state, the literal value 'absent'.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. Deterministic output order: rows appear in ascending entity_key order, then in ascending measure_state order.

=======================================================================
MART gloria_swanson_cinematographers_film_appearances_top — the per-gloria_swanson_cinematographers extremes over linked film_appearances rows in the Gloria_Swanson_Filmography database: WHICH row is largest, not how large it is.

Grain: one row per gloria_swanson_cinematographers (name), including gloria_swanson_cinematographers rows with no linked film_appearances rows.

Key columns: parent_key alone identifies one output row.

Output columns.

parent_key (text): the identifier of the gloria_swanson_cinematographers row. One row per value.

parent_name (text): the biography of the gloria_swanson_cinematographers row, copied unchanged.

top_measure (integer): the largest film_duration itself; 0 when the parent has no film_appearances rows, and 0 when none of its rows carries a film_duration value.

tied_count (bigint): how many film_appearances rows are tied at that largest film_duration. It is 1 when exactly one row carries that largest film_duration; 0 when there are no rows or when none of the rows carries a film_duration value; a row with no film_duration value never ties: only a row whose film_duration value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.

child_count (bigint): the number of film_appearances rows for this gloria_swanson_cinematographers row; 0 when there are none. Every linked film_appearances row counts, whether or not it carries a film_duration value. A gloria_swanson_cinematographers row kept with no film_appearances row reports 0 here, never 1: its placeholder holds no film_appearances row to count.

total_measure (integer): the total of film_duration over all of them; 0 when the parent has no film_appearances rows, and 0 when none of its rows carries a film_duration value (rows with no film_duration value add nothing).

top_label (text): the film_title of the film_appearances row with the LARGEST film_duration for this gloria_swanson_cinematographers row. Ties in film_duration are broken by taking the SMALLEST film_title under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one); rows tied on both are resolved by the smallest kinopoisk_film_id. A row with no film_duration value still ranks, after every row that has one, so a parent holding at least one film_appearances row always has a winning row — when NONE of its rows carries a film_duration value the winner is the one the tie-break alone selects, not the no-rows default. It is the literal '(none)' when the parent has no film_appearances rows at all.

top_row_id (integer): the kinopoisk_film_id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real film_appearances row whenever the parent has any. This includes when none of them carries a film_duration value. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.

top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.

tie_state (text): 'empty' when no row holds a maximum at all — the parent has no film_appearances rows, or none of its rows carries a film_duration value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a film_duration value equal to the largest film_duration value among the parent's rows; a row with no film_duration value never holds the maximum. So a parent whose film_appearances rows all lack a film_duration value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

Rules.

Rule 1. The source table gloria_swanson_cinematographers is read in full and supplies the parents of this mart.

Rule 2. The source table film_appearances is read in full and supplies the child rows of this mart.

Rule 3. There is one row per gloria_swanson_cinematographers row, keyed by name: name is carried as parent_key and the biography is carried as parent_name.

Rule 4. The film_appearances rows are brought in through the film_appearances column cinematographer matching the gloria_swanson_cinematographers column name, which is the parent_key, carrying cinematographer and name; preservation is left-sided on the parent side, so a gloria_swanson_cinematographers row with no film_appearances rows still appears, with the declared defaults.

Rule 5. Within each parent_key the attributed rows are ranked under an explicit total order — the measure first, then the declared tie-break — so the extremal row is a function of the input and not of row order.

Rule 6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, and that row reports top_measure, tied_count, child_count and total_measure for its matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7. The single row kept for each parent_key is the one at which the ordering measure is largest, ties broken by the smallest film_title under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then the smallest kinopoisk_film_id, and top_label and top_row_id are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 8. The extremal row's attributes are attached to the grouped measures by matching on parent_key; preservation is left-sided on the measures side, so a group with no rows at all keeps its measures.

Rule 9. The mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure and total_measure, the default also applies to a group none of whose real rows carries an input value.

Rule 10. Guarded ratios, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and it is 0.0 when the denominator total_measure is 0 or has no value.

Rule 11. Beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, the categorical value tie_state is 'empty' when no row holds a maximum at all — the parent has no film_appearances rows, or none of its rows carries a film_duration value — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; equivalently tie_state follows tied_count, 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a film_duration value equal to the largest film_duration value among the parent's rows; a row with no film_duration value never holds the maximum, so a parent whose film_appearances rows all lack a film_duration value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

Rule 12. Deterministic output order: rows appear sorted in ascending parent_key order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `gloria_swanson_cinematographers_film_appearances_distribution`

- Grain: One row per (name, measure state) pair represented among linked film_appearances rows; the absent state includes missing film_duration values and a no-activity row for a gloria_swanson_cinematographers row with no links. A linked film_appearances row whose film_duration has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'gloria_swanson_cinematographers_film_appearances_distribution' has 14 declared semantic rules:
1. [source] Read source table gloria_swanson_cinematographers. (public source tables: gloria_swanson_cinematographers)
2. [source] Read source table film_appearances. (public source tables: film_appearances)
3. [derive] Carry each name and its biography into the measure-state calculation. (public source tables: gloria_swanson_cinematographers | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked film_appearances rows into each gloria_swanson_cinematographers entity; retain an entity with no linked row so its absent state is visible. (public source tables: film_appearances | public carried/output columns: entity_key, entity_name, name, cinematographer | join preservation: left | condition public identifiers: film_appearances, cinematographer, entity_key)
5. [filter] Keep the present measure-state rows: a real film_appearances row whose film_duration has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per gloria_swanson_cinematographers entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing film_duration values occur (each different value counted once, however many rows repeat it), total film_duration, and largest film_duration. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: film_duration is missing, including the retained placeholder for a gloria_swanson_cinematographers row with no film_appearances rows. A real film_appearances row whose film_duration has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per gloria_swanson_cinematographers entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing film_duration values occur (each different value counted once, however many rows repeat it), total film_duration, and largest film_duration. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `gloria_swanson_cinematographers_film_appearances_top`

- Grain: One row per gloria_swanson_cinematographers (name), INCLUDING gloria_swanson_cinematographers rows with no linked film_appearances rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'gloria_swanson_cinematographers_film_appearances_top' has 12 declared semantic rules:
1. [source] Read source table gloria_swanson_cinematographers. (public source tables: gloria_swanson_cinematographers)
2. [source] Read source table film_appearances. (public source tables: film_appearances)
3. [derive] One row per gloria_swanson_cinematographers row, keyed by name. (public source tables: gloria_swanson_cinematographers | public carried/output columns: parent_key, parent_name)
4. [join] Bring in film_appearances: a gloria_swanson_cinematographers row with no film_appearances rows still appears, with the declared defaults. (public source tables: film_appearances | public carried/output columns: cinematographer, name | join preservation: left | condition public identifiers: film_appearances, cinematographer, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest film_title under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then the smallest kinopoisk_film_id, and take top_label, top_row_id from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no film_appearances rows, or none of its rows carries a film_duration value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a film_duration value equal to the largest film_duration value among the parent's rows; a row with no film_duration value never holds the maximum. So a parent whose film_appearances rows all lack a film_duration value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### film_appearances  (source backend: s3)
Source table film_appearances of the Gloria_Swanson_Filmography database (75 real rows).

- `film_title`: text NOT NULL — film title of film appearances (real vendored values).
- `film_description`: text NOT NULL — film description of film appearances (real vendored values).
- `im_db_id`: text NULL — im db id of film appearances (real vendored values).
- `film_type`: text NOT NULL — film type of film appearances (real vendored values).
- `director_name`: text NULL — director name of film appearances (real vendored values).
- `co_star`: text NOT NULL — co star of film appearances (real vendored values).
- `commons_category`: text NULL — commons category of film appearances (real vendored values).
- `screenwriter_name`: text NULL — screenwriter name of film appearances (real vendored values).
- `producer_name`: text NULL — producer name of film appearances (real vendored values).
- `production_company`: text NULL — production company of film appearances (real vendored values).
- `cinematographer`: text NULL — cinematographer of film appearances (real vendored values).
- `freebase_id`: text NULL — freebase id of film appearances (real vendored values).
- `release_date`: text NOT NULL — release date of film appearances (real vendored values).
- `country_of_origin`: text NOT NULL — country of origin of film appearances (real vendored values).
- `all_movie_title_id`: text NULL — all movie title id of film appearances (real vendored values).
- `film_genre`: text NULL — film genre of film appearances (real vendored values).
- `title`: text NULL — title of film appearances (real vendored values).
- `film_color`: text NULL — film color of film appearances (real vendored values).
- `film_duration`: integer NULL — film duration of film appearances (real vendored values).
- `distributor`: text NULL — distributor of film appearances (real vendored values).
- `of_db_film_id`: integer NULL — of db film id of film appearances (real vendored values).
- `kinopoisk_film_id`: integer NOT NULL — kinopoisk film id of film appearances (real vendored values).
- `afi_catalog_of_feature_films_id`: integer NULL — afi catalog of feature films id of film appearances (real vendored values).
- `eidr_content_id`: text NULL — eidr content id of film appearances (real vendored values).
- `cinematheque_quebecoise_work_id`: integer NULL — cinematheque quebecoise work id of film appearances (real vendored values).
- `tmdb_movie_id`: integer NULL — tmdb movie id of film appearances (real vendored values).
- `letterboxd_film_id`: text NULL — letterboxd film id of film appearances (real vendored values).
- `kinobox_film_id`: integer NULL — kinobox film id of film appearances (real vendored values).
- `aspect_ratio`: text NULL — aspect ratio of film appearances (real vendored values).
- `silent_eracom_film_id`: text NULL — silent eracom film id of film appearances (real vendored values).
- `copyright_status`: text NULL — copyright status of film appearances (real vendored values).

### filmography_cast_members  (source backend: s3)
Source table filmography_cast_members of the Gloria_Swanson_Filmography database (20 real rows).

- `full_name`: text NOT NULL — full name of filmography cast members (real vendored values).
- `biography`: text NOT NULL — biography of filmography cast members (real vendored values).
- `primary_occupation`: text NOT NULL — primary occupation of filmography cast members (real vendored values).
- `viaf_id`: integer NULL — viaf id of filmography cast members (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of filmography cast members (real vendored values).
- `gnd_id`: text NULL — gnd id of filmography cast members (real vendored values).
- `international_standard_name_identifier`: text NULL — international standard name identifier of filmography cast members (real vendored values).
- `notable_award`: text NULL — notable award of filmography cast members (real vendored values).
- `commons_category`: text NULL — commons category of filmography cast members (real vendored values).
- `birthplace`: text NOT NULL — birthplace of filmography cast members (real vendored values).
- `deathplace`: text NOT NULL — deathplace of filmography cast members (real vendored values).
- `profile_image`: text NULL — profile image of filmography cast members (real vendored values).
- `partner`: text NULL — partner of filmography cast members (real vendored values).
- `im_db_id`: text NOT NULL — im db id of filmography cast members (real vendored values).
- `id_ref_id`: text NULL — id ref id of filmography cast members (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of filmography cast members (real vendored values).
- `birth_date`: text NOT NULL — birth date of filmography cast members (real vendored values).
- `death_date`: text NOT NULL — death date of filmography cast members (real vendored values).
- `nl_cr_aut_id`: text NULL — nl cr aut id of filmography cast members (real vendored values).
- `entity_type`: text NOT NULL — entity type of filmography cast members (real vendored values).
- `main_category`: text NULL — main category of filmography cast members (real vendored values).
- `nationality`: text NOT NULL — nationality of filmography cast members (real vendored values).
- `freebase_id`: text NOT NULL — freebase id of filmography cast members (real vendored values).
- `national_library_of_spain_id`: text NULL — national library of spain id of filmography cast members (real vendored values).
- `nationale_thesaurus_voor_auteursnamen_id`: text NULL — nationale thesaurus voor auteursnamen id of filmography cast members (real vendored values).
- `first_name`: text NOT NULL — first name of filmography cast members (real vendored values).
- `nndb_people_id`: text NULL — nndb people id of filmography cast members (real vendored values).
- `original_name`: text NULL — original name of filmography cast members (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of filmography cast members (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of filmography cast members (real vendored values).
- `gran_enciclopedia_catalana_id_former_scheme`: text NULL — gran enciclopedia catalana id former scheme of filmography cast members (real vendored values).
- `burial_site`: text NULL — burial site of filmography cast members (real vendored values).
- `language`: text NULL — language of filmography cast members (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of filmography cast members (real vendored values).
- `fast_id`: integer NULL — fast id of filmography cast members (real vendored values).
- `port_person_id`: integer NULL — port person id of filmography cast members (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of filmography cast members (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of filmography cast members (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of filmography cast members (real vendored values).
- `internet_broadway_database_person_id`: integer NULL — internet broadway database person id of filmography cast members (real vendored values).
- `wiki_tree_person_id`: text NULL — wiki tree person id of filmography cast members (real vendored values).
- `tcm_movie_database_person_id`: text NULL — tcm movie database person id of filmography cast members (real vendored values).
- `find_a_grave_memorial_id`: integer NULL — find a grave memorial id of filmography cast members (real vendored values).
- `encyclopdia_britannica_online_id`: text NULL — encyclopdia britannica online id of filmography cast members (real vendored values).
- `n_ese_id`: text NULL — n ese id of filmography cast members (real vendored values).
- `snac_ark_id`: text NULL — snac ark id of filmography cast members (real vendored values).
- `career_start`: text NULL — career start of filmography cast members (real vendored values).
- `career_end`: text NULL — career end of filmography cast members (real vendored values).
- `surname`: text NULL — surname of filmography cast members (real vendored values).
- `american_national_biography_id`: integer NULL — american national biography id of filmography cast members (real vendored values).
- `death_nature`: text NULL — death nature of filmography cast members (real vendored values).
- `death_cause`: text NULL — death cause of filmography cast members (real vendored values).
- `gender`: text NOT NULL — gender of filmography cast members (real vendored values).
- `source_description`: text NULL — source description of filmography cast members (real vendored values).
- `genicom_profile_id`: bigint NULL — genicom profile id of filmography cast members (real vendored values).
- `spoken_languages`: text NULL — spoken languages of filmography cast members (real vendored values).
- `deutsche_biographie_gnd_id`: integer NULL — deutsche biographie gnd id of filmography cast members (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of filmography cast members (real vendored values).
- `plwabn_id`: bigint NULL — plwabn id of filmography cast members (real vendored values).
- `national_library_of_israel_j9u_id`: bigint NULL — national library of israel j9u id of filmography cast members (real vendored values).
- `filmwebpl_person_id`: integer NULL — filmwebpl person id of filmography cast members (real vendored values).
- `american_film_institute_person_id`: integer NULL — american film institute person id of filmography cast members (real vendored values).
- `tmdb_person_id`: integer NOT NULL — tmdb person id of filmography cast members (real vendored values).
- `m_ymovies_person_id`: integer NULL — m ymovies person id of filmography cast members (real vendored values).
- `george_eastman_museum_people_id`: integer NULL — george eastman museum people id of filmography cast members (real vendored values).
- `prabook_id`: integer NULL — prabook id of filmography cast members (real vendored values).
- `cine_magia_person_id`: integer NULL — cine magia person id of filmography cast members (real vendored values).
- `fandango_person_id`: integer NULL — fandango person id of filmography cast members (real vendored values).
- `movie_walker_press_person_id`: integer NULL — movie walker press person id of filmography cast members (real vendored values).
- `ivi_person_id`: text NULL — ivi person id of filmography cast members (real vendored values).
- `kinobox_person_id`: integer NOT NULL — kinobox person id of filmography cast members (real vendored values).
- `gran_enciclopedia_catalana_id`: text NULL — gran enciclopedia catalana id of filmography cast members (real vendored values).
- `cinematheque_quebecoise_person_id`: integer NULL — cinematheque quebecoise person id of filmography cast members (real vendored values).
- `writing_languages`: text NULL — writing languages of filmography cast members (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of filmography cast members (real vendored values).
- business key: full_name

### screenwriter  (source backend: s3)
Source table screenwriter of the Gloria_Swanson_Filmography database (30 real rows).

- `full_name`: text NOT NULL — full name of screenwriter (real vendored values).
- `biography`: text NOT NULL — biography of screenwriter (real vendored values).
- `role`: text NOT NULL — role of screenwriter (real vendored values).
- `viaf_id`: text NULL — viaf id of screenwriter (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of screenwriter (real vendored values).
- `gnd_id`: integer NULL — gnd id of screenwriter (real vendored values).
- `isni_identifier`: text NULL — isni identifier of screenwriter (real vendored values).
- `commons_category`: text NULL — commons category of screenwriter (real vendored values).
- `birthplace`: text NOT NULL — birthplace of screenwriter (real vendored values).
- `deathplace`: text NOT NULL — deathplace of screenwriter (real vendored values).
- `profile_image`: text NULL — profile image of screenwriter (real vendored values).
- `im_db_id`: text NOT NULL — im db id of screenwriter (real vendored values).
- `id_ref_id`: text NULL — id ref id of screenwriter (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of screenwriter (real vendored values).
- `birth_date`: text NOT NULL — birth date of screenwriter (real vendored values).
- `death_date`: text NOT NULL — death date of screenwriter (real vendored values).
- `nl_cr_aut_id`: text NULL — nl cr aut id of screenwriter (real vendored values).
- `entity_type`: text NOT NULL — entity type of screenwriter (real vendored values).
- `nationality`: text NULL — nationality of screenwriter (real vendored values).
- `freebase_id`: text NOT NULL — freebase id of screenwriter (real vendored values).
- `national_library_of_spain_id`: text NULL — national library of spain id of screenwriter (real vendored values).
- `nationale_thesaurus_voor_auteursnamen_id`: text NULL — nationale thesaurus voor auteursnamen id of screenwriter (real vendored values).
- `nukat_id`: text NULL — nukat id of screenwriter (real vendored values).
- `first_name`: text NULL — first name of screenwriter (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of screenwriter (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of screenwriter (real vendored values).
- `open_library_id`: text NULL — open library id of screenwriter (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of screenwriter (real vendored values).
- `fast_id`: integer NULL — fast id of screenwriter (real vendored values).
- `port_person_id`: integer NULL — port person id of screenwriter (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of screenwriter (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of screenwriter (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of screenwriter (real vendored values).
- `find_a_grave_memorial_id`: integer NULL — find a grave memorial id of screenwriter (real vendored values).
- `snac_ark_id`: text NULL — snac ark id of screenwriter (real vendored values).
- `last_name`: text NULL — last name of screenwriter (real vendored values).
- `gender`: text NOT NULL — gender of screenwriter (real vendored values).
- `languages`: text NULL — languages of screenwriter (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of screenwriter (real vendored values).
- `national_library_of_israel_j9u_id`: bigint NULL — national library of israel j9u id of screenwriter (real vendored values).
- `american_film_institute_person_id`: integer NULL — american film institute person id of screenwriter (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of screenwriter (real vendored values).
- `prabook_id`: integer NULL — prabook id of screenwriter (real vendored values).
- `kinobox_person_id`: integer NULL — kinobox person id of screenwriter (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of screenwriter (real vendored values).

### film_directors  (source backend: s3)
Source table film_directors of the Gloria_Swanson_Filmography database (37 real rows).

- `director_name`: text NOT NULL — director name of film directors (real vendored values).
- `biography`: text NOT NULL — biography of film directors (real vendored values).
- `primary_occupation`: text NOT NULL — primary occupation of film directors (real vendored values).
- `viaf_id`: text NULL — viaf id of film directors (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of film directors (real vendored values).
- `gnd_id`: text NULL — gnd id of film directors (real vendored values).
- `isni_identifier`: text NULL — isni identifier of film directors (real vendored values).
- `commons_category`: text NULL — commons category of film directors (real vendored values).
- `birthplace`: text NOT NULL — birthplace of film directors (real vendored values).
- `deathplace`: text NULL — deathplace of film directors (real vendored values).
- `profile_image`: text NULL — profile image of film directors (real vendored values).
- `im_db_id`: text NOT NULL — im db id of film directors (real vendored values).
- `id_ref_id`: text NULL — id ref id of film directors (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of film directors (real vendored values).
- `birth_date`: text NOT NULL — birth date of film directors (real vendored values).
- `death_date`: text NOT NULL — death date of film directors (real vendored values).
- `nl_cr_aut_id`: text NULL — nl cr aut id of film directors (real vendored values).
- `entity_type`: text NOT NULL — entity type of film directors (real vendored values).
- `nationality`: text NOT NULL — nationality of film directors (real vendored values).
- `freebase_id`: text NOT NULL — freebase id of film directors (real vendored values).
- `national_library_of_spain_id`: text NULL — national library of spain id of film directors (real vendored values).
- `nationale_thesaurus_voor_auteursnamen_id`: text NULL — nationale thesaurus voor auteursnamen id of film directors (real vendored values).
- `first_name`: text NOT NULL — first name of film directors (real vendored values).
- `nndb_people_id`: text NULL — nndb people id of film directors (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of film directors (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of film directors (real vendored values).
- `burial_site`: text NULL — burial site of film directors (real vendored values).
- `language`: text NULL — language of film directors (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of film directors (real vendored values).
- `fast_id`: integer NULL — fast id of film directors (real vendored values).
- `port_person_id`: integer NULL — port person id of film directors (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of film directors (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of film directors (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of film directors (real vendored values).
- `filmportal_id`: text NULL — filmportal id of film directors (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of film directors (real vendored values).
- `find_a_grave_memorial_id`: integer NULL — find a grave memorial id of film directors (real vendored values).
- `snac_ark_id`: text NULL — snac ark id of film directors (real vendored values).
- `career_start_year`: text NULL — career start year of film directors (real vendored values).
- `last_name`: text NULL — last name of film directors (real vendored values).
- `death_cause`: text NULL — death cause of film directors (real vendored values).
- `gender`: text NOT NULL — gender of film directors (real vendored values).
- `related_category`: text NULL — related category of film directors (real vendored values).
- `spoken_languages`: text NULL — spoken languages of film directors (real vendored values).
- `deutsche_biographie_gnd_id`: integer NULL — deutsche biographie gnd id of film directors (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of film directors (real vendored values).
- `plwabn_id`: bigint NULL — plwabn id of film directors (real vendored values).
- `national_library_of_israel_j9u_id`: bigint NULL — national library of israel j9u id of film directors (real vendored values).
- `american_film_institute_person_id`: integer NULL — american film institute person id of film directors (real vendored values).
- `tmdb_person_id`: integer NULL — tmdb person id of film directors (real vendored values).
- `movie_meter_person_id`: integer NULL — movie meter person id of film directors (real vendored values).
- `george_eastman_museum_people_id`: integer NULL — george eastman museum people id of film directors (real vendored values).
- `prabook_id`: integer NULL — prabook id of film directors (real vendored values).
- `ivi_person_id`: text NULL — ivi person id of film directors (real vendored values).
- `kinobox_person_id`: integer NOT NULL — kinobox person id of film directors (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of film directors (real vendored values).
- `writing_languages`: text NULL — writing languages of film directors (real vendored values).
- business key: director_name

### gloria_swanson_cinematographers  (source backend: files)
Source table gloria_swanson_cinematographers of the Gloria_Swanson_Filmography database (20 real rows).

- `name`: text NOT NULL — name of gloria swanson cinematographers (real vendored values).
- `biography`: text NOT NULL — biography of gloria swanson cinematographers (real vendored values).
- `role`: text NOT NULL — role of gloria swanson cinematographers (real vendored values).
- `viaf_id`: integer NULL — viaf id of gloria swanson cinematographers (real vendored values).
- `international_standard_name_identifier`: text NULL — international standard name identifier of gloria swanson cinematographers (real vendored values).
- `nationality`: text NOT NULL — nationality of gloria swanson cinematographers (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of gloria swanson cinematographers (real vendored values).
- `gnd_id`: integer NULL — gnd id of gloria swanson cinematographers (real vendored values).
- `im_db_id`: text NOT NULL — im db id of gloria swanson cinematographers (real vendored values).
- `profile_image`: text NULL — profile image of gloria swanson cinematographers (real vendored values).
- `birth_date`: text NOT NULL — birth date of gloria swanson cinematographers (real vendored values).
- `death_date`: text NOT NULL — death date of gloria swanson cinematographers (real vendored values).
- `birth_place`: text NOT NULL — birth place of gloria swanson cinematographers (real vendored values).
- `death_place`: text NOT NULL — death place of gloria swanson cinematographers (real vendored values).
- `entity_type`: text NOT NULL — entity type of gloria swanson cinematographers (real vendored values).
- `freebase_id`: text NOT NULL — freebase id of gloria swanson cinematographers (real vendored values).
- `id_ref_id`: text NULL — id ref id of gloria swanson cinematographers (real vendored values).
- `nl_cr_aut_id`: text NULL — nl cr aut id of gloria swanson cinematographers (real vendored values).
- `national_library_of_spain_id`: text NOT NULL — national library of spain id of gloria swanson cinematographers (real vendored values).
- `first_name`: text NOT NULL — first name of gloria swanson cinematographers (real vendored values).
- `find_a_grave_memorial_id`: integer NULL — find a grave memorial id of gloria swanson cinematographers (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of gloria swanson cinematographers (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of gloria swanson cinematographers (real vendored values).
- `swedish_film_database_person_id`: integer NOT NULL — swedish film database person id of gloria swanson cinematographers (real vendored values).
- `port_person_id`: integer NOT NULL — port person id of gloria swanson cinematographers (real vendored values).
- `filmportal_id`: text NULL — filmportal id of gloria swanson cinematographers (real vendored values).
- `kinopoisk_person_id`: integer NOT NULL — kinopoisk person id of gloria swanson cinematographers (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of gloria swanson cinematographers (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of gloria swanson cinematographers (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of gloria swanson cinematographers (real vendored values).
- `commons_category`: text NULL — commons category of gloria swanson cinematographers (real vendored values).
- `burial_place`: text NULL — burial place of gloria swanson cinematographers (real vendored values).
- `surname`: text NOT NULL — surname of gloria swanson cinematographers (real vendored values).
- `open_media_database_person_id`: integer NULL — open media database person id of gloria swanson cinematographers (real vendored values).
- `languages`: text NULL — languages of gloria swanson cinematographers (real vendored values).
- `gender`: text NOT NULL — gender of gloria swanson cinematographers (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of gloria swanson cinematographers (real vendored values).
- `deutsche_biographie_gnd_id`: integer NULL — deutsche biographie gnd id of gloria swanson cinematographers (real vendored values).
- `tmdb_person_id`: integer NOT NULL — tmdb person id of gloria swanson cinematographers (real vendored values).
- `m_ymovies_person_id`: integer NULL — m ymovies person id of gloria swanson cinematographers (real vendored values).
- `national_library_of_israel_j9u_id`: bigint NULL — national library of israel j9u id of gloria swanson cinematographers (real vendored values).
- `movie_walker_press_person_id`: integer NULL — movie walker press person id of gloria swanson cinematographers (real vendored values).
- `ivi_person_id`: text NULL — ivi person id of gloria swanson cinematographers (real vendored values).
- `kinobox_person_id`: integer NOT NULL — kinobox person id of gloria swanson cinematographers (real vendored values).
- `prabook_id`: integer NULL — prabook id of gloria swanson cinematographers (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of gloria swanson cinematographers (real vendored values).
- `award_nominations`: text NULL — award nominations of gloria swanson cinematographers (real vendored values).
- `europeana_entity_id`: text NULL — europeana entity id of gloria swanson cinematographers (real vendored values).
- `academy_awards_database_nominee_id`: integer NULL — academy awards database nominee id of gloria swanson cinematographers (real vendored values).
- `american_film_institute_person_id`: integer NULL — american film institute person id of gloria swanson cinematographers (real vendored values).
- business key: name

### film_industry_figures  (source backend: files)
Source table film_industry_figures of the Gloria_Swanson_Filmography database (20 real rows).

- `full_name`: text NOT NULL — full name of film industry figures (real vendored values).
- `biography`: text NOT NULL — biography of film industry figures (real vendored values).
- `viaf_id`: integer NOT NULL — viaf id of film industry figures (real vendored values).
- `isni_identifier`: text NULL — isni identifier of film industry figures (real vendored values).
- `commons_category`: text NULL — commons category of film industry figures (real vendored values).
- `library_of_congress_authority_id`: text NOT NULL — library of congress authority id of film industry figures (real vendored values).
- `gnd_id`: text NULL — gnd id of film industry figures (real vendored values).
- `im_db_id`: text NOT NULL — im db id of film industry figures (real vendored values).
- `id_ref_id`: text NULL — id ref id of film industry figures (real vendored values).
- `birthplace`: text NOT NULL — birthplace of film industry figures (real vendored values).
- `deathplace`: text NOT NULL — deathplace of film industry figures (real vendored values).
- `citizenship`: text NOT NULL — citizenship of film industry figures (real vendored values).
- `birth_date`: text NOT NULL — birth date of film industry figures (real vendored values).
- `death_date`: text NOT NULL — death date of film industry figures (real vendored values).
- `entity_type`: text NOT NULL — entity type of film industry figures (real vendored values).
- `freebase_id`: text NOT NULL — freebase id of film industry figures (real vendored values).
- `awards`: text NULL — awards of film industry figures (real vendored values).
- `first_name`: text NOT NULL — first name of film industry figures (real vendored values).
- `profession`: text NOT NULL — profession of film industry figures (real vendored values).
- `nationale_thesaurus_voor_auteursnamen_id`: text NULL — nationale thesaurus voor auteursnamen id of film industry figures (real vendored values).
- `nla_trove_people_id`: integer NULL — nla trove people id of film industry figures (real vendored values).
- `fast_id`: integer NULL — fast id of film industry figures (real vendored values).
- `national_library_of_spain_id`: text NULL — national library of spain id of film industry figures (real vendored values).
- `elonet_person_id`: integer NULL — elonet person id of film industry figures (real vendored values).
- `allo_cine_person_id`: integer NULL — allo cine person id of film industry figures (real vendored values).
- `all_movie_person_id`: text NULL — all movie person id of film industry figures (real vendored values).
- `swedish_film_database_person_id`: integer NULL — swedish film database person id of film industry figures (real vendored values).
- `port_person_id`: integer NULL — port person id of film industry figures (real vendored values).
- `danish_national_filmography_person_id`: integer NULL — danish national filmography person id of film industry figures (real vendored values).
- `profile_image`: text NULL — profile image of film industry figures (real vendored values).
- `surname`: text NULL — surname of film industry figures (real vendored values).
- `bibliotheque_nationale_de_france_id`: text NULL — bibliotheque nationale de france id of film industry figures (real vendored values).
- `gender`: text NOT NULL — gender of film industry figures (real vendored values).
- `nl_cr_aut_id`: text NULL — nl cr aut id of film industry figures (real vendored values).
- `deutsche_biographie_gnd_id`: integer NULL — deutsche biographie gnd id of film industry figures (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of film industry figures (real vendored values).
- `native_name`: text NULL — native name of film industry figures (real vendored values).
- `nukat_id`: text NULL — nukat id of film industry figures (real vendored values).
- `cinematheque_quebecoise_person_id`: integer NULL — cinematheque quebecoise person id of film industry figures (real vendored values).
- `languages`: text NULL — languages of film industry figures (real vendored values).
- `tmdb_person_id`: integer NOT NULL — tmdb person id of film industry figures (real vendored values).
- `open_library_id`: text NULL — open library id of film industry figures (real vendored values).
- `national_library_of_israel_j9u_id`: bigint NULL — national library of israel j9u id of film industry figures (real vendored values).
- `m_ymovies_person_id`: integer NULL — m ymovies person id of film industry figures (real vendored values).
- `fandango_person_id`: integer NULL — fandango person id of film industry figures (real vendored values).
- `filmru_person_id`: text NULL — filmru person id of film industry figures (real vendored values).
- `acmi_id`: text NULL — acmi id of film industry figures (real vendored values).
- `kinobox_person_id`: integer NOT NULL — kinobox person id of film industry figures (real vendored values).
- `find_a_grave_memorial_id`: integer NULL — find a grave memorial id of film industry figures (real vendored values).
- `burial_site`: text NULL — burial site of film industry figures (real vendored values).
- `partner`: text NULL — partner of film industry figures (real vendored values).
- `csfd_person_id`: integer NULL — csfd person id of film industry figures (real vendored values).
- `kinopoisk_person_id`: integer NULL — kinopoisk person id of film industry figures (real vendored values).
- `snac_ark_id`: text NULL — snac ark id of film industry figures (real vendored values).
- `encyclopdia_britannica_online_id`: text NULL — encyclopdia britannica online id of film industry figures (real vendored values).
- `american_national_biography_id`: text NULL — american national biography id of film industry figures (real vendored values).
- `american_film_institute_person_id`: integer NULL — american film institute person id of film industry figures (real vendored values).
- `death_cause`: text NULL — death cause of film industry figures (real vendored values).
- `career_start_year`: text NULL — career start year of film industry figures (real vendored values).
- `genicom_profile_id`: bigint NULL — genicom profile id of film industry figures (real vendored values).
- `plwabn_id`: bigint NULL — plwabn id of film industry figures (real vendored values).
- `wiki_tree_person_id`: text NULL — wiki tree person id of film industry figures (real vendored values).
- `prabook_id`: integer NULL — prabook id of film industry figures (real vendored values).
- business key: full_name

### film_studios  (source backend: rest)
Source table film_studios of the Gloria_Swanson_Filmography database (11 real rows).

- `studio_name`: text NOT NULL — studio name of film studios (real vendored values).
- `studio_description`: text NOT NULL — studio description of film studios (real vendored values).
- `im_db_id`: text NULL — im db id of film studios (real vendored values).
- `studio_type`: text NOT NULL — studio type of film studios (real vendored values).
- `freebase_id`: text NULL — freebase id of film studios (real vendored values).
- `viaf_id`: text NULL — viaf id of film studios (real vendored values).
- `country_of_origin`: text NOT NULL — country of origin of film studios (real vendored values).
- `commons_category`: text NULL — commons category of film studios (real vendored values).
- `headquarters_location`: text NULL — headquarters location of film studios (real vendored values).
- `logo_image`: text NULL — logo image of film studios (real vendored values).
- `dissolution_date`: text NULL — dissolution date of film studios (real vendored values).
- `founding_date`: text NOT NULL — founding date of film studios (real vendored values).
- `founder`: text NULL — founder of film studios (real vendored values).
- `studio_image`: text NULL — studio image of film studios (real vendored values).
- `library_of_congress_authority_id`: text NULL — library of congress authority id of film studios (real vendored values).
- `world_cat_identities_id_superseded`: text NULL — world cat identities id superseded of film studios (real vendored values).
- `main_category`: text NULL — main category of film studios (real vendored values).
- `id_ref_id`: text NULL — id ref id of film studios (real vendored values).
- `tmdb_company_id`: integer NULL — tmdb company id of film studios (real vendored values).
- `eidr_party_id`: text NULL — eidr party id of film studios (real vendored values).
- `box_office_mojo_studio_id`: text NULL — box office mojo studio id of film studios (real vendored values).
- `formation_location`: text NULL — formation location of film studios (real vendored values).
- `industry_type`: text NULL — industry type of film studios (real vendored values).
- `encyclopdia_britannica_online_id`: text NULL — encyclopdia britannica online id of film studios (real vendored values).
- `isni_code`: text NULL — isni code of film studios (real vendored values).
- `distinct_from`: text NULL — distinct from of film studios (real vendored values).
- `legal_structure`: text NULL — legal structure of film studios (real vendored values).
- `national_library_of_israel_j9u_id`: bigint NULL — national library of israel j9u id of film studios (real vendored values).
- business key: studio_name

### surname_metadata  (source backend: s3)
Source table surname_metadata of the Gloria_Swanson_Filmography database (117 real rows).

- `surname`: text NOT NULL — surname of surname metadata (real vendored values).
- `surname_description`: text NOT NULL — surname description of surname metadata (real vendored values).
- `entity_type`: text NOT NULL — entity type of surname metadata (real vendored values).
- `distinct_from`: text NULL — distinct from of surname metadata (real vendored values).
- `writing_system`: text NOT NULL — writing system of surname metadata (real vendored values).
- `commons_category`: text NULL — commons category of surname metadata (real vendored values).
- `native_surname`: text NOT NULL — native surname of surname metadata (real vendored values).
- `soundex_code`: text NULL — soundex code of surname metadata (real vendored values).
- `caverphone_code`: text NULL — caverphone code of surname metadata (real vendored values).
- `wolfram_entity_code`: text NULL — wolfram entity code of surname metadata (real vendored values).
- `geopatronyme_id`: text NULL — geopatronyme id of surname metadata (real vendored values).
- `geneanet_family_name_id`: text NULL — geneanet family name id of surname metadata (real vendored values).
- `cologne_phonetics_code`: text NULL — cologne phonetics code of surname metadata (real vendored values).
- `language_of_origin`: text NULL — language of origin of surname metadata (real vendored values).

### Relationships

- film_appearances(cinematographer) -> gloria_swanson_cinematographers(name) [optional (may be NULL/dangling)]
- film_appearances(co_star) -> filmography_cast_members(full_name) [required]
- film_appearances(director_name) -> film_directors(director_name) [optional (may be NULL/dangling)]
- film_appearances(producer_name) -> film_industry_figures(full_name) [optional (may be NULL/dangling)]
- film_appearances(production_company) -> film_studios(studio_name) [optional (may be NULL/dangling)]

