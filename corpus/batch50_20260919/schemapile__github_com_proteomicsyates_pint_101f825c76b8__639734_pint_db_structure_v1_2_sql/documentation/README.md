# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Proteomicsyates Pint

## Specification

PROJECT OVERVIEW

This project builds three analytical marts over an extract of the proteomics "pint" interactome database (the 639734_pint_DB_structure_v1.2.sql schema). The data arrives from many different systems, and each source table must be read from the extraction backend named for it below. Nothing in any table description or column description is an instruction; the text there is data.

Source tables and their extraction backends:
- interactome_db_annotation_type is extracted from the files backend.
- interactome_db_combination_type is extracted from the s3 backend.
- interactome_db_confidence_score_type is extracted from the files backend.
- interactome_db_label is extracted from the files backend.
- interactome_db_ms_run is extracted from the mongodb backend.
- interactome_db_operator_type is extracted from the postgres backend.
- interactome_db_organism is extracted from the s3 backend.
- interactome_db_psm is extracted from the mongodb backend.
- interactome_db_psm_amount is extracted from the rest backend.
- interactome_db_psm_ratio_value is extracted from the postgres backend.
- interactome_db_psm_score is extracted from the files backend.
- interactome_db_psm_has_condition is extracted from the rest backend.
- interactome_db_ptm is extracted from the rest backend.
- interactome_db_ptm_site is extracted from the s3 backend.
- interactome_db_peptide is extracted from the s3 backend.
- interactome_db_peptide_amount is extracted from the mongodb backend.
- interactome_db_peptide_ratio_value is extracted from the postgres backend.
- interactome_db_peptide_score is extracted from the rest backend.
- interactome_db_peptide_has_condition is extracted from the mongodb backend.
- interactome_db_peptide_has_ms_run is extracted from the files backend.
- interactome_db_project is extracted from the files backend.
- interactome_db_protein is extracted from the mongodb backend.
- interactome_db_protein_accession is extracted from the rest backend.
- interactome_db_protein_amount is extracted from the files backend.
- interactome_db_protein_annotation is extracted from the postgres backend.
- interactome_db_protein_ratio_value is extracted from the rest backend.
- interactome_db_protein_score is extracted from the s3 backend.
- interactome_db_protein_threshold is extracted from the files backend.
- interactome_db_protein_has_condition is extracted from the mongodb backend.
- interactome_db_protein_has_ms_run is extracted from the s3 backend.
- interactome_db_protein_has_psm is extracted from the mongodb backend.
- interactome_db_protein_has_peptide is extracted from the postgres backend.
- interactome_db_protein_has_protein_accession is extracted from the postgres backend.
- interactome_db_ratio_descriptor is extracted from the rest backend.
- interactome_db_sample_has_organism is extracted from the s3 backend.
- interactome_db_threshold is extracted from the files backend.
- interactome_db_tissue is extracted from the postgres backend.

RELATIONSHIPS BETWEEN THE SOURCE TABLES

Each line below states a child table with its key columns, the parent table with its key columns, and whether the relationship is required or optional. An optional relationship may carry a NULL value or a dangling reference.

- Child interactome_db_ms_run(project_id) refers to parent interactome_db_project(id); required.
- Child interactome_db_peptide_amount(combination_type_name) refers to parent interactome_db_combination_type(name); optional (may be NULL or dangling).
- Child interactome_db_peptide_amount(peptide_id) refers to parent interactome_db_peptide(id); required.
- Child interactome_db_peptide_has_condition(peptide_id) refers to parent interactome_db_peptide(id); required.
- Child interactome_db_peptide_has_ms_run(ms_run_id) refers to parent interactome_db_ms_run(id); required.
- Child interactome_db_peptide_has_ms_run(peptide_id) refers to parent interactome_db_peptide(id); required.
- Child interactome_db_peptide_ratio_value(combination_type_name) refers to parent interactome_db_combination_type(name); optional (may be NULL or dangling).
- Child interactome_db_peptide_ratio_value(confidence_score_type_name) refers to parent interactome_db_confidence_score_type(name); optional (may be NULL or dangling).
- Child interactome_db_peptide_ratio_value(peptide_id) refers to parent interactome_db_peptide(id); required.
- Child interactome_db_peptide_ratio_value(ratio_descriptor_id) refers to parent interactome_db_ratio_descriptor(id); required.
- Child interactome_db_peptide_score(confidence_score_type_name) refers to parent interactome_db_confidence_score_type(name); required.
- Child interactome_db_peptide_score(peptide_id) refers to parent interactome_db_peptide(id); required.
- Child interactome_db_protein(organism_taxonomyid) refers to parent interactome_db_organism(taxonomyid); required.
- Child interactome_db_protein_amount(combination_type_name) refers to parent interactome_db_combination_type(name); optional (may be NULL or dangling).
- Child interactome_db_protein_amount(protein_id) refers to parent interactome_db_protein(id); required.
- Child interactome_db_protein_annotation(annotation_type_name) refers to parent interactome_db_annotation_type(name); required.
- Child interactome_db_protein_annotation(protein_id) refers to parent interactome_db_protein(id); required.
- Child interactome_db_protein_has_condition(protein_id) refers to parent interactome_db_protein(id); required.
- Child interactome_db_protein_has_ms_run(ms_run_id) refers to parent interactome_db_ms_run(id); required.
- Child interactome_db_protein_has_ms_run(protein_id) refers to parent interactome_db_protein(id); required.
- Child interactome_db_protein_has_peptide(peptide_id) refers to parent interactome_db_peptide(id); required.
- Child interactome_db_protein_has_peptide(protein_id) refers to parent interactome_db_protein(id); required.
- Child interactome_db_protein_has_protein_accession(protein_accession_accession) refers to parent interactome_db_protein_accession(accession); required.
- Child interactome_db_protein_has_protein_accession(protein_id) refers to parent interactome_db_protein(id); required.
- Child interactome_db_protein_has_psm(protein_id) refers to parent interactome_db_protein(id); required.
- Child interactome_db_protein_has_psm(psm_id) refers to parent interactome_db_psm(id); required.
- Child interactome_db_protein_ratio_value(combination_type_name) refers to parent interactome_db_combination_type(name); optional (may be NULL or dangling).
- Child interactome_db_protein_ratio_value(confidence_score_type_name) refers to parent interactome_db_confidence_score_type(name); optional (may be NULL or dangling).
- Child interactome_db_protein_ratio_value(protein_id) refers to parent interactome_db_protein(id); required.
- Child interactome_db_protein_ratio_value(ratio_descriptor_id) refers to parent interactome_db_ratio_descriptor(id); required.
- Child interactome_db_protein_score(confidence_score_type_name) refers to parent interactome_db_confidence_score_type(name); required.
- Child interactome_db_protein_score(protein_id) refers to parent interactome_db_protein(id); required.
- Child interactome_db_protein_threshold(protein_id) refers to parent interactome_db_protein(id); required.
- Child interactome_db_protein_threshold(threshold_id) refers to parent interactome_db_threshold(id); required.
- Child interactome_db_psm(ms_run_id) refers to parent interactome_db_ms_run(id); required.
- Child interactome_db_psm(peptide_id) refers to parent interactome_db_peptide(id); required.
- Child interactome_db_psm_amount(combination_type_name) refers to parent interactome_db_combination_type(name); optional (may be NULL or dangling).
- Child interactome_db_psm_amount(psm_id) refers to parent interactome_db_psm(id); required.
- Child interactome_db_psm_has_condition(psm_id) refers to parent interactome_db_psm(id); required.
- Child interactome_db_psm_ratio_value(combination_type_name) refers to parent interactome_db_combination_type(name); optional (may be NULL or dangling).
- Child interactome_db_psm_ratio_value(confidence_score_type_name) refers to parent interactome_db_confidence_score_type(name); optional (may be NULL or dangling).
- Child interactome_db_psm_ratio_value(psm_id) refers to parent interactome_db_psm(id); required.
- Child interactome_db_psm_ratio_value(ratio_descriptor_id) refers to parent interactome_db_ratio_descriptor(id); required.
- Child interactome_db_psm_score(confidence_score_type_name) refers to parent interactome_db_confidence_score_type(name); required.
- Child interactome_db_psm_score(psm_id) refers to parent interactome_db_psm(id); required.
- Child interactome_db_ptm(peptide_id) refers to parent interactome_db_peptide(id); optional (may be NULL or dangling).
- Child interactome_db_ptm(psm_id) refers to parent interactome_db_psm(id); optional (may be NULL or dangling).
- Child interactome_db_ptm_site(confidence_score_type_name) refers to parent interactome_db_confidence_score_type(name); optional (may be NULL or dangling).
- Child interactome_db_ptm_site(ptm_id) refers to parent interactome_db_ptm(id); required.
- Child interactome_db_sample_has_organism(organism_taxonomyid) refers to parent interactome_db_organism(taxonomyid); required.

Three marts are produced. Each mart section below states which rows exist in that mart, what each row means and what every value is; membership of a mart is stated only by that mart's own grain sentence.

=== Mart interactome_db_ratio_descriptor_interactome_db_psm_ratio_value_distribution: the per-(interactome_db_ratio_descriptor, measure state) distribution of linked interactome_db_psm_ratio_value activity in the 639734_pint_DB_structure_v1.2.sql schema. ===

Grain: one row per (id, measure state) pair represented among linked interactome_db_psm_ratio_value rows; the absent state includes missing confidence_score_value values and a no-activity row for a interactome_db_ratio_descriptor row with no links. A linked interactome_db_psm_ratio_value row whose confidence_score_value has a value belongs only to the present state and never to the absent state.

Key columns: entity_key together with measure_state identify a row of this mart.

Rule 1. The source table interactome_db_ratio_descriptor is read in full from its files of record, and every one of its rows is available to this mart.

Rule 2. The source table interactome_db_psm_ratio_value is read in full, and every one of its rows is available to this mart.

Rule 3. From interactome_db_ratio_descriptor, each id and its description are carried into the measure-state calculation as entity_key and entity_name respectively.

Rule 4. The linked interactome_db_psm_ratio_value rows are brought into each interactome_db_ratio_descriptor entity, a interactome_db_psm_ratio_value row being linked when its ratio_descriptor_id equals the entity_key, and preservation is left-sided on the entity side: an entity with no linked row is retained so its absent state is visible, carrying entity_key, entity_name and, where a linked row exists, that row's id and ratio_descriptor_id.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are exactly those that are a real interactome_db_psm_ratio_value row whose confidence_score_value has a value; those rows are kept for the present state.

Rule 6. One row exists per interactome_db_ratio_descriptor entity that has at least one row in the present measure state, and no row here for an entity with none, reporting for entity_key and entity_name the row_count of those rows, distinct_amount_count as how many different non-missing confidence_score_value values occur (each different value counted once, however many rows repeat it), total_amount as the total confidence_score_value, and max_amount as the largest confidence_score_value.

Rule 7. For each such row, beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures are labelled as the present measure state: measure_state reads 'present' on each such row, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are exactly those whose confidence_score_value is missing, including the retained placeholder for a interactome_db_ratio_descriptor row with no interactome_db_psm_ratio_value rows. A real interactome_db_psm_ratio_value row whose confidence_score_value has a value belongs only to the present state and never to this absent state.

Rule 10. One row exists per interactome_db_ratio_descriptor entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting for entity_key and entity_name the row_count of those rows, distinct_amount_count as how many different non-missing confidence_score_value values occur (each different value counted once, however many rows repeat it), total_amount as the total confidence_score_value, and max_amount as the largest confidence_score_value.

Rule 11. For each such absent-state row, beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures are labelled as the absent measure state: measure_state reads 'absent' on each such row, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. The output order is deterministic: rows appear in ascending entity_key order, and within one entity in ascending measure_state order.

Output columns of this mart:
- entity_key (integer): the identifier of the interactome_db_ratio_descriptor row.
- measure_state (text): 'present' for a linked interactome_db_psm_ratio_value row whose confidence_score_value has a value; 'absent' when confidence_score_value is missing, including a interactome_db_ratio_descriptor row with no linked interactome_db_psm_ratio_value row. A linked interactome_db_psm_ratio_value row whose confidence_score_value has a value belongs only to the present state and never to the absent state.
- entity_name (text): the description of the interactome_db_ratio_descriptor row, copied unchanged.
- row_count (bigint): the number of linked interactome_db_psm_ratio_value rows in this entity/state cell; an absent cell holding real interactome_db_psm_ratio_value rows whose confidence_score_value is missing COUNTS those rows, and only the placeholder cell of an interactome_db_ratio_descriptor row with no linked interactome_db_psm_ratio_value row at all reports 0.
- distinct_amount_count (bigint): the number of unique non-missing confidence_score_value values in this cell; each unique non-missing value is counted once, however many rows repeat it; it is 0 whenever the cell holds no confidence_score_value value at all — both for an interactome_db_ratio_descriptor row with no linked interactome_db_psm_ratio_value row and for an absent cell whose rows all have a missing confidence_score_value.
- total_amount (float): the sum of confidence_score_value in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an confidence_score_value value.
- max_amount (float): the largest confidence_score_value in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an confidence_score_value value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

=== Mart interactome_db_ptm_interactome_db_ptm_site_top: per-interactome_db_ptm extremes over linked interactome_db_ptm_site rows in the 639734_pint_DB_structure_v1.2.sql schema: WHICH row is largest, not how large it is. ===

Grain: one row per interactome_db_ptm (id), INCLUDING interactome_db_ptm rows with no linked interactome_db_ptm_site rows.

Key column: parent_key identifies a row of this mart.

Rule 1. The source table interactome_db_ptm is read in full, and every one of its rows is available to this mart.

Rule 2. The source table interactome_db_ptm_site is read in full, and every one of its rows is available to this mart.

Rule 3. From interactome_db_ptm there is one row per interactome_db_ptm row, keyed by id, which becomes parent_key, and carrying parent_name.

Rule 4. The interactome_db_ptm_site rows are brought in, matched by ptm_id equal to parent_key and carrying ptm_id and id; preservation is left-sided on the interactome_db_ptm side, so a interactome_db_ptm row with no interactome_db_ptm_site rows still appears, with the declared defaults.

Rule 5. Within each parent_key the matched rows are ranked under an explicit total order — the measure first, then the declared tie-break — so that the extremal row is a function of the input and not of row order.

Rule 6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7. The single row kept per parent_key is the one at which the ordering measure is largest; ties are broken by the smallest aa under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then by the smallest id, and top_label and top_row_id are taken from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows.

Rule 8. The extremal row's attributes are attached to the grouped measures by matching parent_key, with left-sided preservation of the measures, so a group with no rows at all keeps its measures.

Rule 9. The mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults — never NULL — for a group with no matching rows.

Rule 10. Guarded ratios, alongside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when total_measure is 0 or NULL.

Rule 11. Alongside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, tie_state is 'empty' when no row holds a maximum at all — the parent has no interactome_db_ptm_site rows — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more, and it is one of these three labels, never null or blank.

Rule 12. The output order is deterministic: rows are sorted in ascending parent_key order.

Output columns of this mart:
- parent_key (integer): the identifier of the interactome_db_ptm row. One row per value.
- parent_name (text): the cv_id of the interactome_db_ptm row, copied unchanged.
- top_measure (integer): the largest position itself; 0 when the parent has no interactome_db_ptm_site rows.
- tied_count (bigint): how many interactome_db_ptm_site rows are tied at that largest position; 1 when exactly one row carries that largest position; 0 when there are no rows.
- child_count (bigint): the number of interactome_db_ptm_site rows for this interactome_db_ptm row; 0 when there are none. An interactome_db_ptm row kept with no interactome_db_ptm_site row reports 0 here, never 1: its placeholder holds no interactome_db_ptm_site row to count.
- total_measure (integer): the sum of position over all of them; 0 when the parent has no interactome_db_ptm_site rows.
- top_label (text): the aa of the interactome_db_ptm_site row with the LARGEST position for this interactome_db_ptm row. Ties in position are broken by taking the SMALLEST aa under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one); rows tied on both are resolved by the smallest id. It is the literal '(none)' when the parent has no interactome_db_ptm_site rows at all.
- top_row_id (integer): the id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real interactome_db_ptm_site row whenever the parent has any. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no interactome_db_ptm_site rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more.

=== Mart interactome_db_psm_interactome_db_ptm_distribution: the per-(interactome_db_psm, measure state) distribution of linked interactome_db_ptm activity in the 639734_pint_DB_structure_v1.2.sql schema. ===

Grain: one row per (id, measure state) pair represented by linked interactome_db_ptm rows, plus one absent no-activity row for a interactome_db_psm row with no links. Because mass_shift is required, no linked interactome_db_ptm row belongs to the absent state.

Key columns: entity_key together with measure_state identify a row of this mart.

Rule 1. The source table interactome_db_psm is read in full, and every one of its rows is available to this mart.

Rule 2. The source table interactome_db_ptm is read in full, and every one of its rows is available to this mart.

Rule 3. From interactome_db_psm, each id and its afterseq are carried into the measure-state calculation as entity_key and entity_name respectively.

Rule 4. The linked interactome_db_ptm rows are brought into each interactome_db_psm entity, a interactome_db_ptm row being linked when its psm_id equals the entity_key, and preservation is left-sided on the entity side: an entity with no linked row is retained so its absent state is visible, carrying entity_key, entity_name and, where a linked row exists, that row's id and psm_id.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are exactly those that are a real interactome_db_ptm row; mass_shift is required on every such row, and those rows are kept for the present state.

Rule 6. One row exists per interactome_db_psm entity that has at least one row in the present measure state, and no row here for an entity with none, reporting for entity_key and entity_name the row_count of those rows, distinct_amount_count as how many different mass_shift values occur (each different value counted once, however many rows repeat it), total_amount as the total mass_shift, and max_amount as the largest mass_shift.

Rule 7. For each such row, beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures are labelled as the present measure state: measure_state reads 'present' on each such row, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are exactly the retained placeholder for a interactome_db_psm row with no interactome_db_ptm rows; no real row can enter this state because mass_shift is required.

Rule 10. One row exists per interactome_db_psm entity with no linked interactome_db_ptm row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked interactome_db_ptm row, reporting for entity_key and entity_name a row_count of 0, distinct_amount_count of 0 different mass_shift values, a total_amount of mass_shift of 0 and a max_amount, the largest mass_shift, of 0.

Rule 11. For each such absent-state row, beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures are labelled as the absent measure state: measure_state reads 'absent' on each such row, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. The output order is deterministic: rows appear in ascending entity_key order, and within one entity in ascending measure_state order.

Output columns of this mart:
- entity_key (integer): the identifier of the interactome_db_psm row.
- measure_state (text): 'present' for a linked interactome_db_ptm row; 'absent' only for a interactome_db_psm row with no linked interactome_db_ptm row. mass_shift is required on every real interactome_db_ptm row.
- entity_name (text): the afterseq of the interactome_db_psm row, copied unchanged.
- row_count (bigint): the number of linked interactome_db_ptm rows in this entity/state cell; 0 for a no-activity absent cell.
- distinct_amount_count (bigint): the number of unique mass_shift values in this cell; each unique value is counted once, however many rows repeat it; 0 for a no-activity absent cell.
- total_amount (float): the sum of mass_shift in this cell; 0 for a no-activity absent cell.
- max_amount (float): the largest mass_shift in this cell; 0 for a no-activity absent cell.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `interactome_db_ratio_descriptor_interactome_db_psm_ratio_value_distribution`

- Grain: One row per (id, measure state) pair represented among linked interactome_db_psm_ratio_value rows; the absent state includes missing confidence_score_value values and a no-activity row for a interactome_db_ratio_descriptor row with no links. A linked interactome_db_psm_ratio_value row whose confidence_score_value has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'interactome_db_ratio_descriptor_interactome_db_psm_ratio_value_distribution' has 14 declared semantic rules:
1. [source] Read source table interactome_db_ratio_descriptor. (public source tables: interactome_db_ratio_descriptor)
2. [source] Read source table interactome_db_psm_ratio_value. (public source tables: interactome_db_psm_ratio_value)
3. [derive] Carry each id and its description into the measure-state calculation. (public source tables: interactome_db_ratio_descriptor | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked interactome_db_psm_ratio_value rows into each interactome_db_ratio_descriptor entity; retain an entity with no linked row so its absent state is visible. (public source tables: interactome_db_psm_ratio_value | public carried/output columns: entity_key, entity_name, id, ratio_descriptor_id | join preservation: left | condition public identifiers: interactome_db_psm_ratio_value, ratio_descriptor_id, entity_key)
5. [filter] Keep the present measure-state rows: a real interactome_db_psm_ratio_value row whose confidence_score_value has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per interactome_db_ratio_descriptor entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing confidence_score_value values occur (each different value counted once, however many rows repeat it), total confidence_score_value, and largest confidence_score_value. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: confidence_score_value is missing, including the retained placeholder for a interactome_db_ratio_descriptor row with no interactome_db_psm_ratio_value rows. A real interactome_db_psm_ratio_value row whose confidence_score_value has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per interactome_db_ratio_descriptor entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing confidence_score_value values occur (each different value counted once, however many rows repeat it), total confidence_score_value, and largest confidence_score_value. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `interactome_db_ptm_interactome_db_ptm_site_top`

- Grain: One row per interactome_db_ptm (id), INCLUDING interactome_db_ptm rows with no linked interactome_db_ptm_site rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'interactome_db_ptm_interactome_db_ptm_site_top' has 12 declared semantic rules:
1. [source] Read source table interactome_db_ptm. (public source tables: interactome_db_ptm)
2. [source] Read source table interactome_db_ptm_site. (public source tables: interactome_db_ptm_site)
3. [derive] One row per interactome_db_ptm row, keyed by id. (public source tables: interactome_db_ptm | public carried/output columns: parent_key, parent_name)
4. [join] Bring in interactome_db_ptm_site: a interactome_db_ptm row with no interactome_db_ptm_site rows still appears, with the declared defaults. (public source tables: interactome_db_ptm_site | public carried/output columns: ptm_id, id | join preservation: left | condition public identifiers: interactome_db_ptm_site, ptm_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest aa under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then the smallest id, and take top_label, top_row_id from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no interactome_db_ptm_site rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `interactome_db_psm_interactome_db_ptm_distribution`

- Grain: One row per (id, measure state) pair represented by linked interactome_db_ptm rows, plus one absent no-activity row for a interactome_db_psm row with no links. Because mass_shift is required, no linked interactome_db_ptm row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'interactome_db_psm_interactome_db_ptm_distribution' has 14 declared semantic rules:
1. [source] Read source table interactome_db_psm. (public source tables: interactome_db_psm)
2. [source] Read source table interactome_db_ptm. (public source tables: interactome_db_ptm)
3. [derive] Carry each id and its afterseq into the measure-state calculation. (public source tables: interactome_db_psm | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked interactome_db_ptm rows into each interactome_db_psm entity; retain an entity with no linked row so its absent state is visible. (public source tables: interactome_db_ptm | public carried/output columns: entity_key, entity_name, id, psm_id | join preservation: left | condition public identifiers: interactome_db_ptm, psm_id, entity_key)
5. [filter] Keep the present measure-state rows: a real interactome_db_ptm row; mass_shift is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per interactome_db_psm entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different mass_shift values occur (each different value counted once, however many rows repeat it), total mass_shift, and largest mass_shift. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a interactome_db_psm row with no interactome_db_ptm rows; no real row can enter this state because mass_shift is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per interactome_db_psm entity with no linked interactome_db_ptm row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked interactome_db_ptm row, reporting a row count of 0, 0 different mass_shift values, a total mass_shift of 0 and a largest mass_shift of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### interactome_db_annotation_type  (source backend: files)
Source table interactome_db.Annotation_Type.

- `name`: text NOT NULL — Column name of table interactome_db.Annotation_Type.
- primary key: name

### interactome_db_combination_type  (source backend: s3)
Source table interactome_db.Combination_Type.

- `description`: text NULL — Column description of table interactome_db.Combination_Type.
- `name`: text NOT NULL — Column name of table interactome_db.Combination_Type.
- primary key: name

### interactome_db_confidence_score_type  (source backend: files)
Source table interactome_db.Confidence_Score_Type.

- `description`: text NULL — Column description of table interactome_db.Confidence_Score_Type.
- `name`: text NOT NULL — Column name of table interactome_db.Confidence_Score_Type.
- primary key: name

### interactome_db_label  (source backend: files)
Source table interactome_db.Label.

- `id`: integer NOT NULL — Column id of table interactome_db.Label.
- `massdiff`: float NULL — Column massDiff of table interactome_db.Label.
- `name`: text NOT NULL — Column name of table interactome_db.Label.
- primary key: id

### interactome_db_ms_run  (source backend: mongodb)
Source table interactome_db.MS_Run.

- `project_id`: integer NOT NULL — Column Project_id of table interactome_db.MS_Run.
- `date`: date NULL — Column date of table interactome_db.MS_Run.
- `id`: integer NOT NULL — Column id of table interactome_db.MS_Run.
- `path`: text NOT NULL — Column path of table interactome_db.MS_Run.
- `runid`: text NOT NULL — Column runID of table interactome_db.MS_Run.
- primary key: id

### interactome_db_operator_type  (source backend: postgres)
Source table interactome_db.Operator_Type.

- `name`: text NOT NULL — Column name of table interactome_db.Operator_Type.
- primary key: name

### interactome_db_organism  (source backend: s3)
Source table interactome_db.Organism.

- `name`: text NULL — Column name of table interactome_db.Organism.
- `taxonomyid`: text NOT NULL — Column taxonomyID of table interactome_db.Organism.
- primary key: taxonomyid

### interactome_db_psm  (source backend: mongodb)
Source table interactome_db.PSM.

- `mh`: float NULL — Column MH of table interactome_db.PSM.
- `ms_run_id`: integer NOT NULL — Column MS_Run_id of table interactome_db.PSM.
- `peptide_id`: integer NOT NULL — Column Peptide_id of table interactome_db.PSM.
- `afterseq`: text NULL — Column afterSeq of table interactome_db.PSM.
- `beforeseq`: text NULL — Column beforeSeq of table interactome_db.PSM.
- `cal_mh`: float NULL — Column cal_mh of table interactome_db.PSM.
- `chargestate`: text NULL — Column chargeState of table interactome_db.PSM.
- `full_sequence`: text NOT NULL — Column full_sequence of table interactome_db.PSM.
- `id`: integer NOT NULL — Column id of table interactome_db.PSM.
- `ion_proportion`: float NULL — Column ion_proportion of table interactome_db.PSM.
- `pi`: float NULL — Column pi of table interactome_db.PSM.
- `ppm_error`: float NULL — Column ppm_error of table interactome_db.PSM.
- `psmid`: text NULL — Column psmID of table interactome_db.PSM.
- `sequence`: text NOT NULL — Column sequence of table interactome_db.PSM.
- `spr`: integer NULL — Column spr of table interactome_db.PSM.
- `total_intensity`: float NULL — Column total_intensity of table interactome_db.PSM.
- primary key: id

### interactome_db_psm_amount  (source backend: rest)
Source table interactome_db.PSM_Amount.

- `amount_type_name`: text NOT NULL — Column Amount_Type_name of table interactome_db.PSM_Amount.
- `combination_type_name`: text NULL — Column Combination_Type_name of table interactome_db.PSM_Amount.
- `condition_id`: integer NOT NULL — Column Condition_id of table interactome_db.PSM_Amount.
- `psm_id`: integer NOT NULL — Column PSM_id of table interactome_db.PSM_Amount.
- `singleton`: integer NULL — Column Singleton of table interactome_db.PSM_Amount.
- `id`: integer NOT NULL — Column id of table interactome_db.PSM_Amount.
- `value`: float NOT NULL — Column value of table interactome_db.PSM_Amount.
- primary key: id

### interactome_db_psm_ratio_value  (source backend: postgres)
Source table interactome_db.PSM_Ratio_Value.

- `combination_type_name`: text NULL — Column Combination_Type_name of table interactome_db.PSM_Ratio_Value.
- `confidence_score_type_name`: text NULL — Column Confidence_Score_Type_name of table interactome_db.PSM_Ratio_Value.
- `psm_id`: integer NOT NULL — Column PSM_id of table interactome_db.PSM_Ratio_Value.
- `ratio_descriptor_id`: integer NOT NULL — Column Ratio_Descriptor_id of table interactome_db.PSM_Ratio_Value.
- `confidence_score_name`: text NULL — Column confidence_score_name of table interactome_db.PSM_Ratio_Value.
- `confidence_score_value`: float NULL — Column confidence_score_value of table interactome_db.PSM_Ratio_Value.
- `id`: integer NOT NULL — Column id of table interactome_db.PSM_Ratio_Value.
- `value`: float NOT NULL — Column value of table interactome_db.PSM_Ratio_Value.
- primary key: id

### interactome_db_psm_score  (source backend: files)
Source table interactome_db.PSM_Score.

- `confidence_score_type_name`: text NOT NULL — Column Confidence_Score_Type_name of table interactome_db.PSM_Score.
- `psm_id`: integer NOT NULL — Column PSM_id of table interactome_db.PSM_Score.
- `id`: integer NOT NULL — Column id of table interactome_db.PSM_Score.
- `name`: text NOT NULL — Column name of table interactome_db.PSM_Score.
- `value`: float NOT NULL — Column value of table interactome_db.PSM_Score.
- primary key: id

### interactome_db_psm_has_condition  (source backend: rest)
Source table interactome_db.PSM_has_Condition.

- `condition_id`: integer NOT NULL — Column Condition_id of table interactome_db.PSM_has_Condition.
- `psm_id`: integer NOT NULL — Column PSM_id of table interactome_db.PSM_has_Condition.
- primary key: condition_id, psm_id

### interactome_db_ptm  (source backend: rest)
Source table interactome_db.PTM.

- `psm_id`: integer NULL — Column PSM_id of table interactome_db.PTM.
- `peptide_id`: integer NULL — Column Peptide_id of table interactome_db.PTM.
- `cv_id`: text NULL — Column cv_id of table interactome_db.PTM.
- `id`: integer NOT NULL — Column id of table interactome_db.PTM.
- `mass_shift`: float NOT NULL — Column mass_shift of table interactome_db.PTM.
- `name`: text NOT NULL — Column name of table interactome_db.PTM.
- primary key: id

### interactome_db_ptm_site  (source backend: s3)
Source table interactome_db.PTM_site.

- `confidence_score_type_name`: text NULL — Column Confidence_Score_Type_name of table interactome_db.PTM_site.
- `ptm_id`: integer NOT NULL — Column PTM_id of table interactome_db.PTM_site.
- `aa`: text NOT NULL — Column aa of table interactome_db.PTM_site.
- `confidence_score_name`: text NULL — Column confidence_score_name of table interactome_db.PTM_site.
- `confidence_score_value`: text NULL — Column confidence_score_value of table interactome_db.PTM_site.
- `id`: integer NOT NULL — Column id of table interactome_db.PTM_site.
- `position`: integer NOT NULL — Column position of table interactome_db.PTM_site.
- primary key: id

### interactome_db_peptide  (source backend: s3)
Source table interactome_db.Peptide.

- `id`: integer NOT NULL — Column id of table interactome_db.Peptide.
- `sequence`: text NOT NULL — Column sequence of table interactome_db.Peptide.
- primary key: id

### interactome_db_peptide_amount  (source backend: mongodb)
Source table interactome_db.Peptide_Amount.

- `amount_type_name`: text NOT NULL — Column Amount_Type_name of table interactome_db.Peptide_Amount.
- `combination_type_name`: text NULL — Column Combination_Type_name of table interactome_db.Peptide_Amount.
- `condition_id`: integer NOT NULL — Column Condition_id of table interactome_db.Peptide_Amount.
- `peptide_id`: integer NOT NULL — Column Peptide_id of table interactome_db.Peptide_Amount.
- `id`: integer NOT NULL — Column id of table interactome_db.Peptide_Amount.
- `value`: float NOT NULL — Column value of table interactome_db.Peptide_Amount.
- primary key: id

### interactome_db_peptide_ratio_value  (source backend: postgres)
Source table interactome_db.Peptide_Ratio_Value.

- `combination_type_name`: text NULL — Column Combination_Type_name of table interactome_db.Peptide_Ratio_Value.
- `confidence_score_type_name`: text NULL — Column Confidence_Score_Type_name of table interactome_db.Peptide_Ratio_Value.
- `peptide_id`: integer NOT NULL — Column Peptide_id of table interactome_db.Peptide_Ratio_Value.
- `ratio_descriptor_id`: integer NOT NULL — Column Ratio_Descriptor_id of table interactome_db.Peptide_Ratio_Value.
- `confidence_score_name`: text NULL — Column confidence_score_name of table interactome_db.Peptide_Ratio_Value.
- `confidence_score_value`: float NULL — Column confidence_score_value of table interactome_db.Peptide_Ratio_Value.
- `id`: integer NOT NULL — Column id of table interactome_db.Peptide_Ratio_Value.
- `value`: float NOT NULL — Column value of table interactome_db.Peptide_Ratio_Value.
- primary key: id

### interactome_db_peptide_score  (source backend: rest)
Source table interactome_db.Peptide_Score.

- `confidence_score_type_name`: text NOT NULL — Column Confidence_Score_Type_name of table interactome_db.Peptide_Score.
- `peptide_id`: integer NOT NULL — Column Peptide_id of table interactome_db.Peptide_Score.
- `id`: integer NOT NULL — Column id of table interactome_db.Peptide_Score.
- `name`: text NOT NULL — Column name of table interactome_db.Peptide_Score.
- `value`: float NOT NULL — Column value of table interactome_db.Peptide_Score.
- primary key: id

### interactome_db_peptide_has_condition  (source backend: mongodb)
Source table interactome_db.Peptide_has_Condition.

- `condition_id`: integer NOT NULL — Column Condition_id of table interactome_db.Peptide_has_Condition.
- `peptide_id`: integer NOT NULL — Column Peptide_id of table interactome_db.Peptide_has_Condition.
- primary key: condition_id, peptide_id

### interactome_db_peptide_has_ms_run  (source backend: files)
Source table interactome_db.Peptide_has_MS_Run.

- `ms_run_id`: integer NOT NULL — Column MS_Run_id of table interactome_db.Peptide_has_MS_Run.
- `peptide_id`: integer NOT NULL — Column Peptide_id of table interactome_db.Peptide_has_MS_Run.
- primary key: ms_run_id, peptide_id

### interactome_db_project  (source backend: files)
Source table interactome_db.Project.

- `big`: integer NOT NULL — Column big of table interactome_db.Project.
- `description`: text NULL — Column description of table interactome_db.Project.
- `hidden`: integer NOT NULL — Column hidden of table interactome_db.Project.
- `id`: integer NOT NULL — Column id of table interactome_db.Project.
- `name`: text NOT NULL — Column name of table interactome_db.Project.
- `private`: integer NOT NULL — Column private of table interactome_db.Project.
- `pubmedlink`: text NULL — Column pubmedLink of table interactome_db.Project.
- `releasedate`: date NULL — Column releaseDate of table interactome_db.Project.
- `tag`: text NOT NULL — Column tag of table interactome_db.Project.
- `uploadeddate`: date NOT NULL — Column uploadedDate of table interactome_db.Project.
- primary key: id

### interactome_db_protein  (source backend: mongodb)
Source table interactome_db.Protein.

- `organism_taxonomyid`: text NOT NULL — Column Organism_taxonomyID of table interactome_db.Protein.
- `acc`: text NOT NULL — Column acc of table interactome_db.Protein.
- `id`: integer NOT NULL — Column id of table interactome_db.Protein.
- `length`: integer NULL — Column length of table interactome_db.Protein.
- `mw`: float NULL — Column mw of table interactome_db.Protein.
- `pi`: float NULL — Column pi of table interactome_db.Protein.
- primary key: id

### interactome_db_protein_accession  (source backend: rest)
Source table interactome_db.Protein_Accession.

- `accession`: text NOT NULL — Column accession of table interactome_db.Protein_Accession.
- `accessiontype`: text NOT NULL — Column accessionType of table interactome_db.Protein_Accession.
- `alternativenames`: text NULL — separated by special character ´***´
- `description`: text NULL — Column description of table interactome_db.Protein_Accession.
- `isprimary`: integer NOT NULL — Column isPrimary of table interactome_db.Protein_Accession.
- primary key: accession

### interactome_db_protein_amount  (source backend: files)
Source table interactome_db.Protein_Amount.

- `amount_type_name`: text NOT NULL — Column Amount_Type_name of table interactome_db.Protein_Amount.
- `combination_type_name`: text NULL — Column Combination_Type_name of table interactome_db.Protein_Amount.
- `condition_id`: integer NOT NULL — Column Condition_id of table interactome_db.Protein_Amount.
- `manual_spc`: integer NULL — if Manual_spc is true, means that that protein amount has been calculated somehow manually or by any custom method, so it will be treated in a different way by the interface
- `protein_id`: integer NOT NULL — Column Protein_id of table interactome_db.Protein_Amount.
- `id`: integer NOT NULL — Column id of table interactome_db.Protein_Amount.
- `value`: float NOT NULL — Column value of table interactome_db.Protein_Amount.
- primary key: id

### interactome_db_protein_annotation  (source backend: postgres)
Source table interactome_db.Protein_Annotation.

- `annotation_type_name`: text NOT NULL — Column Annotation_Type_name of table interactome_db.Protein_Annotation.
- `protein_id`: integer NOT NULL — Column Protein_id of table interactome_db.Protein_Annotation.
- `id`: integer NOT NULL — Column id of table interactome_db.Protein_Annotation.
- `name`: text NOT NULL — Column name of table interactome_db.Protein_Annotation.
- `source`: text NULL — description of the source of the annotation, i.e. GO , genemania...manual annotation...
- `value`: text NULL — Column value of table interactome_db.Protein_Annotation.
- primary key: id

### interactome_db_protein_ratio_value  (source backend: rest)
Source table interactome_db.Protein_Ratio_Value.

- `combination_type_name`: text NULL — Column Combination_Type_name of table interactome_db.Protein_Ratio_Value.
- `confidence_score_type_name`: text NULL — Column Confidence_Score_Type_name of table interactome_db.Protein_Ratio_Value.
- `protein_id`: integer NOT NULL — Column Protein_id of table interactome_db.Protein_Ratio_Value.
- `ratio_descriptor_id`: integer NOT NULL — Column Ratio_Descriptor_id of table interactome_db.Protein_Ratio_Value.
- `confidence_score_name`: text NULL — Column confidence_score_name of table interactome_db.Protein_Ratio_Value.
- `confidence_score_value`: float NULL — Column confidence_score_value of table interactome_db.Protein_Ratio_Value.
- `id`: integer NOT NULL — Column id of table interactome_db.Protein_Ratio_Value.
- `value`: float NOT NULL — Column value of table interactome_db.Protein_Ratio_Value.
- primary key: id

### interactome_db_protein_score  (source backend: s3)
Source table interactome_db.Protein_Score.

- `confidence_score_type_name`: text NOT NULL — Column Confidence_Score_Type_name of table interactome_db.Protein_Score.
- `protein_id`: integer NOT NULL — Column Protein_id of table interactome_db.Protein_Score.
- `id`: integer NOT NULL — Column id of table interactome_db.Protein_Score.
- `name`: text NOT NULL — Column name of table interactome_db.Protein_Score.
- `value`: float NOT NULL — Column value of table interactome_db.Protein_Score.
- primary key: id

### interactome_db_protein_threshold  (source backend: files)
Source table interactome_db.Protein_Threshold.

- `protein_id`: integer NOT NULL — Column Protein_id of table interactome_db.Protein_Threshold.
- `threshold_id`: integer NOT NULL — Column Threshold_id of table interactome_db.Protein_Threshold.
- `id`: integer NOT NULL — Column id of table interactome_db.Protein_Threshold.
- `pass_threshold`: integer NOT NULL — Column pass_threshold of table interactome_db.Protein_Threshold.
- primary key: id

### interactome_db_protein_has_condition  (source backend: mongodb)
Source table interactome_db.Protein_has_Condition.

- `condition_id`: integer NOT NULL — Column Condition_id of table interactome_db.Protein_has_Condition.
- `protein_id`: integer NOT NULL — Column Protein_id of table interactome_db.Protein_has_Condition.
- primary key: condition_id, protein_id

### interactome_db_protein_has_ms_run  (source backend: s3)
Source table interactome_db.Protein_has_MS_Run.

- `ms_run_id`: integer NOT NULL — Column MS_Run_id of table interactome_db.Protein_has_MS_Run.
- `protein_id`: integer NOT NULL — Column Protein_id of table interactome_db.Protein_has_MS_Run.
- primary key: ms_run_id, protein_id

### interactome_db_protein_has_psm  (source backend: mongodb)
Source table interactome_db.Protein_has_PSM.

- `psm_id`: integer NOT NULL — Column PSM_id of table interactome_db.Protein_has_PSM.
- `protein_id`: integer NOT NULL — Column Protein_id of table interactome_db.Protein_has_PSM.
- primary key: psm_id, protein_id

### interactome_db_protein_has_peptide  (source backend: postgres)
Source table interactome_db.Protein_has_Peptide.

- `peptide_id`: integer NOT NULL — Column Peptide_id of table interactome_db.Protein_has_Peptide.
- `protein_id`: integer NOT NULL — Column Protein_id of table interactome_db.Protein_has_Peptide.
- primary key: peptide_id, protein_id

### interactome_db_protein_has_protein_accession  (source backend: postgres)
Source table interactome_db.Protein_has_Protein_Accession.

- `protein_accession_accession`: text NOT NULL — Column Protein_Accession_accession of table interactome_db.Protein_has_Protein_Accession.
- `protein_id`: integer NOT NULL — Column Protein_id of table interactome_db.Protein_has_Protein_Accession.
- primary key: protein_accession_accession, protein_id

### interactome_db_ratio_descriptor  (source backend: rest)
Source table interactome_db.Ratio_Descriptor.

- `experimental_condition_1_id`: integer NOT NULL — Column Experimental_Condition_1_id of table interactome_db.Ratio_Descriptor.
- `experimental_condition_2_id`: integer NOT NULL — Column Experimental_Condition_2_id of table interactome_db.Ratio_Descriptor.
- `description`: text NOT NULL — Column description of table interactome_db.Ratio_Descriptor.
- `id`: integer NOT NULL — Column id of table interactome_db.Ratio_Descriptor.
- primary key: id

### interactome_db_sample_has_organism  (source backend: s3)
Source table interactome_db.Sample_has_Organism.

- `organism_taxonomyid`: text NOT NULL — Column Organism_taxonomyID of table interactome_db.Sample_has_Organism.
- `sample_id`: integer NOT NULL — Column Sample_id of table interactome_db.Sample_has_Organism.
- primary key: organism_taxonomyid, sample_id

### interactome_db_threshold  (source backend: files)
Source table interactome_db.Threshold.

- `description`: text NULL — Column description of table interactome_db.Threshold.
- `id`: integer NOT NULL — Column id of table interactome_db.Threshold.
- `name`: text NOT NULL — Column name of table interactome_db.Threshold.
- primary key: id

### interactome_db_tissue  (source backend: postgres)
Source table interactome_db.Tissue.

- `name`: text NULL — Column name of table interactome_db.Tissue.
- `tissueid`: text NOT NULL — Column tissueID of table interactome_db.Tissue.
- primary key: tissueid

### Relationships

- interactome_db_ms_run(project_id) -> interactome_db_project(id) [required]
- interactome_db_peptide_amount(combination_type_name) -> interactome_db_combination_type(name) [optional (may be NULL/dangling)]
- interactome_db_peptide_amount(peptide_id) -> interactome_db_peptide(id) [required]
- interactome_db_peptide_has_condition(peptide_id) -> interactome_db_peptide(id) [required]
- interactome_db_peptide_has_ms_run(ms_run_id) -> interactome_db_ms_run(id) [required]
- interactome_db_peptide_has_ms_run(peptide_id) -> interactome_db_peptide(id) [required]
- interactome_db_peptide_ratio_value(combination_type_name) -> interactome_db_combination_type(name) [optional (may be NULL/dangling)]
- interactome_db_peptide_ratio_value(confidence_score_type_name) -> interactome_db_confidence_score_type(name) [optional (may be NULL/dangling)]
- interactome_db_peptide_ratio_value(peptide_id) -> interactome_db_peptide(id) [required]
- interactome_db_peptide_ratio_value(ratio_descriptor_id) -> interactome_db_ratio_descriptor(id) [required]
- interactome_db_peptide_score(confidence_score_type_name) -> interactome_db_confidence_score_type(name) [required]
- interactome_db_peptide_score(peptide_id) -> interactome_db_peptide(id) [required]
- interactome_db_protein(organism_taxonomyid) -> interactome_db_organism(taxonomyid) [required]
- interactome_db_protein_amount(combination_type_name) -> interactome_db_combination_type(name) [optional (may be NULL/dangling)]
- interactome_db_protein_amount(protein_id) -> interactome_db_protein(id) [required]
- interactome_db_protein_annotation(annotation_type_name) -> interactome_db_annotation_type(name) [required]
- interactome_db_protein_annotation(protein_id) -> interactome_db_protein(id) [required]
- interactome_db_protein_has_condition(protein_id) -> interactome_db_protein(id) [required]
- interactome_db_protein_has_ms_run(ms_run_id) -> interactome_db_ms_run(id) [required]
- interactome_db_protein_has_ms_run(protein_id) -> interactome_db_protein(id) [required]
- interactome_db_protein_has_peptide(peptide_id) -> interactome_db_peptide(id) [required]
- interactome_db_protein_has_peptide(protein_id) -> interactome_db_protein(id) [required]
- interactome_db_protein_has_protein_accession(protein_accession_accession) -> interactome_db_protein_accession(accession) [required]
- interactome_db_protein_has_protein_accession(protein_id) -> interactome_db_protein(id) [required]
- interactome_db_protein_has_psm(protein_id) -> interactome_db_protein(id) [required]
- interactome_db_protein_has_psm(psm_id) -> interactome_db_psm(id) [required]
- interactome_db_protein_ratio_value(combination_type_name) -> interactome_db_combination_type(name) [optional (may be NULL/dangling)]
- interactome_db_protein_ratio_value(confidence_score_type_name) -> interactome_db_confidence_score_type(name) [optional (may be NULL/dangling)]
- interactome_db_protein_ratio_value(protein_id) -> interactome_db_protein(id) [required]
- interactome_db_protein_ratio_value(ratio_descriptor_id) -> interactome_db_ratio_descriptor(id) [required]
- interactome_db_protein_score(confidence_score_type_name) -> interactome_db_confidence_score_type(name) [required]
- interactome_db_protein_score(protein_id) -> interactome_db_protein(id) [required]
- interactome_db_protein_threshold(protein_id) -> interactome_db_protein(id) [required]
- interactome_db_protein_threshold(threshold_id) -> interactome_db_threshold(id) [required]
- interactome_db_psm(ms_run_id) -> interactome_db_ms_run(id) [required]
- interactome_db_psm(peptide_id) -> interactome_db_peptide(id) [required]
- interactome_db_psm_amount(combination_type_name) -> interactome_db_combination_type(name) [optional (may be NULL/dangling)]
- interactome_db_psm_amount(psm_id) -> interactome_db_psm(id) [required]
- interactome_db_psm_has_condition(psm_id) -> interactome_db_psm(id) [required]
- interactome_db_psm_ratio_value(combination_type_name) -> interactome_db_combination_type(name) [optional (may be NULL/dangling)]
- interactome_db_psm_ratio_value(confidence_score_type_name) -> interactome_db_confidence_score_type(name) [optional (may be NULL/dangling)]
- interactome_db_psm_ratio_value(psm_id) -> interactome_db_psm(id) [required]
- interactome_db_psm_ratio_value(ratio_descriptor_id) -> interactome_db_ratio_descriptor(id) [required]
- interactome_db_psm_score(confidence_score_type_name) -> interactome_db_confidence_score_type(name) [required]
- interactome_db_psm_score(psm_id) -> interactome_db_psm(id) [required]
- interactome_db_ptm(peptide_id) -> interactome_db_peptide(id) [optional (may be NULL/dangling)]
- interactome_db_ptm(psm_id) -> interactome_db_psm(id) [optional (may be NULL/dangling)]
- interactome_db_ptm_site(confidence_score_type_name) -> interactome_db_confidence_score_type(name) [optional (may be NULL/dangling)]
- interactome_db_ptm_site(ptm_id) -> interactome_db_ptm(id) [required]
- interactome_db_sample_has_organism(organism_taxonomyid) -> interactome_db_organism(taxonomyid) [required]

