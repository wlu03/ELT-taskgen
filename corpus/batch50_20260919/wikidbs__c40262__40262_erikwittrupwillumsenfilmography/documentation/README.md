# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Erikwittrupwillumsenfilmography

## Specification

PROJECT OVERVIEW

This project builds one analytical mart from the ErikWittrupWillumsenFilmography database. Five source tables feed the work, and each must be extracted from the backend named here.

Source table filmographydetails must be extracted from the mongodb backend. It holds one row per film with filmtitle, filmdescription, imdbid, filmtype, filmdirector, directorofphotography, originallanguage, releasedate, dnffilmid, countryoforigin, filmcolor, allmovietitleid, csfdfilmid, elfilmfilmid, kinopoiskfilmid, danskefilmfilmid, cast, screenwriter, filmeditor, eidrcontentid, danskfilmogtvtitleid, filmwebplfilmid, filmgenre, agerating, letterboxdfilmid, filmduration, filmformat, tmdbmovieid, filmfrontfilmidarchived, lumierefilmid, trakttvid, kinoboxfilmid and mediakey. Every filmographydetails row carries a filmduration value; filmduration is required on every real filmographydetails row.

Source table filmindustryprofessionals must be extracted from the mongodb backend. Its business key is fullname, and it describes directors and other film-industry people.

Source table danishfilmindustrypersonnel must be extracted from the files backend. Its business key is fullname, and it describes Danish film-industry personnel, including screenwriters.

Source table filmeditor must be extracted from the rest backend. Its business key is fullname, and it describes film editors.

Source table filmographycastdetails must be extracted from the mongodb backend. Its business key is actorname, and it describes cast members, each with a biography.

RELATIONSHIPS BETWEEN THE SOURCE TABLES

The child table filmographydetails with its key cast refers to the parent table filmographycastdetails with its key actorname; this relationship is optional, so the cast value may be null or may refer to no filmographycastdetails row at all.

The child table filmographydetails with its key filmdirector refers to the parent table filmindustryprofessionals with its key fullname; this relationship is required, so every filmdirector value corresponds to a filmindustryprofessionals row.

The child table filmographydetails with its key filmeditor refers to the parent table filmeditor with its key fullname; this relationship is optional, so the filmeditor value may be null or may refer to no filmeditor row at all.

The child table filmographydetails with its key screenwriter refers to the parent table danishfilmindustrypersonnel with its key fullname; this relationship is optional, so the screenwriter value may be null or may refer to no danishfilmindustrypersonnel row at all.

MART: filmographycastdetails_filmographydetails_distribution — the per-(filmographycastdetails, measure state) distribution of linked filmographydetails rows in the ErikWittrupWillumsenFilmography database.

Grain. One row per (actorname, measure state) pair represented by linked filmographydetails rows, plus one absent no-activity row for a filmographycastdetails row with no links. Because filmduration is required, no linked filmographydetails row belongs to the absent state.

Key columns. The two key columns of this mart are entity_key and measure_state; together they identify one output row.

Output columns.

entity_key (text) is the identifier of the filmographycastdetails row.

measure_state (text) is 'present' for a linked filmographydetails row, and 'absent' only for a filmographycastdetails row with no linked filmographydetails row; filmduration is required on every real filmographydetails row.

entity_name (text) is the biography of the filmographycastdetails row, copied unchanged.

row_count (bigint) is the number of linked filmographydetails rows in this entity/state cell; it is 0 for a no-activity absent cell.

distinct_amount_count (bigint) is the number of unique filmduration values in this cell; each unique value is counted once, however many rows repeat it; it is 0 for a no-activity absent cell.

total_amount (integer) is the total of filmduration in this cell; it is 0 for a no-activity absent cell.

max_amount (integer) is the largest filmduration in this cell; it is 0 for a no-activity absent cell.

max_amount_share (float) is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

How the mart is built.

Rule 1. The source table filmographycastdetails is read in full.

Rule 2. The source table filmographydetails is read in full.

Rule 3. From filmographycastdetails, each actorname and its biography are carried into the measure-state calculation as entity_key and entity_name.

Rule 4. The linked filmographydetails rows are brought into each filmographycastdetails entity, matching a filmographydetails row to an entity when its cast value equals that entity's entity_key (the actorname); preservation is left-sided on the filmographycastdetails side, so an entity with no linked filmographydetails row is retained with a placeholder so its absent state is visible, and entity_key, entity_name, actorname and cast are carried.

Rule 5. The present measure-state rows are kept: those where there is a real filmographydetails row, filmduration being required on every such row; entity_key and entity_name are carried on these rows.

Rule 6. The present measure state reports one row per filmographycastdetails entity that has at least one row in that state, and no row here for an entity with none, reporting entity_key, entity_name, row_count as the number of those rows, distinct_amount_count as how many different filmduration values occur (each different value counted once, however many rows repeat it), total_amount as the total filmduration, and max_amount as the largest filmduration.

Rule 7. On each present-state row, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and is 0.0 when total_amount is 0; entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share are carried.

Rule 8. These measures are labelled as the present measure state, so measure_state reads 'present' on them, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows are kept: the retained placeholder for a filmographycastdetails row with no filmographydetails rows; no real row can enter this state because filmduration is required. entity_key and entity_name are carried on these rows.

Rule 10. The absent measure state reports one row per filmographycastdetails entity with no linked filmographydetails row at all, whose retained placeholder is its one row in that state, and no row here for an entity that has a linked filmographydetails row, reporting entity_key, entity_name, a row_count of 0, 0 different filmduration values as distinct_amount_count, a total filmduration of 0 as total_amount and a largest filmduration of 0 as max_amount.

Rule 11. On each absent-state row, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and is 0.0 when total_amount is 0; entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share are carried.

Rule 12. These measures are labelled as the absent measure state, so measure_state reads 'absent' on them, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list, keeping all rows of both: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other; each such row carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 14. The output order is deterministic: rows appear in ascending entity_key order, and within one entity in ascending measure_state order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `filmographycastdetails_filmographydetails_distribution`

- Grain: One row per (actorname, measure state) pair represented by linked filmographydetails rows, plus one absent no-activity row for a filmographycastdetails row with no links. Because filmduration is required, no linked filmographydetails row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'filmographycastdetails_filmographydetails_distribution' has 14 declared semantic rules:
1. [source] Read source table filmographycastdetails. (public source tables: filmographycastdetails)
2. [source] Read source table filmographydetails. (public source tables: filmographydetails)
3. [derive] Carry each actorname and its biography into the measure-state calculation. (public source tables: filmographycastdetails | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked filmographydetails rows into each filmographycastdetails entity; retain an entity with no linked row so its absent state is visible. (public source tables: filmographydetails | public carried/output columns: entity_key, entity_name, actorname, cast | join preservation: left | condition public identifiers: filmographydetails, cast, entity_key)
5. [filter] Keep the present measure-state rows: a real filmographydetails row; filmduration is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per filmographycastdetails entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different filmduration values occur (each different value counted once, however many rows repeat it), total filmduration, and largest filmduration. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a filmographycastdetails row with no filmographydetails rows; no real row can enter this state because filmduration is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per filmographycastdetails entity with no linked filmographydetails row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked filmographydetails row, reporting a row count of 0, 0 different filmduration values, a total filmduration of 0 and a largest filmduration of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### filmographydetails  (source backend: mongodb)
Source table filmographydetails of the ErikWittrupWillumsenFilmography database (25 real rows).

- `filmtitle`: text NOT NULL — filmtitle of filmographydetails (real vendored values).
- `filmdescription`: text NOT NULL — filmdescription of filmographydetails (real vendored values).
- `imdbid`: text NULL — imdbid of filmographydetails (real vendored values).
- `filmtype`: text NOT NULL — filmtype of filmographydetails (real vendored values).
- `filmdirector`: text NOT NULL — filmdirector of filmographydetails (real vendored values).
- `directorofphotography`: text NOT NULL — directorofphotography of filmographydetails (real vendored values).
- `originallanguage`: text NULL — originallanguage of filmographydetails (real vendored values).
- `releasedate`: text NOT NULL — releasedate of filmographydetails (real vendored values).
- `dnffilmid`: integer NOT NULL — dnffilmid of filmographydetails (real vendored values).
- `countryoforigin`: text NOT NULL — countryoforigin of filmographydetails (real vendored values).
- `filmcolor`: text NULL — filmcolor of filmographydetails (real vendored values).
- `allmovietitleid`: text NULL — allmovietitleid of filmographydetails (real vendored values).
- `csfdfilmid`: integer NULL — csfdfilmid of filmographydetails (real vendored values).
- `elfilmfilmid`: integer NULL — elfilmfilmid of filmographydetails (real vendored values).
- `kinopoiskfilmid`: integer NULL — kinopoiskfilmid of filmographydetails (real vendored values).
- `danskefilmfilmid`: integer NULL — danskefilmfilmid of filmographydetails (real vendored values).
- `cast`: text NULL — cast of filmographydetails (real vendored values).
- `screenwriter`: text NULL — screenwriter of filmographydetails (real vendored values).
- `filmeditor`: text NULL — filmeditor of filmographydetails (real vendored values).
- `eidrcontentid`: text NULL — eidrcontentid of filmographydetails (real vendored values).
- `danskfilmogtvtitleid`: integer NULL — danskfilmogtvtitleid of filmographydetails (real vendored values).
- `filmwebplfilmid`: integer NULL — filmwebplfilmid of filmographydetails (real vendored values).
- `filmgenre`: text NULL — filmgenre of filmographydetails (real vendored values).
- `agerating`: text NULL — agerating of filmographydetails (real vendored values).
- `letterboxdfilmid`: text NULL — letterboxdfilmid of filmographydetails (real vendored values).
- `filmduration`: integer NOT NULL — filmduration of filmographydetails (real vendored values).
- `filmformat`: text NULL — filmformat of filmographydetails (real vendored values).
- `tmdbmovieid`: integer NULL — tmdbmovieid of filmographydetails (real vendored values).
- `filmfrontfilmidarchived`: integer NULL — filmfrontfilmidarchived of filmographydetails (real vendored values).
- `lumierefilmid`: integer NULL — lumierefilmid of filmographydetails (real vendored values).
- `trakttvid`: text NULL — trakttvid of filmographydetails (real vendored values).
- `kinoboxfilmid`: integer NULL — kinoboxfilmid of filmographydetails (real vendored values).
- `mediakey`: text NULL — mediakey of filmographydetails (real vendored values).

### filmindustryprofessionals  (source backend: mongodb)
Source table filmindustryprofessionals of the ErikWittrupWillumsenFilmography database (14 real rows).

- `fullname`: text NOT NULL — fullname of filmindustryprofessionals (real vendored values).
- `biography`: text NULL — biography of filmindustryprofessionals (real vendored values).
- `birthdate`: text NULL — birthdate of filmindustryprofessionals (real vendored values).
- `deathdate`: text NULL — deathdate of filmindustryprofessionals (real vendored values).
- `entitytype`: text NOT NULL — entitytype of filmindustryprofessionals (real vendored values).
- `birthplace`: text NULL — birthplace of filmindustryprofessionals (real vendored values).
- `profession`: text NULL — profession of filmindustryprofessionals (real vendored values).
- `gender`: text NOT NULL — gender of filmindustryprofessionals (real vendored values).
- `imdbid`: text NULL — imdbid of filmindustryprofessionals (real vendored values).
- `nationality`: text NULL — nationality of filmindustryprofessionals (real vendored values).
- `firstname`: text NULL — firstname of filmindustryprofessionals (real vendored values).
- `viafid`: text NULL — viafid of filmindustryprofessionals (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of filmindustryprofessionals (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of filmindustryprofessionals (real vendored values).
- `danishnationalfilmographypersonid`: integer NOT NULL — danishnationalfilmographypersonid of filmindustryprofessionals (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of filmindustryprofessionals (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of filmindustryprofessionals (real vendored values).
- `danskefilmpersonid`: integer NULL — danskefilmpersonid of filmindustryprofessionals (real vendored values).
- `lastname`: text NOT NULL — lastname of filmindustryprofessionals (real vendored values).
- `gravsteddkid`: text NULL — gravsteddkid of filmindustryprofessionals (real vendored values).
- `danskfilmogtvpersonid`: integer NULL — danskfilmogtvpersonid of filmindustryprofessionals (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of filmindustryprofessionals (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of filmindustryprofessionals (real vendored values).
- business key: fullname

### danishfilmindustrypersonnel  (source backend: files)
Source table danishfilmindustrypersonnel of the ErikWittrupWillumsenFilmography database (14 real rows).

- `fullname`: text NOT NULL — fullname of danishfilmindustrypersonnel (real vendored values).
- `biography`: text NULL — biography of danishfilmindustrypersonnel (real vendored values).
- `birthdate`: text NULL — birthdate of danishfilmindustrypersonnel (real vendored values).
- `deathdate`: text NULL — deathdate of danishfilmindustrypersonnel (real vendored values).
- `entitytype`: text NOT NULL — entitytype of danishfilmindustrypersonnel (real vendored values).
- `birthplace`: text NULL — birthplace of danishfilmindustrypersonnel (real vendored values).
- `deathplace`: text NULL — deathplace of danishfilmindustrypersonnel (real vendored values).
- `profession`: text NULL — profession of danishfilmindustrypersonnel (real vendored values).
- `gender`: text NOT NULL — gender of danishfilmindustrypersonnel (real vendored values).
- `imdbid`: text NULL — imdbid of danishfilmindustrypersonnel (real vendored values).
- `nationality`: text NULL — nationality of danishfilmindustrypersonnel (real vendored values).
- `firstname`: text NULL — firstname of danishfilmindustrypersonnel (real vendored values).
- `viafid`: text NULL — viafid of danishfilmindustrypersonnel (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of danishfilmindustrypersonnel (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of danishfilmindustrypersonnel (real vendored values).
- `scopedkpersonid`: integer NULL — scopedkpersonid of danishfilmindustrypersonnel (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of danishfilmindustrypersonnel (real vendored values).
- `danishnationalfilmographypersonid`: integer NOT NULL — danishnationalfilmographypersonid of danishfilmindustrypersonnel (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of danishfilmindustrypersonnel (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of danishfilmindustrypersonnel (real vendored values).
- `danskefilmpersonid`: integer NULL — danskefilmpersonid of danishfilmindustrypersonnel (real vendored values).
- `lastname`: text NOT NULL — lastname of danishfilmindustrypersonnel (real vendored values).
- `gravsteddkid`: text NULL — gravsteddkid of danishfilmindustrypersonnel (real vendored values).
- `danskfilmogtvpersonid`: integer NULL — danskfilmogtvpersonid of danishfilmindustrypersonnel (real vendored values).
- `freebaseid`: text NULL — freebaseid of danishfilmindustrypersonnel (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of danishfilmindustrypersonnel (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of danishfilmindustrypersonnel (real vendored values).
- business key: fullname

### filmeditor  (source backend: rest)
Source table filmeditor of the ErikWittrupWillumsenFilmography database (16 real rows).

- `fullname`: text NOT NULL — fullname of filmeditor (real vendored values).
- `biography`: text NULL — biography of filmeditor (real vendored values).
- `birthdate`: text NULL — birthdate of filmeditor (real vendored values).
- `deathdate`: text NULL — deathdate of filmeditor (real vendored values).
- `entitytype`: text NOT NULL — entitytype of filmeditor (real vendored values).
- `nationality`: text NULL — nationality of filmeditor (real vendored values).
- `gender`: text NOT NULL — gender of filmeditor (real vendored values).
- `birthplace`: text NULL — birthplace of filmeditor (real vendored values).
- `imdbid`: text NOT NULL — imdbid of filmeditor (real vendored values).
- `profession`: text NULL — profession of filmeditor (real vendored values).
- `danishnationalfilmographypersonid`: integer NOT NULL — danishnationalfilmographypersonid of filmeditor (real vendored values).
- `danskefilmpersonid`: integer NULL — danskefilmpersonid of filmeditor (real vendored values).
- `surname`: text NULL — surname of filmeditor (real vendored values).
- `danskfilmogtvpersonid`: integer NULL — danskfilmogtvpersonid of filmeditor (real vendored values).
- `firstname`: text NULL — firstname of filmeditor (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of filmeditor (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of filmeditor (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of filmeditor (real vendored values).
- business key: fullname

### filmographycastdetails  (source backend: mongodb)
Source table filmographycastdetails of the ErikWittrupWillumsenFilmography database (14 real rows).

- `actorname`: text NOT NULL — actorname of filmographycastdetails (real vendored values).
- `biography`: text NOT NULL — biography of filmographycastdetails (real vendored values).
- `gender`: text NOT NULL — gender of filmographycastdetails (real vendored values).
- `roletype`: text NOT NULL — roletype of filmographycastdetails (real vendored values).
- `viafid`: text NULL — viafid of filmographycastdetails (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of filmographycastdetails (real vendored values).
- `imdbid`: text NOT NULL — imdbid of filmographycastdetails (real vendored values).
- `birthplace`: text NULL — birthplace of filmographycastdetails (real vendored values).
- `nationality`: text NOT NULL — nationality of filmographycastdetails (real vendored values).
- `birthdate`: text NOT NULL — birthdate of filmographycastdetails (real vendored values).
- `entitytype`: text NOT NULL — entitytype of filmographycastdetails (real vendored values).
- `freebaseid`: text NULL — freebaseid of filmographycastdetails (real vendored values).
- `firstname`: text NULL — firstname of filmographycastdetails (real vendored values).
- `languages`: text NULL — languages of filmographycastdetails (real vendored values).
- `swedishfilmdatabasepersonid`: integer NOT NULL — swedishfilmdatabasepersonid of filmographycastdetails (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of filmographycastdetails (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of filmographycastdetails (real vendored values).
- `scopedkpersonid`: integer NULL — scopedkpersonid of filmographycastdetails (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of filmographycastdetails (real vendored values).
- `danishnationalfilmographypersonid`: integer NOT NULL — danishnationalfilmographypersonid of filmographycastdetails (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of filmographycastdetails (real vendored values).
- `surname`: text NULL — surname of filmographycastdetails (real vendored values).
- `danskefilmpersonid`: integer NOT NULL — danskefilmpersonid of filmographycastdetails (real vendored values).
- `danskfilmogtvpersonid`: integer NOT NULL — danskfilmogtvpersonid of filmographycastdetails (real vendored values).
- `tmdbpersonid`: integer NOT NULL — tmdbpersonid of filmographycastdetails (real vendored values).
- `deathdate`: text NULL — deathdate of filmographycastdetails (real vendored values).
- `gyldendalsteaterleksikonid`: text NULL — gyldendalsteaterleksikonid of filmographycastdetails (real vendored values).
- `gravsteddkid`: text NULL — gravsteddkid of filmographycastdetails (real vendored values).
- `kinoboxpersonid`: integer NOT NULL — kinoboxpersonid of filmographycastdetails (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of filmographycastdetails (real vendored values).
- business key: actorname

### Relationships

- filmographydetails(cast) -> filmographycastdetails(actorname) [optional (may be NULL/dangling)]
- filmographydetails(filmdirector) -> filmindustryprofessionals(fullname) [required]
- filmographydetails(filmeditor) -> filmeditor(fullname) [optional (may be NULL/dangling)]
- filmographydetails(screenwriter) -> danishfilmindustrypersonnel(fullname) [optional (may be NULL/dangling)]

