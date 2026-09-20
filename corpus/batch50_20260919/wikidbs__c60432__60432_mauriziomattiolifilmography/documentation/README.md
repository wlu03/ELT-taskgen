# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Mauriziomattiolifilmography

## Specification

PROJECT OVERVIEW

This project builds one analytical mart from the MaurizioMattioliFilmography database. Seven source tables feed it, and each must be extracted from its own backend.

Source table filmographydetails must be extracted from the mongodb backend. Source table filmographycastmembers must be extracted from the rest backend; its business key is actorname. Source table filmdirectors must be extracted from the mongodb backend; its business key is directorname. Source table filmindustryprofessionals must be extracted from the mongodb backend; its business key is fullname. Source table italianfilmeditors must be extracted from the postgres backend; its business key is fullname. Source table italianfilmproducers must be extracted from the s3 backend; its business key is producername. Source table italianartists must be extracted from the rest backend; its business key is artistname.

RELATIONSHIPS BETWEEN THE SOURCE TABLES

Child table filmographydetails with key castmember refers to parent table filmographycastmembers with key actorname, and this relationship is required.

Child table filmographydetails with key filmcomposer refers to parent table italianartists with key artistname, and this relationship is optional (the value may be NULL or dangling).

Child table filmographydetails with key filmdirector refers to parent table filmdirectors with key directorname, and this relationship is optional (the value may be NULL or dangling).

Child table filmographydetails with key filmeditor refers to parent table italianfilmeditors with key fullname, and this relationship is optional (the value may be NULL or dangling).

Child table filmographydetails with key filmproducer refers to parent table italianfilmproducers with key producername, and this relationship is optional (the value may be NULL or dangling).

Child table filmographydetails with key filmscreenwriter refers to parent table filmindustryprofessionals with key fullname, and this relationship is optional (the value may be NULL or dangling).

MART italianfilmproducers_filmographydetails_distribution — Per-(italianfilmproducers, measure state) distribution of linked filmographydetails rows in the MaurizioMattioliFilmography database.

Grain: one row per (producername, measure state) pair represented by linked filmographydetails rows, plus one absent no-activity row for a italianfilmproducers row with no links. Because filmduration is required, no linked filmographydetails row belongs to the absent state.

The key columns of this mart are entity_key and measure_state; together they identify one output row.

Output columns.
- entity_key (text): identifier of the italianfilmproducers row.
- measure_state (text): 'present' for a linked filmographydetails row; 'absent' only for a italianfilmproducers row with no linked filmographydetails row. filmduration is required on every real filmographydetails row.
- entity_name (text): biography of the italianfilmproducers row, copied unchanged.
- row_count (bigint): number of linked filmographydetails rows in this entity/state cell; 0 for a no-activity absent cell.
- distinct_amount_count (bigint): number of unique filmduration values in this cell; each unique value is counted once, however many rows repeat it; 0 for a no-activity absent cell.
- total_amount (integer): total of filmduration in this cell; 0 for a no-activity absent cell.
- max_amount (integer): largest filmduration in this cell; 0 for a no-activity absent cell.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules that build the mart.

Rule 1. Read source table italianfilmproducers.

Rule 2. Read source table filmographydetails.

Rule 3. From source table italianfilmproducers, each producername is carried into the measure-state calculation as entity_key and its biography is carried as entity_name.

Rule 4. The linked filmographydetails rows are brought into each italianfilmproducers entity, matching on the filmographydetails column filmproducer against the entity_key taken from producername; preservation is left-sided, so an entity with no linked row is retained with entity_key, entity_name, producername and filmproducer carried, and its absent state stays visible.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are the ones backed by a real filmographydetails row; filmduration is required on every such row.

Rule 6. There is one row per italianfilmproducers entity that has at least one row in the present measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count, distinct_amount_count as how many different filmduration values occur (each different value counted once, however many rows repeat it), total_amount as the total filmduration, and max_amount as the largest filmduration.

Rule 7. For those present-state rows carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state as the present measure state, the value 'present'.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are the retained placeholders for a italianfilmproducers row with no filmographydetails rows; no real row can enter this state because filmduration is required.

Rule 10. There is one row per italianfilmproducers entity with no linked filmographydetails row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked filmographydetails row, reporting entity_key, entity_name, a row_count of 0, 0 different filmduration values as distinct_amount_count, a total filmduration of 0 as total_amount and a largest filmduration of 0 as max_amount.

Rule 11. For those absent-state rows carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state as the absent measure state, the value 'absent'.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: all rows are combined, every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. Deterministic output order: rows appear in ascending entity_key order, then in ascending measure_state order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `italianfilmproducers_filmographydetails_distribution`

- Grain: One row per (producername, measure state) pair represented by linked filmographydetails rows, plus one absent no-activity row for a italianfilmproducers row with no links. Because filmduration is required, no linked filmographydetails row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'italianfilmproducers_filmographydetails_distribution' has 14 declared semantic rules:
1. [source] Read source table italianfilmproducers. (public source tables: italianfilmproducers)
2. [source] Read source table filmographydetails. (public source tables: filmographydetails)
3. [derive] Carry each producername and its biography into the measure-state calculation. (public source tables: italianfilmproducers | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked filmographydetails rows into each italianfilmproducers entity; retain an entity with no linked row so its absent state is visible. (public source tables: filmographydetails | public carried/output columns: entity_key, entity_name, producername, filmproducer | join preservation: left | condition public identifiers: filmographydetails, filmproducer, entity_key)
5. [filter] Keep the present measure-state rows: a real filmographydetails row; filmduration is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per italianfilmproducers entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different filmduration values occur (each different value counted once, however many rows repeat it), total filmduration, and largest filmduration. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a italianfilmproducers row with no filmographydetails rows; no real row can enter this state because filmduration is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per italianfilmproducers entity with no linked filmographydetails row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked filmographydetails row, reporting a row count of 0, 0 different filmduration values, a total filmduration of 0 and a largest filmduration of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### filmographydetails  (source backend: mongodb)
Source table filmographydetails of the MaurizioMattioliFilmography database (51 real rows).

- `filmtitle`: text NOT NULL — filmtitle of filmographydetails (real vendored values).
- `filmdescription`: text NOT NULL — filmdescription of filmographydetails (real vendored values).
- `imdbid`: text NOT NULL — imdbid of filmographydetails (real vendored values).
- `mediatype`: text NOT NULL — mediatype of filmographydetails (real vendored values).
- `filmdirector`: text NULL — filmdirector of filmographydetails (real vendored values).
- `filmscreenwriter`: text NULL — filmscreenwriter of filmographydetails (real vendored values).
- `filmproducer`: text NULL — filmproducer of filmographydetails (real vendored values).
- `castmember`: text NOT NULL — castmember of filmographydetails (real vendored values).
- `releasedate`: text NOT NULL — releasedate of filmographydetails (real vendored values).
- `originallanguage`: text NULL — originallanguage of filmographydetails (real vendored values).
- `countryoforigin`: text NOT NULL — countryoforigin of filmographydetails (real vendored values).
- `originaltitle`: text NULL — originaltitle of filmographydetails (real vendored values).
- `filmeditor`: text NULL — filmeditor of filmographydetails (real vendored values).
- `freebaseid`: text NULL — freebaseid of filmographydetails (real vendored values).
- `filmgenre`: text NOT NULL — filmgenre of filmographydetails (real vendored values).
- `ofdbfilmid`: integer NULL — ofdbfilmid of filmographydetails (real vendored values).
- `filmcolor`: text NULL — filmcolor of filmographydetails (real vendored values).
- `csfdfilmid`: integer NULL — csfdfilmid of filmographydetails (real vendored values).
- `elfilmfilmid`: integer NULL — elfilmfilmid of filmographydetails (real vendored values).
- `kinopoiskfilmid`: integer NULL — kinopoiskfilmid of filmographydetails (real vendored values).
- `filmaffinityfilmid`: integer NULL — filmaffinityfilmid of filmographydetails (real vendored values).
- `filmduration`: integer NOT NULL — filmduration of filmographydetails (real vendored values).
- `filmlocation`: text NULL — filmlocation of filmographydetails (real vendored values).
- `letterboxdfilmid`: text NOT NULL — letterboxdfilmid of filmographydetails (real vendored values).
- `doubanfilmid`: integer NULL — doubanfilmid of filmographydetails (real vendored values).
- `tmdbmovieid`: integer NOT NULL — tmdbmovieid of filmographydetails (real vendored values).
- `lumierefilmid`: integer NULL — lumierefilmid of filmographydetails (real vendored values).
- `trakttvid`: text NULL — trakttvid of filmographydetails (real vendored values).
- `mediakey`: text NULL — mediakey of filmographydetails (real vendored values).
- `kinoboxfilmid`: integer NULL — kinoboxfilmid of filmographydetails (real vendored values).
- `eidrcontentid`: text NULL — eidrcontentid of filmographydetails (real vendored values).
- `filmcomposer`: text NULL — filmcomposer of filmographydetails (real vendored values).
- `thetvdbmovieid`: integer NULL — thetvdbmovieid of filmographydetails (real vendored values).

### filmographycastmembers  (source backend: rest)
Source table filmographycastmembers of the MaurizioMattioliFilmography database (39 real rows).

- `actorname`: text NOT NULL — actorname of filmographycastmembers (real vendored values).
- `biography`: text NOT NULL — biography of filmographycastmembers (real vendored values).
- `gender`: text NOT NULL — gender of filmographycastmembers (real vendored values).
- `profession`: text NOT NULL — profession of filmographycastmembers (real vendored values).
- `imdbid`: text NOT NULL — imdbid of filmographycastmembers (real vendored values).
- `gndid`: integer NULL — gndid of filmographycastmembers (real vendored values).
- `birthdate`: text NOT NULL — birthdate of filmographycastmembers (real vendored values).
- `entitytype`: text NOT NULL — entitytype of filmographycastmembers (real vendored values).
- `birthplace`: text NOT NULL — birthplace of filmographycastmembers (real vendored values).
- `firstname`: text NOT NULL — firstname of filmographycastmembers (real vendored values).
- `nationality`: text NOT NULL — nationality of filmographycastmembers (real vendored values).
- `commonscategory`: text NULL — commonscategory of filmographycastmembers (real vendored values).
- `discogsartistid`: integer NULL — discogsartistid of filmographycastmembers (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of filmographycastmembers (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of filmographycastmembers (real vendored values).
- `portpersonid`: integer NULL — portpersonid of filmographycastmembers (real vendored values).
- `sbnauthorid`: text NULL — sbnauthorid of filmographycastmembers (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of filmographycastmembers (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of filmographycastmembers (real vendored values).
- `languages`: text NOT NULL — languages of filmographycastmembers (real vendored values).
- `profileimage`: text NULL — profileimage of filmographycastmembers (real vendored values).
- `isnicode`: text NULL — isnicode of filmographycastmembers (real vendored values).
- `viafid`: integer NULL — viafid of filmographycastmembers (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of filmographycastmembers (real vendored values).
- `careerstart`: text NULL — careerstart of filmographycastmembers (real vendored values).
- `freebaseid`: text NULL — freebaseid of filmographycastmembers (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of filmographycastmembers (real vendored values).
- `cinematografoitnameorcompanyid`: integer NULL — cinematografoitnameorcompanyid of filmographycastmembers (real vendored values).
- `filmtvitpersonid`: integer NULL — filmtvitpersonid of filmographycastmembers (real vendored values).
- `movieplayerpersonid`: integer NULL — movieplayerpersonid of filmographycastmembers (real vendored values).
- `mymoviespersonid`: integer NULL — mymoviespersonid of filmographycastmembers (real vendored values).
- `prabookid`: integer NULL — prabookid of filmographycastmembers (real vendored values).
- `cinemagiapersonid`: integer NULL — cinemagiapersonid of filmographycastmembers (real vendored values).
- `cinematografoitnameorcompanyidnew`: text NULL — cinematografoitnameorcompanyidnew of filmographycastmembers (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of filmographycastmembers (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of filmographycastmembers (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of filmographycastmembers (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of filmographycastmembers (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of filmographycastmembers (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of filmographycastmembers (real vendored values).
- `nativename`: text NULL — nativename of filmographycastmembers (real vendored values).
- `mymoviesactoridformerscheme`: integer NULL — mymoviesactoridformerscheme of filmographycastmembers (real vendored values).
- `surname`: text NULL — surname of filmographycastmembers (real vendored values).
- `idrefid`: text NULL — idrefid of filmographycastmembers (real vendored values).
- `rottentomatoesid`: text NULL — rottentomatoesid of filmographycastmembers (real vendored values).
- `ivipersonid`: text NULL — ivipersonid of filmographycastmembers (real vendored values).
- business key: actorname

### filmdirectors  (source backend: mongodb)
Source table filmdirectors of the MaurizioMattioliFilmography database (28 real rows).

- `directorname`: text NOT NULL — directorname of filmdirectors (real vendored values).
- `biography`: text NULL — biography of filmdirectors (real vendored values).
- `profession`: text NOT NULL — profession of filmdirectors (real vendored values).
- `viafid`: text NULL — viafid of filmdirectors (real vendored values).
- `isniidentifier`: text NULL — isniidentifier of filmdirectors (real vendored values).
- `nationality`: text NULL — nationality of filmdirectors (real vendored values).
- `commonscategory`: text NULL — commonscategory of filmdirectors (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of filmdirectors (real vendored values).
- `gndid`: text NULL — gndid of filmdirectors (real vendored values).
- `imdbid`: text NULL — imdbid of filmdirectors (real vendored values).
- `birthdate`: text NULL — birthdate of filmdirectors (real vendored values).
- `entitytype`: text NOT NULL — entitytype of filmdirectors (real vendored values).
- `sbnauthorid`: text NULL — sbnauthorid of filmdirectors (real vendored values).
- `freebaseid`: text NULL — freebaseid of filmdirectors (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of filmdirectors (real vendored values).
- `firstname`: text NOT NULL — firstname of filmdirectors (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of filmdirectors (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of filmdirectors (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of filmdirectors (real vendored values).
- `portpersonid`: integer NULL — portpersonid of filmdirectors (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of filmdirectors (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of filmdirectors (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of filmdirectors (real vendored values).
- `openmlolauthorid`: integer NULL — openmlolauthorid of filmdirectors (real vendored values).
- `languages`: text NULL — languages of filmdirectors (real vendored values).
- `gender`: text NOT NULL — gender of filmdirectors (real vendored values).
- `relatedcategory`: text NULL — relatedcategory of filmdirectors (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of filmdirectors (real vendored values).
- `deutschebiographiegndid`: text NULL — deutschebiographiegndid of filmdirectors (real vendored values).
- `surname`: text NULL — surname of filmdirectors (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of filmdirectors (real vendored values).
- `mymoviespersonid`: integer NULL — mymoviespersonid of filmdirectors (real vendored values).
- `moviemeterpersonid`: integer NULL — moviemeterpersonid of filmdirectors (real vendored values).
- `birthplace`: text NULL — birthplace of filmdirectors (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of filmdirectors (real vendored values).
- business key: directorname

### filmindustryprofessionals  (source backend: mongodb)
Source table filmindustryprofessionals of the MaurizioMattioliFilmography database (28 real rows).

- `fullname`: text NOT NULL — fullname of filmindustryprofessionals (real vendored values).
- `biography`: text NOT NULL — biography of filmindustryprofessionals (real vendored values).
- `gender`: text NOT NULL — gender of filmindustryprofessionals (real vendored values).
- `primaryrole`: text NOT NULL — primaryrole of filmindustryprofessionals (real vendored values).
- `imdbid`: text NULL — imdbid of filmindustryprofessionals (real vendored values).
- `gndid`: integer NULL — gndid of filmindustryprofessionals (real vendored values).
- `birthdate`: text NOT NULL — birthdate of filmindustryprofessionals (real vendored values).
- `entitytype`: text NOT NULL — entitytype of filmindustryprofessionals (real vendored values).
- `birthplace`: text NOT NULL — birthplace of filmindustryprofessionals (real vendored values).
- `firstname`: text NOT NULL — firstname of filmindustryprofessionals (real vendored values).
- `nationality`: text NOT NULL — nationality of filmindustryprofessionals (real vendored values).
- `commonscategory`: text NULL — commonscategory of filmindustryprofessionals (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of filmindustryprofessionals (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of filmindustryprofessionals (real vendored values).
- `portpersonid`: integer NULL — portpersonid of filmindustryprofessionals (real vendored values).
- `sbnauthorid`: text NULL — sbnauthorid of filmindustryprofessionals (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of filmindustryprofessionals (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of filmindustryprofessionals (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of filmindustryprofessionals (real vendored values).
- `languages`: text NOT NULL — languages of filmindustryprofessionals (real vendored values).
- `profileimage`: text NULL — profileimage of filmindustryprofessionals (real vendored values).
- `isniidentifier`: text NULL — isniidentifier of filmindustryprofessionals (real vendored values).
- `viafid`: integer NULL — viafid of filmindustryprofessionals (real vendored values).
- `relatedworkcategory`: text NULL — relatedworkcategory of filmindustryprofessionals (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of filmindustryprofessionals (real vendored values).
- `freebaseid`: text NOT NULL — freebaseid of filmindustryprofessionals (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of filmindustryprofessionals (real vendored values).
- `moviemeterpersonid`: integer NULL — moviemeterpersonid of filmindustryprofessionals (real vendored values).
- `mymoviespersonid`: integer NULL — mymoviespersonid of filmindustryprofessionals (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of filmindustryprofessionals (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of filmindustryprofessionals (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of filmindustryprofessionals (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of filmindustryprofessionals (real vendored values).
- `surname`: text NULL — surname of filmindustryprofessionals (real vendored values).
- `ivipersonid`: text NULL — ivipersonid of filmindustryprofessionals (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of filmindustryprofessionals (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of filmindustryprofessionals (real vendored values).
- `idrefid`: text NULL — idrefid of filmindustryprofessionals (real vendored values).
- business key: fullname

### italianfilmeditors  (source backend: postgres)
Source table italianfilmeditors of the MaurizioMattioliFilmography database (13 real rows).

- `fullname`: text NOT NULL — fullname of italianfilmeditors (real vendored values).
- `professiondescription`: text NOT NULL — professiondescription of italianfilmeditors (real vendored values).
- `gender`: text NOT NULL — gender of italianfilmeditors (real vendored values).
- `birthcity`: text NULL — birthcity of italianfilmeditors (real vendored values).
- `viafid`: text NULL — viafid of italianfilmeditors (real vendored values).
- `citizenshipcountry`: text NULL — citizenshipcountry of italianfilmeditors (real vendored values).
- `imdbid`: text NOT NULL — imdbid of italianfilmeditors (real vendored values).
- `birthdate`: text NULL — birthdate of italianfilmeditors (real vendored values).
- `deathdate`: text NULL — deathdate of italianfilmeditors (real vendored values).
- `entitytype`: text NOT NULL — entitytype of italianfilmeditors (real vendored values).
- `freebaseid`: text NULL — freebaseid of italianfilmeditors (real vendored values).
- `firstname`: text NOT NULL — firstname of italianfilmeditors (real vendored values).
- `profession`: text NOT NULL — profession of italianfilmeditors (real vendored values).
- `gndid`: integer NULL — gndid of italianfilmeditors (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of italianfilmeditors (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of italianfilmeditors (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of italianfilmeditors (real vendored values).
- `portpersonid`: integer NULL — portpersonid of italianfilmeditors (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of italianfilmeditors (real vendored values).
- `scopedkpersonid`: integer NULL — scopedkpersonid of italianfilmeditors (real vendored values).
- `danishnationalfilmographypersonid`: integer NULL — danishnationalfilmographypersonid of italianfilmeditors (real vendored values).
- `filmportalid`: text NULL — filmportalid of italianfilmeditors (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of italianfilmeditors (real vendored values).
- `languageproficiency`: text NULL — languageproficiency of italianfilmeditors (real vendored values).
- `lastname`: text NULL — lastname of italianfilmeditors (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of italianfilmeditors (real vendored values).
- `deutschebiographiegndid`: integer NULL — deutschebiographiegndid of italianfilmeditors (real vendored values).
- `cinemathequequebecoisepersonid`: integer NULL — cinemathequequebecoisepersonid of italianfilmeditors (real vendored values).
- `tmdbpersonid`: integer NOT NULL — tmdbpersonid of italianfilmeditors (real vendored values).
- `ivipersonid`: text NULL — ivipersonid of italianfilmeditors (real vendored values).
- `kinoboxpersonid`: integer NOT NULL — kinoboxpersonid of italianfilmeditors (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of italianfilmeditors (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of italianfilmeditors (real vendored values).
- business key: fullname

### italianfilmproducers  (source backend: s3)
Source table italianfilmproducers of the MaurizioMattioliFilmography database (13 real rows).

- `producername`: text NOT NULL — producername of italianfilmproducers (real vendored values).
- `biography`: text NULL — biography of italianfilmproducers (real vendored values).
- `gender`: text NOT NULL — gender of italianfilmproducers (real vendored values).
- `nationality`: text NULL — nationality of italianfilmproducers (real vendored values).
- `birthplace`: text NOT NULL — birthplace of italianfilmproducers (real vendored values).
- `birthdate`: text NULL — birthdate of italianfilmproducers (real vendored values).
- `entitytype`: text NOT NULL — entitytype of italianfilmproducers (real vendored values).
- `freebaseid`: text NULL — freebaseid of italianfilmproducers (real vendored values).
- `profession`: text NOT NULL — profession of italianfilmproducers (real vendored values).
- `firstname`: text NOT NULL — firstname of italianfilmproducers (real vendored values).
- `languageproficiency`: text NULL — languageproficiency of italianfilmproducers (real vendored values).
- `viafid`: integer NULL — viafid of italianfilmproducers (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of italianfilmproducers (real vendored values).
- `gndid`: text NULL — gndid of italianfilmproducers (real vendored values).
- `imdbid`: text NULL — imdbid of italianfilmproducers (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of italianfilmproducers (real vendored values).
- `danishnationalfilmographypersonid`: integer NULL — danishnationalfilmographypersonid of italianfilmproducers (real vendored values).
- `surname`: text NULL — surname of italianfilmproducers (real vendored values).
- `plwabnid`: bigint NULL — plwabnid of italianfilmproducers (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of italianfilmproducers (real vendored values).
- `sbnauthorid`: text NULL — sbnauthorid of italianfilmproducers (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of italianfilmproducers (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of italianfilmproducers (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of italianfilmproducers (real vendored values).
- `mymoviespersonid`: integer NULL — mymoviespersonid of italianfilmproducers (real vendored values).
- business key: producername

### italianartists  (source backend: rest)
Source table italianartists of the MaurizioMattioliFilmography database (24 real rows).

- `artistname`: text NOT NULL — artistname of italianartists (real vendored values).
- `biography`: text NOT NULL — biography of italianartists (real vendored values).
- `imdbid`: text NULL — imdbid of italianartists (real vendored values).
- `sbnauthorid`: text NULL — sbnauthorid of italianartists (real vendored values).
- `profession`: text NOT NULL — profession of italianartists (real vendored values).
- `birthplace`: text NOT NULL — birthplace of italianartists (real vendored values).
- `gndid`: text NULL — gndid of italianartists (real vendored values).
- `viafid`: integer NULL — viafid of italianartists (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of italianartists (real vendored values).
- `idrefid`: text NULL — idrefid of italianartists (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of italianartists (real vendored values).
- `commonscategory`: text NULL — commonscategory of italianartists (real vendored values).
- `musicbrainzartistid`: text NULL — musicbrainzartistid of italianartists (real vendored values).
- `isniidentifier`: text NULL — isniidentifier of italianartists (real vendored values).
- `birthdate`: text NOT NULL — birthdate of italianartists (real vendored values).
- `profileimage`: text NULL — profileimage of italianartists (real vendored values).
- `entitytype`: text NOT NULL — entitytype of italianartists (real vendored values).
- `freebaseid`: text NOT NULL — freebaseid of italianartists (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of italianartists (real vendored values).
- `firstname`: text NOT NULL — firstname of italianartists (real vendored values).
- `discogsartistid`: integer NULL — discogsartistid of italianartists (real vendored values).
- `allmusicartistid`: text NULL — allmusicartistid of italianartists (real vendored values).
- `filmportalid`: text NULL — filmportalid of italianartists (real vendored values).
- `nationality`: text NOT NULL — nationality of italianartists (real vendored values).
- `danishnationalfilmographypersonid`: integer NULL — danishnationalfilmographypersonid of italianartists (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of italianartists (real vendored values).
- `languages`: text NULL — languages of italianartists (real vendored values).
- `muziekwebperformerid`: text NULL — muziekwebperformerid of italianartists (real vendored values).
- `gender`: text NOT NULL — gender of italianartists (real vendored values).
- `lastname`: text NULL — lastname of italianartists (real vendored values).
- `europeanaentity`: text NULL — europeanaentity of italianartists (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of italianartists (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of italianartists (real vendored values).
- `officialwebsite`: text NULL — officialwebsite of italianartists (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of italianartists (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of italianartists (real vendored values).
- business key: artistname

### Relationships

- filmographydetails(castmember) -> filmographycastmembers(actorname) [required]
- filmographydetails(filmcomposer) -> italianartists(artistname) [optional (may be NULL/dangling)]
- filmographydetails(filmdirector) -> filmdirectors(directorname) [optional (may be NULL/dangling)]
- filmographydetails(filmeditor) -> italianfilmeditors(fullname) [optional (may be NULL/dangling)]
- filmographydetails(filmproducer) -> italianfilmproducers(producername) [optional (may be NULL/dangling)]
- filmographydetails(filmscreenwriter) -> filmindustryprofessionals(fullname) [optional (may be NULL/dangling)]

