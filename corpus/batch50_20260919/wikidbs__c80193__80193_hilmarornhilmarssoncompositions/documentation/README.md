# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Hilmarornhilmarssoncompositions

## Specification

PROJECT OVERVIEW

This project builds one analytical mart from the HilmarOrnHilmarssonCompositions database. Nine source tables feed the work, and each must be extracted from the backend named here.

The source table filmographyandsoundtracks must be extracted from the postgres backend; it holds one row per film with its filmtitle, filmdescription, imdbid, filmtype, filmdirector, leadcast, filmaffinityfilmid, language, screenwriter, filmproducer, cinematographer, productioncountry, filmgenre, releasedate, composer, filmcolor, filmduration and the various external catalogue identifiers.

The source table filmdirectors must be extracted from the s3 backend; its business key is directorname.

The source table notableindividuals must be extracted from the postgres backend; its business key is fullname, and it carries biography, profession, birthdate, nationality and related attributes.

The source table directorofphotography must be extracted from the s3 backend; its business key is name.

The source table director must be extracted from the mongodb backend; its business key is directorname.

The source table composerrelatedgivennames must be extracted from the rest backend; it lists given-name labels with their descriptions and phonetic codes.

The source table composerfamilynames must be extracted from the rest backend; it lists surnames with their descriptions and phonetic codes.

The source table filmgenres must be extracted from the mongodb backend; its business key is genrename.

The source table notableartistsandaffiliates must be extracted from the rest backend; its business key is artistname.

The source table filmdirectorcategories must be extracted from the mongodb backend; its business key is categorylabel.

RELATIONSHIPS BETWEEN SOURCES

Child table director, through its column relatedcategory, refers to parent table filmdirectorcategories, through its column categorylabel; this relationship is optional (the value may be NULL or dangling).

Child table filmographyandsoundtracks, through its column cinematographer, refers to parent table directorofphotography, through its column name; this relationship is optional (the value may be NULL or dangling).

Child table filmographyandsoundtracks, through its column filmdirector, refers to parent table director, through its column directorname; this relationship is required.

Child table filmographyandsoundtracks, through its column filmgenre, refers to parent table filmgenres, through its column genrename; this relationship is required.

Child table filmographyandsoundtracks, through its column filmproducer, refers to parent table notableindividuals, through its column fullname; this relationship is optional (the value may be NULL or dangling).

Child table filmographyandsoundtracks, through its column leadcast, refers to parent table notableartistsandaffiliates, through its column artistname; this relationship is optional (the value may be NULL or dangling).

Child table filmographyandsoundtracks, through its column screenwriter, refers to parent table filmdirectors, through its column directorname; this relationship is optional (the value may be NULL or dangling).

MART notableindividuals_filmographyandsoundtracks_distribution — Per-(notableindividuals, measure state) distribution of linked filmographyandsoundtracks rows in the HilmarOrnHilmarssonCompositions database.

Grain: one row per (fullname, measure state) pair represented among linked filmographyandsoundtracks rows; the absent state includes missing filmduration values and a no-activity row for a notableindividuals row with no links. A linked filmographyandsoundtracks row whose filmduration has a value belongs only to the present state and never to the absent state.

The key columns of this mart are entity_key and measure_state; together they identify one output row.

Output columns

entity_key (text): the identifier of the notableindividuals row.

measure_state (text): 'present' for a linked filmographyandsoundtracks row whose filmduration has a value; 'absent' when filmduration is missing, including a notableindividuals row with no linked filmographyandsoundtracks row. A linked filmographyandsoundtracks row whose filmduration has a value belongs only to the present state and never to the absent state.

entity_name (text): the biography of the notableindividuals row, copied unchanged.

row_count (bigint): the number of linked filmographyandsoundtracks rows in this entity/state cell; a absent cell holding real filmographyandsoundtracks rows whose filmduration is missing COUNTS those rows, and only the placeholder cell of a notableindividuals row with no linked filmographyandsoundtracks row at all reports 0.

distinct_amount_count (bigint): the number of unique non-missing filmduration values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no filmduration value at all — both for a notableindividuals row with no linked filmographyandsoundtracks row and for an absent cell whose rows all have a missing filmduration.

total_amount (integer): the total of filmduration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an filmduration value.

max_amount (integer): the largest filmduration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an filmduration value.

max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

How the mart is built

Rule 1. The source table notableindividuals is read in full, and every one of its rows is available to the work below.

Rule 2. The source table filmographyandsoundtracks is read in full, and every one of its rows is available to the work below.

Rule 3. From notableindividuals, each fullname and its biography are carried into the measure-state calculation as entity_key and entity_name respectively.

Rule 4. The filmographyandsoundtracks rows that match an entity are brought into each notableindividuals entity, matching on filmographyandsoundtracks column filmproducer against entity_key, which is fullname; preservation is left-sided from the notableindividuals side, so an entity with no matching filmographyandsoundtracks row is retained as a placeholder carrying entity_key, entity_name, fullname and filmproducer, making its absent state visible.

Rule 5. The present measure-state rows are kept, carrying entity_key and entity_name: a row is in the present measure state when it is a real filmographyandsoundtracks row whose filmduration has a value.

Rule 6. For the present measure state there is one row per notableindividuals entity that has at least one row in the present measure state, and no row here for an entity with none, reporting per entity_key and entity_name the row count as row_count, how many different non-missing filmduration values occur (each different value counted once, however many rows repeat it) as distinct_amount_count, the total filmduration as total_amount, and the largest filmduration as max_amount.

Rule 7. For each of those present-state summaries, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, the value max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures are labelled as the present measure state: each such row carries entity_key, measure_state holding the value present, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows are kept, carrying entity_key and entity_name: filmduration is missing, including the retained placeholder for a notableindividuals row with no filmographyandsoundtracks rows. A real filmographyandsoundtracks row whose filmduration has a value belongs only to the present state and never to this absent state.

Rule 10. For the absent measure state there is one row per notableindividuals entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting per entity_key and entity_name the row count as row_count, how many different non-missing filmduration values occur (each different value counted once, however many rows repeat it) as distinct_amount_count, the total filmduration as total_amount, and the largest filmduration as max_amount.

Rule 11. For each of those absent-state summaries, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, the value max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures are labelled as the absent measure state: each such row carries entity_key, measure_state holding the value absent, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. The output has a deterministic order: rows appear in ascending entity_key order, and within the same entity_key in ascending measure_state order — entity first, then measure state.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `notableindividuals_filmographyandsoundtracks_distribution`

- Grain: One row per (fullname, measure state) pair represented among linked filmographyandsoundtracks rows; the absent state includes missing filmduration values and a no-activity row for a notableindividuals row with no links. A linked filmographyandsoundtracks row whose filmduration has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'notableindividuals_filmographyandsoundtracks_distribution' has 14 declared semantic rules:
1. [source] Read source table notableindividuals. (public source tables: notableindividuals)
2. [source] Read source table filmographyandsoundtracks. (public source tables: filmographyandsoundtracks)
3. [derive] Carry each fullname and its biography into the measure-state calculation. (public source tables: notableindividuals | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked filmographyandsoundtracks rows into each notableindividuals entity; retain an entity with no linked row so its absent state is visible. (public source tables: filmographyandsoundtracks | public carried/output columns: entity_key, entity_name, fullname, filmproducer | join preservation: left | condition public identifiers: filmographyandsoundtracks, filmproducer, entity_key)
5. [filter] Keep the present measure-state rows: a real filmographyandsoundtracks row whose filmduration has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per notableindividuals entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing filmduration values occur (each different value counted once, however many rows repeat it), total filmduration, and largest filmduration. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: filmduration is missing, including the retained placeholder for a notableindividuals row with no filmographyandsoundtracks rows. A real filmographyandsoundtracks row whose filmduration has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per notableindividuals entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing filmduration values occur (each different value counted once, however many rows repeat it), total filmduration, and largest filmduration. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### filmographyandsoundtracks  (source backend: postgres)
Source table filmographyandsoundtracks of the HilmarOrnHilmarssonCompositions database (23 real rows).

- `filmtitle`: text NOT NULL — filmtitle of filmographyandsoundtracks (real vendored values).
- `filmdescription`: text NOT NULL — filmdescription of filmographyandsoundtracks (real vendored values).
- `imdbid`: text NOT NULL — imdbid of filmographyandsoundtracks (real vendored values).
- `filmtype`: text NOT NULL — filmtype of filmographyandsoundtracks (real vendored values).
- `filmdirector`: text NOT NULL — filmdirector of filmographyandsoundtracks (real vendored values).
- `leadcast`: text NULL — leadcast of filmographyandsoundtracks (real vendored values).
- `filmaffinityfilmid`: integer NULL — filmaffinityfilmid of filmographyandsoundtracks (real vendored values).
- `language`: text NOT NULL — language of filmographyandsoundtracks (real vendored values).
- `screenwriter`: text NULL — screenwriter of filmographyandsoundtracks (real vendored values).
- `filmproducer`: text NULL — filmproducer of filmographyandsoundtracks (real vendored values).
- `cinematographer`: text NULL — cinematographer of filmographyandsoundtracks (real vendored values).
- `productioncountry`: text NOT NULL — productioncountry of filmographyandsoundtracks (real vendored values).
- `filmgenre`: text NOT NULL — filmgenre of filmographyandsoundtracks (real vendored values).
- `freebaseid`: text NULL — freebaseid of filmographyandsoundtracks (real vendored values).
- `rottentomatoesid`: text NULL — rottentomatoesid of filmographyandsoundtracks (real vendored values).
- `releasedate`: text NOT NULL — releasedate of filmographyandsoundtracks (real vendored values).
- `allmovietitleid`: text NULL — allmovietitleid of filmographyandsoundtracks (real vendored values).
- `dnffilmid`: integer NULL — dnffilmid of filmographyandsoundtracks (real vendored values).
- `allocinefilmid`: integer NULL — allocinefilmid of filmographyandsoundtracks (real vendored values).
- `originaltitle`: text NULL — originaltitle of filmographyandsoundtracks (real vendored values).
- `composer`: text NOT NULL — composer of filmographyandsoundtracks (real vendored values).
- `filmcolor`: text NULL — filmcolor of filmographyandsoundtracks (real vendored values).
- `filmduration`: integer NULL — filmduration of filmographyandsoundtracks (real vendored values).
- `kinopoiskfilmid`: integer NOT NULL — kinopoiskfilmid of filmographyandsoundtracks (real vendored values).
- `scopedkfilmid`: integer NULL — scopedkfilmid of filmographyandsoundtracks (real vendored values).
- `elfilmfilmid`: integer NULL — elfilmfilmid of filmographyandsoundtracks (real vendored values).
- `tcmmoviedatabasefilmid`: integer NULL — tcmmoviedatabasefilmid of filmographyandsoundtracks (real vendored values).
- `portfilmid`: integer NULL — portfilmid of filmographyandsoundtracks (real vendored values).
- `csfdfilmid`: integer NULL — csfdfilmid of filmographyandsoundtracks (real vendored values).
- `ldifid`: integer NULL — ldifid of filmographyandsoundtracks (real vendored values).
- `ofdbfilmid`: integer NULL — ofdbfilmid of filmographyandsoundtracks (real vendored values).
- `eidrcontentid`: text NULL — eidrcontentid of filmographyandsoundtracks (real vendored values).
- `moviemeterfilmid`: integer NULL — moviemeterfilmid of filmographyandsoundtracks (real vendored values).
- `isancode`: text NULL — isancode of filmographyandsoundtracks (real vendored values).
- `lumierefilmid`: integer NULL — lumierefilmid of filmographyandsoundtracks (real vendored values).
- `tmdbmovieid`: integer NULL — tmdbmovieid of filmographyandsoundtracks (real vendored values).
- `letterboxdfilmid`: text NULL — letterboxdfilmid of filmographyandsoundtracks (real vendored values).
- `trakttvid`: text NULL — trakttvid of filmographyandsoundtracks (real vendored values).
- `kinoboxfilmid`: integer NOT NULL — kinoboxfilmid of filmographyandsoundtracks (real vendored values).
- `plexmediakey`: text NULL — plexmediakey of filmographyandsoundtracks (real vendored values).
- `filmvandaagid`: text NULL — filmvandaagid of filmographyandsoundtracks (real vendored values).
- `filmwebplfilmid`: integer NULL — filmwebplfilmid of filmographyandsoundtracks (real vendored values).
- `doubanfilmid`: integer NULL — doubanfilmid of filmographyandsoundtracks (real vendored values).
- `kvikmyndirfilmid`: integer NULL — kvikmyndirfilmid of filmographyandsoundtracks (real vendored values).

### filmdirectors  (source backend: s3)
Source table filmdirectors of the HilmarOrnHilmarssonCompositions database (11 real rows).

- `directorname`: text NOT NULL — directorname of filmdirectors (real vendored values).
- `biography`: text NULL — biography of filmdirectors (real vendored values).
- `gender`: text NOT NULL — gender of filmdirectors (real vendored values).
- `viafid`: text NOT NULL — viafid of filmdirectors (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of filmdirectors (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of filmdirectors (real vendored values).
- `gndid`: integer NULL — gndid of filmdirectors (real vendored values).
- `imdbid`: text NOT NULL — imdbid of filmdirectors (real vendored values).
- `profileimage`: text NULL — profileimage of filmdirectors (real vendored values).
- `commonscategory`: text NULL — commonscategory of filmdirectors (real vendored values).
- `birthdate`: text NOT NULL — birthdate of filmdirectors (real vendored values).
- `entitytype`: text NOT NULL — entitytype of filmdirectors (real vendored values).
- `birthplace`: text NOT NULL — birthplace of filmdirectors (real vendored values).
- `freebaseid`: text NOT NULL — freebaseid of filmdirectors (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of filmdirectors (real vendored values).
- `nationality`: text NOT NULL — nationality of filmdirectors (real vendored values).
- `profession`: text NOT NULL — profession of filmdirectors (real vendored values).
- `firstname`: text NOT NULL — firstname of filmdirectors (real vendored values).
- `nationalethesaurusvoorauteursnamenid`: text NULL — nationalethesaurusvoorauteursnamenid of filmdirectors (real vendored values).
- `nlcrautid`: text NULL — nlcrautid of filmdirectors (real vendored values).
- `selibrid`: integer NULL — selibrid of filmdirectors (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of filmdirectors (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of filmdirectors (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of filmdirectors (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of filmdirectors (real vendored values).
- `portpersonid`: integer NULL — portpersonid of filmdirectors (real vendored values).
- `fastid`: integer NULL — fastid of filmdirectors (real vendored values).
- `danishnationalfilmographypersonid`: integer NULL — danishnationalfilmographypersonid of filmdirectors (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of filmdirectors (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of filmdirectors (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of filmdirectors (real vendored values).
- `lastname`: text NULL — lastname of filmdirectors (real vendored values).
- `idrefid`: text NULL — idrefid of filmdirectors (real vendored values).
- `languages`: text NULL — languages of filmdirectors (real vendored values).
- `librisuri`: text NULL — librisuri of filmdirectors (real vendored values).
- `relatedcategory`: text NULL — relatedcategory of filmdirectors (real vendored values).
- `nationallibraryofkoreaid`: text NULL — nationallibraryofkoreaid of filmdirectors (real vendored values).
- `storenorskeleksikonid`: text NULL — storenorskeleksikonid of filmdirectors (real vendored values).
- `discogsartistid`: integer NULL — discogsartistid of filmdirectors (real vendored values).
- `deutschebiographiegndid`: integer NULL — deutschebiographiegndid of filmdirectors (real vendored values).
- `nukatid`: text NULL — nukatid of filmdirectors (real vendored values).
- `norafid`: integer NULL — norafid of filmdirectors (real vendored values).
- `plwabnid`: bigint NULL — plwabnid of filmdirectors (real vendored values).
- `tmdbpersonid`: integer NOT NULL — tmdbpersonid of filmdirectors (real vendored values).
- `moviemeterpersonid`: integer NULL — moviemeterpersonid of filmdirectors (real vendored values).
- `neseid`: text NULL — neseid of filmdirectors (real vendored values).
- `denstoredanskeid`: text NULL — denstoredanskeid of filmdirectors (real vendored values).
- `awards`: text NULL — awards of filmdirectors (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of filmdirectors (real vendored values).
- `danskefilmpersonid`: integer NULL — danskefilmpersonid of filmdirectors (real vendored values).
- `danskfilmogtvpersonid`: integer NULL — danskfilmogtvpersonid of filmdirectors (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of filmdirectors (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of filmdirectors (real vendored values).
- business key: directorname

### notableindividuals  (source backend: postgres)
Source table notableindividuals of the HilmarOrnHilmarssonCompositions database (12 real rows).

- `fullname`: text NOT NULL — fullname of notableindividuals (real vendored values).
- `biography`: text NULL — biography of notableindividuals (real vendored values).
- `profession`: text NULL — profession of notableindividuals (real vendored values).
- `viafid`: bigint NULL — viafid of notableindividuals (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of notableindividuals (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of notableindividuals (real vendored values).
- `gndid`: integer NULL — gndid of notableindividuals (real vendored values).
- `imdbid`: text NULL — imdbid of notableindividuals (real vendored values).
- `birthdate`: text NULL — birthdate of notableindividuals (real vendored values).
- `nationality`: text NULL — nationality of notableindividuals (real vendored values).
- `entitytype`: text NOT NULL — entitytype of notableindividuals (real vendored values).
- `birthplace`: text NULL — birthplace of notableindividuals (real vendored values).
- `freebaseid`: text NULL — freebaseid of notableindividuals (real vendored values).
- `honorsawarded`: text NULL — honorsawarded of notableindividuals (real vendored values).
- `firstname`: text NOT NULL — firstname of notableindividuals (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of notableindividuals (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of notableindividuals (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of notableindividuals (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of notableindividuals (real vendored values).
- `portpersonid`: integer NULL — portpersonid of notableindividuals (real vendored values).
- `danishnationalfilmographypersonid`: integer NULL — danishnationalfilmographypersonid of notableindividuals (real vendored values).
- `filmportalid`: text NULL — filmportalid of notableindividuals (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of notableindividuals (real vendored values).
- `lastname`: text NULL — lastname of notableindividuals (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of notableindividuals (real vendored values).
- `gender`: text NOT NULL — gender of notableindividuals (real vendored values).
- `profileimage`: text NULL — profileimage of notableindividuals (real vendored values).
- `danskfilmogtvpersonid`: integer NULL — danskfilmogtvpersonid of notableindividuals (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of notableindividuals (real vendored values).
- `deutschebiographiegndid`: integer NULL — deutschebiographiegndid of notableindividuals (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of notableindividuals (real vendored values).
- `ivipersonid`: text NULL — ivipersonid of notableindividuals (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of notableindividuals (real vendored values).
- business key: fullname

### directorofphotography  (source backend: s3)
Source table directorofphotography of the HilmarOrnHilmarssonCompositions database (11 real rows).

- `name`: text NOT NULL — name of directorofphotography (real vendored values).
- `biography`: text NULL — biography of directorofphotography (real vendored values).
- `gender`: text NOT NULL — gender of directorofphotography (real vendored values).
- `birthplace`: text NULL — birthplace of directorofphotography (real vendored values).
- `nationality`: text NULL — nationality of directorofphotography (real vendored values).
- `viafid`: text NULL — viafid of directorofphotography (real vendored values).
- `gndid`: text NULL — gndid of directorofphotography (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of directorofphotography (real vendored values).
- `entitytype`: text NOT NULL — entitytype of directorofphotography (real vendored values).
- `profession`: text NOT NULL — profession of directorofphotography (real vendored values).
- `freebaseid`: text NULL — freebaseid of directorofphotography (real vendored values).
- `birthdate`: text NULL — birthdate of directorofphotography (real vendored values).
- `imdbid`: text NOT NULL — imdbid of directorofphotography (real vendored values).
- `firstname`: text NOT NULL — firstname of directorofphotography (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of directorofphotography (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of directorofphotography (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of directorofphotography (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of directorofphotography (real vendored values).
- `portpersonid`: integer NULL — portpersonid of directorofphotography (real vendored values).
- `scopedkpersonid`: integer NULL — scopedkpersonid of directorofphotography (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of directorofphotography (real vendored values).
- `danishnationalfilmographypersonid`: integer NULL — danishnationalfilmographypersonid of directorofphotography (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of directorofphotography (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of directorofphotography (real vendored values).
- `nukatid`: text NULL — nukatid of directorofphotography (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of directorofphotography (real vendored values).
- `lastname`: text NULL — lastname of directorofphotography (real vendored values).
- `tmdbpersonid`: integer NOT NULL — tmdbpersonid of directorofphotography (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of directorofphotography (real vendored values).
- `kinoboxpersonid`: integer NOT NULL — kinoboxpersonid of directorofphotography (real vendored values).
- `danskefilmpersonid`: integer NULL — danskefilmpersonid of directorofphotography (real vendored values).
- `nlcrautid`: text NULL — nlcrautid of directorofphotography (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of directorofphotography (real vendored values).
- business key: name

### director  (source backend: mongodb)
Source table director of the HilmarOrnHilmarssonCompositions database (16 real rows).

- `directorname`: text NOT NULL — directorname of director (real vendored values).
- `biography`: text NULL — biography of director (real vendored values).
- `gender`: text NOT NULL — gender of director (real vendored values).
- `viafid`: text NULL — viafid of director (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of director (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of director (real vendored values).
- `gndid`: text NULL — gndid of director (real vendored values).
- `imdbid`: text NOT NULL — imdbid of director (real vendored values).
- `commonscategory`: text NULL — commonscategory of director (real vendored values).
- `birthdate`: text NULL — birthdate of director (real vendored values).
- `entitytype`: text NOT NULL — entitytype of director (real vendored values).
- `birthplace`: text NULL — birthplace of director (real vendored values).
- `freebaseid`: text NULL — freebaseid of director (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of director (real vendored values).
- `nationality`: text NULL — nationality of director (real vendored values).
- `profession`: text NOT NULL — profession of director (real vendored values).
- `firstname`: text NOT NULL — firstname of director (real vendored values).
- `nationalethesaurusvoorauteursnamenid`: text NULL — nationalethesaurusvoorauteursnamenid of director (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of director (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of director (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of director (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of director (real vendored values).
- `portpersonid`: integer NULL — portpersonid of director (real vendored values).
- `scopedkpersonid`: integer NULL — scopedkpersonid of director (real vendored values).
- `danishnationalfilmographypersonid`: integer NULL — danishnationalfilmographypersonid of director (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of director (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of director (real vendored values).
- `lastname`: text NULL — lastname of director (real vendored values).
- `idrefid`: text NULL — idrefid of director (real vendored values).
- `languages`: text NULL — languages of director (real vendored values).
- `relatedcategory`: text NULL — relatedcategory of director (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of director (real vendored values).
- `moviemeterpersonid`: integer NULL — moviemeterpersonid of director (real vendored values).
- `awards`: text NULL — awards of director (real vendored values).
- `ivipersonid`: text NULL — ivipersonid of director (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of director (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of director (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of director (real vendored values).
- business key: directorname

### composerrelatedgivennames  (source backend: rest)
Source table composerrelatedgivennames of the HilmarOrnHilmarssonCompositions database (64 real rows).

- `givennamelabel`: text NOT NULL — givennamelabel of composerrelatedgivennames (real vendored values).
- `namedescription`: text NOT NULL — namedescription of composerrelatedgivennames (real vendored values).
- `nametype`: text NOT NULL — nametype of composerrelatedgivennames (real vendored values).
- `nameday`: text NULL — nameday of composerrelatedgivennames (real vendored values).
- `namedifferentiation`: text NULL — namedifferentiation of composerrelatedgivennames (real vendored values).
- `languageoforigin`: text NULL — languageoforigin of composerrelatedgivennames (real vendored values).
- `nativenamelabel`: text NOT NULL — nativenamelabel of composerrelatedgivennames (real vendored values).
- `familynameassociation`: text NULL — familynameassociation of composerrelatedgivennames (real vendored values).
- `nameequivalence`: text NULL — nameequivalence of composerrelatedgivennames (real vendored values).
- `writingsystemused`: text NOT NULL — writingsystemused of composerrelatedgivennames (real vendored values).
- `soundexcode`: text NULL — soundexcode of composerrelatedgivennames (real vendored values).
- `colognephoneticscode`: text NULL — colognephoneticscode of composerrelatedgivennames (real vendored values).
- `caverphonecode`: text NULL — caverphonecode of composerrelatedgivennames (real vendored values).
- `commonscategoryreference`: text NULL — commonscategoryreference of composerrelatedgivennames (real vendored values).
- `storenorskeleksikonid`: text NULL — storenorskeleksikonid of composerrelatedgivennames (real vendored values).
- `nederlandsevoornamenbankid`: text NULL — nederlandsevoornamenbankid of composerrelatedgivennames (real vendored values).

### composerfamilynames  (source backend: rest)
Source table composerfamilynames of the HilmarOrnHilmarssonCompositions database (41 real rows).

- `surname`: text NOT NULL — surname of composerfamilynames (real vendored values).
- `surnamedescription`: text NOT NULL — surnamedescription of composerfamilynames (real vendored values).
- `category`: text NOT NULL — category of composerfamilynames (real vendored values).
- `language`: text NULL — language of composerfamilynames (real vendored values).
- `nativesurname`: text NULL — nativesurname of composerfamilynames (real vendored values).
- `script`: text NULL — script of composerfamilynames (real vendored values).
- `soundexcode`: text NULL — soundexcode of composerfamilynames (real vendored values).
- `colognephoneticscode`: text NULL — colognephoneticscode of composerfamilynames (real vendored values).
- `caverphonecode`: text NULL — caverphonecode of composerfamilynames (real vendored values).
- `geneanetfamilynameid`: text NULL — geneanetfamilynameid of composerfamilynames (real vendored values).
- `commonscategory`: text NULL — commonscategory of composerfamilynames (real vendored values).
- `distinctionnote`: text NULL — distinctionnote of composerfamilynames (real vendored values).

### filmgenres  (source backend: mongodb)
Source table filmgenres of the HilmarOrnHilmarssonCompositions database (10 real rows).

- `genrename`: text NOT NULL — genrename of filmgenres (real vendored values).
- `genredescription`: text NOT NULL — genredescription of filmgenres (real vendored values).
- `commonscategory`: text NULL — commonscategory of filmgenres (real vendored values).
- `instanceof`: text NOT NULL — instanceof of filmgenres (real vendored values).
- `maincategory`: text NOT NULL — maincategory of filmgenres (real vendored values).
- `gndid`: text NULL — gndid of filmgenres (real vendored values).
- `freebaseid`: text NULL — freebaseid of filmgenres (real vendored values).
- `representativeimage`: text NULL — representativeimage of filmgenres (real vendored values).
- `quoratopicid`: text NULL — quoratopicid of filmgenres (real vendored values).
- `babelnetid`: text NULL — babelnetid of filmgenres (real vendored values).
- `libraryofcongressgenreformtermsid`: text NULL — libraryofcongressgenreformtermsid of filmgenres (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of filmgenres (real vendored values).
- `iabcode`: integer NULL — iabcode of filmgenres (real vendored values).
- `ysoid`: integer NULL — ysoid of filmgenres (real vendored values).
- `kbpediaid`: text NULL — kbpediaid of filmgenres (real vendored values).
- `subclassof`: text NOT NULL — subclassof of filmgenres (real vendored values).
- `xtopicid`: bigint NULL — xtopicid of filmgenres (real vendored values).
- `nationallibraryofisraelj9uid`: bigint NULL — nationallibraryofisraelj9uid of filmgenres (real vendored values).
- `norwegianthesaurusongenreandformid`: integer NULL — norwegianthesaurusongenreandformid of filmgenres (real vendored values).
- `nlcrautid`: text NULL — nlcrautid of filmgenres (real vendored values).
- `suddeutschezeitungtopicid`: text NULL — suddeutschezeitungtopicid of filmgenres (real vendored values).
- `rateyourmusicfilmgenreid`: text NULL — rateyourmusicfilmgenreid of filmgenres (real vendored values).
- `itunesgenreid`: integer NULL — itunesgenreid of filmgenres (real vendored values).
- `wikikidsid`: text NULL — wikikidsid of filmgenres (real vendored values).
- `relatedlist`: text NULL — relatedlist of filmgenres (real vendored values).
- `allmoviegenreid`: text NULL — allmoviegenreid of filmgenres (real vendored values).
- `gsafdid`: text NULL — gsafdid of filmgenres (real vendored values).
- business key: genrename

### notableartistsandaffiliates  (source backend: rest)
Source table notableartistsandaffiliates of the HilmarOrnHilmarssonCompositions database (17 real rows).

- `artistname`: text NOT NULL — artistname of notableartistsandaffiliates (real vendored values).
- `biography`: text NOT NULL — biography of notableartistsandaffiliates (real vendored values).
- `gender`: text NOT NULL — gender of notableartistsandaffiliates (real vendored values).
- `wikimediacategory`: text NULL — wikimediacategory of notableartistsandaffiliates (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of notableartistsandaffiliates (real vendored values).
- `viafid`: text NULL — viafid of notableartistsandaffiliates (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of notableartistsandaffiliates (real vendored values).
- `birthplace`: text NULL — birthplace of notableartistsandaffiliates (real vendored values).
- `profileimage`: text NULL — profileimage of notableartistsandaffiliates (real vendored values).
- `awards`: text NULL — awards of notableartistsandaffiliates (real vendored values).
- `profession`: text NOT NULL — profession of notableartistsandaffiliates (real vendored values).
- `musicbrainzartistid`: text NULL — musicbrainzartistid of notableartistsandaffiliates (real vendored values).
- `gndid`: integer NULL — gndid of notableartistsandaffiliates (real vendored values).
- `imdbid`: text NULL — imdbid of notableartistsandaffiliates (real vendored values).
- `idrefid`: text NULL — idrefid of notableartistsandaffiliates (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of notableartistsandaffiliates (real vendored values).
- `birthdate`: text NOT NULL — birthdate of notableartistsandaffiliates (real vendored values).
- `entitytype`: text NOT NULL — entitytype of notableartistsandaffiliates (real vendored values).
- `freebaseid`: text NULL — freebaseid of notableartistsandaffiliates (real vendored values).
- `nationality`: text NOT NULL — nationality of notableartistsandaffiliates (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of notableartistsandaffiliates (real vendored values).
- `firstname`: text NULL — firstname of notableartistsandaffiliates (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of notableartistsandaffiliates (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of notableartistsandaffiliates (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of notableartistsandaffiliates (real vendored values).
- `portpersonid`: integer NULL — portpersonid of notableartistsandaffiliates (real vendored values).
- `scopedkpersonid`: integer NULL — scopedkpersonid of notableartistsandaffiliates (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of notableartistsandaffiliates (real vendored values).
- `danishnationalfilmographypersonid`: integer NULL — danishnationalfilmographypersonid of notableartistsandaffiliates (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of notableartistsandaffiliates (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of notableartistsandaffiliates (real vendored values).
- `nlcrautid`: text NULL — nlcrautid of notableartistsandaffiliates (real vendored values).
- `nationalethesaurusvoorauteursnamenid`: text NULL — nationalethesaurusvoorauteursnamenid of notableartistsandaffiliates (real vendored values).
- `lastname`: text NULL — lastname of notableartistsandaffiliates (real vendored values).
- `almamater`: text NULL — almamater of notableartistsandaffiliates (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of notableartistsandaffiliates (real vendored values).
- `openmediadatabasepersonid`: integer NULL — openmediadatabasepersonid of notableartistsandaffiliates (real vendored values).
- `careerstartyear`: text NULL — careerstartyear of notableartistsandaffiliates (real vendored values).
- `languages`: text NULL — languages of notableartistsandaffiliates (real vendored values).
- `mymoviespersonid`: integer NULL — mymoviespersonid of notableartistsandaffiliates (real vendored values).
- `prabookid`: integer NULL — prabookid of notableartistsandaffiliates (real vendored values).
- `cinemagiapersonid`: integer NULL — cinemagiapersonid of notableartistsandaffiliates (real vendored values).
- `ivipersonid`: text NULL — ivipersonid of notableartistsandaffiliates (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of notableartistsandaffiliates (real vendored values).
- `doubanmoviecelebrityid`: integer NULL — doubanmoviecelebrityid of notableartistsandaffiliates (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of notableartistsandaffiliates (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of notableartistsandaffiliates (real vendored values).
- business key: artistname

### filmdirectorcategories  (source backend: mongodb)
Source table filmdirectorcategories of the HilmarOrnHilmarssonCompositions database (10 real rows).

- `categorylabel`: text NOT NULL — categorylabel of filmdirectorcategories (real vendored values).
- `categorydescription`: text NOT NULL — categorydescription of filmdirectorcategories (real vendored values).
- `categorytype`: text NOT NULL — categorytype of filmdirectorcategories (real vendored values).
- `relatedtopics`: text NOT NULL — relatedtopics of filmdirectorcategories (real vendored values).
- `containedtopics`: text NOT NULL — containedtopics of filmdirectorcategories (real vendored values).
- business key: categorylabel

### Relationships

- director(relatedcategory) -> filmdirectorcategories(categorylabel) [optional (may be NULL/dangling)]
- filmographyandsoundtracks(cinematographer) -> directorofphotography(name) [optional (may be NULL/dangling)]
- filmographyandsoundtracks(filmdirector) -> director(directorname) [required]
- filmographyandsoundtracks(filmgenre) -> filmgenres(genrename) [required]
- filmographyandsoundtracks(filmproducer) -> notableindividuals(fullname) [optional (may be NULL/dangling)]
- filmographyandsoundtracks(leadcast) -> notableartistsandaffiliates(artistname) [optional (may be NULL/dangling)]
- filmographyandsoundtracks(screenwriter) -> filmdirectors(directorname) [optional (may be NULL/dangling)]

