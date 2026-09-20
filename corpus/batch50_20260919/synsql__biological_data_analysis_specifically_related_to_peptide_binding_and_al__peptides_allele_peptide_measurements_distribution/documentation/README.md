# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Biological Data Analysis Specifically Related To Peptide Binding And Al

## Specification

PROJECT OVERVIEW

This project analyses peptide binding measurements and builds two marts from four source tables. Each source table must be extracted from its own backend, as follows. The source table allele_peptide_measurements must be extracted from the mongodb backend. The source table peptides must be extracted from the mongodb backend. The source table alleles must be extracted from the rest backend. The source table measurement_sources must be extracted from the rest backend.

The source tables relate to one another as follows, and every one of these relationships is optional, meaning the child value may be missing or may point at no parent row at all.

Relationship 1: the child table allele_peptide_measurements with key allele_id refers to the parent table alleles with key allele_id; this relationship is optional (the child allele_id may be NULL or dangling).

Relationship 2: the child table allele_peptide_measurements with key measurement_source_id refers to the parent table measurement_sources with key source_id; this relationship is optional (the child measurement_source_id may be NULL or dangling).

Relationship 3: the child table allele_peptide_measurements with key original_allele_id refers to the parent table alleles with key allele_id; this relationship is optional (the child original_allele_id may be NULL or dangling).

Relationship 4: the child table allele_peptide_measurements with key peptide_id refers to the parent table peptides with key peptide_id; this relationship is optional (the child peptide_id may be NULL or dangling).

An allele_peptide_measurements row is attributed to a peptides row when its peptide_id equals that peptides row's peptide_id; such a row is called a linked allele_peptide_measurements row of that peptides row. All comparisons of text are plain case-sensitive comparisons of the stored text, using the warehouse default order, in which every uppercase letter sorts before every lowercase one.

==========================================================
MART peptides_allele_peptide_measurements_distribution — Per-(peptides, measure state) distribution of linked allele_peptide_measurements rows in the __biological_data_analysis_specifically_related_to_peptide_binding_and_al scenario.
==========================================================

Grain: one row per (peptide_id, measure state) pair represented among linked allele_peptide_measurements rows; the absent state includes missing measurement_value values and a no-activity row for a peptides row with no links. A linked allele_peptide_measurements row whose measurement_value has a value belongs only to the present state and never to the absent state.

The key columns of this mart are entity_key and measure_state; together they identify one output row.

Output columns:

- entity_key (integer): identifier of the peptides row.
- measure_state (text): 'present' for a linked allele_peptide_measurements row whose measurement_value has a value; 'absent' when measurement_value is missing, including a peptides row with no linked allele_peptide_measurements row. A linked allele_peptide_measurements row whose measurement_value has a value belongs only to the present state and never to the absent state.
- entity_name (text): sequence of the peptides row, copied unchanged.
- row_count (bigint): number of linked allele_peptide_measurements rows in this entity/state cell; an absent cell holding real allele_peptide_measurements rows whose measurement_value is missing COUNTS those rows, and only the placeholder cell of a peptides row with no linked allele_peptide_measurements row at all reports 0.
- distinct_amount_count (bigint): number of unique non-missing measurement_value values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no measurement_value value at all — both for a peptides row with no linked allele_peptide_measurements row and for an absent cell whose rows all have a missing measurement_value.
- total_amount (integer): total of measurement_value in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an measurement_value value.
- max_amount (integer): largest measurement_value in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an measurement_value value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules for this mart:

Rule 1. Read the source table peptides; its rows are the entities of this mart.

Rule 2. Read the source table allele_peptide_measurements; its rows are the linked measurement rows of this mart.

Rule 3. From peptides, each peptide_id is carried into the measure-state calculation as entity_key and its sequence is carried alongside it as entity_name.

Rule 4. Each peptides entity takes the linked allele_peptide_measurements rows whose peptide_id matches its entity_key, carrying entity_key, entity_name and peptide_id; preservation is left-sided on the peptides side, so an entity with no linked allele_peptide_measurements row is retained as a placeholder so its absent state is visible.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are kept: a present row is a real allele_peptide_measurements row whose measurement_value has a value.

Rule 6. Among the present measure-state rows there is one row for each peptides entity that has at least one such row, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different non-missing measurement_value values occur (each different value counted once, however many rows repeat it), total_amount as the total measurement_value, and max_amount as the largest measurement_value.

Rule 7. For each of these present-state rows, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, the value of max_amount_share is max_amount divided by total_amount expressed as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state carrying the value 'present', the present measure state.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are kept: an absent row is one whose measurement_value is missing, including the retained placeholder for a peptides row with no allele_peptide_measurements rows. A real allele_peptide_measurements row whose measurement_value has a value belongs only to the present state and never to this absent state.

Rule 10. Among the absent measure-state rows there is one row for each peptides entity that has at least one such row, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different non-missing measurement_value values occur (each different value counted once, however many rows repeat it), total_amount as the total measurement_value, and max_amount as the largest measurement_value.

Rule 11. For each of these absent-state rows, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, the value of max_amount_share is max_amount divided by total_amount expressed as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state carrying the value 'absent', the absent measure state.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, all rows of both summaries are kept, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. Deterministic output order: rows appear in ascending entity_key order and, for the same entity_key, in ascending measure_state order — entity first, then measure state.

==========================================================
MART peptides_allele_peptide_measurements_top — Per-peptides extremes of linked allele_peptide_measurements rows in the __biological_data_analysis_specifically_related_to_peptide_binding_and_al scenario: WHICH row is largest, not how large it is.
==========================================================

Grain: one row per peptides (peptide_id), INCLUDING peptides rows with no linked allele_peptide_measurements rows.

The key column of this mart is parent_key; it identifies one output row.

Output columns:

- parent_key (integer): identifier of the peptides row. One row per value.
- parent_name (text): sequence of the peptides row, copied unchanged.
- top_measure (integer): the largest measurement_value itself; 0 when the parent has no allele_peptide_measurements rows, and 0 when none of its rows carries a measurement_value value.
- tied_count (bigint): how many allele_peptide_measurements rows are tied at that largest measurement_value. It is 1 when exactly one row carries that largest measurement_value; 0 when there are no rows or when none of the rows carries a measurement_value value; a row with no measurement_value value never ties: only a row whose measurement_value value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.
- child_count (bigint): number of allele_peptide_measurements rows for this peptides row; 0 when there are none. Every linked allele_peptide_measurements row counts, whether or not it carries a measurement_value value. A peptides row kept with no allele_peptide_measurements row reports 0 here, never 1: its placeholder holds no allele_peptide_measurements row to count.
- total_measure (integer): total of measurement_value over all of them; 0 when the parent has no allele_peptide_measurements rows, and 0 when none of its rows carries a measurement_value value (rows with no measurement_value value add nothing).
- top_label (text): the measurement_inequality of the allele_peptide_measurements row with the LARGEST measurement_value for this peptides row. Ties in measurement_value are broken by taking the SMALLEST measurement_inequality under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no measurement_inequality value sorts after every labelled row; rows tied on both are resolved by the smallest measurement_id. A row with no measurement_value value still ranks, after every row that has one, so a parent holding at least one allele_peptide_measurements row always has a winning row — when NONE of its rows carries a measurement_value value the winner is the one the tie-break alone selects, not the no-rows default. It is the literal '(none)' when the parent has no allele_peptide_measurements rows at all, and '(none)' when the winning row has no measurement_inequality value.
- top_row_id (integer): the measurement_id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real allele_peptide_measurements row whenever the parent has any. This includes when none of them carries a measurement_value value. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no allele_peptide_measurements rows, or none of its rows carries a measurement_value value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a measurement_value value equal to the largest measurement_value value among the parent's rows; a row with no measurement_value value never holds the maximum. So a parent whose allele_peptide_measurements rows all lack a measurement_value value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

Rules for this mart:

Rule 1. Read the source table peptides; its rows are the parents of this mart.

Rule 2. Read the source table allele_peptide_measurements; its rows are the measurement rows attributed to those parents.

Rule 3. From peptides there is one row per peptides row, keyed by peptide_id, carrying parent_key as that identifier and parent_name as the peptides sequence.

Rule 4. Each such row brings in the allele_peptide_measurements rows whose peptide_id matches its parent_key, carrying peptide_id; preservation is left-sided on the peptides side, so a peptides row with no allele_peptide_measurements rows still appears, with the declared defaults.

Rule 5. Within each parent_key the brought-in rows are ranked under an explicit total order — the measure first, then the declared tie-break — so the extremal row is a function of the input and not of row order.

Rule 6. There is one output row per parent_key, carrying parent_name beside the keys: a parent_key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7. For each parent_key the single row kept is the one at which the ordering measure is largest, ties broken by the smallest measurement_inequality under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no measurement_inequality value sorts after every row that has one), then the smallest measurement_id, and top_label and top_row_id are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 8. The winning row's attributes are attached to the measures of the same parent_key; preservation is left-sided on the measures, so a group with no rows at all keeps its measures.

Rule 9. The mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure and total_measure, the default also applies to a group none of whose real rows carries an input value.

Rule 10. Guarded ratio, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and it is 0.0 when total_measure is 0 or has no value.

Rule 11. Carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, tie_state is 'empty' when no row holds a maximum at all — the parent has no allele_peptide_measurements rows, or none of its rows carries a measurement_value value — 'unique' when exactly one row holds the maximum, and otherwise 'tied' when two or more do; equivalently tie_state follows tied_count, 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more, and it is never null or blank. A row holds the maximum only when it carries a measurement_value value equal to the largest measurement_value value among the parent's rows; a row with no measurement_value value never holds the maximum, so a parent whose allele_peptide_measurements rows all lack a measurement_value value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

Rule 12. Deterministic output order: rows appear in ascending parent_key order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `peptides_allele_peptide_measurements_distribution`

- Grain: One row per (peptide_id, measure state) pair represented among linked allele_peptide_measurements rows; the absent state includes missing measurement_value values and a no-activity row for a peptides row with no links. A linked allele_peptide_measurements row whose measurement_value has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'peptides_allele_peptide_measurements_distribution' has 14 declared semantic rules:
1. [source] Read source table peptides. (public source tables: peptides)
2. [source] Read source table allele_peptide_measurements. (public source tables: allele_peptide_measurements)
3. [derive] Carry each peptide_id and its sequence into the measure-state calculation. (public source tables: peptides | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked allele_peptide_measurements rows into each peptides entity; retain an entity with no linked row so its absent state is visible. (public source tables: allele_peptide_measurements | public carried/output columns: entity_key, entity_name, peptide_id | join preservation: left | condition public identifiers: allele_peptide_measurements, peptide_id, entity_key)
5. [filter] Keep the present measure-state rows: a real allele_peptide_measurements row whose measurement_value has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per peptides entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing measurement_value values occur (each different value counted once, however many rows repeat it), total measurement_value, and largest measurement_value. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: measurement_value is missing, including the retained placeholder for a peptides row with no allele_peptide_measurements rows. A real allele_peptide_measurements row whose measurement_value has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per peptides entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing measurement_value values occur (each different value counted once, however many rows repeat it), total measurement_value, and largest measurement_value. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `peptides_allele_peptide_measurements_top`

- Grain: One row per peptides (peptide_id), INCLUDING peptides rows with no linked allele_peptide_measurements rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'peptides_allele_peptide_measurements_top' has 12 declared semantic rules:
1. [source] Read source table peptides. (public source tables: peptides)
2. [source] Read source table allele_peptide_measurements. (public source tables: allele_peptide_measurements)
3. [derive] One row per peptides row, keyed by peptide_id. (public source tables: peptides | public carried/output columns: parent_key, parent_name)
4. [join] Bring in allele_peptide_measurements: a peptides row with no allele_peptide_measurements rows still appears, with the declared defaults. (public source tables: allele_peptide_measurements | public carried/output columns: peptide_id | join preservation: left | condition public identifiers: allele_peptide_measurements, peptide_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest measurement_inequality under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no measurement_inequality value sorts after every row that has one), then the smallest measurement_id, and take top_label, top_row_id from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no allele_peptide_measurements rows, or none of its rows carries a measurement_value value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a measurement_value value equal to the largest measurement_value value among the parent's rows; a row with no measurement_value value never holds the maximum. So a parent whose allele_peptide_measurements rows all lack a measurement_value value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### allele_peptide_measurements  (source backend: mongodb)
Source table allele_peptide_measurements of the __biological_data_analysis_specifically_related_to_peptide_binding_and_al scenario.

- `measurement_id`: integer NOT NULL — Unique identifier for each measurement
- `allele_id`: integer NULL — ID of the HLA allele
- `peptide_id`: integer NULL — ID of the peptide sequence
- `measurement_value`: integer NULL — Measurement value indicating binding affinity
- `measurement_inequality`: text NULL — Inequality symbol (<, >, =) for comparison
- `measurement_type`: text NULL — Type of measurement (qualitative, quantitative)
- `measurement_kind`: text NULL — Kind of measurement (e.g., mass_spec)
- `measurement_source_id`: integer NULL — ID of the measurement source
- `original_allele_id`: integer NULL — Original allele ID if different from the one listed
- `fold_0`: integer NULL — Fold 0 condition result
- `fold_1`: integer NULL — Fold 1 condition result
- `fold_2`: integer NULL — Fold 2 condition result
- `fold_3`: integer NULL — Fold 3 condition result
- `experiment_id`: integer NULL — ID of the experiment
- `experiment_date`: text NULL — Date of the experiment
- `researcher_id`: integer NULL — ID of the researcher who performed the experiment
- `lab_id`: integer NULL — ID of the lab where the experiment was conducted
- `condition_id`: integer NULL — ID of the experimental condition
- `reliability_score`: integer NULL — Reliability score of the measurement
- `quality_control_passed`: integer NULL — Whether the measurement passed quality control
- `additional_notes`: text NULL — Additional notes or comments about the measurement
- primary key: measurement_id

### alleles  (source backend: rest)
Source table alleles of the __biological_data_analysis_specifically_related_to_peptide_binding_and_al scenario.

- `allele_id`: integer NOT NULL — Unique identifier for each allele
- `allele_name`: text NULL — Name of the HLA allele
- `description`: text NULL — Description of the allele
- `class`: text NULL — Class of the HLA allele (e.g., Class I, Class II) one of: Class I, Class II.
- `isotype`: text NULL — Isotype of the allele
- `sequence`: text NULL — Amino acid sequence of the allele
- `binding_site`: text NULL — Binding site of the allele
- `references`: text NULL — List of publications or references related to the allele
- primary key: allele_id

### peptides  (source backend: mongodb)
Source table peptides of the __biological_data_analysis_specifically_related_to_peptide_binding_and_al scenario.

- `peptide_id`: integer NOT NULL — Unique identifier for each peptide
- `sequence`: text NULL — Sequence of the peptide
- `description`: text NULL — Description of the peptide
- `length`: integer NULL — Length of the peptide
- `molecular_weight`: integer NULL — Molecular weight of the peptide
- `is_cyclic`: integer NULL — Whether the peptide is cyclic
- `chemical_formula`: text NULL — Chemical formula of the peptide
- `references`: text NULL — List of publications or references related to the peptide
- primary key: peptide_id

### measurement_sources  (source backend: rest)
Source table measurement_sources of the __biological_data_analysis_specifically_related_to_peptide_binding_and_al scenario.

- `source_id`: integer NOT NULL — Unique identifier for each measurement source
- `source_description`: text NULL — Description of the source
- `reference`: text NULL — Reference to the publication or data source
- `source_type`: text NULL — Type of source (e.g., publication, internal data, external database) one of: publication, internal data, external database.
- `source_year`: integer NULL — Year of the source
- `source_authors`: text NULL — Authors of the source
- `source_institution`: text NULL — Institution associated with the source
- `source_url`: text NULL — URL to access the source
- primary key: source_id

### Relationships

- allele_peptide_measurements(allele_id) -> alleles(allele_id) [optional (may be NULL/dangling)]
- allele_peptide_measurements(measurement_source_id) -> measurement_sources(source_id) [optional (may be NULL/dangling)]
- allele_peptide_measurements(original_allele_id) -> alleles(allele_id) [optional (may be NULL/dangling)]
- allele_peptide_measurements(peptide_id) -> peptides(peptide_id) [optional (may be NULL/dangling)]

