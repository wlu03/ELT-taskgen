# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Anatolipapanovvoiceactinglegacy

## Specification

PROJECT OVERVIEW: Anatoli Papanov Voice Acting Legacy

This project builds two analytical marts over the AnatoliPapanovVoiceActingLegacy database. Seven source tables are involved, and each must be extracted from its own backend.

Source tables and their extraction backends:
- filmographyandcontributions must be extracted from the mongodb backend. It holds one row per film contribution, with filmtitle, filmdescription, imdbid, filmtype, releasedate, animatorrufilmid, productioncompany, countryoforigin, kinopoiskfilmid, filmdirector, screenwriter, voiceactor, animationmethod, originallanguage, productiondesigner, musiccomposer, lumierefilmid, bigcartoondatabaseid, defafilmdatabaseiddeprecated, filmduration, cinematographer and googleknowledgegraphid.
- sovietactorsprofile must be extracted from the files backend. Its business key is fullname.
- sovietwriters must be extracted from the files backend. Its business key is writername, and it carries writerdescription among its attributes.
- sovietanimatorsanddirectors must be extracted from the s3 backend. Its business key is fullname.
- notablecomposers must be extracted from the s3 backend. Its business key is composername.
- notableartistsandanimators must be extracted from the s3 backend. Its business key is fullname.
- russiangivennames must be extracted from the rest backend.
- voiceactorfamilynames must be extracted from the mongodb backend. Its business key is surname.

Relationships between the sources, each labelled exactly as the source schema declares it:
- The child table filmographyandcontributions with key filmdirector relates to the parent table sovietanimatorsanddirectors with key fullname; this relationship is required.
- The child table filmographyandcontributions with key musiccomposer relates to the parent table notablecomposers with key composername; this relationship is optional (the child value may be NULL or dangling).
- The child table filmographyandcontributions with key productiondesigner relates to the parent table notableartistsandanimators with key fullname; this relationship is optional (the child value may be NULL or dangling).
- The child table filmographyandcontributions with key screenwriter relates to the parent table sovietwriters with key writername; this relationship is optional (the child value may be NULL or dangling).
- The child table filmographyandcontributions with key voiceactor relates to the parent table sovietactorsprofile with key fullname; this relationship is required.
- The child table notablecomposers with key lastname relates to the parent table voiceactorfamilynames with key surname; this relationship is optional (the child value may be NULL or dangling).

Throughout both marts, a filmographyandcontributions row is attributed to a sovietwriters row when the screenwriter text of the film equals the writername of that writer. Such a film is called a linked filmographyandcontributions row of that sovietwriters row.

============================================================
Mart sovietwriters_filmographyandcontributions_distribution — the per-(sovietwriters, measure state) distribution of linked filmographyandcontributions rows in the AnatoliPapanovVoiceActingLegacy database.

Grain: one row per (writername, measure state) pair represented among linked filmographyandcontributions rows; the absent state includes missing filmduration values and a no-activity row for a sovietwriters row with no links. A linked filmographyandcontributions row whose filmduration has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state together identify one output row.

Rule 1. The source table sovietwriters is read as an input of this mart.

Rule 2. The source table filmographyandcontributions is read as an input of this mart.

Rule 3. From sovietwriters, each writername and its writerdescription is carried into the measure-state calculation as entity_key and entity_name respectively.

Rule 4. The linked filmographyandcontributions rows are brought into each sovietwriters entity, matching the screenwriter of filmographyandcontributions to the writername behind entity_key; preservation is left-sided on the sovietwriters side, so an entity with no linked row is retained so that its absent state is visible, and entity_key, entity_name, writername and screenwriter are carried forward.

Rule 5. The present measure-state rows are kept: a real filmographyandcontributions row whose filmduration has a value, carrying entity_key and entity_name.

Rule 6. Within the present measure state there is one row per sovietwriters entity that has at least one row in that state, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different non-missing filmduration values occur (each different value counted once, however many rows repeat it), total_amount as the total filmduration, and max_amount as the largest filmduration.

Rule 7. For those present-state rows, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures are labelled as the present measure state: measure_state reads 'present' on every such row, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows are kept, carrying entity_key and entity_name: filmduration is missing, including the retained placeholder for a sovietwriters row with no filmographyandcontributions rows. A real filmographyandcontributions row whose filmduration has a value belongs only to the present state and never to this absent state.

Rule 10. Within the absent measure state there is one row per sovietwriters entity that has at least one row in that state, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different non-missing filmduration values occur (each different value counted once, however many rows repeat it), total_amount as the total filmduration, and max_amount as the largest filmduration.

Rule 11. For those absent-state rows, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures are labelled as the absent measure state: measure_state reads 'absent' on every such row, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list holding all rows of both: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other; each stacked row carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 14. Deterministic output order: rows appear in ascending entity_key order, and within one entity_key in ascending measure_state order.

Output columns of sovietwriters_filmographyandcontributions_distribution:
- entity_key (text): identifier of the sovietwriters row.
- measure_state (text): 'present' for a linked filmographyandcontributions row whose filmduration has a value; 'absent' when filmduration is missing, including a sovietwriters row with no linked filmographyandcontributions row. A linked filmographyandcontributions row whose filmduration has a value belongs only to the present state and never to the absent state.
- entity_name (text): writerdescription of the sovietwriters row, copied unchanged.
- row_count (bigint): number of linked filmographyandcontributions rows in this entity/state cell; an absent cell holding real filmographyandcontributions rows whose filmduration is missing COUNTS those rows, and only the placeholder cell of a sovietwriters row with no linked filmographyandcontributions row at all reports 0.
- distinct_amount_count (bigint): number of unique non-missing filmduration values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no filmduration value at all — both for a sovietwriters row with no linked filmographyandcontributions row and for an absent cell whose rows all have a missing filmduration.
- total_amount (decimal): sum of filmduration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an filmduration value.
- max_amount (decimal): largest filmduration in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an filmduration value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

============================================================
Mart sovietwriters_filmographyandcontributions_top — per-sovietwriters extremes over linked filmographyandcontributions rows in the AnatoliPapanovVoiceActingLegacy database: WHICH row is largest, not how large it is.

Grain: one row per sovietwriters (writername), INCLUDING sovietwriters rows with no linked filmographyandcontributions rows.

Key column: parent_key identifies one output row.

Rule 1. The source table sovietwriters is read as an input of this mart.

Rule 2. The source table filmographyandcontributions is read as an input of this mart.

Rule 3. There is one row per sovietwriters row, keyed by writername, which becomes parent_key, with parent_name carried beside it.

Rule 4. filmographyandcontributions is brought in, matching the screenwriter of filmographyandcontributions to the writername behind parent_key and carrying screenwriter and writername; preservation is left-sided on the sovietwriters side, so a sovietwriters row with no filmographyandcontributions rows still appears, with the declared defaults.

Rule 5. Within each parent_key the matched rows are ranked under an explicit total order — the measure first, then the declared tie-break — so the extremal row is a function of the input and not of row order.

Rule 6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7. For each parent_key the single row kept is the one at which the ordering measure is largest, ties broken by the smallest filmtitle under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then the smallest animatorrufilmid, and top_label and top_row_id are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 8. The extremal row's attributes are attached to the measures of the same parent_key; preservation is left-sided on the measures side, so a group with no rows at all keeps its measures.

Rule 9. The mart columns are named: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure and total_measure, the default also applies to a group none of whose real rows carries an input value.

Rule 10. Guarded ratios, alongside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when total_measure is 0 or has no value.

Rule 11. tie_state, reported alongside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, is a categorical mapping with no numeric boundary: 'empty' when no row holds a maximum at all — the parent has no filmographyandcontributions rows, or none of its rows carries a filmduration value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do; equivalently tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more, and it is never null or blank. A row holds the maximum only when it carries a filmduration value equal to the largest filmduration value among the parent's rows; a row with no filmduration value never holds the maximum, so a parent whose filmographyandcontributions rows all lack a filmduration value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

Rule 12. Deterministic output order: rows appear sorted in ascending parent_key order.

Output columns of sovietwriters_filmographyandcontributions_top:
- parent_key (text): identifier of the sovietwriters row. One row per value.
- parent_name (text): writerdescription of the sovietwriters row, copied unchanged.
- top_measure (decimal): the largest filmduration itself; 0 when the parent has no filmographyandcontributions rows, and 0 when none of its rows carries a filmduration value.
- tied_count (bigint): how many filmographyandcontributions rows are tied at that largest filmduration. 1 when exactly one row carries that largest filmduration; 0 when there are no rows or when none of the rows carries a filmduration value; a row with no filmduration value never ties: only a row whose filmduration value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.
- child_count (bigint): number of filmographyandcontributions rows for this sovietwriters row; 0 when there are none. Every linked filmographyandcontributions row counts, whether or not it carries a filmduration value. A sovietwriters row kept with no filmographyandcontributions row reports 0 here, never 1: its placeholder holds no filmographyandcontributions row to count.
- total_measure (decimal): sum of filmduration over all of them; 0 when the parent has no filmographyandcontributions rows, and 0 when none of its rows carries a filmduration value (rows with no filmduration value add nothing).
- top_label (text): the filmtitle of the filmographyandcontributions row with the LARGEST filmduration for this sovietwriters row. Ties in filmduration are broken by taking the SMALLEST filmtitle under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one); rows tied on both are resolved by the smallest animatorrufilmid. A row with no filmduration value still ranks, after every row that has one, so a parent holding at least one filmographyandcontributions row always has a winning row — when NONE of its rows carries a filmduration value the winner is the one the tie-break alone selects, not the no-rows default. The literal '(none)' when the parent has no filmographyandcontributions rows at all.
- top_row_id (integer): the animatorrufilmid of that same extremal row — the winner under the SAME total order, so it is the identifier of a real filmographyandcontributions row whenever the parent has any. This includes when none of them carries a filmduration value. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no filmographyandcontributions rows, or none of its rows carries a filmduration value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a filmduration value equal to the largest filmduration value among the parent's rows; a row with no filmduration value never holds the maximum. So a parent whose filmographyandcontributions rows all lack a filmduration value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `sovietwriters_filmographyandcontributions_distribution`

- Grain: One row per (writername, measure state) pair represented among linked filmographyandcontributions rows; the absent state includes missing filmduration values and a no-activity row for a sovietwriters row with no links. A linked filmographyandcontributions row whose filmduration has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'sovietwriters_filmographyandcontributions_distribution' has 14 declared semantic rules:
1. [source] Read source table sovietwriters. (public source tables: sovietwriters)
2. [source] Read source table filmographyandcontributions. (public source tables: filmographyandcontributions)
3. [derive] Carry each writername and its writerdescription into the measure-state calculation. (public source tables: sovietwriters | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked filmographyandcontributions rows into each sovietwriters entity; retain an entity with no linked row so its absent state is visible. (public source tables: filmographyandcontributions | public carried/output columns: entity_key, entity_name, writername, screenwriter | join preservation: left | condition public identifiers: filmographyandcontributions, screenwriter, entity_key)
5. [filter] Keep the present measure-state rows: a real filmographyandcontributions row whose filmduration has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per sovietwriters entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing filmduration values occur (each different value counted once, however many rows repeat it), total filmduration, and largest filmduration. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: filmduration is missing, including the retained placeholder for a sovietwriters row with no filmographyandcontributions rows. A real filmographyandcontributions row whose filmduration has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per sovietwriters entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing filmduration values occur (each different value counted once, however many rows repeat it), total filmduration, and largest filmduration. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `sovietwriters_filmographyandcontributions_top`

- Grain: One row per sovietwriters (writername), INCLUDING sovietwriters rows with no linked filmographyandcontributions rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'sovietwriters_filmographyandcontributions_top' has 12 declared semantic rules:
1. [source] Read source table sovietwriters. (public source tables: sovietwriters)
2. [source] Read source table filmographyandcontributions. (public source tables: filmographyandcontributions)
3. [derive] One row per sovietwriters row, keyed by writername. (public source tables: sovietwriters | public carried/output columns: parent_key, parent_name)
4. [join] Bring in filmographyandcontributions: a sovietwriters row with no filmographyandcontributions rows still appears, with the declared defaults. (public source tables: filmographyandcontributions | public carried/output columns: screenwriter, writername | join preservation: left | condition public identifiers: filmographyandcontributions, screenwriter, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest filmtitle under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then the smallest animatorrufilmid, and take top_label, top_row_id from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no filmographyandcontributions rows, or none of its rows carries a filmduration value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a filmduration value equal to the largest filmduration value among the parent's rows; a row with no filmduration value never holds the maximum. So a parent whose filmographyandcontributions rows all lack a filmduration value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### filmographyandcontributions  (source backend: mongodb)
Source table filmographyandcontributions of the AnatoliPapanovVoiceActingLegacy database (35 real rows).

- `filmtitle`: text NOT NULL — filmtitle of filmographyandcontributions (real vendored values).
- `filmdescription`: text NOT NULL — filmdescription of filmographyandcontributions (real vendored values).
- `imdbid`: text NULL — imdbid of filmographyandcontributions (real vendored values).
- `filmtype`: text NOT NULL — filmtype of filmographyandcontributions (real vendored values).
- `releasedate`: text NOT NULL — releasedate of filmographyandcontributions (real vendored values).
- `animatorrufilmid`: integer NOT NULL — animatorrufilmid of filmographyandcontributions (real vendored values).
- `productioncompany`: text NOT NULL — productioncompany of filmographyandcontributions (real vendored values).
- `countryoforigin`: text NOT NULL — countryoforigin of filmographyandcontributions (real vendored values).
- `kinopoiskfilmid`: integer NULL — kinopoiskfilmid of filmographyandcontributions (real vendored values).
- `filmdirector`: text NOT NULL — filmdirector of filmographyandcontributions (real vendored values).
- `screenwriter`: text NULL — screenwriter of filmographyandcontributions (real vendored values).
- `voiceactor`: text NOT NULL — voiceactor of filmographyandcontributions (real vendored values).
- `animationmethod`: text NOT NULL — animationmethod of filmographyandcontributions (real vendored values).
- `originallanguage`: text NULL — originallanguage of filmographyandcontributions (real vendored values).
- `productiondesigner`: text NULL — productiondesigner of filmographyandcontributions (real vendored values).
- `musiccomposer`: text NULL — musiccomposer of filmographyandcontributions (real vendored values).
- `lumierefilmid`: integer NULL — lumierefilmid of filmographyandcontributions (real vendored values).
- `bigcartoondatabaseid`: integer NULL — bigcartoondatabaseid of filmographyandcontributions (real vendored values).
- `defafilmdatabaseiddeprecated`: text NULL — defafilmdatabaseiddeprecated of filmographyandcontributions (real vendored values).
- `filmduration`: decimal NULL — filmduration of filmographyandcontributions (real vendored values).
- `cinematographer`: text NULL — cinematographer of filmographyandcontributions (real vendored values).
- `googleknowledgegraphid`: text NULL — googleknowledgegraphid of filmographyandcontributions (real vendored values).

### sovietactorsprofile  (source backend: files)
Source table sovietactorsprofile of the AnatoliPapanovVoiceActingLegacy database (14 real rows).

- `fullname`: text NOT NULL — fullname of sovietactorsprofile (real vendored values).
- `biography`: text NOT NULL — biography of sovietactorsprofile (real vendored values).
- `profession`: text NOT NULL — profession of sovietactorsprofile (real vendored values).
- `birthplace`: text NULL — birthplace of sovietactorsprofile (real vendored values).
- `partner`: text NULL — partner of sovietactorsprofile (real vendored values).
- `viafid`: text NULL — viafid of sovietactorsprofile (real vendored values).
- `imdbid`: text NOT NULL — imdbid of sovietactorsprofile (real vendored values).
- `commonscategory`: text NULL — commonscategory of sovietactorsprofile (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of sovietactorsprofile (real vendored values).
- `gndid`: integer NULL — gndid of sovietactorsprofile (real vendored values).
- `entitytype`: text NOT NULL — entitytype of sovietactorsprofile (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of sovietactorsprofile (real vendored values).
- `birthdate`: text NOT NULL — birthdate of sovietactorsprofile (real vendored values).
- `freebaseid`: text NULL — freebaseid of sovietactorsprofile (real vendored values).
- `honorsawarded`: text NOT NULL — honorsawarded of sovietactorsprofile (real vendored values).
- `nationality`: text NOT NULL — nationality of sovietactorsprofile (real vendored values).
- `firstname`: text NULL — firstname of sovietactorsprofile (real vendored values).
- `almamater`: text NULL — almamater of sovietactorsprofile (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of sovietactorsprofile (real vendored values).
- `profileimage`: text NULL — profileimage of sovietactorsprofile (real vendored values).
- `languages`: text NULL — languages of sovietactorsprofile (real vendored values).
- `affiliatedinstitution`: text NULL — affiliatedinstitution of sovietactorsprofile (real vendored values).
- `animatorrupersonid`: integer NULL — animatorrupersonid of sovietactorsprofile (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of sovietactorsprofile (real vendored values).
- `careerstartyear`: text NULL — careerstartyear of sovietactorsprofile (real vendored values).
- `gender`: text NOT NULL — gender of sovietactorsprofile (real vendored values).
- `tmdbpersonid`: integer NOT NULL — tmdbpersonid of sovietactorsprofile (real vendored values).
- `filmrupersonid`: text NULL — filmrupersonid of sovietactorsprofile (real vendored values).
- `kinoboxpersonid`: integer NOT NULL — kinoboxpersonid of sovietactorsprofile (real vendored values).
- `deathlocation`: text NULL — deathlocation of sovietactorsprofile (real vendored values).
- `deathdate`: text NULL — deathdate of sovietactorsprofile (real vendored values).
- `burialsite`: text NULL — burialsite of sovietactorsprofile (real vendored values).
- business key: fullname

### sovietwriters  (source backend: files)
Source table sovietwriters of the AnatoliPapanovVoiceActingLegacy database (18 real rows).

- `writername`: text NOT NULL — writername of sovietwriters (real vendored values).
- `writerdescription`: text NULL — writerdescription of sovietwriters (real vendored values).
- `imdbid`: text NULL — imdbid of sovietwriters (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of sovietwriters (real vendored values).
- `viafid`: text NOT NULL — viafid of sovietwriters (real vendored values).
- `gndid`: text NULL — gndid of sovietwriters (real vendored values).
- `writerisni`: text NULL — writerisni of sovietwriters (real vendored values).
- `birthplace`: text NULL — birthplace of sovietwriters (real vendored values).
- `deathplace`: text NULL — deathplace of sovietwriters (real vendored values).
- `citizenshipcountry`: text NOT NULL — citizenshipcountry of sovietwriters (real vendored values).
- `entitytype`: text NOT NULL — entitytype of sovietwriters (real vendored values).
- `birthdate`: text NOT NULL — birthdate of sovietwriters (real vendored values).
- `deathdate`: text NULL — deathdate of sovietwriters (real vendored values).
- `freebaseid`: text NULL — freebaseid of sovietwriters (real vendored values).
- `profession`: text NOT NULL — profession of sovietwriters (real vendored values).
- `firstname`: text NOT NULL — firstname of sovietwriters (real vendored values).
- `maincategory`: text NULL — maincategory of sovietwriters (real vendored values).
- `languagesspokenwrittensigned`: text NOT NULL — languagesspokenwrittensigned of sovietwriters (real vendored values).
- `awardsreceived`: text NULL — awardsreceived of sovietwriters (real vendored values).
- `burialplace`: text NULL — burialplace of sovietwriters (real vendored values).
- `educationinstitution`: text NULL — educationinstitution of sovietwriters (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of sovietwriters (real vendored values).
- `idrefid`: text NULL — idrefid of sovietwriters (real vendored values).
- `animatorrupersonid`: integer NULL — animatorrupersonid of sovietwriters (real vendored values).
- `nlcrautid`: text NULL — nlcrautid of sovietwriters (real vendored values).
- `writinglanguage`: text NULL — writinglanguage of sovietwriters (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of sovietwriters (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of sovietwriters (real vendored values).
- `gender`: text NOT NULL — gender of sovietwriters (real vendored values).
- `nukatid`: text NULL — nukatid of sovietwriters (real vendored values).
- `plwabnid`: bigint NULL — plwabnid of sovietwriters (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of sovietwriters (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of sovietwriters (real vendored values).
- `nationallibraryoflatviaid`: text NULL — nationallibraryoflatviaid of sovietwriters (real vendored values).
- business key: writername

### sovietanimatorsanddirectors  (source backend: s3)
Source table sovietanimatorsanddirectors of the AnatoliPapanovVoiceActingLegacy database (16 real rows).

- `fullname`: text NOT NULL — fullname of sovietanimatorsanddirectors (real vendored values).
- `biographicaldescription`: text NOT NULL — biographicaldescription of sovietanimatorsanddirectors (real vendored values).
- `primaryoccupation`: text NOT NULL — primaryoccupation of sovietanimatorsanddirectors (real vendored values).
- `viafid`: integer NOT NULL — viafid of sovietanimatorsanddirectors (real vendored values).
- `internationalstandardnameidentifier`: text NULL — internationalstandardnameidentifier of sovietanimatorsanddirectors (real vendored values).
- `imdbid`: text NOT NULL — imdbid of sovietanimatorsanddirectors (real vendored values).
- `birthplace`: text NULL — birthplace of sovietanimatorsanddirectors (real vendored values).
- `deathplace`: text NULL — deathplace of sovietanimatorsanddirectors (real vendored values).
- `citizenship`: text NOT NULL — citizenship of sovietanimatorsanddirectors (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of sovietanimatorsanddirectors (real vendored values).
- `birthdate`: text NOT NULL — birthdate of sovietanimatorsanddirectors (real vendored values).
- `deathdate`: text NULL — deathdate of sovietanimatorsanddirectors (real vendored values).
- `entitytype`: text NOT NULL — entitytype of sovietanimatorsanddirectors (real vendored values).
- `freebaseid`: text NULL — freebaseid of sovietanimatorsanddirectors (real vendored values).
- `awards`: text NULL — awards of sovietanimatorsanddirectors (real vendored values).
- `firstname`: text NOT NULL — firstname of sovietanimatorsanddirectors (real vendored values).
- `gndid`: text NULL — gndid of sovietanimatorsanddirectors (real vendored values).
- `csfdpersonid`: integer NULL — csfdpersonid of sovietanimatorsanddirectors (real vendored values).
- `animatorrupersonid`: integer NOT NULL — animatorrupersonid of sovietanimatorsanddirectors (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of sovietanimatorsanddirectors (real vendored values).
- `gender`: text NOT NULL — gender of sovietanimatorsanddirectors (real vendored values).
- `copyrightstatus`: text NULL — copyrightstatus of sovietanimatorsanddirectors (real vendored values).
- `languages`: text NULL — languages of sovietanimatorsanddirectors (real vendored values).
- `tmdbpersonid`: integer NOT NULL — tmdbpersonid of sovietanimatorsanddirectors (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of sovietanimatorsanddirectors (real vendored values).
- `almamater`: text NULL — almamater of sovietanimatorsanddirectors (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of sovietanimatorsanddirectors (real vendored values).
- `maincategory`: text NULL — maincategory of sovietanimatorsanddirectors (real vendored values).
- business key: fullname

### notablecomposers  (source backend: s3)
Source table notablecomposers of the AnatoliPapanovVoiceActingLegacy database (17 real rows).

- `composername`: text NOT NULL — composername of notablecomposers (real vendored values).
- `composerdescription`: text NOT NULL — composerdescription of notablecomposers (real vendored values).
- `composeroccupation`: text NOT NULL — composeroccupation of notablecomposers (real vendored values).
- `musicbrainzartistid`: text NOT NULL — musicbrainzartistid of notablecomposers (real vendored values).
- `libraryofcongressauthorityid`: text NULL — libraryofcongressauthorityid of notablecomposers (real vendored values).
- `viafid`: integer NOT NULL — viafid of notablecomposers (real vendored values).
- `gndid`: text NULL — gndid of notablecomposers (real vendored values).
- `nlcrautid`: text NULL — nlcrautid of notablecomposers (real vendored values).
- `composereducation`: text NULL — composereducation of notablecomposers (real vendored values).
- `composerawards`: text NULL — composerawards of notablecomposers (real vendored values).
- `entitytype`: text NOT NULL — entitytype of notablecomposers (real vendored values).
- `birthdate`: text NOT NULL — birthdate of notablecomposers (real vendored values).
- `birthplace`: text NULL — birthplace of notablecomposers (real vendored values).
- `freebaseid`: text NULL — freebaseid of notablecomposers (real vendored values).
- `bibliothequenationaledefranceid`: text NULL — bibliothequenationaledefranceid of notablecomposers (real vendored values).
- `citizenship`: text NOT NULL — citizenship of notablecomposers (real vendored values).
- `firstname`: text NULL — firstname of notablecomposers (real vendored values).
- `imdbid`: text NOT NULL — imdbid of notablecomposers (real vendored values).
- `discogsartistid`: integer NULL — discogsartistid of notablecomposers (real vendored values).
- `nukatid`: text NULL — nukatid of notablecomposers (real vendored values).
- `isniidentifier`: text NULL — isniidentifier of notablecomposers (real vendored values).
- `idrefid`: text NULL — idrefid of notablecomposers (real vendored values).
- `languages`: text NULL — languages of notablecomposers (real vendored values).
- `europeanaentity`: text NULL — europeanaentity of notablecomposers (real vendored values).
- `musicalgenre`: text NULL — musicalgenre of notablecomposers (real vendored values).
- `gender`: text NOT NULL — gender of notablecomposers (real vendored values).
- `nationallibraryofisraelj9uid`: bigint NULL — nationallibraryofisraelj9uid of notablecomposers (real vendored values).
- `plwabnid`: bigint NULL — plwabnid of notablecomposers (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of notablecomposers (real vendored values).
- `musicalinstrument`: text NULL — musicalinstrument of notablecomposers (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of notablecomposers (real vendored values).
- `lastname`: text NULL — lastname of notablecomposers (real vendored values).
- `deathdate`: text NULL — deathdate of notablecomposers (real vendored values).
- `deathplace`: text NULL — deathplace of notablecomposers (real vendored values).
- `burialplace`: text NULL — burialplace of notablecomposers (real vendored values).
- `animatorrupersonid`: integer NULL — animatorrupersonid of notablecomposers (real vendored values).
- `worldcatidentitiesidsuperseded`: text NULL — worldcatidentitiesidsuperseded of notablecomposers (real vendored values).
- `biografijaruid`: text NULL — biografijaruid of notablecomposers (real vendored values).
- `prabookid`: integer NULL — prabookid of notablecomposers (real vendored values).
- business key: composername

### notableartistsandanimators  (source backend: s3)
Source table notableartistsandanimators of the AnatoliPapanovVoiceActingLegacy database (12 real rows).

- `fullname`: text NOT NULL — fullname of notableartistsandanimators (real vendored values).
- `biographicalsummary`: text NULL — biographicalsummary of notableartistsandanimators (real vendored values).
- `viafid`: text NULL — viafid of notableartistsandanimators (real vendored values).
- `imdbid`: text NOT NULL — imdbid of notableartistsandanimators (real vendored values).
- `entitytype`: text NOT NULL — entitytype of notableartistsandanimators (real vendored values).
- `birthdate`: text NOT NULL — birthdate of notableartistsandanimators (real vendored values).
- `birthplace`: text NULL — birthplace of notableartistsandanimators (real vendored values).
- `awards`: text NULL — awards of notableartistsandanimators (real vendored values).
- `freebaseid`: text NULL — freebaseid of notableartistsandanimators (real vendored values).
- `education`: text NULL — education of notableartistsandanimators (real vendored values).
- `citizenship`: text NULL — citizenship of notableartistsandanimators (real vendored values).
- `profession`: text NOT NULL — profession of notableartistsandanimators (real vendored values).
- `firstname`: text NULL — firstname of notableartistsandanimators (real vendored values).
- `animatorrupersonid`: integer NULL — animatorrupersonid of notableartistsandanimators (real vendored values).
- `gender`: text NOT NULL — gender of notableartistsandanimators (real vendored values).
- `copyrightstatus`: text NULL — copyrightstatus of notableartistsandanimators (real vendored values).
- `languages`: text NULL — languages of notableartistsandanimators (real vendored values).
- `tmdbpersonid`: integer NULL — tmdbpersonid of notableartistsandanimators (real vendored values).
- `kinoboxpersonid`: integer NULL — kinoboxpersonid of notableartistsandanimators (real vendored values).
- `deathplace`: text NULL — deathplace of notableartistsandanimators (real vendored values).
- `deathdate`: text NULL — deathdate of notableartistsandanimators (real vendored values).
- `surname`: text NULL — surname of notableartistsandanimators (real vendored values).
- business key: fullname

### russiangivennames  (source backend: rest)
Source table russiangivennames of the AnatoliPapanovVoiceActingLegacy database (55 real rows).

- `name`: text NOT NULL — name of russiangivennames (real vendored values).
- `description`: text NOT NULL — description of russiangivennames (real vendored values).
- `nametype`: text NOT NULL — nametype of russiangivennames (real vendored values).
- `alternativename`: text NULL — alternativename of russiangivennames (real vendored values).
- `distinctfrom`: text NULL — distinctfrom of russiangivennames (real vendored values).
- `nativename`: text NOT NULL — nativename of russiangivennames (real vendored values).
- `script`: text NOT NULL — script of russiangivennames (real vendored values).
- `transliteration`: text NULL — transliteration of russiangivennames (real vendored values).
- `language`: text NULL — language of russiangivennames (real vendored values).
- `commonscategory`: text NULL — commonscategory of russiangivennames (real vendored values).
- `sourcereference`: text NULL — sourcereference of russiangivennames (real vendored values).
- `googleknowledgegraphid`: text NULL — googleknowledgegraphid of russiangivennames (real vendored values).

### voiceactorfamilynames  (source backend: mongodb)
Source table voiceactorfamilynames of the AnatoliPapanovVoiceActingLegacy database (10 real rows).

- `surname`: text NOT NULL — surname of voiceactorfamilynames (real vendored values).
- `description`: text NOT NULL — description of voiceactorfamilynames (real vendored values).
- `category`: text NOT NULL — category of voiceactorfamilynames (real vendored values).
- `script`: text NOT NULL — script of voiceactorfamilynames (real vendored values).
- `nativesurname`: text NOT NULL — nativesurname of voiceactorfamilynames (real vendored values).
- `commonscategory`: text NULL — commonscategory of voiceactorfamilynames (real vendored values).
- `distinctfrom`: text NULL — distinctfrom of voiceactorfamilynames (real vendored values).
- `language`: text NULL — language of voiceactorfamilynames (real vendored values).
- business key: surname

### Relationships

- filmographyandcontributions(filmdirector) -> sovietanimatorsanddirectors(fullname) [required]
- filmographyandcontributions(musiccomposer) -> notablecomposers(composername) [optional (may be NULL/dangling)]
- filmographyandcontributions(productiondesigner) -> notableartistsandanimators(fullname) [optional (may be NULL/dangling)]
- filmographyandcontributions(screenwriter) -> sovietwriters(writername) [optional (may be NULL/dangling)]
- filmographyandcontributions(voiceactor) -> sovietactorsprofile(fullname) [required]
- notablecomposers(lastname) -> voiceactorfamilynames(surname) [optional (may be NULL/dangling)]

