# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Educational Expenditure Data And Analysis

## Specification

PROJECT OVERVIEW

This project, Educational Expenditure Data And Analysis, builds two analytical marts over the __educational_expenditure_data_and_analysis__ scenario. Seven source tables feed the scenario, and each must be extracted from its own declared backend.

Source tables and their extraction backends:
- Source table regions must be extracted from the s3 backend. It holds one row per educational region, identified by region_code (its primary key), with region_name, region_type (one of urban, rural, suburban), population, number_of_schools, contact_person and contact_email; every column except region_code may be missing.
- Source table school_years must be extracted from the files backend. It holds one row per school year, identified by schl_year (its primary key), with schl_year_desc, start_date, end_date and is_current, all of which may be missing.
- Source table data_categories must be extracted from the rest backend. It holds one row per category_group (its primary key), with category, category_description and is_active, all of which may be missing.
- Source table expenditures must be extracted from the mongodb backend. It holds one row per expenditure record, identified by exp_id (its primary key), with region_code, schl_year, category_group, category, exp_usd_amt, expenditure_date, funding_source_id, budgeted_amount, actual_amount, variance, description, created_by, created_at, updated_by and updated_at, all of which may be missing.
- Source table funding_sources must be extracted from the mongodb backend. It holds one row per funding source, identified by fund_id (its primary key), with fund_name, description, is_active, budget_limit and available_balance, all of which may be missing.
- Source table expenditure_funding must be extracted from the files backend. It holds one row per exp_id (its primary key), with fund_id, allocation_date, allocation_amount and notes, all of which may be missing.
- Source table users must be extracted from the rest backend. It holds one row per user, identified by user_id (its primary key), with user_name, email, role (one of administrator, finance officer, policymaker), phone_number, last_login, status (one of active, inactive), created_at and updated_at; every column except user_id may be missing.

Relationships declared by the source schema:
- Child table expenditures with key created_by refers to parent table users with key user_id; this relationship is optional, so created_by may be missing or may name a user_id that does not exist.
- Child table expenditures with key funding_source_id refers to parent table funding_sources with key fund_id; this relationship is optional, so funding_source_id may be missing or may name a fund_id that does not exist.
- Child table expenditures with key region_code refers to parent table regions with key region_code; this relationship is optional, so region_code may be missing or may name a region_code that does not exist.
- Child table expenditures with key schl_year refers to parent table school_years with key schl_year; this relationship is optional, so schl_year may be missing or may name a schl_year that does not exist.
- Child table expenditures with key updated_by refers to parent table users with key user_id; this relationship is optional, so updated_by may be missing or may name a user_id that does not exist.

Throughout both marts, an expenditures row is attributed to a users row when the expenditures row's created_by equals that users row's user_id. Values are compared as stored; no trimming, casing or type coercion is applied beyond what the declared types imply.

=== Mart users_expenditures_distribution: per-(users, measure state) distribution of linked expenditures rows in the __educational_expenditure_data_and_analysis__ scenario ===

Grain: one row per (user_id, measure state) pair represented among linked expenditures rows; the absent state includes missing exp_usd_amt values and a no-activity row for a users row with no links. A linked expenditures row whose exp_usd_amt has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state together identify one output row.

Output columns:
- entity_key (integer): identifier of the users row.
- measure_state (text): 'present' for a linked expenditures row whose exp_usd_amt has a value; 'absent' when exp_usd_amt is missing, including a users row with no linked expenditures row. A linked expenditures row whose exp_usd_amt has a value belongs only to the present state and never to the absent state.
- entity_name (text): user_name of the users row, copied unchanged.
- row_count (bigint): number of linked expenditures rows in this entity/state cell; an absent cell holding real expenditures rows whose exp_usd_amt is missing COUNTS those rows, and only the placeholder cell of an users row with no linked expenditures row at all reports 0.
- distinct_amount_count (bigint): number of unique non-missing exp_usd_amt values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no exp_usd_amt value at all — both for an users row with no linked expenditures row and for an absent cell whose rows all have a missing exp_usd_amt.
- total_amount (integer): total of exp_usd_amt in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an exp_usd_amt value.
- max_amount (integer): largest exp_usd_amt in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an exp_usd_amt value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules producing this mart:

Rule 1. The source table users is read in full as an input to this mart.

Rule 2. The source table expenditures is read in full as an input to this mart.

Rule 3. From users, each user_id is carried into the measure-state calculation as entity_key and its user_name is carried as entity_name.

Rule 4. The linked expenditures rows are brought into each users entity — an expenditures row is linked to the entity when its created_by equals the users row's user_id, that is, the entity_key — and preservation is left-sided on the users side: an entity with no linked expenditures row is retained so its absent state is visible, carrying entity_key, entity_name, user_id and created_by.

Rule 5. The present measure-state rows kept are those that are a real expenditures row whose exp_usd_amt has a value, carrying entity_key and entity_name.

Rule 6. In the present measure state there is one row per users entity that has at least one row in that state, and no row here for an entity with none, reporting for entity_key and entity_name the row count as row_count, how many different non-missing exp_usd_amt values occur as distinct_amount_count (each different value counted once, however many rows repeat it), the total exp_usd_amt as total_amount, and the largest exp_usd_amt as max_amount.

Rule 7. For each present-state row, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0. Its row carries entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 8. These measures are labelled as the present measure state: measure_state reads 'present' on every such row, which carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9. The absent measure-state rows kept are those whose exp_usd_amt is missing, including the retained placeholder for a users row with no expenditures rows, carrying entity_key and entity_name. A real expenditures row whose exp_usd_amt has a value belongs only to the present state and never to this absent state.

Rule 10. In the absent measure state there is one row per users entity that has at least one row in that state, and no row here for an entity with none, reporting for entity_key and entity_name the row count as row_count, how many different non-missing exp_usd_amt values occur as distinct_amount_count (each different value counted once, however many rows repeat it), the total exp_usd_amt as total_amount, and the largest exp_usd_amt as max_amount.

Rule 11. For each absent-state row, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0. Its row carries entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 12. These measures are labelled as the absent measure state: measure_state reads 'absent' on every such row, which carries entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other; all rows of both are kept, carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 14. The deterministic output order is by entity, then measure state: rows appear in ascending entity_key order and, within one entity_key, in ascending measure_state order.

=== Mart users_expenditures_top: per-users extremes of linked expenditures rows in the __educational_expenditure_data_and_analysis__ scenario — WHICH row is largest, not how large it is ===

Grain: one row per users (user_id), INCLUDING users rows with no linked expenditures rows.

Key column: parent_key identifies one output row.

Output columns:
- parent_key (integer): identifier of the users row. One row per value.
- parent_name (text): user_name of the users row, copied unchanged.
- top_measure (integer): the largest exp_usd_amt itself; 0 when the parent has no expenditures rows, and 0 when none of its rows carries a exp_usd_amt value.
- tied_count (bigint): how many expenditures rows are tied at that largest exp_usd_amt. It is 1 when exactly one row carries that largest exp_usd_amt; 0 when there are no rows or when none of the rows carries a exp_usd_amt value; a row with no exp_usd_amt value never ties: only a row whose exp_usd_amt value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.
- child_count (bigint): number of expenditures rows for this users row; 0 when there are none. Every linked expenditures row counts, whether or not it carries an exp_usd_amt value. An users row kept with no expenditures row reports 0 here, never 1: its placeholder holds no expenditures row to count.
- total_measure (integer): total of exp_usd_amt over all of them; 0 when the parent has no expenditures rows, and 0 when none of its rows carries a exp_usd_amt value (rows with no exp_usd_amt value add nothing).
- top_label (text): the category_group of the expenditures row with the LARGEST exp_usd_amt for this users row. Ties in exp_usd_amt are broken by taking the SMALLEST category_group under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no category_group value sorts after every labelled row; rows tied on both are resolved by the smallest exp_id. A row with no exp_usd_amt value still ranks, after every row that has one, so a parent holding at least one expenditures row always has a winning row — when NONE of its rows carries a exp_usd_amt value the winner is the one the tie-break alone selects, not the no-rows default. It is the literal '(none)' when the parent has no expenditures rows at all, and '(none)' when the winning row has no category_group value.
- top_row_id (integer): the exp_id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real expenditures row whenever the parent has any. This includes when none of them carries a exp_usd_amt value. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no expenditures rows, or none of its rows carries a exp_usd_amt value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do.

Rules producing this mart:

Rule 1. The source table users is read in full as an input to this mart.

Rule 2. The source table expenditures is read in full as an input to this mart.

Rule 3. From users there is one row per users row, keyed by user_id, carried as parent_key with its user_name carried as parent_name.

Rule 4. The expenditures rows are brought in — an expenditures row belongs to the users row whose user_id equals its created_by, that is, the parent_key — and preservation is left-sided on the users side: a users row with no expenditures rows still appears, with the declared defaults, carrying created_by and user_id.

Rule 5. Within each parent_key the attributed rows are ranked under an explicit total order (the measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order.

Rule 6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7. For each parent_key the single row kept is the one at which the ordering measure is largest, ties broken by the smallest category_group under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no category_group value sorts after every row that has one), then the smallest exp_id, and top_label, top_row_id are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 8. The extremal row's attributes are attached to the grouped measures by matching on parent_key, with left-sided preservation of the measures: a group with no rows at all keeps its measures.

Rule 9. The mart columns are named: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id, where top_measure and total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure and total_measure, the default also applies to a group none of whose real rows carries an input value.

Rule 10. Guarded ratio: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when total_measure is 0 or has no value; its row carries parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share.

Rule 11. tie_state is 'empty' when no row holds a maximum at all — the parent has no expenditures rows, or none of its rows carries a exp_usd_amt value — 'unique' when exactly one row holds the maximum, and otherwise 'tied' when two or more do; equivalently tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more, and it is never null or blank. A row holds the maximum only when it carries a exp_usd_amt value equal to the largest exp_usd_amt value among the parent's rows; a row with no exp_usd_amt value never holds the maximum, so a parent whose expenditures rows all lack a exp_usd_amt value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name, beside parent_key, parent_name, top_measure, child_count, total_measure and top_measure_share.

Rule 12. The deterministic output order is ascending parent_key.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `users_expenditures_distribution`

- Grain: One row per (user_id, measure state) pair represented among linked expenditures rows; the absent state includes missing exp_usd_amt values and a no-activity row for a users row with no links. A linked expenditures row whose exp_usd_amt has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'users_expenditures_distribution' has 14 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table expenditures. (public source tables: expenditures)
3. [derive] Carry each user_id and its user_name into the measure-state calculation. (public source tables: users | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked expenditures rows into each users entity; retain an entity with no linked row so its absent state is visible. (public source tables: expenditures | public carried/output columns: entity_key, entity_name, user_id, created_by | join preservation: left | condition public identifiers: expenditures, created_by, entity_key)
5. [filter] Keep the present measure-state rows: a real expenditures row whose exp_usd_amt has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per users entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing exp_usd_amt values occur (each different value counted once, however many rows repeat it), total exp_usd_amt, and largest exp_usd_amt. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: exp_usd_amt is missing, including the retained placeholder for a users row with no expenditures rows. A real expenditures row whose exp_usd_amt has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per users entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing exp_usd_amt values occur (each different value counted once, however many rows repeat it), total exp_usd_amt, and largest exp_usd_amt. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `users_expenditures_top`

- Grain: One row per users (user_id), INCLUDING users rows with no linked expenditures rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'users_expenditures_top' has 12 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table expenditures. (public source tables: expenditures)
3. [derive] One row per users row, keyed by user_id. (public source tables: users | public carried/output columns: parent_key, parent_name)
4. [join] Bring in expenditures: a users row with no expenditures rows still appears, with the declared defaults. (public source tables: expenditures | public carried/output columns: created_by, user_id | join preservation: left | condition public identifiers: expenditures, created_by, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest category_group under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no category_group value sorts after every row that has one), then the smallest exp_id, and take top_label, top_row_id from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no expenditures rows, or none of its rows carries a exp_usd_amt value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a exp_usd_amt value equal to the largest exp_usd_amt value among the parent's rows; a row with no exp_usd_amt value never holds the maximum. So a parent whose expenditures rows all lack a exp_usd_amt value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### regions  (source backend: s3)
Source table regions of the __educational_expenditure_data_and_analysis__ scenario.

- `region_code`: integer NOT NULL — Unique identifier for each educational region
- `region_name`: text NULL — Name of the educational region
- `region_type`: text NULL — Type of region (e.g., urban, rural, suburban) one of: urban, rural, suburban.
- `population`: integer NULL — Population of the region
- `number_of_schools`: integer NULL — Number of schools in the region
- `contact_person`: text NULL — Name of the contact person for the region
- `contact_email`: text NULL — Email of the contact person for the region
- primary key: region_code

### school_years  (source backend: files)
Source table school_years of the __educational_expenditure_data_and_analysis__ scenario.

- `schl_year`: integer NOT NULL — Unique identifier for each school year
- `schl_year_desc`: text NULL — Description of the school year
- `start_date`: text NULL — Start date of the school year
- `end_date`: text NULL — End date of the school year
- `is_current`: integer NULL — Indicates if the school year is the current one
- primary key: schl_year

### data_categories  (source backend: rest)
Source table data_categories of the __educational_expenditure_data_and_analysis__ scenario.

- `category_group`: text NOT NULL — General category group of the expenditure data
- `category`: text NULL — Specific category within the expenditure data
- `category_description`: text NULL — Detailed description of the category
- `is_active`: integer NULL — Indicates if the category is currently active
- primary key: category_group

### expenditures  (source backend: mongodb)
Source table expenditures of the __educational_expenditure_data_and_analysis__ scenario.

- `exp_id`: integer NOT NULL — Unique identifier for each expenditure record
- `region_code`: integer NULL — Reference to the educational region
- `schl_year`: integer NULL — Reference to the school year
- `category_group`: text NULL — General category group of the expenditure
- `category`: text NULL — Specific category of the expenditure
- `exp_usd_amt`: integer NULL — Amount of expenditure in USD
- `expenditure_date`: text NULL — Date of the expenditure
- `funding_source_id`: integer NULL — Direct reference to the funding source
- `budgeted_amount`: integer NULL — Budgeted amount for the expenditure
- `actual_amount`: integer NULL — Actual amount spent
- `variance`: integer NULL — Variance between budgeted and actual amounts
- `description`: text NULL — Description of the expenditure
- `created_by`: integer NULL — User ID of the person who created the record
- `created_at`: text NULL — Timestamp when the record was created
- `updated_by`: integer NULL — User ID of the person who last updated the record
- `updated_at`: text NULL — Timestamp when the record was last updated
- primary key: exp_id

### funding_sources  (source backend: mongodb)
Source table funding_sources of the __educational_expenditure_data_and_analysis__ scenario.

- `fund_id`: integer NOT NULL — Unique identifier for each funding source
- `fund_name`: text NULL — Name of the funding source
- `description`: text NULL — Description of the funding source
- `is_active`: integer NULL — Indicates if the funding source is currently active
- `budget_limit`: integer NULL — Budget limit for the funding source
- `available_balance`: integer NULL — Available balance in the funding source
- primary key: fund_id

### expenditure_funding  (source backend: files)
Source table expenditure_funding of the __educational_expenditure_data_and_analysis__ scenario.

- `exp_id`: integer NOT NULL — Reference to the expenditure record
- `fund_id`: integer NULL — Reference to the funding source
- `allocation_date`: text NULL — Date when the funding was allocated
- `allocation_amount`: integer NULL — Amount allocated from the funding source
- `notes`: text NULL — Notes or comments related to the allocation
- primary key: exp_id

### users  (source backend: rest)
Source table users of the __educational_expenditure_data_and_analysis__ scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `user_name`: text NULL — Full name of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., administrator, finance officer, policymaker) one of: administrator, finance officer, policymaker.
- `phone_number`: text NULL — Phone number of the user
- `last_login`: text NULL — Timestamp of the last login
- `status`: text NULL — Status of the user (e.g., active, inactive) one of: active, inactive.
- `created_at`: text NULL — Timestamp when the user account was created
- `updated_at`: text NULL — Timestamp when the user account was last updated
- primary key: user_id

### Relationships

- expenditures(created_by) -> users(user_id) [optional (may be NULL/dangling)]
- expenditures(funding_source_id) -> funding_sources(fund_id) [optional (may be NULL/dangling)]
- expenditures(region_code) -> regions(region_code) [optional (may be NULL/dangling)]
- expenditures(schl_year) -> school_years(schl_year) [optional (may be NULL/dangling)]
- expenditures(updated_by) -> users(user_id) [optional (may be NULL/dangling)]

