# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Covid 19 Pandemic Data Tracking And Analysis

## Specification

PROJECT OVERVIEW

This project, "Covid 19 Pandemic Data Tracking And Analysis", builds two analytical marts over the __covid_19_pandemic_data_tracking_and_analysis__ scenario data. Five source tables are in scope, and each must be extracted from the backend named here.

Source table covid_metrics must be extracted from the mongodb backend. It holds one row per metric entry, identified by metric_id (its primary key), with country_id, date, new_deaths_smoothed_per_million, reproduction_rate, new_tests_smoothed_per_thousand, positive_rate, tests_per_case, people_fully_vaccinated_per_hundred, new_vaccinations_smoothed_per_million, stringency_index, people_fully_vaccinated_per_hundred_2, total_deaths, total_cases, total_tests, total_vaccinations, vaccination_completion_date and source (one of WHO, CDC, local health authorities).

Source table countries must be extracted from the mongodb backend. It holds one row per country, identified by country_id (its primary key), with country_name, population, continent, iso_code, capital, currency, official_language, area_km2, population_density and government_type (one of democracy, monarchy).

Source table vaccination_data must be extracted from the postgres backend. It holds one row per vaccination data entry, identified by vaccination_id (its primary key), with country_id, date, people_fully_vaccinated_per_hundred, new_vaccinations_smoothed_per_million, total_vaccinations, vaccines_used, vaccination_completion_date and source (one of WHO, CDC, local health authorities).

Source table testing_data must be extracted from the files backend. It holds one row per testing data entry, identified by testing_id (its primary key), with country_id, date, new_tests_smoothed_per_thousand, positive_rate, total_tests, testing_type (one of PCR, antigen) and source (one of WHO, CDC, local health authorities).

Source table stringency_measures must be extracted from the postgres backend. It holds one row per stringency measure entry, identified by measure_id (its primary key), with country_id, date, stringency_index, lockdown_status (one of partial, full), travel_restrictions (one of domestic, international), school_closures (one of open, closed), public_events (one of allowed, restricted), workplace_closures (one of essential only, closed), public_transport (one of reduced, normal), stay_home_requirements (one of recommended, mandated) and information_campaigns (one of active, inactive).

RELATIONSHIPS

Child table covid_metrics with key country_id refers to parent table countries with key country_id; this relationship is optional, so covid_metrics.country_id may be NULL or may point at no countries row.

Child table stringency_measures with key country_id refers to parent table countries with key country_id; this relationship is optional, so stringency_measures.country_id may be NULL or may point at no countries row.

Child table testing_data with key country_id refers to parent table countries with key country_id; this relationship is optional, so testing_data.country_id may be NULL or may point at no countries row.

Child table vaccination_data with key country_id refers to parent table countries with key country_id; this relationship is optional, so vaccination_data.country_id may be NULL or may point at no countries row.

A vaccination_data row is "linked" to a countries row when its country_id equals that countries row's country_id. Rounding to 4 decimal places means normal half-up decimal rounding of the fraction value.

=== Mart countries_vaccination_data_cohorts: Per-(countries, status cohort) summary of linked vaccination_data rows in the __covid_19_pandemic_data_tracking_and_analysis__ scenario, with passing and failing cohorts kept separate ===

This mart is a per-(countries, status cohort) summary of linked vaccination_data rows in the __covid_19_pandemic_data_tracking_and_analysis__ scenario, with passing and failing cohorts kept separate.

Grain: one row per (country_id, status cohort) pair represented among linked vaccination_data rows, plus one no-activity row for a countries row with no linked vaccination_data row at all. A countries row whose linked vaccination_data rows all lack a source value is in no cohort and gets no no-activity row, so it has no row in this mart.

The key columns are entity_key and cohort; together they identify one output row.

Output columns:

- entity_key (integer): identifier of the countries row.
- cohort (text): 'passing' for source values ['WHO']; 'failing' for values ['CDC', 'local health authorities']; 'no_activity' when the countries row has no linked vaccination_data row. A linked vaccination_data row whose source has no value belongs to no cohort: it is not counted in any cell, and it does not make the countries row 'no_activity'.
- entity_name (text): country_name of the countries row, copied unchanged.
- link_count (bigint): number of vaccination_data rows in this entity/cohort cell.
- distinct_status_count (bigint): the number of distinct source values represented in this cell.
- total_amount (float): total of people_fully_vaccinated_per_hundred in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an people_fully_vaccinated_per_hundred value.
- max_amount (float): largest people_fully_vaccinated_per_hundred in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an people_fully_vaccinated_per_hundred value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules:

1. The source table countries is read in full as an input of this mart.

2. The source table vaccination_data is read in full as an input of this mart.

3. From countries, each country_id is carried into the cohort calculation as entity_key and its country_name is carried as entity_name.

4. The linked vaccination_data rows are brought onto each countries entity before assigning status cohorts, matching a vaccination_data row to the entity whose entity_key equals that row's country_id; carried here are entity_key, entity_name and country_id, and preservation is left-sided, so a countries entity with no matching vaccination_data row is kept with its vaccination_data side empty.

5. For the passing cohort only the rows whose source belongs to the values ['WHO'] are kept, carrying entity_key and entity_name.

6. There is one row per countries entity that has at least one linked vaccination_data row in the passing cohort, reporting entity_key, entity_name, link_count as the number of those rows, distinct_status_count as how many different source values occur among them, total_amount as the total of their people_fully_vaccinated_per_hundred, and max_amount as their largest people_fully_vaccinated_per_hundred. The total and the largest value read only the rows that carry a people_fully_vaccinated_per_hundred value; a cohort whose rows all lack one reports 0 for both, never empty.

7. For those passing-cohort measures, alongside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, the value max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; the result is 0.0 when total_amount is 0.

8. These measures — entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share — are labelled with cohort 'passing', the passing cohort.

9. For the failing cohort only the rows whose source belongs to the values ['CDC', 'local health authorities'] are kept, carrying entity_key and entity_name.

10. There is one row per countries entity that has at least one linked vaccination_data row in the failing cohort, reporting entity_key, entity_name, link_count as the number of those rows, distinct_status_count as how many different source values occur among them, total_amount as the total of their people_fully_vaccinated_per_hundred, and max_amount as their largest people_fully_vaccinated_per_hundred. The total and the largest value read only the rows that carry a people_fully_vaccinated_per_hundred value; a cohort whose rows all lack one reports 0 for both, never empty.

11. For those failing-cohort measures, alongside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, the value max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; the result is 0.0 when total_amount is 0.

12. These measures — entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share — are labelled with cohort 'failing', the failing cohort.

13. The placeholder row, carrying entity_key and entity_name, is kept for a countries entity with no linked vaccination_data row at all; a countries entity that has linked vaccination_data rows gets no placeholder, even when every one of those rows lacks a source value.

14. There is one row per countries entity with no linked vaccination_data row at all, carrying entity_key and entity_name and reporting link_count of 0 rows, distinct_status_count of 0 different source values, a people_fully_vaccinated_per_hundred total_amount of 0 and a largest people_fully_vaccinated_per_hundred, max_amount, of 0.

15. For those no-activity measures, alongside entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, the value max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; the result is 0.0 when total_amount is 0.

16. These measures — entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share — are labelled with cohort 'no_activity', the no_activity cohort.

17. The disjoint passing and failing cohort summaries are combined into one list, all rows of both kept, each carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

18. The no-activity summaries are added to that list as well, all of them kept, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.

19. Deterministic output order: rows appear sorted ascending by entity, that is entity_key, then by cohort.

=== Mart countries_vaccination_data_distribution: Per-(countries, measure state) distribution of linked vaccination_data rows in the __covid_19_pandemic_data_tracking_and_analysis__ scenario ===

This mart is a per-(countries, measure state) distribution of linked vaccination_data rows in the __covid_19_pandemic_data_tracking_and_analysis__ scenario.

Grain: one row per (country_id, measure state) pair represented among linked vaccination_data rows; the absent state includes missing people_fully_vaccinated_per_hundred values and a no-activity row for a countries row with no links. A linked vaccination_data row whose people_fully_vaccinated_per_hundred has a value belongs only to the present state and never to the absent state.

The key columns are entity_key and measure_state; together they identify one output row.

Output columns:

- entity_key (integer): identifier of the countries row.
- measure_state (text): 'present' for a linked vaccination_data row whose people_fully_vaccinated_per_hundred has a value; 'absent' when people_fully_vaccinated_per_hundred is missing, including a countries row with no linked vaccination_data row. A linked vaccination_data row whose people_fully_vaccinated_per_hundred has a value belongs only to the present state and never to the absent state.
- entity_name (text): country_name of the countries row, copied unchanged.
- row_count (bigint): number of linked vaccination_data rows in this entity/state cell; a absent cell holding real vaccination_data rows whose people_fully_vaccinated_per_hundred is missing COUNTS those rows, and only the placeholder cell of a countries row with no linked vaccination_data row at all reports 0.
- distinct_amount_count (bigint): number of unique non-missing people_fully_vaccinated_per_hundred values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no people_fully_vaccinated_per_hundred value at all — both for a countries row with no linked vaccination_data row and for an absent cell whose rows all have a missing people_fully_vaccinated_per_hundred.
- total_amount (float): total of people_fully_vaccinated_per_hundred in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an people_fully_vaccinated_per_hundred value.
- max_amount (float): largest people_fully_vaccinated_per_hundred in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an people_fully_vaccinated_per_hundred value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules:

1. The source table countries is read in full as an input of this mart.

2. The source table vaccination_data is read in full as an input of this mart.

3. From countries, each country_id is carried into the measure-state calculation as entity_key and its country_name is carried as entity_name.

4. The linked vaccination_data rows are brought onto each countries entity, matching a vaccination_data row to the entity whose entity_key equals that row's country_id; carried here are entity_key, entity_name and country_id, and preservation is left-sided, so an entity with no matching vaccination_data row is retained and its absent state is visible.

5. The present measure-state rows kept, carrying entity_key and entity_name, are the real vaccination_data rows whose people_fully_vaccinated_per_hundred has a value.

6. There is one row per countries entity that has at least one row in the present measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count as the row count, distinct_amount_count as how many different non-missing people_fully_vaccinated_per_hundred values occur (each different value counted once, however many rows repeat it), total_amount as the total people_fully_vaccinated_per_hundred, and max_amount as the largest people_fully_vaccinated_per_hundred.

7. For those present-state measures, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, the value max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; the result is 0.0 when total_amount is 0.

8. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state 'present', the present measure state.

9. The absent measure-state rows kept, carrying entity_key and entity_name, are those whose people_fully_vaccinated_per_hundred is missing, including the retained placeholder for a countries row with no vaccination_data rows. A real vaccination_data row whose people_fully_vaccinated_per_hundred has a value belongs only to the present state and never to this absent state.

10. There is one row per countries entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count as the row count, distinct_amount_count as how many different non-missing people_fully_vaccinated_per_hundred values occur (each different value counted once, however many rows repeat it), total_amount as the total people_fully_vaccinated_per_hundred, and max_amount as the largest people_fully_vaccinated_per_hundred.

11. For those absent-state measures, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, the value max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; the result is 0.0 when total_amount is 0.

12. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state 'absent', the absent measure state.

13. The present-state summary and the absent-state summary are stacked into one output list: every present-state row and every absent-state row is its own output row carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

14. Deterministic output order: rows appear sorted ascending by entity, that is entity_key, then by measure state, measure_state.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `countries_vaccination_data_cohorts`

- Grain: One row per (country_id, status cohort) pair represented among linked vaccination_data rows, plus one no-activity row for a countries row with no linked vaccination_data row at all. A countries row whose linked vaccination_data rows all lack a source value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'countries_vaccination_data_cohorts' has 19 declared semantic rules:
1. [source] Read source table countries. (public source tables: countries)
2. [source] Read source table vaccination_data. (public source tables: vaccination_data)
3. [derive] Carry each country_id and its country_name into the cohort calculation. (public source tables: countries | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked vaccination_data rows into each countries entity before assigning status cohorts. (public source tables: vaccination_data | public carried/output columns: entity_key, entity_name, country_id | join preservation: left | condition public identifiers: vaccination_data, country_id, entity_key)
5. [filter] Keep rows whose source belongs to the passing cohort values ['WHO']. (public carried/output columns: entity_key, entity_name | condition literal specification values: WHO)
6. [distinct] One row per countries entity that has at least one linked vaccination_data row in the passing cohort, reporting the number of those rows, how many different source values occur among them, the total of their people_fully_vaccinated_per_hundred, and their largest people_fully_vaccinated_per_hundred. The total and the largest value read only the rows that carry a people_fully_vaccinated_per_hundred value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose source belongs to the failing cohort values ['CDC', 'local health authorities']. (public carried/output columns: entity_key, entity_name | condition literal specification values: CDC, local health authorities)
10. [distinct] One row per countries entity that has at least one linked vaccination_data row in the failing cohort, reporting the number of those rows, how many different source values occur among them, the total of their people_fully_vaccinated_per_hundred, and their largest people_fully_vaccinated_per_hundred. The total and the largest value read only the rows that carry a people_fully_vaccinated_per_hundred value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a countries entity with no linked vaccination_data row at all; a countries entity that has linked vaccination_data rows gets no placeholder, even when every one of those rows lacks a source value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per countries entity with no linked vaccination_data row at all, reporting 0 rows, 0 different source values, a people_fully_vaccinated_per_hundred total of 0 and a largest people_fully_vaccinated_per_hundred of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

### `countries_vaccination_data_distribution`

- Grain: One row per (country_id, measure state) pair represented among linked vaccination_data rows; the absent state includes missing people_fully_vaccinated_per_hundred values and a no-activity row for a countries row with no links. A linked vaccination_data row whose people_fully_vaccinated_per_hundred has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'countries_vaccination_data_distribution' has 14 declared semantic rules:
1. [source] Read source table countries. (public source tables: countries)
2. [source] Read source table vaccination_data. (public source tables: vaccination_data)
3. [derive] Carry each country_id and its country_name into the measure-state calculation. (public source tables: countries | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked vaccination_data rows into each countries entity; retain an entity with no linked row so its absent state is visible. (public source tables: vaccination_data | public carried/output columns: entity_key, entity_name, country_id | join preservation: left | condition public identifiers: vaccination_data, country_id, entity_key)
5. [filter] Keep the present measure-state rows: a real vaccination_data row whose people_fully_vaccinated_per_hundred has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per countries entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing people_fully_vaccinated_per_hundred values occur (each different value counted once, however many rows repeat it), total people_fully_vaccinated_per_hundred, and largest people_fully_vaccinated_per_hundred. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: people_fully_vaccinated_per_hundred is missing, including the retained placeholder for a countries row with no vaccination_data rows. A real vaccination_data row whose people_fully_vaccinated_per_hundred has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per countries entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing people_fully_vaccinated_per_hundred values occur (each different value counted once, however many rows repeat it), total people_fully_vaccinated_per_hundred, and largest people_fully_vaccinated_per_hundred. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### covid_metrics  (source backend: mongodb)
Source table covid_metrics of the __covid_19_pandemic_data_tracking_and_analysis__ scenario.

- `metric_id`: integer NOT NULL — Unique identifier for each metric entry
- `country_id`: integer NULL — Reference to the country
- `date`: text NULL — Date for which the metrics are recorded
- `new_deaths_smoothed_per_million`: float NULL — Smoothed rate of new deaths per million population
- `reproduction_rate`: float NULL — Estimated reproduction rate of the virus
- `new_tests_smoothed_per_thousand`: float NULL — Smoothed rate of new tests per thousand population
- `positive_rate`: float NULL — Percentage of positive test results
- `tests_per_case`: float NULL — Average number of tests per confirmed case
- `people_fully_vaccinated_per_hundred`: float NULL — Percentage of population fully vaccinated
- `new_vaccinations_smoothed_per_million`: float NULL — Smoothed rate of new vaccinations per million population
- `stringency_index`: float NULL — Index representing the stringency of public health measures
- `people_fully_vaccinated_per_hundred_2`: float NULL — Alternative representation of percentage of population fully vaccinated
- `total_deaths`: integer NULL — Total number of deaths up to the recorded date
- `total_cases`: integer NULL — Total number of confirmed cases up to the recorded date
- `total_tests`: integer NULL — Total number of tests conducted up to the recorded date
- `total_vaccinations`: integer NULL — Total number of vaccinations administered up to the recorded date
- `vaccination_completion_date`: text NULL — Date when a significant percentage of the population is fully vaccinated
- `source`: text NULL — Source of the data (e.g., WHO, CDC, local health authorities) one of: WHO, CDC, local health authorities.
- primary key: metric_id

### countries  (source backend: mongodb)
Source table countries of the __covid_19_pandemic_data_tracking_and_analysis__ scenario.

- `country_id`: integer NOT NULL — Unique identifier for each country
- `country_name`: text NULL — Name of the country
- `population`: integer NULL — Population of the country
- `continent`: text NULL — Continent on which the country is located
- `iso_code`: text NULL — ISO 3166-1 alpha-3 code for the country
- `capital`: text NULL — Capital city of the country
- `currency`: text NULL — Currency used in the country
- `official_language`: text NULL — Official language(s) of the country
- `area_km2`: integer NULL — Area of the country in square kilometers
- `population_density`: float NULL — Population density of the country
- `government_type`: text NULL — Type of government (e.g., democracy, monarchy) one of: democracy, monarchy.
- primary key: country_id

### vaccination_data  (source backend: postgres)
Source table vaccination_data of the __covid_19_pandemic_data_tracking_and_analysis__ scenario.

- `vaccination_id`: integer NOT NULL — Unique identifier for each vaccination data entry
- `country_id`: integer NULL — Reference to the country
- `date`: text NULL — Date for which the vaccination data is recorded
- `people_fully_vaccinated_per_hundred`: float NULL — Percentage of population fully vaccinated
- `new_vaccinations_smoothed_per_million`: float NULL — Smoothed rate of new vaccinations per million population
- `total_vaccinations`: integer NULL — Total number of vaccinations administered up to the recorded date
- `vaccines_used`: text NULL — Types of vaccines used in the country
- `vaccination_completion_date`: text NULL — Date when a significant percentage of the population is fully vaccinated
- `source`: text NULL — Source of the data (e.g., WHO, CDC, local health authorities) one of: WHO, CDC, local health authorities.
- primary key: vaccination_id

### testing_data  (source backend: files)
Source table testing_data of the __covid_19_pandemic_data_tracking_and_analysis__ scenario.

- `testing_id`: integer NOT NULL — Unique identifier for each testing data entry
- `country_id`: integer NULL — Reference to the country
- `date`: text NULL — Date for which the testing data is recorded
- `new_tests_smoothed_per_thousand`: float NULL — Smoothed rate of new tests per thousand population
- `positive_rate`: float NULL — Percentage of positive test results
- `total_tests`: integer NULL — Total number of tests conducted up to the recorded date
- `testing_type`: text NULL — Types of tests conducted (e.g., PCR, antigen) one of: PCR, antigen.
- `source`: text NULL — Source of the data (e.g., WHO, CDC, local health authorities) one of: WHO, CDC, local health authorities.
- primary key: testing_id

### stringency_measures  (source backend: postgres)
Source table stringency_measures of the __covid_19_pandemic_data_tracking_and_analysis__ scenario.

- `measure_id`: integer NOT NULL — Unique identifier for each stringency measure entry
- `country_id`: integer NULL — Reference to the country
- `date`: text NULL — Date for which the stringency index is recorded
- `stringency_index`: float NULL — Index representing the stringency of public health measures
- `lockdown_status`: text NULL — Status of lockdown (e.g., partial, full) one of: partial, full.
- `travel_restrictions`: text NULL — Status of travel restrictions (e.g., domestic, international) one of: domestic, international.
- `school_closures`: text NULL — Status of school closures (e.g., open, closed) one of: open, closed.
- `public_events`: text NULL — Status of public events (e.g., allowed, restricted) one of: allowed, restricted.
- `workplace_closures`: text NULL — Status of workplace closures (e.g., essential only, closed) one of: essential only, closed.
- `public_transport`: text NULL — Status of public transport (e.g., reduced, normal) one of: reduced, normal.
- `stay_home_requirements`: text NULL — Status of stay-at-home requirements (e.g., recommended, mandated) one of: recommended, mandated.
- `information_campaigns`: text NULL — Status of information campaigns (e.g., active, inactive) one of: active, inactive.
- primary key: measure_id

### Relationships

- covid_metrics(country_id) -> countries(country_id) [optional (may be NULL/dangling)]
- stringency_measures(country_id) -> countries(country_id) [optional (may be NULL/dangling)]
- testing_data(country_id) -> countries(country_id) [optional (may be NULL/dangling)]
- vaccination_data(country_id) -> countries(country_id) [optional (may be NULL/dangling)]

