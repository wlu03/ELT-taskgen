# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Arfc Pride

## Specification

PROJECT OVERVIEW

This project builds three analytical marts from a Temoa-style energy-system schema (the 082965_03_uiuc_mga.sql schema). Every source table listed below must be extracted from the backend named beside it, and only from that backend.

Source tables and their extraction backends:
- capacityfactorprocess — extraction backend: files.
- capacityfactortech — extraction backend: s3.
- costfixed — extraction backend: s3.
- costinvest — extraction backend: mongodb.
- costvariable — extraction backend: rest.
- demand — extraction backend: postgres.
- demandspecificdistribution — extraction backend: s3.
- discountrate — extraction backend: s3.
- efficiency — extraction backend: mongodb.
- emissionactivity — extraction backend: rest.
- emissionlimit — extraction backend: files.
- existingcapacity — extraction backend: mongodb.
- globaldiscountrate — extraction backend: s3.
- growthratemax — extraction backend: postgres.
- growthrateseed — extraction backend: files.
- lifetimeloantech — extraction backend: files.
- lifetimeprocess — extraction backend: postgres.
- lifetimetech — extraction backend: files.
- maxactivity — extraction backend: files.
- maxcapacity — extraction backend: files.
- minactivity — extraction backend: s3.
- mincapacity — extraction backend: files.
- output_capacitybyperiodandtech — extraction backend: mongodb.
- output_costs — extraction backend: postgres.
- output_objective — extraction backend: postgres.
- output_vflow_in — extraction backend: files.
- output_vflow_out — extraction backend: mongodb.
- output_v_capacity — extraction backend: rest.
- segfrac — extraction backend: mongodb.
- techinputsplit — extraction backend: postgres.
- techoutputsplit — extraction backend: s3.
- commodities — extraction backend: postgres.
- commodity_labels — extraction backend: s3.
- sector_labels — extraction backend: rest.
- technologies — extraction backend: files.
- technology_labels — extraction backend: s3.
- time_of_day — extraction backend: s3.
- time_period_labels — extraction backend: files.
- time_periods — extraction backend: rest.
- time_season — extraction backend: files.

RELATIONSHIPS IN THE SOURCE SCHEMA

Each statement below pairs a child table with its keys and the parent table with its keys, and labels the relationship required or optional exactly as the source schema declares it.

- Child table capacityfactorprocess (season_name) refers to parent table time_season (t_season); required.
- Child table capacityfactorprocess (tech) refers to parent table technologies (tech); required.
- Child table capacityfactorprocess (time_of_day_name) refers to parent table time_of_day (t_day); required.
- Child table capacityfactortech (season_name) refers to parent table time_season (t_season); required.
- Child table capacityfactortech (tech) refers to parent table technologies (tech); required.
- Child table capacityfactortech (time_of_day_name) refers to parent table time_of_day (t_day); required.
- Child table commodities (flag) refers to parent table commodity_labels (comm_labels); optional (may be NULL or dangling).
- Child table costfixed (periods) refers to parent table time_periods (t_periods); required.
- Child table costfixed (tech) refers to parent table technologies (tech); required.
- Child table costfixed (vintage) refers to parent table time_periods (t_periods); required.
- Child table costinvest (tech) refers to parent table technologies (tech); required.
- Child table costinvest (vintage) refers to parent table time_periods (t_periods); required.
- Child table costvariable (periods) refers to parent table time_periods (t_periods); required.
- Child table costvariable (tech) refers to parent table technologies (tech); required.
- Child table costvariable (vintage) refers to parent table time_periods (t_periods); required.
- Child table demand (demand_comm) refers to parent table commodities (comm_name); required.
- Child table demand (periods) refers to parent table time_periods (t_periods); required.
- Child table demandspecificdistribution (demand_name) refers to parent table commodities (comm_name); required.
- Child table demandspecificdistribution (season_name) refers to parent table time_season (t_season); required.
- Child table demandspecificdistribution (time_of_day_name) refers to parent table time_of_day (t_day); required.
- Child table discountrate (tech) refers to parent table technologies (tech); required.
- Child table discountrate (vintage) refers to parent table time_periods (t_periods); required.
- Child table efficiency (input_comm) refers to parent table commodities (comm_name); required.
- Child table efficiency (output_comm) refers to parent table commodities (comm_name); required.
- Child table efficiency (tech) refers to parent table technologies (tech); required.
- Child table efficiency (vintage) refers to parent table time_periods (t_periods); required.
- Child table emissionactivity (emis_comm) refers to parent table commodities (comm_name); required.
- Child table emissionactivity (input_comm) refers to parent table commodities (comm_name); required.
- Child table emissionactivity (output_comm) refers to parent table commodities (comm_name); required.
- Child table emissionactivity (tech) refers to parent table technologies (tech); required.
- Child table emissionactivity (vintage) refers to parent table time_periods (t_periods); required.
- Child table emissionlimit (emis_comm) refers to parent table commodities (comm_name); required.
- Child table emissionlimit (periods) refers to parent table time_periods (t_periods); required.
- Child table existingcapacity (tech) refers to parent table technologies (tech); required.
- Child table existingcapacity (vintage) refers to parent table time_periods (t_periods); required.
- Child table growthratemax (tech) refers to parent table technologies (tech); optional (may be NULL or dangling).
- Child table growthrateseed (tech) refers to parent table technologies (tech); optional (may be NULL or dangling).
- Child table lifetimeloantech (tech) refers to parent table technologies (tech); required.
- Child table lifetimeprocess (tech) refers to parent table technologies (tech); required.
- Child table lifetimeprocess (vintage) refers to parent table time_periods (t_periods); required.
- Child table lifetimetech (tech) refers to parent table technologies (tech); required.
- Child table maxactivity (periods) refers to parent table time_periods (t_periods); required.
- Child table maxactivity (tech) refers to parent table technologies (tech); required.
- Child table maxcapacity (periods) refers to parent table time_periods (t_periods); required.
- Child table maxcapacity (tech) refers to parent table technologies (tech); required.
- Child table minactivity (periods) refers to parent table time_periods (t_periods); required.
- Child table minactivity (tech) refers to parent table technologies (tech); required.
- Child table mincapacity (periods) refers to parent table time_periods (t_periods); required.
- Child table mincapacity (tech) refers to parent table technologies (tech); required.
- Child table output_capacitybyperiodandtech (sector) refers to parent table sector_labels (sector); optional (may be NULL or dangling).
- Child table output_capacitybyperiodandtech (t_periods) refers to parent table time_periods (t_periods); required.
- Child table output_capacitybyperiodandtech (tech) refers to parent table technologies (tech); required.
- Child table output_costs (sector) refers to parent table sector_labels (sector); optional (may be NULL or dangling).
- Child table output_costs (tech) refers to parent table technologies (tech); required.
- Child table output_costs (vintage) refers to parent table time_periods (t_periods); required.
- Child table output_v_capacity (sector) refers to parent table sector_labels (sector); optional (may be NULL or dangling).
- Child table output_v_capacity (tech) refers to parent table technologies (tech); required.
- Child table output_v_capacity (vintage) refers to parent table time_periods (t_periods); required.
- Child table output_vflow_in (input_comm) refers to parent table commodities (comm_name); required.
- Child table output_vflow_in (output_comm) refers to parent table commodities (comm_name); required.
- Child table output_vflow_in (sector) refers to parent table sector_labels (sector); optional (may be NULL or dangling).
- Child table output_vflow_in (t_day) refers to parent table time_of_day (t_day); required.
- Child table output_vflow_in (t_periods) refers to parent table time_periods (t_periods); required.
- Child table output_vflow_in (tech) refers to parent table technologies (tech); required.
- Child table output_vflow_in (vintage) refers to parent table time_periods (t_periods); required.
- Child table output_vflow_out (input_comm) refers to parent table commodities (comm_name); required.
- Child table output_vflow_out (output_comm) refers to parent table commodities (comm_name); required.
- Child table output_vflow_out (sector) refers to parent table sector_labels (sector); optional (may be NULL or dangling).
- Child table output_vflow_out (t_day) refers to parent table time_of_day (t_day); required.
- Child table output_vflow_out (t_periods) refers to parent table time_periods (t_periods); required.
- Child table output_vflow_out (tech) refers to parent table technologies (tech); required.
- Child table output_vflow_out (vintage) refers to parent table time_periods (t_periods); required.
- Child table segfrac (season_name) refers to parent table time_season (t_season); required.
- Child table segfrac (time_of_day_name) refers to parent table time_of_day (t_day); required.
- Child table techinputsplit (input_comm) refers to parent table commodities (comm_name); required.
- Child table techinputsplit (periods) refers to parent table time_periods (t_periods); required.
- Child table techinputsplit (tech) refers to parent table technologies (tech); required.
- Child table technologies (flag) refers to parent table technology_labels (tech_labels); optional (may be NULL or dangling).
- Child table technologies (sector) refers to parent table sector_labels (sector); optional (may be NULL or dangling).
- Child table techoutputsplit (output_comm) refers to parent table commodities (comm_name); required.
- Child table techoutputsplit (periods) refers to parent table time_periods (t_periods); required.
- Child table techoutputsplit (tech) refers to parent table technologies (tech); required.
- Child table time_periods (flag) refers to parent table time_period_labels (t_period_labels); optional (may be NULL or dangling).

General conventions: rounding to 4 decimal places uses ordinary half-up decimal rounding of the exact fraction. Text comparisons used for ordering are plain case-sensitive comparisons of the stored text, the warehouse default order, in which every uppercase letter sorts before every lowercase one.

====================================================================
MART technologies_techoutputsplit_distribution — Per-(technologies, measure state) distribution of linked techoutputsplit activity in the 082965_03_uiuc_mga.sql schema.

Grain: one row per (tech, measure state) pair represented among linked techoutputsplit rows; the absent state includes missing to_split values and a no-activity row for a technologies row with no links. A linked techoutputsplit row whose to_split has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state together identify a row.

Output columns:
- entity_key (text): identifier of the technologies row.
- measure_state (text): 'present' for a linked techoutputsplit row whose to_split has a value; 'absent' when to_split is missing, including a technologies row with no linked techoutputsplit row. A linked techoutputsplit row whose to_split has a value belongs only to the present state and never to the absent state.
- entity_name (text): tech_category of the technologies row, copied unchanged.
- row_count (bigint): number of linked techoutputsplit rows in this entity/state cell; a absent cell holding real techoutputsplit rows whose to_split is missing COUNTS those rows, and only the placeholder cell of a technologies row with no linked techoutputsplit row at all reports 0.
- distinct_amount_count (bigint): number of unique non-missing to_split values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no to_split value at all — both for a technologies row with no linked techoutputsplit row and for an absent cell whose rows all have a missing to_split.
- total_amount (float): total of to_split in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an to_split value.
- max_amount (float): largest to_split in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an to_split value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules producing this mart:

Rule 1. The source table technologies is read in full.

Rule 2. The source table techoutputsplit is read in full.

Rule 3. From source table technologies, each tech is carried as entity_key and its tech_category is carried as entity_name into the measure-state calculation.

Rule 4. The linked techoutputsplit rows are brought into each technologies entity, matching techoutputsplit on tech against entity_key, carrying entity_key, entity_name and tech; preservation is left-sided on the technologies side, so an entity with no linked row is retained and its absent state stays visible.

Rule 5. The present measure-state rows are kept, carrying entity_key and entity_name: a real techoutputsplit row whose to_split has a value.

Rule 6. One row per technologies entity that has at least one row in the present measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count, distinct_amount_count as how many different non-missing to_split values occur (each different value counted once, however many rows repeat it), total_amount as the total to_split, and max_amount as the largest to_split.

Rule 7. For those present-state measures, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures are labelled with measure_state 'present', the row carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows are kept, carrying entity_key and entity_name: to_split is missing, including the retained placeholder for a technologies row with no techoutputsplit rows. A real techoutputsplit row whose to_split has a value belongs only to the present state and never to this absent state.

Rule 10. One row per technologies entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count, distinct_amount_count as how many different non-missing to_split values occur (each different value counted once, however many rows repeat it), total_amount as the total to_split, and max_amount as the largest to_split.

Rule 11. For those absent-state measures, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures are labelled with measure_state 'absent', the row carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list of entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, all rows combined, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. Deterministic output order: rows appear in ascending entity_key order, then ascending measure_state order.

====================================================================
MART commodities_techoutputsplit_top — Per-commodities extremes over linked techoutputsplit rows in the 082965_03_uiuc_mga.sql schema: WHICH row is largest, not how large it is.

Grain: one row per commodities (comm_name), INCLUDING commodities rows with no linked techoutputsplit rows.

Key column: parent_key identifies a row.

Output columns:
- parent_key (text): identifier of the commodities row. One row per value.
- parent_name (text): comm_desc of the commodities row, copied unchanged.
- top_measure (float): the largest to_split itself; 0 when the parent has no techoutputsplit rows, and 0 when none of its rows carries a to_split value.
- tied_count (bigint): how many techoutputsplit rows are tied at that largest to_split. 1 when exactly one row carries that largest to_split; 0 when there are no rows or when none of the rows carries a to_split value; a row with no to_split value never ties: only a row whose to_split value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.
- child_count (bigint): number of techoutputsplit rows for this commodities row; 0 when there are none. Every linked techoutputsplit row counts, whether or not it carries a to_split value. A commodities row kept with no techoutputsplit row reports 0 here, never 1: its placeholder holds no techoutputsplit row to count.
- total_measure (float): total of to_split over all of them; 0 when the parent has no techoutputsplit rows, and 0 when none of its rows carries a to_split value (rows with no to_split value add nothing).
- top_label (text): the to_split_notes of the techoutputsplit row with the LARGEST to_split for this commodities row. Ties in to_split are broken by taking the SMALLEST to_split_notes under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no to_split_notes value sorts after every labelled row. A row with no to_split value still ranks, after every row that has one, so a parent holding at least one techoutputsplit row always has a winning row — when NONE of its rows carries a to_split value the winner is the one the tie-break alone selects, not the no-rows default. The literal '(none)' when the parent has no techoutputsplit rows at all, and '(none)' when the winning row has no to_split_notes value.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no techoutputsplit rows, or none of its rows carries a to_split value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a to_split value equal to the largest to_split value among the parent's rows; a row with no to_split value never holds the maximum.

Rules producing this mart:

Rule 1. The source table commodities is read in full.

Rule 2. The source table techoutputsplit is read in full.

Rule 3. From source table commodities there is one row per commodities row, keyed by comm_name, carrying parent_key and parent_name.

Rule 4. The techoutputsplit rows are brought in, matching techoutputsplit on output_comm against parent_key and carrying output_comm and comm_name; preservation is left-sided on the commodities side, so a commodities row with no techoutputsplit rows still appears, with the declared defaults.

Rule 5. Within each parent_key the matched rows are ranked under an explicit total order — the measure first, then the declared tie-break — so the extremal row is a function of the input and not of row order.

Rule 6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7. For each parent_key the single row at which the ordering measure is largest survives, ties broken by the smallest to_split_notes under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no to_split_notes value sorts after every row that has one), and top_label is taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 8. The extremal row's attributes are attributed to the grouped measures by parent_key; preservation is left-sided on the measures, so a group with no rows at all keeps its measures.

Rule 9. The mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure and top_label; top_measure and total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure and total_measure, the default also applies to a group none of whose real rows carries an input value.

Rule 10. Guarded ratio, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure and top_label: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when the denominator total_measure is 0 or has no value.

Rule 11. Carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_measure_share, tie_state is 'empty' when no row holds a maximum at all — the parent has no techoutputsplit rows, or none of its rows carries a to_split value — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; equivalently tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a to_split value equal to the largest to_split value among the parent's rows; a row with no to_split value never holds the maximum, so a parent whose techoutputsplit rows all lack a to_split value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label names.

Rule 12. Deterministic output order: rows appear sorted in ascending parent_key order.

====================================================================
MART technologies_techinputsplit_distribution — Per-(technologies, measure state) distribution of linked techinputsplit activity in the 082965_03_uiuc_mga.sql schema.

Grain: one row per (tech, measure state) pair represented among linked techinputsplit rows; the absent state includes missing ti_split values and a no-activity row for a technologies row with no links. A linked techinputsplit row whose ti_split has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state together identify a row.

Output columns:
- entity_key (text): identifier of the technologies row.
- measure_state (text): 'present' for a linked techinputsplit row whose ti_split has a value; 'absent' when ti_split is missing, including a technologies row with no linked techinputsplit row. A linked techinputsplit row whose ti_split has a value belongs only to the present state and never to the absent state.
- entity_name (text): tech_category of the technologies row, copied unchanged.
- row_count (bigint): number of linked techinputsplit rows in this entity/state cell; a absent cell holding real techinputsplit rows whose ti_split is missing COUNTS those rows, and only the placeholder cell of a technologies row with no linked techinputsplit row at all reports 0.
- distinct_amount_count (bigint): number of unique non-missing ti_split values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no ti_split value at all — both for a technologies row with no linked techinputsplit row and for an absent cell whose rows all have a missing ti_split.
- total_amount (float): total of ti_split in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an ti_split value.
- max_amount (float): largest ti_split in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an ti_split value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules producing this mart:

Rule 1. The source table technologies is read in full.

Rule 2. The source table techinputsplit is read in full.

Rule 3. From source table technologies, each tech is carried as entity_key and its tech_category is carried as entity_name into the measure-state calculation.

Rule 4. The linked techinputsplit rows are brought into each technologies entity, matching techinputsplit on tech against entity_key, carrying entity_key, entity_name and tech; preservation is left-sided on the technologies side, so an entity with no linked row is retained and its absent state stays visible.

Rule 5. The present measure-state rows are kept, carrying entity_key and entity_name: a real techinputsplit row whose ti_split has a value.

Rule 6. One row per technologies entity that has at least one row in the present measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count, distinct_amount_count as how many different non-missing ti_split values occur (each different value counted once, however many rows repeat it), total_amount as the total ti_split, and max_amount as the largest ti_split.

Rule 7. For those present-state measures, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures are labelled with measure_state 'present', the row carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows are kept, carrying entity_key and entity_name: ti_split is missing, including the retained placeholder for a technologies row with no techinputsplit rows. A real techinputsplit row whose ti_split has a value belongs only to the present state and never to this absent state.

Rule 10. One row per technologies entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count, distinct_amount_count as how many different non-missing ti_split values occur (each different value counted once, however many rows repeat it), total_amount as the total ti_split, and max_amount as the largest ti_split.

Rule 11. For those absent-state measures, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures are labelled with measure_state 'absent', the row carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list of entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, all rows combined, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. Deterministic output order: rows appear in ascending entity_key order, then ascending measure_state order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `technologies_techoutputsplit_distribution`

- Grain: One row per (tech, measure state) pair represented among linked techoutputsplit rows; the absent state includes missing to_split values and a no-activity row for a technologies row with no links. A linked techoutputsplit row whose to_split has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'technologies_techoutputsplit_distribution' has 14 declared semantic rules:
1. [source] Read source table technologies. (public source tables: technologies)
2. [source] Read source table techoutputsplit. (public source tables: techoutputsplit)
3. [derive] Carry each tech and its tech_category into the measure-state calculation. (public source tables: technologies | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked techoutputsplit rows into each technologies entity; retain an entity with no linked row so its absent state is visible. (public source tables: techoutputsplit | public carried/output columns: entity_key, entity_name, tech | join preservation: left | condition public identifiers: techoutputsplit, tech, entity_key)
5. [filter] Keep the present measure-state rows: a real techoutputsplit row whose to_split has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per technologies entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing to_split values occur (each different value counted once, however many rows repeat it), total to_split, and largest to_split. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: to_split is missing, including the retained placeholder for a technologies row with no techoutputsplit rows. A real techoutputsplit row whose to_split has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per technologies entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing to_split values occur (each different value counted once, however many rows repeat it), total to_split, and largest to_split. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `commodities_techoutputsplit_top`

- Grain: One row per commodities (comm_name), INCLUDING commodities rows with no linked techoutputsplit rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_measure_share, tie_state

```text
Mart 'commodities_techoutputsplit_top' has 12 declared semantic rules:
1. [source] Read source table commodities. (public source tables: commodities)
2. [source] Read source table techoutputsplit. (public source tables: techoutputsplit)
3. [derive] One row per commodities row, keyed by comm_name. (public source tables: commodities | public carried/output columns: parent_key, parent_name)
4. [join] Bring in techoutputsplit: a commodities row with no techoutputsplit rows still appears, with the declared defaults. (public source tables: techoutputsplit | public carried/output columns: output_comm, comm_name | join preservation: left | condition public identifiers: techoutputsplit, output_comm, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest to_split_notes under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no to_split_notes value sorts after every row that has one), and take top_label from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no techoutputsplit rows, or none of its rows carries a to_split value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a to_split value equal to the largest to_split value among the parent's rows; a row with no to_split value never holds the maximum. So a parent whose techoutputsplit rows all lack a to_split value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label names. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `technologies_techinputsplit_distribution`

- Grain: One row per (tech, measure state) pair represented among linked techinputsplit rows; the absent state includes missing ti_split values and a no-activity row for a technologies row with no links. A linked techinputsplit row whose ti_split has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'technologies_techinputsplit_distribution' has 14 declared semantic rules:
1. [source] Read source table technologies. (public source tables: technologies)
2. [source] Read source table techinputsplit. (public source tables: techinputsplit)
3. [derive] Carry each tech and its tech_category into the measure-state calculation. (public source tables: technologies | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked techinputsplit rows into each technologies entity; retain an entity with no linked row so its absent state is visible. (public source tables: techinputsplit | public carried/output columns: entity_key, entity_name, tech | join preservation: left | condition public identifiers: techinputsplit, tech, entity_key)
5. [filter] Keep the present measure-state rows: a real techinputsplit row whose ti_split has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per technologies entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing ti_split values occur (each different value counted once, however many rows repeat it), total ti_split, and largest ti_split. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: ti_split is missing, including the retained placeholder for a technologies row with no techinputsplit rows. A real techinputsplit row whose ti_split has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per technologies entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing ti_split values occur (each different value counted once, however many rows repeat it), total ti_split, and largest ti_split. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### capacityfactorprocess  (source backend: files)
Source table CapacityFactorProcess.

- `cf_process`: float NULL — Column cf_process of table CapacityFactorProcess.
- `cf_process_notes`: text NULL — Column cf_process_notes of table CapacityFactorProcess.
- `season_name`: text NOT NULL — Column season_name of table CapacityFactorProcess.
- `tech`: text NOT NULL — Column tech of table CapacityFactorProcess.
- `time_of_day_name`: text NOT NULL — Column time_of_day_name of table CapacityFactorProcess.
- `vintage`: integer NOT NULL — Column vintage of table CapacityFactorProcess.
- primary key: season_name, tech, time_of_day_name, vintage

### capacityfactortech  (source backend: s3)
Source table CapacityFactorTech.

- `cf_tech`: float NULL — Column cf_tech of table CapacityFactorTech.
- `cf_tech_notes`: text NULL — Column cf_tech_notes of table CapacityFactorTech.
- `season_name`: text NOT NULL — Column season_name of table CapacityFactorTech.
- `tech`: text NOT NULL — Column tech of table CapacityFactorTech.
- `time_of_day_name`: text NOT NULL — Column time_of_day_name of table CapacityFactorTech.
- primary key: season_name, tech, time_of_day_name

### costfixed  (source backend: s3)
Source table CostFixed.

- `cost_fixed`: float NULL — Column cost_fixed of table CostFixed.
- `cost_fixed_notes`: text NULL — Column cost_fixed_notes of table CostFixed.
- `cost_fixed_units`: text NULL — Column cost_fixed_units of table CostFixed.
- `periods`: integer NOT NULL — Column periods of table CostFixed.
- `tech`: text NOT NULL — Column tech of table CostFixed.
- `vintage`: integer NOT NULL — Column vintage of table CostFixed.
- primary key: periods, tech, vintage

### costinvest  (source backend: mongodb)
Source table CostInvest.

- `cost_invest`: float NULL — Column cost_invest of table CostInvest.
- `cost_invest_notes`: text NULL — Column cost_invest_notes of table CostInvest.
- `cost_invest_units`: text NULL — Column cost_invest_units of table CostInvest.
- `tech`: text NOT NULL — Column tech of table CostInvest.
- `vintage`: integer NOT NULL — Column vintage of table CostInvest.
- primary key: tech, vintage

### costvariable  (source backend: rest)
Source table CostVariable.

- `cost_variable`: float NULL — Column cost_variable of table CostVariable.
- `cost_variable_notes`: text NULL — Column cost_variable_notes of table CostVariable.
- `cost_variable_units`: text NULL — Column cost_variable_units of table CostVariable.
- `periods`: integer NOT NULL — Column periods of table CostVariable.
- `tech`: text NOT NULL — Column tech of table CostVariable.
- `vintage`: integer NOT NULL — Column vintage of table CostVariable.
- primary key: periods, tech, vintage

### demand  (source backend: postgres)
Source table Demand.

- `demand`: float NULL — Column demand of table Demand.
- `demand_comm`: text NOT NULL — Column demand_comm of table Demand.
- `demand_notes`: text NULL — Column demand_notes of table Demand.
- `demand_units`: text NULL — Column demand_units of table Demand.
- `periods`: integer NOT NULL — Column periods of table Demand.
- primary key: demand_comm, periods

### demandspecificdistribution  (source backend: s3)
Source table DemandSpecificDistribution.

- `dds`: float NULL — Column dds of table DemandSpecificDistribution.
- `dds_notes`: text NULL — Column dds_notes of table DemandSpecificDistribution.
- `demand_name`: text NOT NULL — Column demand_name of table DemandSpecificDistribution.
- `season_name`: text NOT NULL — Column season_name of table DemandSpecificDistribution.
- `time_of_day_name`: text NOT NULL — Column time_of_day_name of table DemandSpecificDistribution.
- primary key: demand_name, season_name, time_of_day_name

### discountrate  (source backend: s3)
Source table DiscountRate.

- `tech`: text NOT NULL — Column tech of table DiscountRate.
- `tech_rate`: float NULL — Column tech_rate of table DiscountRate.
- `tech_rate_notes`: text NULL — Column tech_rate_notes of table DiscountRate.
- `vintage`: integer NOT NULL — Column vintage of table DiscountRate.
- primary key: tech, vintage

### efficiency  (source backend: mongodb)
Source table Efficiency.

- `eff_notes`: text NULL — Column eff_notes of table Efficiency.
- `efficiency`: float NULL — Column efficiency of table Efficiency.
- `input_comm`: text NOT NULL — Column input_comm of table Efficiency.
- `output_comm`: text NOT NULL — Column output_comm of table Efficiency.
- `tech`: text NOT NULL — Column tech of table Efficiency.
- `vintage`: integer NOT NULL — Column vintage of table Efficiency.
- primary key: input_comm, output_comm, tech, vintage

### emissionactivity  (source backend: rest)
Source table EmissionActivity.

- `emis_act`: float NULL — Column emis_act of table EmissionActivity.
- `emis_act_notes`: text NULL — Column emis_act_notes of table EmissionActivity.
- `emis_act_units`: text NULL — Column emis_act_units of table EmissionActivity.
- `emis_comm`: text NOT NULL — Column emis_comm of table EmissionActivity.
- `input_comm`: text NOT NULL — Column input_comm of table EmissionActivity.
- `output_comm`: text NOT NULL — Column output_comm of table EmissionActivity.
- `tech`: text NOT NULL — Column tech of table EmissionActivity.
- `vintage`: integer NOT NULL — Column vintage of table EmissionActivity.
- primary key: emis_comm, input_comm, output_comm, tech, vintage

### emissionlimit  (source backend: files)
Source table EmissionLimit.

- `emis_comm`: text NOT NULL — Column emis_comm of table EmissionLimit.
- `emis_limit`: float NULL — Column emis_limit of table EmissionLimit.
- `emis_limit_notes`: text NULL — Column emis_limit_notes of table EmissionLimit.
- `emis_limit_units`: text NULL — Column emis_limit_units of table EmissionLimit.
- `periods`: integer NOT NULL — Column periods of table EmissionLimit.
- primary key: emis_comm, periods

### existingcapacity  (source backend: mongodb)
Source table ExistingCapacity.

- `exist_cap`: float NULL — Column exist_cap of table ExistingCapacity.
- `exist_cap_notes`: text NULL — Column exist_cap_notes of table ExistingCapacity.
- `exist_cap_units`: text NULL — Column exist_cap_units of table ExistingCapacity.
- `tech`: text NOT NULL — Column tech of table ExistingCapacity.
- `vintage`: integer NOT NULL — Column vintage of table ExistingCapacity.
- primary key: tech, vintage

### globaldiscountrate  (source backend: s3)
Source table GlobalDiscountRate.

- `rate`: float NULL — Column rate of table GlobalDiscountRate.

### growthratemax  (source backend: postgres)
Source table GrowthRateMax.

- `growthrate_max`: float NULL — Column growthrate_max of table GrowthRateMax.
- `growthrate_max_notes`: text NULL — Column growthrate_max_notes of table GrowthRateMax.
- `tech`: text NULL — Column tech of table GrowthRateMax.

### growthrateseed  (source backend: files)
Source table GrowthRateSeed.

- `growthrate_seed`: float NULL — Column growthrate_seed of table GrowthRateSeed.
- `growthrate_seed_notes`: text NULL — Column growthrate_seed_notes of table GrowthRateSeed.
- `growthrate_seed_units`: text NULL — Column growthrate_seed_units of table GrowthRateSeed.
- `tech`: text NULL — Column tech of table GrowthRateSeed.

### lifetimeloantech  (source backend: files)
Source table LifetimeLoanTech.

- `loan`: float NULL — Column loan of table LifetimeLoanTech.
- `loan_notes`: text NULL — Column loan_notes of table LifetimeLoanTech.
- `tech`: text NOT NULL — Column tech of table LifetimeLoanTech.
- primary key: tech

### lifetimeprocess  (source backend: postgres)
Source table LifetimeProcess.

- `life_process`: float NULL — Column life_process of table LifetimeProcess.
- `life_process_notes`: text NULL — Column life_process_notes of table LifetimeProcess.
- `tech`: text NOT NULL — Column tech of table LifetimeProcess.
- `vintage`: integer NOT NULL — Column vintage of table LifetimeProcess.
- primary key: tech, vintage

### lifetimetech  (source backend: files)
Source table LifetimeTech.

- `life`: float NULL — Column life of table LifetimeTech.
- `life_notes`: text NULL — Column life_notes of table LifetimeTech.
- `tech`: text NOT NULL — Column tech of table LifetimeTech.
- primary key: tech

### maxactivity  (source backend: files)
Source table MaxActivity.

- `maxact`: float NULL — Column maxact of table MaxActivity.
- `maxact_notes`: text NULL — Column maxact_notes of table MaxActivity.
- `maxact_units`: text NULL — Column maxact_units of table MaxActivity.
- `periods`: integer NOT NULL — Column periods of table MaxActivity.
- `tech`: text NOT NULL — Column tech of table MaxActivity.
- primary key: periods, tech

### maxcapacity  (source backend: files)
Source table MaxCapacity.

- `maxcap`: float NULL — Column maxcap of table MaxCapacity.
- `maxcap_notes`: text NULL — Column maxcap_notes of table MaxCapacity.
- `maxcap_units`: text NULL — Column maxcap_units of table MaxCapacity.
- `periods`: integer NOT NULL — Column periods of table MaxCapacity.
- `tech`: text NOT NULL — Column tech of table MaxCapacity.
- primary key: periods, tech

### minactivity  (source backend: s3)
Source table MinActivity.

- `minact`: float NULL — Column minact of table MinActivity.
- `minact_notes`: text NULL — Column minact_notes of table MinActivity.
- `minact_units`: text NULL — Column minact_units of table MinActivity.
- `periods`: integer NOT NULL — Column periods of table MinActivity.
- `tech`: text NOT NULL — Column tech of table MinActivity.
- primary key: periods, tech

### mincapacity  (source backend: files)
Source table MinCapacity.

- `mincap`: float NULL — Column mincap of table MinCapacity.
- `mincap_notes`: text NULL — Column mincap_notes of table MinCapacity.
- `mincap_units`: text NULL — Column mincap_units of table MinCapacity.
- `periods`: integer NOT NULL — Column periods of table MinCapacity.
- `tech`: text NOT NULL — Column tech of table MinCapacity.
- primary key: periods, tech

### output_capacitybyperiodandtech  (source backend: mongodb)
Source table Output_CapacityByPeriodAndTech.

- `capacity`: float NULL — Column capacity of table Output_CapacityByPeriodAndTech.
- `scenario`: text NOT NULL — Column scenario of table Output_CapacityByPeriodAndTech.
- `sector`: text NULL — Column sector of table Output_CapacityByPeriodAndTech.
- `t_periods`: integer NOT NULL — Column t_periods of table Output_CapacityByPeriodAndTech.
- `tech`: text NOT NULL — Column tech of table Output_CapacityByPeriodAndTech.
- primary key: scenario, t_periods, tech

### output_costs  (source backend: postgres)
Source table Output_Costs.

- `output_cost`: float NULL — Column output_cost of table Output_Costs.
- `output_name`: text NOT NULL — Column output_name of table Output_Costs.
- `scenario`: text NOT NULL — Column scenario of table Output_Costs.
- `sector`: text NULL — Column sector of table Output_Costs.
- `tech`: text NOT NULL — Column tech of table Output_Costs.
- `vintage`: integer NOT NULL — Column vintage of table Output_Costs.
- primary key: output_name, scenario, tech, vintage

### output_objective  (source backend: postgres)
Source table Output_Objective.

- `objective_name`: text NULL — Column objective_name of table Output_Objective.
- `scenario`: text NULL — Column scenario of table Output_Objective.
- `total_system_cost`: float NULL — Column total_system_cost of table Output_Objective.

### output_vflow_in  (source backend: files)
Source table Output_VFlow_In.

- `input_comm`: text NOT NULL — Column input_comm of table Output_VFlow_In.
- `output_comm`: text NOT NULL — Column output_comm of table Output_VFlow_In.
- `scenario`: text NOT NULL — Column scenario of table Output_VFlow_In.
- `sector`: text NULL — Column sector of table Output_VFlow_In.
- `t_day`: text NOT NULL — Column t_day of table Output_VFlow_In.
- `t_periods`: integer NOT NULL — Column t_periods of table Output_VFlow_In.
- `t_season`: text NOT NULL — Column t_season of table Output_VFlow_In.
- `tech`: text NOT NULL — Column tech of table Output_VFlow_In.
- `vflow_in`: float NULL — Column vflow_in of table Output_VFlow_In.
- `vintage`: integer NOT NULL — Column vintage of table Output_VFlow_In.
- primary key: input_comm, output_comm, scenario, t_day, t_periods, t_season, tech, vintage

### output_vflow_out  (source backend: mongodb)
Source table Output_VFlow_Out.

- `input_comm`: text NOT NULL — Column input_comm of table Output_VFlow_Out.
- `output_comm`: text NOT NULL — Column output_comm of table Output_VFlow_Out.
- `scenario`: text NOT NULL — Column scenario of table Output_VFlow_Out.
- `sector`: text NULL — Column sector of table Output_VFlow_Out.
- `t_day`: text NOT NULL — Column t_day of table Output_VFlow_Out.
- `t_periods`: integer NOT NULL — Column t_periods of table Output_VFlow_Out.
- `t_season`: text NOT NULL — Column t_season of table Output_VFlow_Out.
- `tech`: text NOT NULL — Column tech of table Output_VFlow_Out.
- `vflow_out`: float NULL — Column vflow_out of table Output_VFlow_Out.
- `vintage`: integer NOT NULL — Column vintage of table Output_VFlow_Out.
- primary key: input_comm, output_comm, scenario, t_day, t_periods, t_season, tech, vintage

### output_v_capacity  (source backend: rest)
Source table Output_V_Capacity.

- `capacity`: float NULL — Column capacity of table Output_V_Capacity.
- `scenario`: text NOT NULL — Column scenario of table Output_V_Capacity.
- `sector`: text NULL — Column sector of table Output_V_Capacity.
- `tech`: text NOT NULL — Column tech of table Output_V_Capacity.
- `vintage`: integer NOT NULL — Column vintage of table Output_V_Capacity.
- primary key: scenario, tech, vintage

### segfrac  (source backend: mongodb)
Source table SegFrac.

- `season_name`: text NOT NULL — Column season_name of table SegFrac.
- `segfrac`: float NULL — Column segfrac of table SegFrac.
- `segfrac_notes`: text NULL — Column segfrac_notes of table SegFrac.
- `time_of_day_name`: text NOT NULL — Column time_of_day_name of table SegFrac.
- primary key: season_name, time_of_day_name

### techinputsplit  (source backend: postgres)
Source table TechInputSplit.

- `input_comm`: text NOT NULL — Column input_comm of table TechInputSplit.
- `periods`: integer NOT NULL — Column periods of table TechInputSplit.
- `tech`: text NOT NULL — Column tech of table TechInputSplit.
- `ti_split`: float NULL — Column ti_split of table TechInputSplit.
- `ti_split_notes`: text NULL — Column ti_split_notes of table TechInputSplit.
- primary key: input_comm, periods, tech

### techoutputsplit  (source backend: s3)
Source table TechOutputSplit.

- `output_comm`: text NOT NULL — Column output_comm of table TechOutputSplit.
- `periods`: integer NOT NULL — Column periods of table TechOutputSplit.
- `tech`: text NOT NULL — Column tech of table TechOutputSplit.
- `to_split`: float NULL — Column to_split of table TechOutputSplit.
- `to_split_notes`: text NULL — Column to_split_notes of table TechOutputSplit.
- primary key: output_comm, periods, tech

### commodities  (source backend: postgres)
Source table commodities.

- `comm_desc`: text NULL — Column comm_desc of table commodities.
- `comm_name`: text NOT NULL — Column comm_name of table commodities.
- `flag`: text NULL — Column flag of table commodities.
- primary key: comm_name

### commodity_labels  (source backend: s3)
Source table commodity_labels.

- `comm_labels`: text NOT NULL — Column comm_labels of table commodity_labels.
- `comm_labels_desc`: text NULL — Column comm_labels_desc of table commodity_labels.
- primary key: comm_labels

### sector_labels  (source backend: rest)
Source table sector_labels.

- `sector`: text NOT NULL — Column sector of table sector_labels.
- primary key: sector

### technologies  (source backend: files)
Source table technologies.

- `flag`: text NULL — Column flag of table technologies.
- `sector`: text NULL — Column sector of table technologies.
- `tech`: text NOT NULL — Column tech of table technologies.
- `tech_category`: text NULL — Column tech_category of table technologies.
- `tech_desc`: text NULL — Column tech_desc of table technologies.
- primary key: tech

### technology_labels  (source backend: s3)
Source table technology_labels.

- `tech_labels`: text NOT NULL — Column tech_labels of table technology_labels.
- `tech_labels_desc`: text NULL — Column tech_labels_desc of table technology_labels.
- primary key: tech_labels

### time_of_day  (source backend: s3)
Source table time_of_day.

- `t_day`: text NOT NULL — Column t_day of table time_of_day.
- primary key: t_day

### time_period_labels  (source backend: files)
Source table time_period_labels.

- `t_period_labels`: text NOT NULL — Column t_period_labels of table time_period_labels.
- `t_period_labels_desc`: text NULL — Column t_period_labels_desc of table time_period_labels.
- primary key: t_period_labels

### time_periods  (source backend: rest)
Source table time_periods.

- `flag`: text NULL — Column flag of table time_periods.
- `t_periods`: integer NOT NULL — Column t_periods of table time_periods.
- primary key: t_periods

### time_season  (source backend: files)
Source table time_season.

- `t_season`: text NOT NULL — Column t_season of table time_season.
- primary key: t_season

### Relationships

- capacityfactorprocess(season_name) -> time_season(t_season) [required]
- capacityfactorprocess(tech) -> technologies(tech) [required]
- capacityfactorprocess(time_of_day_name) -> time_of_day(t_day) [required]
- capacityfactortech(season_name) -> time_season(t_season) [required]
- capacityfactortech(tech) -> technologies(tech) [required]
- capacityfactortech(time_of_day_name) -> time_of_day(t_day) [required]
- commodities(flag) -> commodity_labels(comm_labels) [optional (may be NULL/dangling)]
- costfixed(periods) -> time_periods(t_periods) [required]
- costfixed(tech) -> technologies(tech) [required]
- costfixed(vintage) -> time_periods(t_periods) [required]
- costinvest(tech) -> technologies(tech) [required]
- costinvest(vintage) -> time_periods(t_periods) [required]
- costvariable(periods) -> time_periods(t_periods) [required]
- costvariable(tech) -> technologies(tech) [required]
- costvariable(vintage) -> time_periods(t_periods) [required]
- demand(demand_comm) -> commodities(comm_name) [required]
- demand(periods) -> time_periods(t_periods) [required]
- demandspecificdistribution(demand_name) -> commodities(comm_name) [required]
- demandspecificdistribution(season_name) -> time_season(t_season) [required]
- demandspecificdistribution(time_of_day_name) -> time_of_day(t_day) [required]
- discountrate(tech) -> technologies(tech) [required]
- discountrate(vintage) -> time_periods(t_periods) [required]
- efficiency(input_comm) -> commodities(comm_name) [required]
- efficiency(output_comm) -> commodities(comm_name) [required]
- efficiency(tech) -> technologies(tech) [required]
- efficiency(vintage) -> time_periods(t_periods) [required]
- emissionactivity(emis_comm) -> commodities(comm_name) [required]
- emissionactivity(input_comm) -> commodities(comm_name) [required]
- emissionactivity(output_comm) -> commodities(comm_name) [required]
- emissionactivity(tech) -> technologies(tech) [required]
- emissionactivity(vintage) -> time_periods(t_periods) [required]
- emissionlimit(emis_comm) -> commodities(comm_name) [required]
- emissionlimit(periods) -> time_periods(t_periods) [required]
- existingcapacity(tech) -> technologies(tech) [required]
- existingcapacity(vintage) -> time_periods(t_periods) [required]
- growthratemax(tech) -> technologies(tech) [optional (may be NULL/dangling)]
- growthrateseed(tech) -> technologies(tech) [optional (may be NULL/dangling)]
- lifetimeloantech(tech) -> technologies(tech) [required]
- lifetimeprocess(tech) -> technologies(tech) [required]
- lifetimeprocess(vintage) -> time_periods(t_periods) [required]
- lifetimetech(tech) -> technologies(tech) [required]
- maxactivity(periods) -> time_periods(t_periods) [required]
- maxactivity(tech) -> technologies(tech) [required]
- maxcapacity(periods) -> time_periods(t_periods) [required]
- maxcapacity(tech) -> technologies(tech) [required]
- minactivity(periods) -> time_periods(t_periods) [required]
- minactivity(tech) -> technologies(tech) [required]
- mincapacity(periods) -> time_periods(t_periods) [required]
- mincapacity(tech) -> technologies(tech) [required]
- output_capacitybyperiodandtech(sector) -> sector_labels(sector) [optional (may be NULL/dangling)]
- output_capacitybyperiodandtech(t_periods) -> time_periods(t_periods) [required]
- output_capacitybyperiodandtech(tech) -> technologies(tech) [required]
- output_costs(sector) -> sector_labels(sector) [optional (may be NULL/dangling)]
- output_costs(tech) -> technologies(tech) [required]
- output_costs(vintage) -> time_periods(t_periods) [required]
- output_v_capacity(sector) -> sector_labels(sector) [optional (may be NULL/dangling)]
- output_v_capacity(tech) -> technologies(tech) [required]
- output_v_capacity(vintage) -> time_periods(t_periods) [required]
- output_vflow_in(input_comm) -> commodities(comm_name) [required]
- output_vflow_in(output_comm) -> commodities(comm_name) [required]
- output_vflow_in(sector) -> sector_labels(sector) [optional (may be NULL/dangling)]
- output_vflow_in(t_day) -> time_of_day(t_day) [required]
- output_vflow_in(t_periods) -> time_periods(t_periods) [required]
- output_vflow_in(tech) -> technologies(tech) [required]
- output_vflow_in(vintage) -> time_periods(t_periods) [required]
- output_vflow_out(input_comm) -> commodities(comm_name) [required]
- output_vflow_out(output_comm) -> commodities(comm_name) [required]
- output_vflow_out(sector) -> sector_labels(sector) [optional (may be NULL/dangling)]
- output_vflow_out(t_day) -> time_of_day(t_day) [required]
- output_vflow_out(t_periods) -> time_periods(t_periods) [required]
- output_vflow_out(tech) -> technologies(tech) [required]
- output_vflow_out(vintage) -> time_periods(t_periods) [required]
- segfrac(season_name) -> time_season(t_season) [required]
- segfrac(time_of_day_name) -> time_of_day(t_day) [required]
- techinputsplit(input_comm) -> commodities(comm_name) [required]
- techinputsplit(periods) -> time_periods(t_periods) [required]
- techinputsplit(tech) -> technologies(tech) [required]
- technologies(flag) -> technology_labels(tech_labels) [optional (may be NULL/dangling)]
- technologies(sector) -> sector_labels(sector) [optional (may be NULL/dangling)]
- techoutputsplit(output_comm) -> commodities(comm_name) [required]
- techoutputsplit(periods) -> time_periods(t_periods) [required]
- techoutputsplit(tech) -> technologies(tech) [required]
- time_periods(flag) -> time_period_labels(t_period_labels) [optional (may be NULL/dangling)]

