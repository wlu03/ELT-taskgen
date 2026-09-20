# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Lucianosalcefilmography

## Specification

PROJECT OVERVIEW — LucianoSalceFilmography

This project builds two marts from the LucianoSalceFilmography database. Eight source tables are available, and each must be extracted from its own backend.

Source tables and their extraction backends:
- Source table filmographydetails must be extracted from the files backend. It holds one record per film with columns filmtitle, filmdescription, imdbid, originallanguage, mediatype, directorname, castmembers, screenwriters, directorofphotography, producers, narrativelocation, freebaseid, releasedate, moviemeterfilmid, countryoforigin, originaltitle, musiccomposer, filmduration, productiondesigner, ofdbfilmid, filmcolor, elfilmfilmid, kinopoiskfilmid, csfdfilmid, allmovietitleid, portfilmid, eidrcontentid, filmaffinityfilmid, filmeditor, filmgenre, letterboxdfilmid, doubanfilmid, tmdbmovieid, lumierefilmid, trakttvid, kinoboxfilmid, plexmediakey and filmvandaagid.
- Source table cinematographers must be extracted from the mongodb backend. Its business key is name.
- Source table filmindustryprofessionals must be extracted from the rest backend. Its business key is fullname.
- Source table productiondesigner must be extracted from the s3 backend. Its business key is fullname.
- Source table filmcomposers must be extracted from the files backend. Its business key is composername.
- Source table filmographycastandcrew must be extracted from the postgres backend. Its business key is fullname.
- Source table lucianosalcegivennames must be extracted from the postgres backend.
- Source table filmographyfamilynames must be extracted from the rest backend.

Relationships between the source tables. Each is declared exactly as follows:
- Child table filmographydetails with key castmembers refers to parent table filmographycastandcrew with key fullname; this relationship is optional (the child value may be NULL or dangling).
- Child table filmographydetails with key directorofphotography refers to parent table cinematographers with key name; this relationship is optional (the child value may be NULL or dangling).
- Child table filmographydetails with key musiccomposer refers to parent table filmcomposers with key composername; this relationship is optional (the child value may be NULL or dangling).
- Child table filmographydetails with key productiondesigner refers to parent table productiondesigner with key fullname; this relationship is optional (the child value may be NULL or dangling).
- Child table filmographydetails with key screenwriters refers to parent table filmindustryprofessionals with key fullname; this relationship is optional (the child value may be NULL or dangling).

Throughout both marts, a filmographydetails row is said to be linked to a productiondesigner row when the filmographydetails productiondesigner value matches the productiondesigner fullname value.

=== Mart productiondesigner_filmographydetails_rollup ===
This mart is a per-productiondesigner roll-up of linked filmographydetails rows in the LucianoSalceFilmography database, following the link on to cinematographers.

Grain: one row per productiondesigner (fullname), INCLUDING productiondesigner rows with no linked filmographydetails rows.

Key column: parent_key.

Rule 1: the source table productiondesigner is read in full.

Rule 2: the source table filmographydetails is read in full.

Rule 3: the source table cinematographers is read in full.

Rule 4: filmographydetails declares no primary key upstream, so byte-identical duplicate rows can occur in filmographydetails; every such row counts ONCE, however many copies arrive, where two rows are the same when they agree on kinopoiskfilmid, productiondesigner, directorofphotography, filmcolor and filmduration.

Rule 5: there is one row per productiondesigner row, keyed by fullname; that row carries parent_key and parent_name from productiondesigner.

Rule 6 (Hop 1): filmographydetails rows are brought in against the grain by matching the filmographydetails column productiondesigner to the productiondesigner column fullname, which is the parent_key; one productiondesigner row may have many filmographydetails rows, and a productiondesigner row with no filmographydetails rows at all is RETAINED. Preservation is left-sided, so the productiondesigner side keeps all of its rows.

Rule 7 (Hop 2): cinematographers rows are brought in by matching each linked filmographydetails row's directorofphotography to the name of a cinematographers row. A filmographydetails row whose cinematographers row is missing still counts as a link and is RETAINED, so preservation is left-sided on the filmographydetails side, and directorofphotography and name are carried.

Rule 8: there is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 9: the mart columns are named parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount; total_amount, active_amount and max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount and max_amount the default also applies to a group none of whose real rows carries an input value.

Rule 10 (guarded ratio): active_amount_ratio is active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0 or has no value; it is carried beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount and max_amount.

Rule 11: size_band is the size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5, so a value exactly at 2 is 'small' and a value exactly at 5 is 'medium'; every value falls in exactly one band, and size_band is carried beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount and active_amount_ratio.

Rule 12: has_links is 'yes' when this parent has at least one link and 'no' otherwise, so a value exactly at 0 links is 'no'; it is never NULL and is carried beside parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio and size_band.

Rule 13: the deterministic output order is ascending parent_key.

Output columns of productiondesigner_filmographydetails_rollup:
- parent_key (text): identifier of the productiondesigner row; one row per value.
- parent_name (text): biography of the productiondesigner row, copied unchanged.
- link_count (bigint): number of DISTINCT filmographydetails rows linked to this productiondesigner row; 0 when there are none. filmographydetails declares no primary key upstream and byte-identical duplicate rows occur in the source; they count ONCE.
- distinct_child_count (bigint): number of unique cinematographers rows reached through those links. Two links pointing at the same child count ONCE. It is 0 when there are no links. A link whose cinematographers row is missing reaches no cinematographers row and adds nothing to this count.
- active_link_count (bigint): number of linked filmographydetails rows whose filmcolor is one of ['black-and-white'], each counted once even if the row repeats. A parent whose links ALL fail that test reports 0, not a missing row.
- total_amount (integer): total of filmduration over every unique linked row; 0 when there are no links, and 0 when none of the linked rows carries a filmduration value.
- active_amount (integer): total of filmduration over links whose filmcolor is one of ['black-and-white'], each counted once even if the row repeats; 0 when none qualify, and 0 when every qualifying row lacks a filmduration value.
- max_amount (integer): largest filmduration among the linked rows; 0 when there are no links, and 0 when none of the linked rows carries a filmduration value.
- active_amount_ratio (float): active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0.
- size_band (text): size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band.
- has_links (text): 'yes' when this parent has at least one link, 'no' otherwise. Never NULL.

=== Mart productiondesigner_filmographydetails_cohorts ===
This mart is a per-(productiondesigner, observed status cohort) summary of linked filmographydetails rows in the LucianoSalceFilmography database.

Grain: one row per (fullname, status cohort) pair represented among linked filmographydetails rows, plus one no-activity row for a productiondesigner row with no linked filmographydetails row at all. A productiondesigner row whose linked filmographydetails rows all lack a filmcolor value is in no cohort and gets no no-activity row, so it has no row in this mart.

Key columns: entity_key and cohort.

Rule 1: the source table productiondesigner is read in full.

Rule 2: the source table filmographydetails is read in full.

Rule 3: each productiondesigner fullname is carried into the cohort calculation as entity_key, and its biography is carried as entity_name.

Rule 3a (no de-duplication in this mart): unlike productiondesigner_filmographydetails_rollup, this mart has no dedupe step; the source table filmographydetails is read in full, so when byte-identical duplicate filmographydetails rows occur (rows agreeing on kinopoiskfilmid, productiondesigner, directorofphotography, filmcolor and filmduration), EVERY copy is a separate linked row here. Each copy counts separately in link_count, in the filmduration total_amount and in the largest filmduration max_amount of its entity/cohort cell, and therefore in max_amount_share; distinct_status_count counts how many different filmcolor values occur in the cell, so repeated copies do not raise it.

Rule 4: the linked filmographydetails rows are brought into each productiondesigner entity before status cohorts are assigned, matching the filmographydetails column productiondesigner to the productiondesigner column fullname, which is the entity_key, and carrying entity_key and entity_name. Preservation is left-sided, so every productiondesigner entity is kept even when no filmographydetails row matches it.

Rule 5: among those rows, the ones kept for the passing cohort are the rows whose filmcolor belongs to the passing cohort values ['black-and-white'], carrying entity_key and entity_name.

Rule 6: there is one row per productiondesigner entity that has at least one linked filmographydetails row in the passing cohort, reporting as entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount the number of those rows, how many different filmcolor values occur among them, the total of their filmduration, and their largest filmduration. The total and the largest value read only the rows that carry a filmduration value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 7: for those passing-cohort rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it is carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.

Rule 8: these measures are labelled with cohort 'passing', carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 9: the rows kept for the failing cohort are the rows whose filmcolor belongs to the failing cohort values ['color'], carrying entity_key and entity_name.

Rule 10: there is one row per productiondesigner entity that has at least one linked filmographydetails row in the failing cohort, reporting as entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount the number of those rows, how many different filmcolor values occur among them, the total of their filmduration, and their largest filmduration. The total and the largest value read only the rows that carry a filmduration value; a cohort whose rows all lack one reports 0 for both, never empty.

Rule 11: for those failing-cohort rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it is carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.

Rule 12: these measures are labelled with cohort 'failing', carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 13: the placeholder row is kept for a productiondesigner entity with no linked filmographydetails row at all, carrying entity_key and entity_name; a productiondesigner entity that has linked filmographydetails rows gets no placeholder, even when every one of those rows lacks a filmcolor value.

Rule 14: there is one row per productiondesigner entity with no linked filmographydetails row at all, reporting as entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount 0 rows, 0 different filmcolor values, a filmduration total of 0 and a largest filmduration of 0.

Rule 15: for those placeholder rows, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it is carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount.

Rule 16: these measures are labelled with cohort 'no_activity', carried beside entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 17: the disjoint passing and failing cohort summaries are combined, all rows of each being retained, carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 18: the no-activity summaries are added to that combination, all such rows retained, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

Rule 19: the deterministic output order is ascending entity_key, then ascending cohort.

Output columns of productiondesigner_filmographydetails_cohorts:
- entity_key (text): identifier of the productiondesigner row.
- cohort (text): 'passing' for filmcolor values ['black-and-white']; 'failing' for values ['color']; 'no_activity' when the productiondesigner row has no linked filmographydetails row. A linked filmographydetails row whose filmcolor has no value belongs to no cohort: it is not counted in any cell, and it does not make the productiondesigner row 'no_activity'.
- entity_name (text): biography of the productiondesigner row, copied unchanged.
- link_count (bigint): number of filmographydetails rows in this entity/cohort cell.
- distinct_status_count (bigint): number of distinct filmcolor values represented in this cell.
- total_amount (integer): total of filmduration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an filmduration value.
- max_amount (integer): largest filmduration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an filmduration value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `productiondesigner_filmographydetails_rollup`

- Grain: One row per productiondesigner (fullname), INCLUDING productiondesigner rows with no linked filmographydetails rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links

```text
Mart 'productiondesigner_filmographydetails_rollup' has 13 declared semantic rules:
1. [source] Read source table productiondesigner. (public source tables: productiondesigner)
2. [source] Read source table filmographydetails. (public source tables: filmographydetails)
3. [source] Read source table cinematographers. (public source tables: cinematographers)
4. [dedupe] filmographydetails declares no primary key upstream, so byte-identical duplicate rows can occur; every such row counts ONCE, however many copies arrive. (public source tables: filmographydetails | public carried/output columns: kinopoiskfilmid, productiondesigner, directorofphotography, filmcolor, filmduration)
5. [derive] One row per productiondesigner row, keyed by fullname. (public source tables: productiondesigner | public carried/output columns: parent_key, parent_name)
6. [join] Hop 1: bring in filmographydetails against the grain. One productiondesigner row may have many filmographydetails rows, and a productiondesigner row with no filmographydetails rows at all is RETAINED. (public source tables: filmographydetails | public carried/output columns: productiondesigner, fullname | join preservation: left | condition public identifiers: filmographydetails, productiondesigner, parent_key)
7. [join] Hop 2: bring in cinematographers, matching each linked filmographydetails row's directorofphotography to the name of a cinematographers row. A filmographydetails row whose cinematographers row is missing still counts as a link and is RETAINED. (public source tables: cinematographers | public carried/output columns: name, directorofphotography | join preservation: left | condition public identifiers: cinematographers, name)
8. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
9. [derive] Name the mart columns; total_amount, active_amount, max_amount report their declared defaults — never NULL — for a group with no matching rows. For total_amount, active_amount, max_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount)
10. [ratio] Guarded ratios: active_amount_ratio — active_amount divided by total_amount, expressed as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and reported as 0.0 when total_amount is 0. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] size_band — Size band of link_count: 'none' at exactly 0, 'small' for 1-2 INCLUSIVE of 2, 'medium' for 3-5 INCLUSIVE of 5, 'large' above 5. Every value falls in exactly one band. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band | semantic parameters: boundary=a value exactly at 2 is 'small'; a value exactly at 5 is 'medium')
12. [conditional] has_links — 'yes' when this parent has at least one link, 'no' otherwise. Never NULL. (public carried/output columns: parent_key, parent_name, link_count, distinct_child_count, active_link_count, total_amount, active_amount, max_amount, active_amount_ratio, size_band, has_links | semantic parameters: boundary=a value exactly at 0 is 'no')
13. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `productiondesigner_filmographydetails_cohorts`

- Grain: One row per (fullname, status cohort) pair represented among linked filmographydetails rows, plus one no-activity row for a productiondesigner row with no linked filmographydetails row at all. A productiondesigner row whose linked filmographydetails rows all lack a filmcolor value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'productiondesigner_filmographydetails_cohorts' has 19 declared semantic rules:
1. [source] Read source table productiondesigner. (public source tables: productiondesigner)
2. [source] Read source table filmographydetails. (public source tables: filmographydetails)
3. [derive] Carry each fullname and its biography into the cohort calculation. (public source tables: productiondesigner | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked filmographydetails rows into each productiondesigner entity before assigning status cohorts. (public source tables: filmographydetails | public carried/output columns: entity_key, entity_name, fullname, productiondesigner | join preservation: left | condition public identifiers: filmographydetails, productiondesigner, entity_key)
5. [filter] Keep rows whose filmcolor belongs to the passing cohort values ['black-and-white']. (public carried/output columns: entity_key, entity_name | condition literal specification values: black-and-white)
6. [distinct] One row per productiondesigner entity that has at least one linked filmographydetails row in the passing cohort, reporting the number of those rows, how many different filmcolor values occur among them, the total of their filmduration, and their largest filmduration. The total and the largest value read only the rows that carry a filmduration value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose filmcolor belongs to the failing cohort values ['color']. (public carried/output columns: entity_key, entity_name | condition literal specification values: color)
10. [distinct] One row per productiondesigner entity that has at least one linked filmographydetails row in the failing cohort, reporting the number of those rows, how many different filmcolor values occur among them, the total of their filmduration, and their largest filmduration. The total and the largest value read only the rows that carry a filmduration value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a productiondesigner entity with no linked filmographydetails row at all; a productiondesigner entity that has linked filmographydetails rows gets no placeholder, even when every one of those rows lacks a filmcolor value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per productiondesigner entity with no linked filmographydetails row at all, reporting 0 rows, 0 different filmcolor values, a filmduration total of 0 and a largest filmduration of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

## Source tables

### filmographydetails  (source backend: files)
Source table filmographydetails of the LucianoSalceFilmography database (35 real rows).

- `filmtitle`: text NOT NULL — filmtitle of filmographydetails (real vendored values).
- `filmdescription`: text NOT NULL — filmdescription of filmographydetails (real vendored values).
- `imdbid`: text NOT NULL — imdbid of filmographydetails (real vendored values).
- `originallanguage`: text NULL — originallanguage of filmographydetails (real vendored values).
- `mediatype`: text NOT NULL — mediatype of filmographydetails (real vendored values).
- `directorname`: text NOT NULL — directorname of filmographydetails (real vendored values).
- `castmembers`: text NULL — castmembers of filmographydetails (real vendored values).
- `screenwriters`: text NULL — screenwriters of filmographydetails (real vendored values).
- `directorofphotography`: text NULL — directorofphotography of filmographydetails (real vendored values).
- `producers`: text NULL — producers of filmographydetails (real vendored values).
- `narrativelocation`: text NULL — narrativelocation of filmographydetails (real vendored values).
- `freebaseid`: text NULL — freebaseid of filmographydetails (real vendored values).
- `releasedate`: text NOT NULL — releasedate of filmographydetails (real vendored values).
- `moviemeterfilmid`: integer NULL — moviemeterfilmid of filmographydetails (real vendored values).
- `countryoforigin`: text NOT NULL — countryoforigin of filmographydetails (real vendored values).
- `originaltitle`: text NULL — originaltitle of filmographydetails (real vendored values).
- `musiccomposer`: text NULL — musiccomposer of filmographydetails (real vendored values).
- `filmduration`: integer NOT NULL — filmduration of filmographydetails (real vendored values).
- `productiondesigner`: text NULL — productiondesigner of filmographydetails (real vendored values).
- `ofdbfilmid`: integer NULL — ofdbfilmid of filmographydetails (real vendored values).
- `filmcolor`: text NOT NULL — filmcolor of filmographydetails (real vendored values).
- `elfilmfilmid`: integer NULL — elfilmfilmid of filmographydetails (real vendored values).
- `kinopoiskfilmid`: integer NOT NULL — kinopoiskfilmid of filmographydetails (real vendored values).
- `csfdfilmid`: integer NOT NULL — csfdfilmid of filmographydetails (real vendored values).
- `allmovietitleid`: text NULL — allmovietitleid of filmographydetails (real vendored values).
- `portfilmid`: integer NULL — portfilmid of filmographydetails (real vendored values).
- `eidrcontentid`: text NULL — eidrcontentid of filmographydetails (real vendored values).
- `filmaffinityfilmid`: integer NULL — filmaffinityfilmid of filmographydetails (real vendored values).
- `filmeditor`: text NULL — filmeditor of filmographydetails (real vendored values).
- `filmgenre`: text NOT NULL — filmgenre of filmographydetails (real vendored values).
- `letterboxdfilmid`: text NOT NULL — letterboxdfilmid of filmographydetails (real vendored values).
- `doubanfilmid`: integer NULL — doubanfilmid of filmographydetails (real vendored values).
- `tmdbmovieid`: integer NOT NULL — tmdbmovieid of filmographydetails (real vendored values).
- `lumierefilmid`: integer NULL — lumierefilmid of filmographydetails (real vendored values).
- `trakttvid`: text NULL — trakttvid of filmographydetails (real vendored values).
- `kinoboxfilmid`: integer NOT NULL — kinoboxfilmid of filmographydetails (real vendored values).
- `plexmediakey`: text NULL — plexmediakey of filmographydetails (real vendored values).
- `filmvandaagid`: text NULL — filmvandaagid of filmographydetails (real vendored values).

### cinematographers  (source backend: mongodb)
Source table cinematographers of the LucianoSalceFilmography database (16 real rows).

- `name`: text NOT NULL — name of cinematographers (real vendored values).
- `biography`: text NOT NULL — biography of cinematographers (real vendored values).
- `nationality`: text NOT NULL — nationality of cinematographers (real vendored values).
- `birthplace`: text NULL — birthplace of cinematographers (real vendored values).
- `viafid`: integer NULL — viafid of cinematographers (real vendored values).
- `internationalstandardnameidentifierisni`: text NULL — internationalstandardnameidentifierisni of cinematographers (real vendored values).
- `imdbid`: text NOT NULL — imdbid of cinematographers (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of cinematographers (real vendored values).
- `gndid`: text NULL — gndid of cinematographers (real vendored values).
- `idrefid`: text NULL — idrefid of cinematographers (real vendored values).
- `placeofdeath`: text NULL — placeofdeath of cinematographers (real vendored values).
- `entitytype`: text NOT NULL — entitytype of cinematographers (real vendored values).
- `profession`: text NOT NULL — profession of cinematographers (real vendored values).
- `birthdate`: text NULL — birthdate of cinematographers (real vendored values).
- `deathdate`: text NULL — deathdate of cinematographers (real vendored values).
- `freebaseid`: text NULL — freebaseid of cinematographers (real vendored values).
- `firstname`: text NULL — firstname of cinematographers (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of cinematographers (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of cinematographers (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of cinematographers (real vendored values).
- `sbnauthorid`: text NULL — sbnauthorid of cinematographers (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of cinematographers (real vendored values).
- `portpersonid`: integer NULL — portpersonid of cinematographers (real vendored values).
- `scopedkpersonid`: integer NULL — scopedkpersonid of cinematographers (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of cinematographers (real vendored values).
- `csfdpersonid`: integer NOT NULL — csfdpersonid of cinematographers (real vendored values).
- `danishnationalfilmographypersonid`: integer NULL — danishnationalfilmographypersonid of cinematographers (real vendored values).
- `filmportalid`: text NULL — filmportalid of cinematographers (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of cinematographers (real vendored values).
- `cinematografoitnameorcompanyid`: integer NULL — cinematografoitnameorcompanyid of cinematographers (real vendored values).
- `conorsiid`: integer NULL — conorsiid of cinematographers (real vendored values).
- `gender`: text NOT NULL — gender of cinematographers (real vendored values).
- `nlcrautid`: text NULL — nlcrautid of cinematographers (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of cinematographers (real vendored values).
- `deutschebiographiegndid`: integer NULL — deutschebiographiegndid of cinematographers (real vendored values).
- `languages`: text NOT NULL — languages of cinematographers (real vendored values).
- `plwabnid`: bigint NULL — plwabnid of cinematographers (real vendored values).
- `tmdbpersonid`: integer NOT NULL — tmdbpersonid of cinematographers (real vendored values).
- `surname`: text NULL — surname of cinematographers (real vendored values).
- `relatedcategory`: text NULL — relatedcategory of cinematographers (real vendored values).
- `prabookid`: integer NULL — prabookid of cinematographers (real vendored values).
- `nationallibraryofisraelj9uid`: bigint NULL — nationallibraryofisraelj9uid of cinematographers (real vendored values).
- `nukatid`: text NULL — nukatid of cinematographers (real vendored values).
- `cinematografoitnameorcompanyidnew`: text NULL — cinematografoitnameorcompanyidnew of cinematographers (real vendored values).
- `moviewalkerpresspersonid`: integer NULL — moviewalkerpresspersonid of cinematographers (real vendored values).
- `ivipersonid`: text NULL — ivipersonid of cinematographers (real vendored values).
- `kinoboxpersonid`: integer NOT NULL — kinoboxpersonid of cinematographers (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of cinematographers (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of cinematographers (real vendored values).
- business key: name

### filmindustryprofessionals  (source backend: rest)
Source table filmindustryprofessionals of the LucianoSalceFilmography database (23 real rows).

- `fullname`: text NOT NULL — fullname of filmindustryprofessionals (real vendored values).
- `biography`: text NOT NULL — biography of filmindustryprofessionals (real vendored values).
- `primaryoccupation`: text NULL — primaryoccupation of filmindustryprofessionals (real vendored values).
- `viafid`: integer NULL — viafid of filmindustryprofessionals (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of filmindustryprofessionals (real vendored values).
- `nationality`: text NULL — nationality of filmindustryprofessionals (real vendored values).
- `commonscategory`: text NULL — commonscategory of filmindustryprofessionals (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of filmindustryprofessionals (real vendored values).
- `gndid`: text NULL — gndid of filmindustryprofessionals (real vendored values).
- `imdbid`: text NULL — imdbid of filmindustryprofessionals (real vendored values).
- `profileimage`: text NULL — profileimage of filmindustryprofessionals (real vendored values).
- `birthdate`: text NULL — birthdate of filmindustryprofessionals (real vendored values).
- `deathdate`: text NULL — deathdate of filmindustryprofessionals (real vendored values).
- `deathplace`: text NULL — deathplace of filmindustryprofessionals (real vendored values).
- `entitytype`: text NOT NULL — entitytype of filmindustryprofessionals (real vendored values).
- `freebaseid`: text NOT NULL — freebaseid of filmindustryprofessionals (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of filmindustryprofessionals (real vendored values).
- `idrefid`: text NULL — idrefid of filmindustryprofessionals (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of filmindustryprofessionals (real vendored values).
- `firstname`: text NULL — firstname of filmindustryprofessionals (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of filmindustryprofessionals (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of filmindustryprofessionals (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of filmindustryprofessionals (real vendored values).
- `portpersonid`: integer NULL — portpersonid of filmindustryprofessionals (real vendored values).
- `fastid`: integer NULL — fastid of filmindustryprofessionals (real vendored values).
- `filmportalid`: text NULL — filmportalid of filmindustryprofessionals (real vendored values).
- `danishnationalfilmographypersonid`: integer NULL — danishnationalfilmographypersonid of filmindustryprofessionals (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of filmindustryprofessionals (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of filmindustryprofessionals (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of filmindustryprofessionals (real vendored values).
- `languages`: text NULL — languages of filmindustryprofessionals (real vendored values).
- `cinematografoitnameorcompanyid`: integer NULL — cinematografoitnameorcompanyid of filmindustryprofessionals (real vendored values).
- `nationalethesaurusvoorauteursnamenid`: text NULL — nationalethesaurusvoorauteursnamenid of filmindustryprofessionals (real vendored values).
- `lastname`: text NULL — lastname of filmindustryprofessionals (real vendored values).
- `gender`: text NULL — gender of filmindustryprofessionals (real vendored values).
- `sbnauthorid`: text NULL — sbnauthorid of filmindustryprofessionals (real vendored values).
- `careerstartyear`: text NULL — careerstartyear of filmindustryprofessionals (real vendored values).
- `relatedfilmcategory`: text NULL — relatedfilmcategory of filmindustryprofessionals (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of filmindustryprofessionals (real vendored values).
- `deutschebiographiegndid`: text NULL — deutschebiographiegndid of filmindustryprofessionals (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of filmindustryprofessionals (real vendored values).
- `nationallibraryofisraelj9uid`: bigint NULL — nationallibraryofisraelj9uid of filmindustryprofessionals (real vendored values).
- `plwabnid`: bigint NULL — plwabnid of filmindustryprofessionals (real vendored values).
- `cinemathequequebecoisepersonid`: integer NULL — cinemathequequebecoisepersonid of filmindustryprofessionals (real vendored values).
- `sharecatalogueauthorid`: integer NULL — sharecatalogueauthorid of filmindustryprofessionals (real vendored values).
- `moviemeterpersonid`: integer NULL — moviemeterpersonid of filmindustryprofessionals (real vendored values).
- `cinematografoitnameorcompanyidnew`: text NULL — cinematografoitnameorcompanyidnew of filmindustryprofessionals (real vendored values).
- `birthplace`: text NULL — birthplace of filmindustryprofessionals (real vendored values).
- `ivipersonid`: text NULL — ivipersonid of filmindustryprofessionals (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of filmindustryprofessionals (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of filmindustryprofessionals (real vendored values).
- `kinenotepersonid`: integer NULL — kinenotepersonid of filmindustryprofessionals (real vendored values).
- business key: fullname

### productiondesigner  (source backend: s3)
Source table productiondesigner of the LucianoSalceFilmography database (16 real rows).

- `fullname`: text NOT NULL — fullname of productiondesigner (real vendored values).
- `biography`: text NOT NULL — biography of productiondesigner (real vendored values).
- `viafid`: integer NULL — viafid of productiondesigner (real vendored values).
- `isniidentifier`: text NULL — isniidentifier of productiondesigner (real vendored values).
- `gndid`: integer NULL — gndid of productiondesigner (real vendored values).
- `imdbid`: text NOT NULL — imdbid of productiondesigner (real vendored values).
- `birthdate`: text NULL — birthdate of productiondesigner (real vendored values).
- `entitytype`: text NOT NULL — entitytype of productiondesigner (real vendored values).
- `birthplace`: text NULL — birthplace of productiondesigner (real vendored values).
- `freebaseid`: text NOT NULL — freebaseid of productiondesigner (real vendored values).
- `citizenship`: text NOT NULL — citizenship of productiondesigner (real vendored values).
- `profession`: text NOT NULL — profession of productiondesigner (real vendored values).
- `firstname`: text NOT NULL — firstname of productiondesigner (real vendored values).
- `filmportalid`: text NULL — filmportalid of productiondesigner (real vendored values).
- `sbnauthorid`: text NULL — sbnauthorid of productiondesigner (real vendored values).
- `gender`: text NOT NULL — gender of productiondesigner (real vendored values).
- `lastname`: text NOT NULL — lastname of productiondesigner (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of productiondesigner (real vendored values).
- `deutschebiographiegndid`: integer NULL — deutschebiographiegndid of productiondesigner (real vendored values).
- `languages`: text NOT NULL — languages of productiondesigner (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of productiondesigner (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of productiondesigner (real vendored values).
- `kinoboxpersonid`: integer NOT NULL — kinoboxpersonid of productiondesigner (real vendored values).
- `deathdate`: text NULL — deathdate of productiondesigner (real vendored values).
- `deathplace`: text NULL — deathplace of productiondesigner (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of productiondesigner (real vendored values).
- business key: fullname

### filmcomposers  (source backend: files)
Source table filmcomposers of the LucianoSalceFilmography database (18 real rows).

- `composername`: text NOT NULL — composername of filmcomposers (real vendored values).
- `biography`: text NOT NULL — biography of filmcomposers (real vendored values).
- `nationality`: text NOT NULL — nationality of filmcomposers (real vendored values).
- `viafid`: text NOT NULL — viafid of filmcomposers (real vendored values).
- `gndid`: text NULL — gndid of filmcomposers (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of filmcomposers (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of filmcomposers (real vendored values).
- `commonscategory`: text NULL — commonscategory of filmcomposers (real vendored values).
- `birthplace`: text NOT NULL — birthplace of filmcomposers (real vendored values).
- `musicbrainzartistid`: text NOT NULL — musicbrainzartistid of filmcomposers (real vendored values).
- `imdbid`: text NOT NULL — imdbid of filmcomposers (real vendored values).
- `idrefid`: text NULL — idrefid of filmcomposers (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of filmcomposers (real vendored values).
- `birthdate`: text NOT NULL — birthdate of filmcomposers (real vendored values).
- `nlcrautid`: text NULL — nlcrautid of filmcomposers (real vendored values).
- `sbnauthorid`: text NULL — sbnauthorid of filmcomposers (real vendored values).
- `profileimage`: text NULL — profileimage of filmcomposers (real vendored values).
- `entitytype`: text NOT NULL — entitytype of filmcomposers (real vendored values).
- `personalwebsite`: text NULL — personalwebsite of filmcomposers (real vendored values).
- `freebaseid`: text NOT NULL — freebaseid of filmcomposers (real vendored values).
- `primaryoccupation`: text NOT NULL — primaryoccupation of filmcomposers (real vendored values).
- `firstname`: text NOT NULL — firstname of filmcomposers (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of filmcomposers (real vendored values).
- `allmusicartistid`: text NULL — allmusicartistid of filmcomposers (real vendored values).
- `discogsartistid`: integer NOT NULL — discogsartistid of filmcomposers (real vendored values).
- `languages`: text NOT NULL — languages of filmcomposers (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of filmcomposers (real vendored values).
- `filmportalid`: text NULL — filmportalid of filmcomposers (real vendored values).
- `danishnationalfilmographypersonid`: integer NULL — danishnationalfilmographypersonid of filmcomposers (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of filmcomposers (real vendored values).
- `portpersonid`: integer NULL — portpersonid of filmcomposers (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of filmcomposers (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of filmcomposers (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of filmcomposers (real vendored values).
- `musicalinstrument`: text NULL — musicalinstrument of filmcomposers (real vendored values).
- `carnegiehallagentid`: integer NULL — carnegiehallagentid of filmcomposers (real vendored values).
- `cinematografoitnameorcompanyid`: integer NULL — cinematografoitnameorcompanyid of filmcomposers (real vendored values).
- `tmdbpersonid`: integer NOT NULL — tmdbpersonid of filmcomposers (real vendored values).
- `nationaldiscographyofitaliansongartistgroupid`: integer NULL — nationaldiscographyofitaliansongartistgroupid of filmcomposers (real vendored values).
- `gtaaid`: integer NULL — gtaaid of filmcomposers (real vendored values).
- `gender`: text NOT NULL — gender of filmcomposers (real vendored values).
- `lastname`: text NULL — lastname of filmcomposers (real vendored values).
- `ipinumber`: text NULL — ipinumber of filmcomposers (real vendored values).
- `europeanaentity`: text NULL — europeanaentity of filmcomposers (real vendored values).
- `copyrightagency`: text NULL — copyrightagency of filmcomposers (real vendored values).
- `cobisauthorid`: text NULL — cobisauthorid of filmcomposers (real vendored values).
- `deutschebiographiegndid`: text NULL — deutschebiographiegndid of filmcomposers (real vendored values).
- `deathdate`: text NULL — deathdate of filmcomposers (real vendored values).
- `deathplace`: text NULL — deathplace of filmcomposers (real vendored values).
- `plwabnid`: bigint NULL — plwabnid of filmcomposers (real vendored values).
- `muziekwebperformerid`: text NULL — muziekwebperformerid of filmcomposers (real vendored values).
- `nationallibraryofisraelj9uid`: bigint NULL — nationallibraryofisraelj9uid of filmcomposers (real vendored values).
- `kantoid`: text NULL — kantoid of filmcomposers (real vendored values).
- `cinemathequequebecoisepersonid`: integer NULL — cinemathequequebecoisepersonid of filmcomposers (real vendored values).
- `universalmusicfranceartistid`: bigint NULL — universalmusicfranceartistid of filmcomposers (real vendored values).
- `cinematografoitnameorcompanyidnew`: text NULL — cinematografoitnameorcompanyidnew of filmcomposers (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of filmcomposers (real vendored values).
- `kinoboxpersonid`: integer NOT NULL — kinoboxpersonid of filmcomposers (real vendored values).
- `rateyourmusicartistid`: text NULL — rateyourmusicartistid of filmcomposers (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of filmcomposers (real vendored values).
- business key: composername

### filmographycastandcrew  (source backend: postgres)
Source table filmographycastandcrew of the LucianoSalceFilmography database (21 real rows).

- `fullname`: text NOT NULL — fullname of filmographycastandcrew (real vendored values).
- `biography`: text NOT NULL — biography of filmographycastandcrew (real vendored values).
- `role`: text NOT NULL — role of filmographycastandcrew (real vendored values).
- `viafid`: integer NULL — viafid of filmographycastandcrew (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of filmographycastandcrew (real vendored values).
- `nationality`: text NOT NULL — nationality of filmographycastandcrew (real vendored values).
- `musicbrainzartistid`: text NULL — musicbrainzartistid of filmographycastandcrew (real vendored values).
- `commonscategory`: text NULL — commonscategory of filmographycastandcrew (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of filmographycastandcrew (real vendored values).
- `gndid`: integer NULL — gndid of filmographycastandcrew (real vendored values).
- `imdbid`: text NULL — imdbid of filmographycastandcrew (real vendored values).
- `profileimage`: text NULL — profileimage of filmographycastandcrew (real vendored values).
- `findagravememorialid`: integer NULL — findagravememorialid of filmographycastandcrew (real vendored values).
- `birthdate`: text NOT NULL — birthdate of filmographycastandcrew (real vendored values).
- `deathdate`: text NULL — deathdate of filmographycastandcrew (real vendored values).
- `deathlocation`: text NULL — deathlocation of filmographycastandcrew (real vendored values).
- `entitytype`: text NOT NULL — entitytype of filmographycastandcrew (real vendored values).
- `freebaseid`: text NULL — freebaseid of filmographycastandcrew (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of filmographycastandcrew (real vendored values).
- `librariesaustraliaid`: integer NULL — librariesaustraliaid of filmographycastandcrew (real vendored values).
- `idrefid`: text NULL — idrefid of filmographycastandcrew (real vendored values).
- `nationallibraryofspainid`: text NULL — nationallibraryofspainid of filmographycastandcrew (real vendored values).
- `nlcrautid`: text NULL — nlcrautid of filmographycastandcrew (real vendored values).
- `firstname`: text NULL — firstname of filmographycastandcrew (real vendored values).
- `deathcause`: text NULL — deathcause of filmographycastandcrew (real vendored values).
- `discogsartistid`: integer NULL — discogsartistid of filmographycastandcrew (real vendored values).
- `allocinepersonid`: integer NULL — allocinepersonid of filmographycastandcrew (real vendored values).
- `allmoviepersonid`: text NULL — allmoviepersonid of filmographycastandcrew (real vendored values).
- `deathmanner`: text NULL — deathmanner of filmographycastandcrew (real vendored values).
- `swedishfilmdatabasepersonid`: integer NULL — swedishfilmdatabasepersonid of filmographycastandcrew (real vendored values).
- `granenciclopediacatalanaidformerscheme`: text NULL — granenciclopediacatalanaidformerscheme of filmographycastandcrew (real vendored values).
- `portpersonid`: integer NULL — portpersonid of filmographycastandcrew (real vendored values).
- `fastid`: integer NULL — fastid of filmographycastandcrew (real vendored values).
- `filmportalid`: text NULL — filmportalid of filmographycastandcrew (real vendored values).
- `danishnationalfilmographypersonid`: integer NULL — danishnationalfilmographypersonid of filmographycastandcrew (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of filmographycastandcrew (real vendored values).
- `kinopoiskpersonid`: integer NULL — kinopoiskpersonid of filmographycastandcrew (real vendored values).
- `elonetpersonid`: integer NULL — elonetpersonid of filmographycastandcrew (real vendored values).
- `languages`: text NOT NULL — languages of filmographycastandcrew (real vendored values).
- `nndbpeopleid`: text NULL — nndbpeopleid of filmographycastandcrew (real vendored values).
- `norafid`: bigint NULL — norafid of filmographycastandcrew (real vendored values).
- `nukatid`: text NULL — nukatid of filmographycastandcrew (real vendored values).
- `encyclopdiauniversalisid`: text NULL — encyclopdiauniversalisid of filmographycastandcrew (real vendored values).
- `nationaldiscographyofitaliansongartistgroupid`: integer NULL — nationaldiscographyofitaliansongartistgroupid of filmographycastandcrew (real vendored values).
- `cinematografoitnameorcompanyid`: integer NULL — cinematografoitnameorcompanyid of filmographycastandcrew (real vendored values).
- `nationalethesaurusvoorauteursnamenid`: text NULL — nationalethesaurusvoorauteursnamenid of filmographycastandcrew (real vendored values).
- `surname`: text NOT NULL — surname of filmographycastandcrew (real vendored values).
- `awards`: text NULL — awards of filmographycastandcrew (real vendored values).
- `openmediadatabasepersonid`: integer NULL — openmediadatabasepersonid of filmographycastandcrew (real vendored values).
- `treccaniid`: text NULL — treccaniid of filmographycastandcrew (real vendored values).
- `mymoviesactoridformerscheme`: integer NULL — mymoviesactoridformerscheme of filmographycastandcrew (real vendored values).
- `muziekwebperformerid`: text NULL — muziekwebperformerid of filmographycastandcrew (real vendored values).
- `gender`: text NOT NULL — gender of filmographycastandcrew (real vendored values).
- `sbnauthorid`: text NULL — sbnauthorid of filmographycastandcrew (real vendored values).
- `nativename`: text NULL — nativename of filmographycastandcrew (real vendored values).
- `careerstart`: text NULL — careerstart of filmographycastandcrew (real vendored values).
- `nlatrovepeopleid`: integer NULL — nlatrovepeopleid of filmographycastandcrew (real vendored values).
- `cinemagiapersonid`: integer NULL — cinemagiapersonid of filmographycastandcrew (real vendored values).
- `neseid`: text NULL — neseid of filmographycastandcrew (real vendored values).
- `careerend`: text NULL — careerend of filmographycastandcrew (real vendored values).
- `sourcedescription`: text NULL — sourcedescription of filmographycastandcrew (real vendored values).
- `tcmmoviedatabasepersonid`: integer NULL — tcmmoviedatabasepersonid of filmographycastandcrew (real vendored values).
- `genicomprofileid`: bigint NULL — genicomprofileid of filmographycastandcrew (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of filmographycastandcrew (real vendored values).
- `cobisauthorid`: text NULL — cobisauthorid of filmographycastandcrew (real vendored values).
- `deutschebiographiegndid`: integer NULL — deutschebiographiegndid of filmographycastandcrew (real vendored values).
- `filmwebplpersonid`: integer NULL — filmwebplpersonid of filmographycastandcrew (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of filmographycastandcrew (real vendored values).
- `canadiananameauthorityid`: text NULL — canadiananameauthorityid of filmographycastandcrew (real vendored values).
- `nationallibraryofisraelj9uid`: bigint NULL — nationallibraryofisraelj9uid of filmographycastandcrew (real vendored values).
- `nationallibraryoflithuaniaid`: text NULL — nationallibraryoflithuaniaid of filmographycastandcrew (real vendored values).
- `plwabnid`: bigint NULL — plwabnid of filmographycastandcrew (real vendored values).
- `sapaid`: text NULL — sapaid of filmographycastandcrew (real vendored values).
- `cinemathequequebecoisepersonid`: integer NULL — cinemathequequebecoisepersonid of filmographycastandcrew (real vendored values).
- `sharecatalogueauthorid`: integer NULL — sharecatalogueauthorid of filmographycastandcrew (real vendored values).
- `eveneid`: text NULL — eveneid of filmographycastandcrew (real vendored values).
- `iszdbpersonid`: integer NULL — iszdbpersonid of filmographycastandcrew (real vendored values).
- `mymoviespersonid`: integer NULL — mymoviespersonid of filmographycastandcrew (real vendored values).
- `enciclopediadiromapersonid`: integer NULL — enciclopediadiromapersonid of filmographycastandcrew (real vendored values).
- `nlpidold`: text NULL — nlpidold of filmographycastandcrew (real vendored values).
- `documentation`: text NULL — documentation of filmographycastandcrew (real vendored values).
- `fandangopersonid`: integer NULL — fandangopersonid of filmographycastandcrew (real vendored values).
- `raitechepersonid`: text NULL — raitechepersonid of filmographycastandcrew (real vendored values).
- `cinematografoitnameorcompanyidnew`: text NULL — cinematografoitnameorcompanyidnew of filmographycastandcrew (real vendored values).
- `birthlocation`: text NOT NULL — birthlocation of filmographycastandcrew (real vendored values).
- `ivipersonid`: text NULL — ivipersonid of filmographycastandcrew (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of filmographycastandcrew (real vendored values).
- `granenciclopediacatalanaid`: text NULL — granenciclopediacatalanaid of filmographycastandcrew (real vendored values).
- `listalid`: text NULL — listalid of filmographycastandcrew (real vendored values).
- `spousename`: text NULL — spousename of filmographycastandcrew (real vendored values).
- `children`: text NULL — children of filmographycastandcrew (real vendored values).
- `snacarkid`: text NULL — snacarkid of filmographycastandcrew (real vendored values).
- `education`: text NULL — education of filmographycastandcrew (real vendored values).
- `prabookid`: integer NULL — prabookid of filmographycastandcrew (real vendored values).
- `rottentomatoesid`: text NULL — rottentomatoesid of filmographycastandcrew (real vendored values).
- `moviewalkerpresspersonid`: integer NULL — moviewalkerpresspersonid of filmographycastandcrew (real vendored values).
- `birthname`: text NULL — birthname of filmographycastandcrew (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of filmographycastandcrew (real vendored values).
- `website`: text NULL — website of filmographycastandcrew (real vendored values).
- `filmtvitpersonid`: integer NULL — filmtvitpersonid of filmographycastandcrew (real vendored values).
- `unifrancepersonid`: integer NULL — unifrancepersonid of filmographycastandcrew (real vendored values).
- `partner`: text NULL — partner of filmographycastandcrew (real vendored values).
- business key: fullname

### lucianosalcegivennames  (source backend: postgres)
Source table lucianosalcegivennames of the LucianoSalceFilmography database (81 real rows).

- `givenname`: text NOT NULL — givenname of lucianosalcegivennames (real vendored values).
- `description`: text NOT NULL — description of lucianosalcegivennames (real vendored values).
- `instanceof`: text NOT NULL — instanceof of lucianosalcegivennames (real vendored values).
- `languageoforigin`: text NULL — languageoforigin of lucianosalcegivennames (real vendored values).
- `alternativename`: text NULL — alternativename of lucianosalcegivennames (real vendored values).
- `distinctfrom`: text NULL — distinctfrom of lucianosalcegivennames (real vendored values).
- `nativelabel`: text NOT NULL — nativelabel of lucianosalcegivennames (real vendored values).
- `writingsystem`: text NOT NULL — writingsystem of lucianosalcegivennames (real vendored values).
- `soundexcode`: text NULL — soundexcode of lucianosalcegivennames (real vendored values).
- `colognephonetics`: text NULL — colognephonetics of lucianosalcegivennames (real vendored values).
- `caverphonecode`: text NULL — caverphonecode of lucianosalcegivennames (real vendored values).
- `familynameassociation`: text NULL — familynameassociation of lucianosalcegivennames (real vendored values).
- `nominisgivennameid`: text NULL — nominisgivennameid of lucianosalcegivennames (real vendored values).
- `commonscategory`: text NULL — commonscategory of lucianosalcegivennames (real vendored values).
- `sourcedescription`: text NULL — sourcedescription of lucianosalcegivennames (real vendored values).
- `gendervariant`: text NULL — gendervariant of lucianosalcegivennames (real vendored values).
- `attestedin`: text NULL — attestedin of lucianosalcegivennames (real vendored values).
- `pronunciationaudio`: text NULL — pronunciationaudio of lucianosalcegivennames (real vendored values).
- `nederlandsevoornamenbankid`: text NULL — nederlandsevoornamenbankid of lucianosalcegivennames (real vendored values).

### filmographyfamilynames  (source backend: rest)
Source table filmographyfamilynames of the LucianoSalceFilmography database (88 real rows).

- `surname`: text NOT NULL — surname of filmographyfamilynames (real vendored values).
- `description`: text NOT NULL — description of filmographyfamilynames (real vendored values).
- `typeofname`: text NOT NULL — typeofname of filmographyfamilynames (real vendored values).
- `distinctfrom`: text NULL — distinctfrom of filmographyfamilynames (real vendored values).
- `nativesurname`: text NOT NULL — nativesurname of filmographyfamilynames (real vendored values).
- `language`: text NULL — language of filmographyfamilynames (real vendored values).
- `script`: text NOT NULL — script of filmographyfamilynames (real vendored values).
- `commonscategory`: text NULL — commonscategory of filmographyfamilynames (real vendored values).
- `soundexcode`: text NULL — soundexcode of filmographyfamilynames (real vendored values).
- `geopatronymeid`: text NULL — geopatronymeid of filmographyfamilynames (real vendored values).
- `geneanetfamilynameid`: text NULL — geneanetfamilynameid of filmographyfamilynames (real vendored values).
- `colognephoneticscode`: text NULL — colognephoneticscode of filmographyfamilynames (real vendored values).
- `caverphonecode`: text NULL — caverphonecode of filmographyfamilynames (real vendored values).
- `wolframentitycode`: text NULL — wolframentitycode of filmographyfamilynames (real vendored values).

### Relationships

- filmographydetails(castmembers) -> filmographycastandcrew(fullname) [optional (may be NULL/dangling)]
- filmographydetails(directorofphotography) -> cinematographers(name) [optional (may be NULL/dangling)]
- filmographydetails(musiccomposer) -> filmcomposers(composername) [optional (may be NULL/dangling)]
- filmographydetails(productiondesigner) -> productiondesigner(fullname) [optional (may be NULL/dangling)]
- filmographydetails(screenwriters) -> filmindustryprofessionals(fullname) [optional (may be NULL/dangling)]

