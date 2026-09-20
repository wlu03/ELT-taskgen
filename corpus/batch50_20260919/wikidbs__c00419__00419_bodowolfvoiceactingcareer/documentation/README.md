# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Bodowolfvoiceactingcareer

## Specification

PROJECT OVERVIEW

This project builds one analytical mart for the BodoWolfVoiceActingCareer database, which describes the voice-acting career of Bodo Wolf across Star Trek episodes, characters, collaborators and related people.

Eleven source tables feed this project, and each must be extracted from the backend named with it here. The table startrekepisodesfeaturingbodowolf must be extracted from the postgres backend. The table voiceactors must be extracted from the mongodb backend. The table voiceactingcollaborators must be extracted from the postgres backend. The table screenwritersconnectedtobodowolf must be extracted from the mongodb backend. The table voiceactorprofiles must be extracted from the postgres backend. The table startrekcharacters must be extracted from the files backend. The table performingartist must be extracted from the postgres backend. The table voiceactingroles must be extracted from the files backend. The table characternames must be extracted from the rest backend. The table surnamemetadata must be extracted from the s3 backend. The table spouse must be extracted from the s3 backend.

Business keys, where the schema declares them, are: actorname for voiceactors, charactername for voiceactingcollaborators, screenwritername for screenwritersconnectedtobodowolf, actorname for voiceactorprofiles, charactername for startrekcharacters, fullname for performingartist, rolelabel for voiceactingroles.

RELATIONSHIPS BETWEEN THE SOURCE TABLES

Child table startrekcharacters with key actorname refers to parent table performingartist with key fullname; this relationship is optional, so actorname may be null or may name a fullname that is not present.

Child table startrekcharacters with key characteroccupation refers to parent table voiceactingroles with key rolelabel; this relationship is optional, so characteroccupation may be null or may name a rolelabel that is not present.

Child table startrekepisodesfeaturingbodowolf with key director refers to parent table voiceactorprofiles with key actorname; this relationship is required, so every director value corresponds to an actorname.

Child table startrekepisodesfeaturingbodowolf with key leadactor refers to parent table voiceactingcollaborators with key charactername; this relationship is optional, so leadactor may be null or may name a charactername that is not present.

Child table startrekepisodesfeaturingbodowolf with key maincharacter refers to parent table startrekcharacters with key charactername; this relationship is optional, so maincharacter may be null or may name a charactername that is not present.

Child table startrekepisodesfeaturingbodowolf with key screenwriter refers to parent table screenwritersconnectedtobodowolf with key screenwritername; this relationship is optional, so screenwriter may be null or may name a screenwritername that is not present.

Child table startrekepisodesfeaturingbodowolf with key voiceactor refers to parent table voiceactors with key actorname; this relationship is required, so every voiceactor value corresponds to an actorname.

MART voiceactors_startrekepisodesfeaturingbodowolf_distribution — Per-(voiceactors, measure state) distribution of linked startrekepisodesfeaturingbodowolf rows in the BodoWolfVoiceActingCareer database.

Grain. One row per (actorname, measure state) pair represented among linked startrekepisodesfeaturingbodowolf rows; the absent state includes missing productioncode values and a no-activity row for a voiceactors row with no links. A linked startrekepisodesfeaturingbodowolf row whose productioncode has a value belongs only to the present state and never to the absent state.

Key columns. The key columns of this mart are entity_key and measure_state; together they identify one output row.

Rule 1. The source table voiceactors is read in full and supplies the entities of this mart.

Rule 2. The source table startrekepisodesfeaturingbodowolf is read in full and supplies the episode rows measured here.

Rule 3. From voiceactors, each actorname is carried into the measure-state calculation as entity_key and its actordescription is carried alongside as entity_name.

Rule 4. The startrekepisodesfeaturingbodowolf rows whose voiceactor matches an entity_key are brought into that voiceactors entity, carrying entity_key, entity_name, actorname and voiceactor; preservation is left-sided on the voiceactors side, so an entity with no matching startrekepisodesfeaturingbodowolf row is retained as a placeholder and its absent state stays visible.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are the ones kept where there is a real startrekepisodesfeaturingbodowolf row whose productioncode has a value.

Rule 6. In the present measure state there is one row per voiceactors entity that has at least one such row, and no row here for an entity with none; each such entity reports entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount — the row count, how many different non-missing productioncode values occur (each different value counted once, however many rows repeat it), the total productioncode, and the largest productioncode.

Rule 7. For these present-state rows carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share, the value of max_amount_share is max_amount divided by total_amount expressed as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures are labelled as the present measure state, so measure_state reads present on each of them, and the row carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are the ones kept where productioncode is missing, including the retained placeholder for a voiceactors row with no startrekepisodesfeaturingbodowolf rows. A real startrekepisodesfeaturingbodowolf row whose productioncode has a value belongs only to the present state and never to this absent state.

Rule 10. In the absent measure state there is one row per voiceactors entity that has at least one such row, and no row here for an entity with none; each such entity reports entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount — the row count, how many different non-missing productioncode values occur (each different value counted once, however many rows repeat it), the total productioncode, and the largest productioncode.

Rule 11. For these absent-state rows carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share, the value of max_amount_share is max_amount divided by total_amount expressed as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures are labelled as the absent measure state, so measure_state reads absent on each of them, and the row carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, all rows of both summaries are kept, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. Deterministic output order carrying entity_key and measure_state: rows appear in ascending entity order first, then in ascending measure state order within an entity.

Output columns.

entity_key (text): the identifier of the voiceactors row.

measure_state (text): 'present' for a linked startrekepisodesfeaturingbodowolf row whose productioncode has a value; 'absent' when productioncode is missing, including a voiceactors row with no linked startrekepisodesfeaturingbodowolf row. A linked startrekepisodesfeaturingbodowolf row whose productioncode has a value belongs only to the present state and never to the absent state.

entity_name (text): the actordescription of the voiceactors row, copied unchanged.

row_count (bigint): the number of linked startrekepisodesfeaturingbodowolf rows in this entity/state cell; an absent cell holding real startrekepisodesfeaturingbodowolf rows whose productioncode is missing COUNTS those rows, and only the placeholder cell of a voiceactors row with no linked startrekepisodesfeaturingbodowolf row at all reports 0.

distinct_amount_count (bigint): the number of unique non-missing productioncode values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no productioncode value at all — both for a voiceactors row with no linked startrekepisodesfeaturingbodowolf row and for an absent cell whose rows all have a missing productioncode.

total_amount (integer): the total of productioncode in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an productioncode value.

max_amount (integer): the largest productioncode in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an productioncode value.

max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `voiceactors_startrekepisodesfeaturingbodowolf_distribution`

- Grain: One row per (actorname, measure state) pair represented among linked startrekepisodesfeaturingbodowolf rows; the absent state includes missing productioncode values and a no-activity row for a voiceactors row with no links. A linked startrekepisodesfeaturingbodowolf row whose productioncode has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'voiceactors_startrekepisodesfeaturingbodowolf_distribution' has 14 declared semantic rules:
1. [source] Read source table voiceactors. (public source tables: voiceactors)
2. [source] Read source table startrekepisodesfeaturingbodowolf. (public source tables: startrekepisodesfeaturingbodowolf)
3. [derive] Carry each actorname and its actordescription into the measure-state calculation. (public source tables: voiceactors | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked startrekepisodesfeaturingbodowolf rows into each voiceactors entity; retain an entity with no linked row so its absent state is visible. (public source tables: startrekepisodesfeaturingbodowolf | public carried/output columns: entity_key, entity_name, actorname, voiceactor | join preservation: left | condition public identifiers: startrekepisodesfeaturingbodowolf, voiceactor, entity_key)
5. [filter] Keep the present measure-state rows: a real startrekepisodesfeaturingbodowolf row whose productioncode has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per voiceactors entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing productioncode values occur (each different value counted once, however many rows repeat it), total productioncode, and largest productioncode. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: productioncode is missing, including the retained placeholder for a voiceactors row with no startrekepisodesfeaturingbodowolf rows. A real startrekepisodesfeaturingbodowolf row whose productioncode has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per voiceactors entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing productioncode values occur (each different value counted once, however many rows repeat it), total productioncode, and largest productioncode. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### startrekepisodesfeaturingbodowolf  (source backend: postgres)
Source table startrekepisodesfeaturingbodowolf of the BodoWolfVoiceActingCareer database (122 real rows).

- `episodetitle`: text NOT NULL — episodetitle of startrekepisodesfeaturingbodowolf (real vendored values).
- `episodedescription`: text NOT NULL — episodedescription of startrekepisodesfeaturingbodowolf (real vendored values).
- `episodetype`: text NOT NULL — episodetype of startrekepisodesfeaturingbodowolf (real vendored values).
- `imdbid`: text NULL — imdbid of startrekepisodesfeaturingbodowolf (real vendored values).
- `screenwriter`: text NULL — screenwriter of startrekepisodesfeaturingbodowolf (real vendored values).
- `airdate`: text NOT NULL — airdate of startrekepisodesfeaturingbodowolf (real vendored values).
- `originallanguage`: text NOT NULL — originallanguage of startrekepisodesfeaturingbodowolf (real vendored values).
- `countryoforigin`: text NOT NULL — countryoforigin of startrekepisodesfeaturingbodowolf (real vendored values).
- `director`: text NOT NULL — director of startrekepisodesfeaturingbodowolf (real vendored values).
- `colorformat`: text NULL — colorformat of startrekepisodesfeaturingbodowolf (real vendored values).
- `episodeduration`: integer NOT NULL — episodeduration of startrekepisodesfeaturingbodowolf (real vendored values).
- `distributor`: text NULL — distributor of startrekepisodesfeaturingbodowolf (real vendored values).
- `title`: text NOT NULL — title of startrekepisodesfeaturingbodowolf (real vendored values).
- `voiceactor`: text NOT NULL — voiceactor of startrekepisodesfeaturingbodowolf (real vendored values).
- `csfdfilmid`: integer NULL — csfdfilmid of startrekepisodesfeaturingbodowolf (real vendored values).
- `eidrcontentid`: text NOT NULL — eidrcontentid of startrekepisodesfeaturingbodowolf (real vendored values).
- `isan`: text NULL — isan of startrekepisodesfeaturingbodowolf (real vendored values).
- `openmediadatabasefilmid`: integer NULL — openmediadatabasefilmid of startrekepisodesfeaturingbodowolf (real vendored values).
- `bfinationalarchiveworkid`: integer NULL — bfinationalarchiveworkid of startrekepisodesfeaturingbodowolf (real vendored values).
- `freebaseid`: text NULL — freebaseid of startrekepisodesfeaturingbodowolf (real vendored values).
- `trakttvid`: text NULL — trakttvid of startrekepisodesfeaturingbodowolf (real vendored values).
- `seriesname`: text NULL — seriesname of startrekepisodesfeaturingbodowolf (real vendored values).
- `tvcomid`: text NULL — tvcomid of startrekepisodesfeaturingbodowolf (real vendored values).
- `productioncode`: integer NULL — productioncode of startrekepisodesfeaturingbodowolf (real vendored values).
- `seasoninfo`: text NULL — seasoninfo of startrekepisodesfeaturingbodowolf (real vendored values).
- `narrativelocation`: text NULL — narrativelocation of startrekepisodesfeaturingbodowolf (real vendored values).
- `firstperformancedate`: text NULL — firstperformancedate of startrekepisodesfeaturingbodowolf (real vendored values).
- `maincharacter`: text NULL — maincharacter of startrekepisodesfeaturingbodowolf (real vendored values).
- `leadactor`: text NULL — leadactor of startrekepisodesfeaturingbodowolf (real vendored values).
- `aspectratio`: text NULL — aspectratio of startrekepisodesfeaturingbodowolf (real vendored values).
- `netflixid`: integer NULL — netflixid of startrekepisodesfeaturingbodowolf (real vendored values).
- `fictionaluniverse`: text NULL — fictionaluniverse of startrekepisodesfeaturingbodowolf (real vendored values).
- `rottentomatoesid`: text NULL — rottentomatoesid of startrekepisodesfeaturingbodowolf (real vendored values).
- `allmovietitleid`: text NULL — allmovietitleid of startrekepisodesfeaturingbodowolf (real vendored values).
- `cinemagiatitleid`: integer NULL — cinemagiatitleid of startrekepisodesfeaturingbodowolf (real vendored values).
- `historicalsetting`: text NULL — historicalsetting of startrekepisodesfeaturingbodowolf (real vendored values).
- `fandomarticleid`: text NULL — fandomarticleid of startrekepisodesfeaturingbodowolf (real vendored values).
- `sourcereference`: text NULL — sourcereference of startrekepisodesfeaturingbodowolf (real vendored values).
- `wikitrekarticleid`: text NULL — wikitrekarticleid of startrekepisodesfeaturingbodowolf (real vendored values).
- `franchisename`: text NULL — franchisename of startrekepisodesfeaturingbodowolf (real vendored values).
- `distributionformat`: text NULL — distributionformat of startrekepisodesfeaturingbodowolf (real vendored values).
- `startrekcomdatabaseid`: text NULL — startrekcomdatabaseid of startrekepisodesfeaturingbodowolf (real vendored values).
- `environmentsetting`: text NULL — environmentsetting of startrekepisodesfeaturingbodowolf (real vendored values).
- `nextepisode`: text NULL — nextepisode of startrekepisodesfeaturingbodowolf (real vendored values).
- `previousepisode`: text NULL — previousepisode of startrekepisodesfeaturingbodowolf (real vendored values).
- `copyrightstatus`: text NULL — copyrightstatus of startrekepisodesfeaturingbodowolf (real vendored values).

### voiceactors  (source backend: mongodb)
Source table voiceactors of the BodoWolfVoiceActingCareer database (16 real rows).

- `actorname`: text NOT NULL — actorname of voiceactors (real vendored values).
- `actordescription`: text NOT NULL — actordescription of voiceactors (real vendored values).
- `viafid`: bigint NULL — viafid of voiceactors (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of voiceactors (real vendored values).
- `primaryoccupation`: text NOT NULL — primaryoccupation of voiceactors (real vendored values).
- `musicbrainzartistid`: text NULL — musicbrainzartistid of voiceactors (real vendored values).
- `gndid`: integer NULL — gndid of voiceactors (real vendored values).
- `imdbid`: text NULL — imdbid of voiceactors (real vendored values).
- `entitytype`: text NOT NULL — entitytype of voiceactors (real vendored values).
- `birthplace`: text NULL — birthplace of voiceactors (real vendored values).
- `birthdate`: text NOT NULL — birthdate of voiceactors (real vendored values).
- `freebaseid`: text NULL — freebaseid of voiceactors (real vendored values).
- `nationality`: text NOT NULL — nationality of voiceactors (real vendored values).
- `firstname`: text NOT NULL — firstname of voiceactors (real vendored values).
- `discogsartistid`: integer NULL — discogsartistid of voiceactors (real vendored values).
- `languages`: text NOT NULL — languages of voiceactors (real vendored values).
- `filmportalid`: text NULL — filmportalid of voiceactors (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of voiceactors (real vendored values).
- `nativename`: text NULL — nativename of voiceactors (real vendored values).
- `surname`: text NULL — surname of voiceactors (real vendored values).
- `gender`: text NOT NULL — gender of voiceactors (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of voiceactors (real vendored values).
- `deutschebiographiegndid`: integer NULL — deutschebiographiegndid of voiceactors (real vendored values).
- `deutschesynchronkarteidubbingvoiceactorid`: integer NULL — deutschesynchronkarteidubbingvoiceactorid of voiceactors (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of voiceactors (real vendored values).
- `deutschesynchronkarteipersonid`: text NOT NULL — deutschesynchronkarteipersonid of voiceactors (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of voiceactors (real vendored values).
- `googleknowledgegraphid`: text NULL — googleknowledgegraphid of voiceactors (real vendored values).
- `myanimelistpeopleid`: integer NULL — myanimelistpeopleid of voiceactors (real vendored values).
- business key: actorname

### voiceactingcollaborators  (source backend: postgres)
Source table voiceactingcollaborators of the BodoWolfVoiceActingCareer database (20 real rows).

- `charactername`: text NOT NULL — charactername of voiceactingcollaborators (real vendored values).
- `characterdescription`: text NOT NULL — characterdescription of voiceactingcollaborators (real vendored values).
- `characterplaceofbirth`: text NULL — characterplaceofbirth of voiceactingcollaborators (real vendored values).
- `characterspouse`: text NULL — characterspouse of voiceactingcollaborators (real vendored values).
- `commonscategory`: text NULL — commonscategory of voiceactingcollaborators (real vendored values).
- `characteroccupation`: text NOT NULL — characteroccupation of voiceactingcollaborators (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of voiceactingcollaborators (real vendored values).
- `viafid`: text NULL — viafid of voiceactingcollaborators (real vendored values).
- `gndid`: integer NULL — gndid of voiceactingcollaborators (real vendored values).
- `isnicode`: text NULL — isnicode of voiceactingcollaborators (real vendored values).
- `imdbid`: text NOT NULL — imdbid of voiceactingcollaborators (real vendored values).
- `characterdateofbirth`: text NOT NULL — characterdateofbirth of voiceactingcollaborators (real vendored values).
- `entitytype`: text NOT NULL — entitytype of voiceactingcollaborators (real vendored values).
- `freebaseid`: text NOT NULL — freebaseid of voiceactingcollaborators (real vendored values).
- `charactercountryofcitizenship`: text NOT NULL — charactercountryofcitizenship of voiceactingcollaborators (real vendored values).
- `charactergivenname`: text NULL — charactergivenname of voiceactingcollaborators (real vendored values).
- `nndbpeopleid`: text NULL — nndbpeopleid of voiceactingcollaborators (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of voiceactingcollaborators (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of voiceactingcollaborators (real vendored values).
- `charactereducationalinstitution`: text NULL — charactereducationalinstitution of voiceactingcollaborators (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of voiceactingcollaborators (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of voiceactingcollaborators (real vendored values).
- `portpersonid`: integer NULL — portpersonid of voiceactingcollaborators (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of voiceactingcollaborators (real vendored values).
- `characterfamilyname`: text NULL — characterfamilyname of voiceactingcollaborators (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of voiceactingcollaborators (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of voiceactingcollaborators (real vendored values).
- `scopedkpersonid`: integer NULL — scopedkpersonid of voiceactingcollaborators (real vendored values).
- `wikitreepersonid`: text NULL — wikitreepersonid of voiceactingcollaborators (real vendored values).
- `workperiodstart`: text NULL — workperiodstart of voiceactingcollaborators (real vendored values).
- `cinemagiapersonid`: integer NULL — cinemagiapersonid of voiceactingcollaborators (real vendored values).
- `tmdbpersonid`: integer NOT NULL — tmdbpersonid of voiceactingcollaborators (real vendored values).
- `languagesspokenwrittensigned`: text NULL — languagesspokenwrittensigned of voiceactingcollaborators (real vendored values).
- `behindthevoiceactorspersonid`: text NULL — behindthevoiceactorspersonid of voiceactingcollaborators (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of voiceactingcollaborators (real vendored values).
- `charactergender`: text NOT NULL — charactergender of voiceactingcollaborators (real vendored values).
- `tradingcarddatabasepersonid`: integer NULL — tradingcarddatabasepersonid of voiceactingcollaborators (real vendored values).
- `filmwebplpersonid`: integer NULL — filmwebplpersonid of voiceactingcollaborators (real vendored values).
- `characternativelanguage`: text NULL — characternativelanguage of voiceactingcollaborators (real vendored values).
- `characterbirthname`: text NULL — characterbirthname of voiceactingcollaborators (real vendored values).
- `plwabnid`: bigint NULL — plwabnid of voiceactingcollaborators (real vendored values).
- `fandomarticleid`: text NULL — fandomarticleid of voiceactingcollaborators (real vendored values).
- `startrekcomdatabaseid`: text NULL — startrekcomdatabaseid of voiceactingcollaborators (real vendored values).
- `rottentomatoesid`: text NULL — rottentomatoesid of voiceactingcollaborators (real vendored values).
- `mymoviespersonid`: integer NULL — mymoviespersonid of voiceactingcollaborators (real vendored values).
- `characterimage`: text NULL — characterimage of voiceactingcollaborators (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of voiceactingcollaborators (real vendored values).
- `ivipersonid`: text NULL — ivipersonid of voiceactingcollaborators (real vendored values).
- `kinoboxpersonid`: integer NOT NULL — kinoboxpersonid of voiceactingcollaborators (real vendored values).
- `listalid`: text NULL — listalid of voiceactingcollaborators (real vendored values).
- `deutschesynchronkarteiactorid`: integer NULL — deutschesynchronkarteiactorid of voiceactingcollaborators (real vendored values).
- `doubanmoviecelebrityid`: integer NULL — doubanmoviecelebrityid of voiceactingcollaborators (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of voiceactingcollaborators (real vendored values).
- `deutschesynchronkarteipersonid`: text NULL — deutschesynchronkarteipersonid of voiceactingcollaborators (real vendored values).
- `characterwritinglanguage`: text NULL — characterwritinglanguage of voiceactingcollaborators (real vendored values).
- business key: charactername

### screenwritersconnectedtobodowolf  (source backend: mongodb)
Source table screenwritersconnectedtobodowolf of the BodoWolfVoiceActingCareer database (21 real rows).

- `screenwritername`: text NOT NULL — screenwritername of screenwritersconnectedtobodowolf (real vendored values).
- `biography`: text NOT NULL — biography of screenwritersconnectedtobodowolf (real vendored values).
- `gender`: text NOT NULL — gender of screenwritersconnectedtobodowolf (real vendored values).
- `profession`: text NOT NULL — profession of screenwritersconnectedtobodowolf (real vendored values).
- `nationality`: text NULL — nationality of screenwritersconnectedtobodowolf (real vendored values).
- `entitytype`: text NOT NULL — entitytype of screenwritersconnectedtobodowolf (real vendored values).
- `freebaseid`: text NULL — freebaseid of screenwritersconnectedtobodowolf (real vendored values).
- `birthdate`: text NULL — birthdate of screenwritersconnectedtobodowolf (real vendored values).
- `imdbid`: text NULL — imdbid of screenwritersconnectedtobodowolf (real vendored values).
- `firstname`: text NULL — firstname of screenwritersconnectedtobodowolf (real vendored values).
- `viafid`: integer NULL — viafid of screenwritersconnectedtobodowolf (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of screenwritersconnectedtobodowolf (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of screenwritersconnectedtobodowolf (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of screenwritersconnectedtobodowolf (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of screenwritersconnectedtobodowolf (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of screenwritersconnectedtobodowolf (real vendored values).
- `portpersonid`: integer NULL — portpersonid of screenwritersconnectedtobodowolf (real vendored values).
- `birthplace`: text NULL — birthplace of screenwritersconnectedtobodowolf (real vendored values).
- `isniidentifier`: text NULL — isniidentifier of screenwritersconnectedtobodowolf (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of screenwritersconnectedtobodowolf (real vendored values).
- `languages`: text NULL — languages of screenwritersconnectedtobodowolf (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of screenwritersconnectedtobodowolf (real vendored values).
- `lastname`: text NULL — lastname of screenwritersconnectedtobodowolf (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of screenwritersconnectedtobodowolf (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of screenwritersconnectedtobodowolf (real vendored values).
- `almamater`: text NULL — almamater of screenwritersconnectedtobodowolf (real vendored values).
- `fandomarticleid`: text NULL — fandomarticleid of screenwritersconnectedtobodowolf (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of screenwritersconnectedtobodowolf (real vendored values).
- business key: screenwritername

### voiceactorprofiles  (source backend: postgres)
Source table voiceactorprofiles of the BodoWolfVoiceActingCareer database (33 real rows).

- `actorname`: text NOT NULL — actorname of voiceactorprofiles (real vendored values).
- `biography`: text NULL — biography of voiceactorprofiles (real vendored values).
- `viafid`: text NULL — viafid of voiceactorprofiles (real vendored values).
- `imdbid`: text NOT NULL — imdbid of voiceactorprofiles (real vendored values).
- `gender`: text NOT NULL — gender of voiceactorprofiles (real vendored values).
- `entitytype`: text NOT NULL — entitytype of voiceactorprofiles (real vendored values).
- `birthdate`: text NULL — birthdate of voiceactorprofiles (real vendored values).
- `profession`: text NULL — profession of voiceactorprofiles (real vendored values).
- `nationality`: text NULL — nationality of voiceactorprofiles (real vendored values).
- `firstname`: text NULL — firstname of voiceactorprofiles (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of voiceactorprofiles (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of voiceactorprofiles (real vendored values).
- `portpersonid`: integer NULL — portpersonid of voiceactorprofiles (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of voiceactorprofiles (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of voiceactorprofiles (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of voiceactorprofiles (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of voiceactorprofiles (real vendored values).
- `birthplace`: text NULL — birthplace of voiceactorprofiles (real vendored values).
- `snacarkid`: text NULL — snacarkid of voiceactorprofiles (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of voiceactorprofiles (real vendored values).
- `lastname`: text NULL — lastname of voiceactorprofiles (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of voiceactorprofiles (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of voiceactorprofiles (real vendored values).
- `fandomarticleid`: text NULL — fandomarticleid of voiceactorprofiles (real vendored values).
- `gndid`: text NULL — gndid of voiceactorprofiles (real vendored values).
- `freebaseid`: text NULL — freebaseid of voiceactorprofiles (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of voiceactorprofiles (real vendored values).
- `moviemeterpersonid`: integer NULL — moviemeterpersonid of voiceactorprofiles (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of voiceactorprofiles (real vendored values).
- `ivipersonid`: text NULL — ivipersonid of voiceactorprofiles (real vendored values).
- `kinoboxpersonid`: integer NOT NULL — kinoboxpersonid of voiceactorprofiles (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of voiceactorprofiles (real vendored values).
- `careerstartyear`: text NULL — careerstartyear of voiceactorprofiles (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of voiceactorprofiles (real vendored values).
- business key: actorname

### startrekcharacters  (source backend: files)
Source table startrekcharacters of the BodoWolfVoiceActingCareer database (17 real rows).

- `charactername`: text NOT NULL — charactername of startrekcharacters (real vendored values).
- `characterdescription`: text NOT NULL — characterdescription of startrekcharacters (real vendored values).
- `gender`: text NOT NULL — gender of startrekcharacters (real vendored values).
- `imdbid`: text NULL — imdbid of startrekcharacters (real vendored values).
- `freebaseid`: text NULL — freebaseid of startrekcharacters (real vendored values).
- `charactertype`: text NOT NULL — charactertype of startrekcharacters (real vendored values).
- `narrativeuniverse`: text NOT NULL — narrativeuniverse of startrekcharacters (real vendored values).
- `appearanceinwork`: text NOT NULL — appearanceinwork of startrekcharacters (real vendored values).
- `roleinnarrative`: text NULL — roleinnarrative of startrekcharacters (real vendored values).
- `militaryrank`: text NULL — militaryrank of startrekcharacters (real vendored values).
- `characteroccupation`: text NULL — characteroccupation of startrekcharacters (real vendored values).
- `fandomarticleid`: text NOT NULL — fandomarticleid of startrekcharacters (real vendored values).
- `comicvineid`: text NULL — comicvineid of startrekcharacters (real vendored values).
- `goodreadscharacterid`: integer NULL — goodreadscharacterid of startrekcharacters (real vendored values).
- `sourcedescription`: text NULL — sourcedescription of startrekcharacters (real vendored values).
- `archenemy`: text NULL — archenemy of startrekcharacters (real vendored values).
- `actorname`: text NULL — actorname of startrekcharacters (real vendored values).
- `franchise`: text NOT NULL — franchise of startrekcharacters (real vendored values).
- `startrekcomdatabaseid`: text NULL — startrekcomdatabaseid of startrekcharacters (real vendored values).
- `personalitydatabaseprofileid`: integer NULL — personalitydatabaseprofileid of startrekcharacters (real vendored values).
- `tradingcarddatabasepersonid`: integer NULL — tradingcarddatabasepersonid of startrekcharacters (real vendored values).
- `firstname`: text NULL — firstname of startrekcharacters (real vendored values).
- `characterresidence`: text NULL — characterresidence of startrekcharacters (real vendored values).
- `gamingwikinetworkarticleid`: text NULL — gamingwikinetworkarticleid of startrekcharacters (real vendored values).
- business key: charactername

### performingartist  (source backend: postgres)
Source table performingartist of the BodoWolfVoiceActingCareer database (15 real rows).

- `fullname`: text NOT NULL — fullname of performingartist (real vendored values).
- `biography`: text NOT NULL — biography of performingartist (real vendored values).
- `gender`: text NOT NULL — gender of performingartist (real vendored values).
- `commonscategory`: text NOT NULL — commonscategory of performingartist (real vendored values).
- `primaryoccupation`: text NOT NULL — primaryoccupation of performingartist (real vendored values).
- `nationality`: text NOT NULL — nationality of performingartist (real vendored values).
- `birthplace`: text NOT NULL — birthplace of performingartist (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of performingartist (real vendored values).
- `viafid`: text NOT NULL — viafid of performingartist (real vendored values).
- `gndid`: integer NULL — gndid of performingartist (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of performingartist (real vendored values).
- `musicbrainzartistid`: text NULL — musicbrainzartistid of performingartist (real vendored values).
- `imdbid`: text NOT NULL — imdbid of performingartist (real vendored values).
- `profileimage`: text NOT NULL — profileimage of performingartist (real vendored values).
- `birthdate`: text NOT NULL — birthdate of performingartist (real vendored values).
- `entitytype`: text NOT NULL — entitytype of performingartist (real vendored values).
- `freebaseid`: text NOT NULL — freebaseid of performingartist (real vendored values).
- `surname`: text NULL — surname of performingartist (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of performingartist (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of performingartist (real vendored values).
- `internetbroadwaydatabasepersonid`: integer NULL — internetbroadwaydatabasepersonid of performingartist (real vendored values).
- `twitterusername`: text NULL — twitterusername of performingartist (real vendored values).
- `commonsgallery`: text NULL — commonsgallery of performingartist (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of performingartist (real vendored values).
- `allmoviepersonid`: text NOT NULL — allmoviepersonid of performingartist (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of performingartist (real vendored values).
- `portpersonid`: integer NULL — portpersonid of performingartist (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of performingartist (real vendored values).
- `nndbpeopleid`: text NULL — nndbpeopleid of performingartist (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of performingartist (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of performingartist (real vendored values).
- `scopedkpersonid`: integer NULL — scopedkpersonid of performingartist (real vendored values).
- `wikitreepersonid`: text NULL — wikitreepersonid of performingartist (real vendored values).
- `firstname`: text NULL — firstname of performingartist (real vendored values).
- `snacarkid`: text NULL — snacarkid of performingartist (real vendored values).
- `careerstartyear`: text NULL — careerstartyear of performingartist (real vendored values).
- `birthname`: text NULL — birthname of performingartist (real vendored values).
- `discogsartistid`: integer NULL — discogsartistid of performingartist (real vendored values).
- `cinemagiapersonid`: integer NULL — cinemagiapersonid of performingartist (real vendored values).
- `tmdbpersonid`: integer NOT NULL — tmdbpersonid of performingartist (real vendored values).
- `almamater`: text NULL — almamater of performingartist (real vendored values).
- `maincategory`: text NULL — maincategory of performingartist (real vendored values).
- `openmediadatabasepersonid`: integer NULL — openmediadatabasepersonid of performingartist (real vendored values).
- `languagesspoken`: text NULL — languagesspoken of performingartist (real vendored values).
- `norafid`: integer NULL — norafid of performingartist (real vendored values).
- `nukatid`: text NULL — nukatid of performingartist (real vendored values).
- `internetspeculativefictiondatabaseauthorid`: integer NULL — internetspeculativefictiondatabaseauthorid of performingartist (real vendored values).
- `rottentomatoesid`: text NULL — rottentomatoesid of performingartist (real vendored values).
- `fandomarticleid`: text NOT NULL — fandomarticleid of performingartist (real vendored values).
- `tradingcarddatabasepersonid`: integer NULL — tradingcarddatabasepersonid of performingartist (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of performingartist (real vendored values).
- `fandangopersonid`: integer NULL — fandangopersonid of performingartist (real vendored values).
- `socialmediafollowers`: integer NULL — socialmediafollowers of performingartist (real vendored values).
- `behindthevoiceactorspersonid`: text NULL — behindthevoiceactorspersonid of performingartist (real vendored values).
- `startrekcomdatabaseid`: text NULL — startrekcomdatabaseid of performingartist (real vendored values).
- `movieplayerpersonid`: integer NULL — movieplayerpersonid of performingartist (real vendored values).
- `filmtvitpersonid`: integer NULL — filmtvitpersonid of performingartist (real vendored values).
- `mymoviespersonid`: integer NULL — mymoviespersonid of performingartist (real vendored values).
- `nationallibraryofkoreaid`: text NULL — nationallibraryofkoreaid of performingartist (real vendored values).
- `plwabnid`: bigint NULL — plwabnid of performingartist (real vendored values).
- `prabookid`: integer NULL — prabookid of performingartist (real vendored values).
- `filmrupersonid`: text NOT NULL — filmrupersonid of performingartist (real vendored values).
- `doubanmoviecelebrityid`: integer NULL — doubanmoviecelebrityid of performingartist (real vendored values).
- `ivipersonid`: text NULL — ivipersonid of performingartist (real vendored values).
- `filmwebplpersonid`: integer NULL — filmwebplpersonid of performingartist (real vendored values).
- `kinoboxpersonid`: integer NOT NULL — kinoboxpersonid of performingartist (real vendored values).
- `listalid`: text NULL — listalid of performingartist (real vendored values).
- `deutschesynchronkarteiactorid`: integer NULL — deutschesynchronkarteiactorid of performingartist (real vendored values).
- `animeconscomguestid`: integer NULL — animeconscomguestid of performingartist (real vendored values).
- `nativelanguage`: text NULL — nativelanguage of performingartist (real vendored values).
- `writinglanguage`: text NULL — writinglanguage of performingartist (real vendored values).
- `deutschesynchronkarteipersonid`: text NULL — deutschesynchronkarteipersonid of performingartist (real vendored values).
- `gamingwikinetworkarticleid`: text NULL — gamingwikinetworkarticleid of performingartist (real vendored values).
- business key: fullname

### voiceactingroles  (source backend: files)
Source table voiceactingroles of the BodoWolfVoiceActingCareer database (16 real rows).

- `rolelabel`: text NOT NULL — rolelabel of voiceactingroles (real vendored values).
- `roledescription`: text NOT NULL — roledescription of voiceactingroles (real vendored values).
- `roletype`: text NOT NULL — roletype of voiceactingroles (real vendored values).
- `rolesubclass`: text NULL — rolesubclass of voiceactingroles (real vendored values).
- `freebaseid`: text NULL — freebaseid of voiceactingroles (real vendored values).
- `commonscategory`: text NULL — commonscategory of voiceactingroles (real vendored values).
- `femaleformofrolelabel`: text NULL — femaleformofrolelabel of voiceactingroles (real vendored values).
- `fieldofoccupation`: text NULL — fieldofoccupation of voiceactingroles (real vendored values).
- `roleimage`: text NULL — roleimage of voiceactingroles (real vendored values).
- `maincategory`: text NULL — maincategory of voiceactingroles (real vendored values).
- `distinctfrom`: text NULL — distinctfrom of voiceactingroles (real vendored values).
- `wordnet31synsetid`: text NULL — wordnet31synsetid of voiceactingroles (real vendored values).
- `maleformofrolelabel`: text NULL — maleformofrolelabel of voiceactingroles (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of voiceactingroles (real vendored values).
- `nationallibraryofisraelj9uid`: bigint NULL — nationallibraryofisraelj9uid of voiceactingroles (real vendored values).
- business key: rolelabel

### characternames  (source backend: rest)
Source table characternames of the BodoWolfVoiceActingCareer database (91 real rows).

- `charactername`: text NOT NULL — charactername of characternames (real vendored values).
- `namedescription`: text NOT NULL — namedescription of characternames (real vendored values).
- `nametype`: text NOT NULL — nametype of characternames (real vendored values).
- `alternatespelling`: text NULL — alternatespelling of characternames (real vendored values).
- `distinctfrom`: text NULL — distinctfrom of characternames (real vendored values).
- `familynameassociation`: text NULL — familynameassociation of characternames (real vendored values).
- `nativespelling`: text NOT NULL — nativespelling of characternames (real vendored values).
- `scripttype`: text NOT NULL — scripttype of characternames (real vendored values).
- `soundexcode`: text NULL — soundexcode of characternames (real vendored values).
- `colognephoneticscode`: text NULL — colognephoneticscode of characternames (real vendored values).
- `caverphonecode`: text NULL — caverphonecode of characternames (real vendored values).
- `nominisgivennameid`: text NULL — nominisgivennameid of characternames (real vendored values).
- `languageassociation`: text NULL — languageassociation of characternames (real vendored values).
- `nameattestation`: text NULL — nameattestation of characternames (real vendored values).
- `commonscategory`: text NULL — commonscategory of characternames (real vendored values).
- `pronunciationaudiofile`: text NULL — pronunciationaudiofile of characternames (real vendored values).
- `nederlandsevoornamenbankid`: text NULL — nederlandsevoornamenbankid of characternames (real vendored values).
- `namedayobserved`: text NULL — namedayobserved of characternames (real vendored values).

### surnamemetadata  (source backend: s3)
Source table surnamemetadata of the BodoWolfVoiceActingCareer database (94 real rows).

- `surname`: text NOT NULL — surname of surnamemetadata (real vendored values).
- `description`: text NOT NULL — description of surnamemetadata (real vendored values).
- `languageoforigin`: text NULL — languageoforigin of surnamemetadata (real vendored values).
- `typeofname`: text NOT NULL — typeofname of surnamemetadata (real vendored values).
- `alternativespelling`: text NULL — alternativespelling of surnamemetadata (real vendored values).
- `writingsystem`: text NULL — writingsystem of surnamemetadata (real vendored values).
- `nativespelling`: text NULL — nativespelling of surnamemetadata (real vendored values).
- `commonscategory`: text NULL — commonscategory of surnamemetadata (real vendored values).
- `digitaldictionaryofsurnamesingermanyid`: integer NULL — digitaldictionaryofsurnamesingermanyid of surnamemetadata (real vendored values).
- `soundexcode`: text NULL — soundexcode of surnamemetadata (real vendored values).
- `colognephoneticscode`: text NULL — colognephoneticscode of surnamemetadata (real vendored values).
- `caverphonecode`: text NULL — caverphonecode of surnamemetadata (real vendored values).
- `equivalentname`: text NULL — equivalentname of surnamemetadata (real vendored values).
- `geopatronymeid`: text NULL — geopatronymeid of surnamemetadata (real vendored values).
- `geneanetfamilynameid`: text NULL — geneanetfamilynameid of surnamemetadata (real vendored values).
- `sourcedescription`: text NULL — sourcedescription of surnamemetadata (real vendored values).
- `wolframentitycode`: text NULL — wolframentitycode of surnamemetadata (real vendored values).

### spouse  (source backend: s3)
Source table spouse of the BodoWolfVoiceActingCareer database (20 real rows).

- `fullname`: text NOT NULL — fullname of spouse (real vendored values).
- `biography`: text NOT NULL — biography of spouse (real vendored values).
- `gender`: text NOT NULL — gender of spouse (real vendored values).
- `profession`: text NOT NULL — profession of spouse (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of spouse (real vendored values).
- `viafid`: text NULL — viafid of spouse (real vendored values).
- `nationality`: text NOT NULL — nationality of spouse (real vendored values).
- `birthplace`: text NOT NULL — birthplace of spouse (real vendored values).
- `imdbid`: text NOT NULL — imdbid of spouse (real vendored values).
- `birthdate`: text NOT NULL — birthdate of spouse (real vendored values).
- `entitytype`: text NOT NULL — entitytype of spouse (real vendored values).
- `partner`: text NOT NULL — partner of spouse (real vendored values).
- `freebaseid`: text NULL — freebaseid of spouse (real vendored values).
- `firstname`: text NOT NULL — firstname of spouse (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of spouse (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of spouse (real vendored values).
- `wikitreepersonid`: text NULL — wikitreepersonid of spouse (real vendored values).
- `careerstartyear`: text NULL — careerstartyear of spouse (real vendored values).
- `surname`: text NULL — surname of spouse (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of spouse (real vendored values).
- `languages`: text NULL — languages of spouse (real vendored values).
- `primarylanguage`: text NULL — primarylanguage of spouse (real vendored values).
- `writinglanguage`: text NULL — writinglanguage of spouse (real vendored values).
- `cinemagiapersonid`: integer NULL — cinemagiapersonid of spouse (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of spouse (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of spouse (real vendored values).
- `almamater`: text NULL — almamater of spouse (real vendored values).
- `prabookid`: integer NULL — prabookid of spouse (real vendored values).

### Relationships

- startrekcharacters(actorname) -> performingartist(fullname) [optional (may be NULL/dangling)]
- startrekcharacters(characteroccupation) -> voiceactingroles(rolelabel) [optional (may be NULL/dangling)]
- startrekepisodesfeaturingbodowolf(director) -> voiceactorprofiles(actorname) [required]
- startrekepisodesfeaturingbodowolf(leadactor) -> voiceactingcollaborators(charactername) [optional (may be NULL/dangling)]
- startrekepisodesfeaturingbodowolf(maincharacter) -> startrekcharacters(charactername) [optional (may be NULL/dangling)]
- startrekepisodesfeaturingbodowolf(screenwriter) -> screenwritersconnectedtobodowolf(screenwritername) [optional (may be NULL/dangling)]
- startrekepisodesfeaturingbodowolf(voiceactor) -> voiceactors(actorname) [required]

