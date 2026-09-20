# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# 529 College Savings Program Rankings And Information

## Specification

529 College Savings Program Rankings And Information — Project Overview

This project builds two published marts from the 529_college_savings_program_rankings_and_information scenario. Every source table must be read from the extraction backend named here, and from nowhere else.

Source tables and their extraction backends:
- Source table plans is extracted from the files backend. It holds plan_id, state_id, plan_name, performance_score, details_link, management_fees, minimum_contribution, maximum_contribution, tax_benefits and investment_options, with plan_id as its primary key.
- Source table states is extracted from the files backend. It holds state_id, state_name, abbreviation, state_tax_benefit and state_website, with state_id as its primary key.
- Source table users is extracted from the postgres backend. It holds user_id, user_name, email, role (one of parent, financial advisor, admin), password_hash, phone_number, address, city, state_id and zip_code, with user_id as its primary key.
- Source table user_favorites is extracted from the s3 backend. It holds favorite_id, user_id, plan_id, added_date and notes, with favorite_id as its primary key.
- Source table plan_performance is extracted from the files backend. It holds performance_id, plan_id, year, performance_score and rank, with performance_id as its primary key.
- Source table plan_investment_options is extracted from the s3 backend. It holds option_id, plan_id, option_name, option_type (one of stock, bond, ETF) and option_description, with option_id as its primary key.
- Source table plan_expenses is extracted from the postgres backend. It holds expense_id, plan_id, expense_type (one of management fee, administrative fee) and expense_amount, with expense_id as its primary key.
- Source table user_recommendations is extracted from the mongodb backend. It holds recommendation_id, user_id, plan_id and recommendation_date, with recommendation_id as its primary key.
- Source table reporting is extracted from the rest backend. It holds report_id, user_id, report_type (one of plan performance, user engagement), report_date and report_data, with report_id as its primary key.

Relationships between the source tables, each labelled exactly as the source schema declares it:
- Child table plan_expenses with key plan_id refers to parent table plans with key plan_id; this relationship is optional (may be NULL or dangling).
- Child table plan_investment_options with key plan_id refers to parent table plans with key plan_id; this relationship is optional (may be NULL or dangling).
- Child table plan_performance with key plan_id refers to parent table plans with key plan_id; this relationship is optional (may be NULL or dangling).
- Child table plans with key state_id refers to parent table states with key state_id; this relationship is optional (may be NULL or dangling).
- Child table reporting with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).
- Child table user_favorites with key plan_id refers to parent table plans with key plan_id; this relationship is optional (may be NULL or dangling).
- Child table user_favorites with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).
- Child table user_recommendations with key plan_id refers to parent table plans with key plan_id; this relationship is optional (may be NULL or dangling).
- Child table user_recommendations with key user_id refers to parent table users with key user_id; this relationship is optional (may be NULL or dangling).

Values are compared literally as written; text values such as management fee, administrative fee, plan performance, user engagement, parent, financial advisor and admin are matched exactly.

Mart plans_plan_expenses_cohorts — a per-(plans, status cohort) summary of linked plan_expenses rows in the 529_college_savings_program_rankings_and_information scenario, with passing and failing cohorts kept separate.

Grain: one row per (plan_id, status cohort) pair represented among linked plan_expenses rows, plus one no-activity row for a plans row with no linked plan_expenses row at all. A plans row whose linked plan_expenses rows all lack a expense_type value is in no cohort and gets no no-activity row, so it has no row in this mart.

Key columns: entity_key and cohort together identify a row of this mart.

Output columns of plans_plan_expenses_cohorts:
- entity_key (integer): identifier of the plans row.
- cohort (text): 'passing' for expense_type values ['management fee']; 'failing' for values ['administrative fee']; 'no_activity' when the plans row has no linked plan_expenses row. A linked plan_expenses row whose expense_type has no value belongs to no cohort: it is not counted in any cell, and it does not make the plans row 'no_activity'.
- entity_name (text): plan_name of the plans row, copied unchanged.
- link_count (bigint): number of plan_expenses rows in this entity/cohort cell.
- distinct_status_count (bigint): number of different expense_type values represented in this cell.
- total_amount (float): total of expense_amount in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an expense_amount value.
- max_amount (float): largest expense_amount in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an expense_amount value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules for plans_plan_expenses_cohorts, each stated as the outcome it produces:

1. Source table plans is read in full as an input to this mart.

2. Source table plan_expenses is read in full as an input to this mart.

3. Each plans row contributes its plan_id as entity_key and its plan_name as entity_name, and both entity_key and entity_name are carried into the cohort calculation.

4. The plan_expenses rows matching a plans entity — those whose plan_id equals that entity's entity_key — are brought in and attributed to that entity before status cohorts are assigned, with entity_key, entity_name and plan_id carried; preservation is left-sided from the plans side, so a plans entity with no matching plan_expenses row is retained.

5. For the passing cohort, only rows whose expense_type is management fee are kept, carrying entity_key and entity_name.

6. The passing cohort yields one row per plans entity that has at least one linked plan_expenses row in that cohort, carrying entity_key and entity_name and reporting link_count as the number of those rows, distinct_status_count as how many different expense_type values occur among them, total_amount as the total of their expense_amount, and max_amount as their largest expense_amount. The total and the largest value read only the rows that carry an expense_amount value; a cohort whose rows all lack one reports 0 for both, never empty.

7. For each passing-cohort row, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, the reported max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

8. These passing measures are labelled with cohort 'passing', so each such row carries entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

9. For the failing cohort, only rows whose expense_type is administrative fee are kept, carrying entity_key and entity_name.

10. The failing cohort yields one row per plans entity that has at least one linked plan_expenses row in that cohort, carrying entity_key and entity_name and reporting link_count as the number of those rows, distinct_status_count as how many different expense_type values occur among them, total_amount as the total of their expense_amount, and max_amount as their largest expense_amount. The total and the largest value read only the rows that carry an expense_amount value; a cohort whose rows all lack one reports 0 for both, never empty.

11. For each failing-cohort row, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, the reported max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

12. These failing measures are labelled with cohort 'failing', so each such row carries entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

13. The placeholder row is kept, carrying entity_key and entity_name, for a plans entity with no linked plan_expenses row at all; a plans entity that has linked plan_expenses rows gets no placeholder, even when every one of those rows lacks a expense_type value.

14. The placeholder yields one row per plans entity with no linked plan_expenses row at all, carrying entity_key and entity_name and reporting link_count of 0 rows, distinct_status_count of 0 different expense_type values, a expense_amount total_amount of 0 and a largest expense_amount, max_amount, of 0.

15. For each placeholder row, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, the reported max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

16. These placeholder measures are labelled with cohort 'no_activity', so each such row carries entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

17. The disjoint passing and failing cohort summaries are combined into one body of rows, every row of both kept, each carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

18. The no-activity summaries are added to that body, every one of them kept, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

19. The output order is deterministic: rows appear in ascending entity_key order and, within one entity, in ascending cohort order.

Mart users_reporting_bands — a per-users banding of linked reporting activity in the 529_college_savings_program_rankings_and_information scenario, over the value domain the source schema itself declares.

Grain: one row per users (user_id), including users rows with no linked reporting rows.

Key column: parent_key identifies a row of this mart.

Output columns of users_reporting_bands:
- parent_key (integer): identifier of the users row; one row per value.
- parent_name (text): user_name of the users row, copied unchanged.
- parent_status (text): role of the users row, copied unchanged. Declared domain: ['parent', 'financial advisor', 'admin'].
- link_count (bigint): number of reporting rows for this users row; 0 when there are none. Every linked reporting row counts, whatever its report_type value. An users row kept with no reporting row reports 0 here, never 1: its placeholder holds no reporting row to count.
- passing_count (bigint): of those, how many have report_type in ['plan performance']. 0, never missing, when none do.
- failing_count (bigint): how many have report_type in ['user engagement']. 0 when none do.
- distinct_status_count (bigint): how many different report_type values occur among them.
- passing_ratio (float): passing_count divided by link_count as a fraction between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links.
- adoption_band (text): band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or above (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the higher band.
- status_group (text): parent_status mapped value by value: 'parent' becomes 'active'; 'financial advisor' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never NULL.

Rules for users_reporting_bands, each stated as the outcome it produces:

1. Source table users is read in full as an input to this mart.

2. Source table reporting is read in full as an input to this mart.

3. Each users row contributes exactly one row, keyed by user_id, carrying parent_key, parent_name and parent_status.

4. The reporting rows are brought in (hop 1 of 1), each attributed to the users row whose parent_key equals that reporting row's user_id, with user_id carried; preservation is left-sided, so users rows with no matching reporting row are retained and report the declared defaults.

5. There is one output row per parent_key, carrying parent_name and parent_status beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a set of rows, reporting link_count, passing_count, failing_count and distinct_status_count for that row's matching rows. A set with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

6. The mart columns are named parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count.

7. Guarded ratios: alongside parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count, passing_ratio is passing_count divided by link_count as a fraction between 0 and 1 (not a percentage), rounded to 4 decimals, and is 0.0 when there are no links, that is when the denominator is 0 or has no value.

8. adoption_band, reported beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count and passing_ratio, is the band of passing_ratio decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or above (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), and otherwise 'low' below 0.5; boundaries are inclusive of the higher band, so a value exactly at 0.8 is 'high' and a value exactly at 0.5 is 'medium'.

9. status_group, reported beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio and adoption_band, is parent_status mapped value by value over the declared domain parent, financial advisor, admin: 'parent' becomes 'active'; 'financial advisor' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — otherwise becomes 'unmapped'. It is never NULL.

10. The output order is deterministic: rows are sorted in ascending parent_key order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `plans_plan_expenses_cohorts`

- Grain: One row per (plan_id, status cohort) pair represented among linked plan_expenses rows, plus one no-activity row for a plans row with no linked plan_expenses row at all. A plans row whose linked plan_expenses rows all lack a expense_type value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'plans_plan_expenses_cohorts' has 19 declared semantic rules:
1. [source] Read source table plans. (public source tables: plans)
2. [source] Read source table plan_expenses. (public source tables: plan_expenses)
3. [derive] Carry each plan_id and its plan_name into the cohort calculation. (public source tables: plans | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked plan_expenses rows into each plans entity before assigning status cohorts. (public source tables: plan_expenses | public carried/output columns: entity_key, entity_name, plan_id | join preservation: left | condition public identifiers: plan_expenses, plan_id, entity_key)
5. [filter] Keep rows whose expense_type belongs to the passing cohort values ['management fee']. (public carried/output columns: entity_key, entity_name | condition literal specification values: management fee)
6. [distinct] One row per plans entity that has at least one linked plan_expenses row in the passing cohort, reporting the number of those rows, how many different expense_type values occur among them, the total of their expense_amount, and their largest expense_amount. The total and the largest value read only the rows that carry an expense_amount value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose expense_type belongs to the failing cohort values ['administrative fee']. (public carried/output columns: entity_key, entity_name | condition literal specification values: administrative fee)
10. [distinct] One row per plans entity that has at least one linked plan_expenses row in the failing cohort, reporting the number of those rows, how many different expense_type values occur among them, the total of their expense_amount, and their largest expense_amount. The total and the largest value read only the rows that carry an expense_amount value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a plans entity with no linked plan_expenses row at all; a plans entity that has linked plan_expenses rows gets no placeholder, even when every one of those rows lacks a expense_type value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per plans entity with no linked plan_expenses row at all, reporting 0 rows, 0 different expense_type values, a expense_amount total of 0 and a largest expense_amount of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

### `users_reporting_bands`

- Grain: One row per users (user_id), INCLUDING users rows with no linked reporting rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group

```text
Mart 'users_reporting_bands' has 10 declared semantic rules:
1. [source] Read source table users. (public source tables: users)
2. [source] Read source table reporting. (public source tables: reporting)
3. [derive] One row per users row, keyed by user_id. (public source tables: users | public carried/output columns: parent_key, parent_name, parent_status)
4. [join] Bring in reporting (hop 1 of 1): rows with no matching reporting row are RETAINED and report the declared defaults. (public source tables: reporting | public carried/output columns: user_id | join preservation: left | condition public identifiers: reporting, user_id, parent_key)
5. [filtered_aggregate] One output row per parent_key, carrying parent_name, parent_status beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, passing_count, failing_count, distinct_status_count for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
6. [derive] Name the mart columns. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
7. [ratio] Guarded ratios: passing_ratio — passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
8. [conditional] adoption_band — Band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the HIGHER band. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band | semantic parameters: boundary=a value exactly at 0.8 is 'high'; a value exactly at 0.5 is 'medium'; domain=parent, financial advisor, admin)
9. [conditional] status_group — parent_status mapped value by value: 'parent' becomes 'active'; 'financial advisor' becomes 'other_1'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never NULL. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group | semantic parameters: boundary=categorical mapping; no numeric boundary; domain=parent, financial advisor, admin)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### plans  (source backend: files)
Source table plans of the 529_college_savings_program_rankings_and_information scenario.

- `plan_id`: integer NOT NULL — Unique identifier for each plan
- `state_id`: integer NULL — Reference to the state where the plan is offered
- `plan_name`: text NULL — Name of the 529 College Savings Plan
- `performance_score`: float NULL — Performance score of the plan
- `details_link`: text NULL — Link to the plan details or enrollment page
- `management_fees`: float NULL — Annual management fees for the plan
- `minimum_contribution`: float NULL — Minimum contribution required for the plan
- `maximum_contribution`: float NULL — Maximum contribution allowed for the plan
- `tax_benefits`: text NULL — Tax benefits offered by the plan
- `investment_options`: text NULL — Investment options available in the plan
- primary key: plan_id

### states  (source backend: files)
Source table states of the 529_college_savings_program_rankings_and_information scenario.

- `state_id`: integer NOT NULL — Unique identifier for each state
- `state_name`: text NULL — Name of the state
- `abbreviation`: text NULL — Two-letter abbreviation of the state
- `state_tax_benefit`: text NULL — Description of state tax benefits for 529 plans
- `state_website`: text NULL — Link to the state's official website
- primary key: state_id

### users  (source backend: postgres)
Source table users of the 529_college_savings_program_rankings_and_information scenario.

- `user_id`: integer NOT NULL — Unique identifier for each user
- `user_name`: text NULL — Full name of the user
- `email`: text NULL — Email address of the user
- `role`: text NULL — Role of the user (e.g., parent, financial advisor, admin) one of: parent, financial advisor, admin.
- `password_hash`: text NULL — Hashed password for user authentication
- `phone_number`: text NULL — Phone number of the user
- `address`: text NULL — Street address of the user
- `city`: text NULL — City where the user resides
- `state_id`: integer NULL — State where the user resides
- `zip_code`: text NULL — Zip code of the user's address
- primary key: user_id

### user_favorites  (source backend: s3)
Source table user_favorites of the 529_college_savings_program_rankings_and_information scenario.

- `favorite_id`: integer NOT NULL — Unique identifier for each favorite entry
- `user_id`: integer NULL — ID of the user who saved the plan
- `plan_id`: integer NULL — ID of the plan saved as a favorite
- `added_date`: text NULL — Date when the plan was added to favorites
- `notes`: text NULL — User notes or comments about the plan
- primary key: favorite_id

### plan_performance  (source backend: files)
Source table plan_performance of the 529_college_savings_program_rankings_and_information scenario.

- `performance_id`: integer NOT NULL — Unique identifier for each performance entry
- `plan_id`: integer NULL — ID of the plan
- `year`: integer NULL — Year for which the performance score is recorded
- `performance_score`: float NULL — Performance score of the plan for the specified year
- `rank`: integer NULL — Ranking of the plan within its peer group
- primary key: performance_id

### plan_investment_options  (source backend: s3)
Source table plan_investment_options of the 529_college_savings_program_rankings_and_information scenario.

- `option_id`: integer NOT NULL — Unique identifier for each investment option
- `plan_id`: integer NULL — ID of the plan
- `option_name`: text NULL — Name of the investment option
- `option_type`: text NULL — Type of investment option (e.g., stock, bond, ETF) one of: stock, bond, ETF.
- `option_description`: text NULL — Description of the investment option
- primary key: option_id

### plan_expenses  (source backend: postgres)
Source table plan_expenses of the 529_college_savings_program_rankings_and_information scenario.

- `expense_id`: integer NOT NULL — Unique identifier for each expense
- `plan_id`: integer NULL — ID of the plan
- `expense_type`: text NULL — Type of expense (e.g., management fee, administrative fee) one of: management fee, administrative fee.
- `expense_amount`: float NULL — Amount of the expense
- primary key: expense_id

### user_recommendations  (source backend: mongodb)
Source table user_recommendations of the 529_college_savings_program_rankings_and_information scenario.

- `recommendation_id`: integer NOT NULL — Unique identifier for each recommendation
- `user_id`: integer NULL — ID of the user
- `plan_id`: integer NULL — ID of the recommended plan
- `recommendation_date`: text NULL — Date when the recommendation was made
- primary key: recommendation_id

### reporting  (source backend: rest)
Source table reporting of the 529_college_savings_program_rankings_and_information scenario.

- `report_id`: integer NOT NULL — Unique identifier for each report
- `user_id`: integer NULL — ID of the user who generated the report
- `report_type`: text NULL — Type of report (e.g., plan performance, user engagement) one of: plan performance, user engagement.
- `report_date`: text NULL — Date when the report was generated
- `report_data`: text NULL — Data contained in the report
- primary key: report_id

### Relationships

- plan_expenses(plan_id) -> plans(plan_id) [optional (may be NULL/dangling)]
- plan_investment_options(plan_id) -> plans(plan_id) [optional (may be NULL/dangling)]
- plan_performance(plan_id) -> plans(plan_id) [optional (may be NULL/dangling)]
- plans(state_id) -> states(state_id) [optional (may be NULL/dangling)]
- reporting(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- user_favorites(plan_id) -> plans(plan_id) [optional (may be NULL/dangling)]
- user_favorites(user_id) -> users(user_id) [optional (may be NULL/dangling)]
- user_recommendations(plan_id) -> plans(plan_id) [optional (may be NULL/dangling)]
- user_recommendations(user_id) -> users(user_id) [optional (may be NULL/dangling)]

