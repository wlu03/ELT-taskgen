# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# dlt extraction: workable

## Specification

PROJECT OVERVIEW

This project builds two analytical marts over data extracted by a dlt pipeline from a recruiting API ("workable"). Every source table below must be read from the extraction backend named beside it; a solver's pipeline must extract each table from exactly that backend.

Source tables and their extraction backends:
- Table candidates is extracted from the postgres backend. It holds one record per candidate, keyed by id, with an incremental cursor column updated_at, a curator-authored candidate_specialty column and a raw payload body.
- Table candidates_activities is extracted from the files backend. Each record carries a parent link _candidates_id, plus activity_rating, activity_channel and a raw payload body.
- Table candidates_offer is extracted from the s3 backend. Each record carries a parent link _candidates_id and a raw payload body.
- Table custom_attributes is extracted from the postgres backend and holds a raw payload body per record.
- Table events is extracted from the files backend and holds a raw payload body per record.
- Table jobs is extracted from the postgres backend. Each record has the deterministic surrogate primary key _jobs_surrogate_id, a curator-authored job_shortcode and a raw payload body.
- Table jobs_activities is extracted from the mongodb backend. Each record carries a parent link _jobs_id and a raw payload body.
- Table jobs_application_form is extracted from the mongodb backend. Each record carries a parent link _jobs_id and a raw payload body.
- Table jobs_custom_attributes is extracted from the s3 backend. Each record carries a parent link _jobs_id and a raw payload body.
- Table jobs_members is extracted from the postgres backend. Each record carries a parent link _jobs_id and a raw payload body.
- Table jobs_questions is extracted from the files backend. Each record carries a parent link _jobs_id and a raw payload body.
- Table jobs_recruiters is extracted from the files backend. Each record carries a parent link _jobs_id and a raw payload body.
- Table jobs_stages is extracted from the s3 backend. Each record carries a parent link _jobs_id, a curator-authored stage_position, a curator-authored stage_slug and a raw payload body.
- Table members is extracted from the rest backend and holds a raw payload body per record.
- Table recruiters is extracted from the files backend and holds a raw payload body per record.
- Table requisitions is extracted from the mongodb backend and holds a raw payload body per record.
- Table stages is extracted from the mongodb backend and holds a raw payload body per record.

Relationships between the extracted tables (each stated with its child table and key, its parent table and key, and whether it is required or optional):

- Child table candidates_activities with key _candidates_id refers to parent table candidates with key id; this relationship is required.
- Child table candidates_offer with key _candidates_id refers to parent table candidates with key id; this relationship is required.
- Child table jobs_activities with key _jobs_id refers to parent table jobs with key _jobs_surrogate_id; this relationship is required.
- Child table jobs_application_form with key _jobs_id refers to parent table jobs with key _jobs_surrogate_id; this relationship is required.
- Child table jobs_custom_attributes with key _jobs_id refers to parent table jobs with key _jobs_surrogate_id; this relationship is required.
- Child table jobs_members with key _jobs_id refers to parent table jobs with key _jobs_surrogate_id; this relationship is required.
- Child table jobs_questions with key _jobs_id refers to parent table jobs with key _jobs_surrogate_id; this relationship is required.
- Child table jobs_recruiters with key _jobs_id refers to parent table jobs with key _jobs_surrogate_id; this relationship is required.
- Child table jobs_stages with key _jobs_id refers to parent table jobs with key _jobs_surrogate_id; this relationship is required.

Two marts are produced: jobs_stages_top and dim_jobs. Each is described in its own section below.


=== Mart jobs_stages_top: per-jobs extremes over extracted jobs_stages records — WHICH record is largest, not how large it is. ===

Grain: one row per jobs (_jobs_surrogate_id), INCLUDING jobs rows with no linked jobs_stages rows (that is, including jobs rows to which no jobs_stages row is attributed).

Key column: parent_key.

Output columns:
- parent_key (bigint): identifier of the jobs row. One row per value.
- parent_name (text): job_shortcode of the jobs row, copied unchanged.
- top_measure (integer): the largest stage_position itself; 0 when the parent has no jobs_stages rows, and 0 when none of its rows carries a stage_position value.
- tied_count (bigint): how many jobs_stages rows are tied at that largest stage_position. It is 1 when exactly one row carries that largest stage_position; it is 0 when there are no rows or when none of the rows carries a stage_position value; a row with no stage_position value never ties: only a row whose stage_position value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.
- child_count (bigint): number of jobs_stages rows for this jobs row; 0 when there are none. Every linked jobs_stages row counts, whether or not it carries a stage_position value. A jobs row kept with no jobs_stages row reports 0 here, never 1: its placeholder holds no jobs_stages row to count.
- total_measure (integer): total of stage_position over all of them; 0 when the parent has no jobs_stages rows, and 0 when none of its rows carries a stage_position value (rows with no stage_position value add nothing).
- top_label (text): the stage_slug of the jobs_stages row with the LARGEST stage_position for this jobs row. Ties in stage_position are broken by taking the SMALLEST stage_slug under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no stage_slug value sorts after every labelled row. A row with no stage_position value still ranks, after every row that has one, so a parent holding at least one jobs_stages row always has a winning row — when NONE of its rows carries a stage_position value the winner is the one the tie-break alone selects, not the no-rows default. top_label is the literal '(none)' when the parent has no jobs_stages rows at all, and '(none)' when the winning row has no stage_slug value.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no jobs_stages rows, or none of its rows carries a stage_position value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do.

Rules that build this mart:

1. The extracted source table jobs is read and supplies the parents of this mart.

2. The extracted source table jobs_stages is read and supplies the child records measured by this mart.

3. From source table jobs there is one row per jobs row, keyed by _jobs_surrogate_id, carrying parent_key (that _jobs_surrogate_id value) and parent_name (the job_shortcode of that jobs row, copied unchanged).

4. Records of jobs_stages are brought in by matching jobs_stages._jobs_id to parent_key, carrying _jobs_id beside _jobs_surrogate_id; preservation is left-sided, so a jobs row with no jobs_stages rows still appears, with the declared defaults.

5. Within each parent_key the matched rows are ranked under an explicit total order — the measure stage_position first, then the declared tie-break on stage_slug — so the extremal row is a function of the input and not of row order.

6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

7. For each parent_key the single row at which the ordering measure stage_position is largest survives, ties broken by the smallest stage_slug under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), and a row with no stage_slug value sorts after every row that has one; top_label is taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

8. The winning extremal row's attributes are attached to the measures of the same parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

9. The mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure and top_label; top_measure and total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure and total_measure the default also applies to a group none of whose real rows carries an input value.

10. Beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure and top_label the guarded ratio top_measure_share is reported: top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimal places, and 0.0 when the denominator total_measure is 0 or has no value.

11. Beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_measure_share, tie_state is 'empty' when no row holds a maximum at all — the parent has no jobs_stages rows, or none of its rows carries a stage_position value — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; this is a categorical mapping with no numeric boundary. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more; a row holds the maximum only when it carries a stage_position value equal to the largest stage_position value among the parent's rows, a row with no stage_position value never holds the maximum, so a parent whose jobs_stages rows all lack a stage_position value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label names.

12. Deterministic output order: rows are sorted in ascending parent_key order.


=== Mart dim_jobs: one row per 'jobs' record extracted from the API. ===

Grain: one row per jobs (_jobs_surrogate_id).

Key column: _jobs_surrogate_id.

Output columns:
- _jobs_surrogate_id (bigint): the jobs primary key column '_jobs_surrogate_id'.
- jobs_activities_count (bigint): number of 'jobs_activities' records extracted for this 'jobs' record, and 0 when none.

Rules that build this mart:

1. The extracted source table jobs is read and supplies the records of this mart.

2. The extracted source table jobs_activities is read and supplies the child records counted by this mart.

3. The mart key column _jobs_surrogate_id is formed from source table jobs.

4. Records of jobs_activities are brought in by matching jobs_activities._jobs_id to _jobs_surrogate_id, carrying _jobs_surrogate_id and _jobs_id; preservation is left-sided, so every jobs record appears whether or not it has matching jobs_activities records.

5. There is one output row per _jobs_surrogate_id, reporting jobs_activities_count for that row's matching rows.

6. The mart columns are named _jobs_surrogate_id and jobs_activities_count.

7. Deterministic output order: rows are sorted in ascending _jobs_surrogate_id order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `jobs_stages_top`

- Grain: One row per jobs (_jobs_surrogate_id), INCLUDING jobs rows with no linked jobs_stages rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_measure_share, tie_state

```text
Mart 'jobs_stages_top' has 12 declared semantic rules:
1. [source] Read source table jobs. (public source tables: jobs)
2. [source] Read source table jobs_stages. (public source tables: jobs_stages)
3. [derive] One row per jobs row, keyed by _jobs_surrogate_id. (public source tables: jobs | public carried/output columns: parent_key, parent_name)
4. [join] Bring in jobs_stages: a jobs row with no jobs_stages rows still appears, with the declared defaults. (public source tables: jobs_stages | public carried/output columns: _jobs_id, _jobs_surrogate_id | join preservation: left | condition public identifiers: jobs_stages, _jobs_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest stage_slug under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no stage_slug value sorts after every row that has one), and take top_label from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no jobs_stages rows, or none of its rows carries a stage_position value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a stage_position value equal to the largest stage_position value among the parent's rows; a row with no stage_position value never holds the maximum. So a parent whose jobs_stages rows all lack a stage_position value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label names. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `dim_jobs`

- Grain: one row per jobs (_jobs_surrogate_id)
- Unique key: _jobs_surrogate_id
- Required columns: _jobs_surrogate_id, jobs_activities_count

```text
Mart 'dim_jobs' has 7 declared semantic rules:
1. [source] Read source table jobs. (public source tables: jobs)
2. [source] Read source table jobs_activities. (public source tables: jobs_activities)
3. [derive] Form the mart key columns _jobs_surrogate_id from source table jobs. (public source tables: jobs | public carried/output columns: _jobs_surrogate_id)
4. [join] Bring in jobs_activities: every jobs record appears whether or not it has matching jobs_activities records. (public source tables: jobs_activities | public carried/output columns: _jobs_surrogate_id, _jobs_id | join preservation: left | condition public identifiers: jobs_activities, _jobs_id, _jobs_surrogate_id)
5. [aggregate] One output row per _jobs_surrogate_id, reporting jobs_activities_count for that row's matching rows. (public carried/output columns: _jobs_surrogate_id, jobs_activities_count)
6. [derive] Name the mart columns. (public carried/output columns: _jobs_surrogate_id, jobs_activities_count)
7. [tie_break] Deterministic output order: sort by _jobs_surrogate_id. (public carried/output columns: _jobs_surrogate_id)
```

## Source tables

### candidates  (source backend: postgres)
dlt resource 'candidates' at /candidates (write_disposition=merge, cursor=updated_at)

- `id`: bigint NOT NULL — primary key of dlt resource 'candidates' (synthesized from the dlt manifest (no observed schema))
- `updated_at`: timestamp NOT NULL — incremental cursor of dlt resource 'candidates' (dlt cursor path 'updated_at')
- `candidate_specialty`: text NULL — curator-authored column of dlt resource 'candidates' (source: curator-invented)
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: id

### candidates_activities  (source backend: files)
dlt transformer 'candidates_activities' at /candidates_activities (write_disposition=append, opt-in: emitted only when load_details)

- `_candidates_id`: bigint NOT NULL — parent link to 'candidates' (dlt include_from_parent convention; synthesized from the dlt manifest (no observed schema))
- `activity_rating`: decimal NULL — curator-authored column of dlt transformer 'candidates_activities' (source: curator-invented)
- `activity_channel`: text NULL — curator-authored column of dlt transformer 'candidates_activities' (source: curator-invented)
- `payload`: json NULL — raw endpoint record body (synthesized schema)

### candidates_offer  (source backend: s3)
dlt transformer 'candidates_offer' at /candidates_offer (write_disposition=append, opt-in: emitted only when load_details)

- `_candidates_id`: bigint NOT NULL — parent link to 'candidates' (dlt include_from_parent convention; synthesized from the dlt manifest (no observed schema))
- `payload`: json NULL — raw endpoint record body (synthesized schema)

### custom_attributes  (source backend: postgres)
dlt resource 'custom_attributes' at /custom_attributes (write_disposition=replace)

- `payload`: json NULL — raw endpoint record body (synthesized schema)

### events  (source backend: files)
dlt resource 'events' at /events (write_disposition=replace)

- `payload`: json NULL — raw endpoint record body (synthesized schema)

### jobs  (source backend: postgres)
dlt resource 'jobs' at /jobs (write_disposition=replace)

- `_jobs_surrogate_id`: bigint NOT NULL — deterministic surrogate primary key of dlt resource 'jobs', synthesized because the connector declares none (synthesized from the dlt manifest (no observed schema))
- `job_shortcode`: text NULL — curator-authored column of dlt resource 'jobs' (source: curator-invented)
- `payload`: json NULL — raw endpoint record body (synthesized schema)
- primary key: _jobs_surrogate_id

### jobs_activities  (source backend: mongodb)
dlt transformer 'jobs_activities' at /jobs_activities (write_disposition=replace, opt-in: emitted only when load_details)

- `_jobs_id`: bigint NOT NULL — parent link to 'jobs' (dlt include_from_parent convention; synthesized from the dlt manifest (no observed schema))
- `payload`: json NULL — raw endpoint record body (synthesized schema)

### jobs_application_form  (source backend: mongodb)
dlt transformer 'jobs_application_form' at /jobs_application_form (write_disposition=replace, opt-in: emitted only when load_details)

- `_jobs_id`: bigint NOT NULL — parent link to 'jobs' (dlt include_from_parent convention; synthesized from the dlt manifest (no observed schema))
- `payload`: json NULL — raw endpoint record body (synthesized schema)

### jobs_custom_attributes  (source backend: s3)
dlt transformer 'jobs_custom_attributes' at /jobs_custom_attributes (write_disposition=replace, opt-in: emitted only when load_details)

- `_jobs_id`: bigint NOT NULL — parent link to 'jobs' (dlt include_from_parent convention; synthesized from the dlt manifest (no observed schema))
- `payload`: json NULL — raw endpoint record body (synthesized schema)

### jobs_members  (source backend: postgres)
dlt transformer 'jobs_members' at /jobs_members (write_disposition=replace, opt-in: emitted only when load_details)

- `_jobs_id`: bigint NOT NULL — parent link to 'jobs' (dlt include_from_parent convention; synthesized from the dlt manifest (no observed schema))
- `payload`: json NULL — raw endpoint record body (synthesized schema)

### jobs_questions  (source backend: files)
dlt transformer 'jobs_questions' at /jobs_questions (write_disposition=replace, opt-in: emitted only when load_details)

- `_jobs_id`: bigint NOT NULL — parent link to 'jobs' (dlt include_from_parent convention; synthesized from the dlt manifest (no observed schema))
- `payload`: json NULL — raw endpoint record body (synthesized schema)

### jobs_recruiters  (source backend: files)
dlt transformer 'jobs_recruiters' at /jobs_recruiters (write_disposition=replace, opt-in: emitted only when load_details)

- `_jobs_id`: bigint NOT NULL — parent link to 'jobs' (dlt include_from_parent convention; synthesized from the dlt manifest (no observed schema))
- `payload`: json NULL — raw endpoint record body (synthesized schema)

### jobs_stages  (source backend: s3)
dlt transformer 'jobs_stages' at /jobs_stages (write_disposition=replace, opt-in: emitted only when load_details)

- `_jobs_id`: bigint NOT NULL — parent link to 'jobs' (dlt include_from_parent convention; synthesized from the dlt manifest (no observed schema))
- `stage_position`: integer NULL — curator-authored column of dlt transformer 'jobs_stages' (source: curator-invented)
- `stage_slug`: text NULL — curator-authored column of dlt transformer 'jobs_stages' (source: curator-invented)
- `payload`: json NULL — raw endpoint record body (synthesized schema)

### members  (source backend: rest)
dlt resource 'members' at /members (write_disposition=replace)

- `payload`: json NULL — raw endpoint record body (synthesized schema)

### recruiters  (source backend: files)
dlt resource 'recruiters' at /recruiters (write_disposition=replace)

- `payload`: json NULL — raw endpoint record body (synthesized schema)

### requisitions  (source backend: mongodb)
dlt resource 'requisitions' at /requisitions (write_disposition=replace)

- `payload`: json NULL — raw endpoint record body (synthesized schema)

### stages  (source backend: mongodb)
dlt resource 'stages' at /stages (write_disposition=replace)

- `payload`: json NULL — raw endpoint record body (synthesized schema)

### Relationships

- candidates_activities(_candidates_id) -> candidates(id) [required]
- candidates_offer(_candidates_id) -> candidates(id) [required]
- jobs_activities(_jobs_id) -> jobs(_jobs_surrogate_id) [required]
- jobs_application_form(_jobs_id) -> jobs(_jobs_surrogate_id) [required]
- jobs_custom_attributes(_jobs_id) -> jobs(_jobs_surrogate_id) [required]
- jobs_members(_jobs_id) -> jobs(_jobs_surrogate_id) [required]
- jobs_questions(_jobs_id) -> jobs(_jobs_surrogate_id) [required]
- jobs_recruiters(_jobs_id) -> jobs(_jobs_surrogate_id) [required]
- jobs_stages(_jobs_id) -> jobs(_jobs_surrogate_id) [required]

