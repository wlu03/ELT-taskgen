# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# Cache Management Systems

## Specification

PROJECT OVERVIEW: Cache Management Systems

This project builds two analytical marts over the __cache_management_systems__ scenario. Five source tables must be extracted, each from its own backend, and the extraction backend of each table is fixed.

The source table caches must be extracted from the rest backend. It holds one row per cache type, identified by cache_id, with cache_name, cache_type (one of in-memory, distributed, file-based), description, the access-mode indicators read_only, nonstrict_read_write, read_write and transactional, max_size, eviction_policy (one of LRU, FIFO), time_to_live_seconds, distributed and version.

The source table cache_configurations must be extracted from the files backend. It holds one row per configuration, identified by config_id, with cache_id, parameter, value, data_type, description and required.

The source table applications must be extracted from the postgres backend. It holds one row per application, identified by app_id, with app_name, description, created_at, updated_at and active.

The source table cache_usage must be extracted from the postgres backend. It holds one row per usage event, identified by usage_id, with app_id, cache_id, usage_date, hits, misses, evictions, invalidations and hit_ratio.

The source table cache_monitoring must be extracted from the s3 backend. It holds one row per monitoring event, identified by monitoring_id, with cache_id, metric, value, data_type, unit, source and timestamp.

Relationships between the source tables, exactly as declared:

- The child table cache_configurations refers through its cache_id column to the parent table caches through its cache_id column. This relationship is optional: the child cache_id may be null or may point at no parent row.
- The child table cache_monitoring refers through its cache_id column to the parent table caches through its cache_id column. This relationship is optional: the child cache_id may be null or may point at no parent row.
- The child table cache_usage refers through its app_id column to the parent table applications through its app_id column. This relationship is optional: the child app_id may be null or may point at no parent row.
- The child table cache_usage refers through its cache_id column to the parent table caches through its cache_id column. This relationship is optional: the child cache_id may be null or may point at no parent row.

Throughout this document, a cache_usage row is said to be linked to, or attributed to, a caches row when its cache_id equals that caches row's cache_id. A cache_usage row whose cache_id has no value, or whose cache_id matches no caches row, is linked to no caches row.

=== Mart caches_cache_usage_distribution — Per-(caches, measure state) distribution of linked cache_usage rows in the __cache_management_systems__ scenario ===
This mart, caches_cache_usage_distribution, gives the per-(caches, measure state) distribution of linked cache_usage rows in the __cache_management_systems__ scenario.

Grain: one row per (cache_id, measure state) pair represented among linked cache_usage rows; the absent state includes missing hits values and a no-activity row for a caches row with no links. A linked cache_usage row whose hits has a value belongs only to the present state and never to the absent state.

The key columns of this mart are entity_key and measure_state; together they identify one output row.

Rule 1 — the source table caches is read in full; every caches row is available to this mart.

Rule 2 — the source table cache_usage is read in full; every cache_usage row is available to this mart.

Rule 3 — from caches, each cache_id is carried into the measure-state calculation as entity_key and its cache_name is carried alongside it as entity_name.

Rule 4 — the linked cache_usage rows are brought into each caches entity, matching the cache_usage column cache_id to entity_key, carrying entity_key, entity_name and cache_id; preservation is left-sided on the caches side, so an entity with no linked row is retained and its absent state stays visible.

Rule 5 — the present measure-state rows, carrying entity_key and entity_name, are kept: a real cache_usage row whose hits has a value.

Rule 6 — the present measure state gives one row per caches entity that has at least one row in the present measure state, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different non-missing hits values occur (each different value counted once, however many rows repeat it), total_amount as the total hits, and max_amount as the largest hits.

Rule 7 — for those present-state rows, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 8 — these measures, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share, are labelled in measure_state as the present measure state, the text value 'present'.

Rule 9 — the absent measure-state rows, carrying entity_key and entity_name, are kept: hits is missing, including the retained placeholder for a caches row with no cache_usage rows. A real cache_usage row whose hits has a value belongs only to the present state and never to this absent state.

Rule 10 — the absent measure state gives one row per caches entity that has at least one row in the absent measure state, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different non-missing hits values occur (each different value counted once, however many rows repeat it), total_amount as the total hits, and max_amount as the largest hits.

Rule 11 — for those absent-state rows, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rule 12 — these measures, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share, are labelled in measure_state as the absent measure state, the text value 'absent'.

Rule 13 — the present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. All rows of both summaries are kept.

Rule 14 — deterministic output order, carrying entity_key and measure_state: rows appear in ascending entity_key order, and within one entity in ascending measure_state order.

Output columns of caches_cache_usage_distribution:

- entity_key (integer): the identifier of the caches row.
- measure_state (text): 'present' for a linked cache_usage row whose hits has a value; 'absent' when hits is missing, including a caches row with no linked cache_usage row. A linked cache_usage row whose hits has a value belongs only to the present state and never to the absent state.
- entity_name (text): the cache_name of the caches row, copied unchanged.
- row_count (bigint): the number of linked cache_usage rows in this entity/state cell; a absent cell holding real cache_usage rows whose hits is missing COUNTS those rows, and only the placeholder cell of a caches row with no linked cache_usage row at all reports 0.
- distinct_amount_count (bigint): the number of unique non-missing hits values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no hits value at all — both for a caches row with no linked cache_usage row and for an absent cell whose rows all have a missing hits.
- total_amount (integer): the total of hits in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an hits value.
- max_amount (integer): the largest hits in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an hits value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

=== Mart caches_cache_usage_top — Per-caches extremes of linked cache_usage rows in the __cache_management_systems__ scenario: WHICH row is largest, not how large it is ===
This mart, caches_cache_usage_top, gives the per-caches extremes of linked cache_usage rows in the __cache_management_systems__ scenario: WHICH row is largest, not how large it is.

Grain: one row per caches (cache_id), INCLUDING caches rows with no linked cache_usage rows.

The key column of this mart is parent_key; it identifies one output row.

Rule 1 — the source table caches is read in full; every caches row is available to this mart.

Rule 2 — the source table cache_usage is read in full; every cache_usage row is available to this mart.

Rule 3 — from caches there is one row per caches row, keyed by cache_id as parent_key, carrying parent_name.

Rule 4 — cache_usage is brought in, matching the cache_usage column cache_id to parent_key and carrying cache_id; preservation is left-sided on the caches side, so a caches row with no cache_usage rows still appears, with the declared defaults.

Rule 5 — within each group of parent_key the brought-together rows are ranked under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order.

Rule 6 — one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7 — the single row per parent_key at which the ordering measure is largest is kept, ties broken by the smallest usage_date under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no usage_date value sorts after every row that has one), then the smallest usage_id, and top_label, top_row_id are taken from that winning row beside parent_key. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 8 — the extremal row's attributes are attached to the grouped measures, matching on parent_key; preservation is left-sided on the measures side, so a group with no rows at all keeps its measures.

Rule 9 — the mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value.

Rule 10 — guarded ratios, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0 or has no value.

Rule 11 — tie_state, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, is 'empty' when no row holds a maximum at all — the parent has no cache_usage rows, or none of its rows carries a hits value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do; equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more, and it is always one of those three text values, never null or blank. A row holds the maximum only when it carries a hits value equal to the largest hits value among the parent's rows; a row with no hits value never holds the maximum, so a parent whose cache_usage rows all lack a hits value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

Rule 12 — deterministic output order, carrying parent_key: rows appear sorted in ascending parent_key order.

Output columns of caches_cache_usage_top:

- parent_key (integer): the identifier of the caches row. One row per value.
- parent_name (text): the cache_name of the caches row, copied unchanged.
- top_measure (integer): the largest hits itself; 0 when the parent has no cache_usage rows, and 0 when none of its rows carries a hits value.
- tied_count (bigint): how many cache_usage rows are tied at that largest hits. It is 1 when exactly one row carries that largest hits; 0 when there are no rows or when none of the rows carries a hits value; a row with no hits value never ties: only a row whose hits value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.
- child_count (bigint): the number of cache_usage rows for this caches row; 0 when there are none. Every linked cache_usage row counts, whether or not it carries a hits value. A caches row kept with no cache_usage row reports 0 here, never 1: its placeholder holds no cache_usage row to count.
- total_measure (integer): the total of hits over all of them; 0 when the parent has no cache_usage rows, and 0 when none of its rows carries a hits value (rows with no hits value add nothing).
- top_label (text): the usage_date of the cache_usage row with the LARGEST hits for this caches row. Ties in hits are broken by taking the SMALLEST usage_date under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no usage_date value sorts after every labelled row; rows tied on both are resolved by the smallest usage_id. A row with no hits value still ranks, after every row that has one, so a parent holding at least one cache_usage row always has a winning row — when NONE of its rows carries a hits value the winner is the one the tie-break alone selects, not the no-rows default. The literal '(none)' when the parent has no cache_usage rows at all, and '(none)' when the winning row has no usage_date value.
- top_row_id (integer): the usage_id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real cache_usage row whenever the parent has any. This includes when none of them carries a hits value. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no cache_usage rows, or none of its rows carries a hits value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a hits value equal to the largest hits value among the parent's rows; a row with no hits value never holds the maximum. So a parent whose cache_usage rows all lack a hits value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `caches_cache_usage_distribution`

- Grain: One row per (cache_id, measure state) pair represented among linked cache_usage rows; the absent state includes missing hits values and a no-activity row for a caches row with no links. A linked cache_usage row whose hits has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'caches_cache_usage_distribution' has 14 declared semantic rules:
1. [source] Read source table caches. (public source tables: caches)
2. [source] Read source table cache_usage. (public source tables: cache_usage)
3. [derive] Carry each cache_id and its cache_name into the measure-state calculation. (public source tables: caches | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked cache_usage rows into each caches entity; retain an entity with no linked row so its absent state is visible. (public source tables: cache_usage | public carried/output columns: entity_key, entity_name, cache_id | join preservation: left | condition public identifiers: cache_usage, cache_id, entity_key)
5. [filter] Keep the present measure-state rows: a real cache_usage row whose hits has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per caches entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing hits values occur (each different value counted once, however many rows repeat it), total hits, and largest hits. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: hits is missing, including the retained placeholder for a caches row with no cache_usage rows. A real cache_usage row whose hits has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per caches entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing hits values occur (each different value counted once, however many rows repeat it), total hits, and largest hits. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `caches_cache_usage_top`

- Grain: One row per caches (cache_id), INCLUDING caches rows with no linked cache_usage rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'caches_cache_usage_top' has 12 declared semantic rules:
1. [source] Read source table caches. (public source tables: caches)
2. [source] Read source table cache_usage. (public source tables: cache_usage)
3. [derive] One row per caches row, keyed by cache_id. (public source tables: caches | public carried/output columns: parent_key, parent_name)
4. [join] Bring in cache_usage: a caches row with no cache_usage rows still appears, with the declared defaults. (public source tables: cache_usage | public carried/output columns: cache_id | join preservation: left | condition public identifiers: cache_usage, cache_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest usage_date under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no usage_date value sorts after every row that has one), then the smallest usage_id, and take top_label, top_row_id from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no cache_usage rows, or none of its rows carries a hits value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a hits value equal to the largest hits value among the parent's rows; a row with no hits value never holds the maximum. So a parent whose cache_usage rows all lack a hits value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### caches  (source backend: rest)
Source table caches of the __cache_management_systems__ scenario.

- `cache_id`: integer NOT NULL — Unique identifier for each cache type
- `cache_name`: text NULL — Name of the cache
- `cache_type`: text NULL — Type of the cache (e.g., in-memory, distributed, file-based) one of: in-memory, distributed, file-based.
- `description`: text NULL — Description of the cache
- `read_only`: integer NULL — Indicates if the cache supports read-only access
- `nonstrict_read_write`: integer NULL — Indicates if the cache supports nonstrict-read-write access
- `read_write`: integer NULL — Indicates if the cache supports read-write access
- `transactional`: integer NULL — Indicates if the cache supports transactional access
- `max_size`: integer NULL — Maximum size of the cache in bytes
- `eviction_policy`: text NULL — Policy for evicting items from the cache (e.g., LRU, FIFO) one of: LRU, FIFO.
- `time_to_live_seconds`: integer NULL — Time-to-live for cache entries in seconds
- `distributed`: integer NULL — Indicates if the cache is distributed
- `version`: text NULL — Version of the cache implementation
- primary key: cache_id

### cache_configurations  (source backend: files)
Source table cache_configurations of the __cache_management_systems__ scenario.

- `config_id`: integer NOT NULL — Unique identifier for each configuration
- `cache_id`: integer NULL — Reference to the cache type
- `parameter`: text NULL — Name of the configuration parameter
- `value`: text NULL — Value of the configuration parameter
- `data_type`: text NULL — Data type of the configuration parameter
- `description`: text NULL — Description of the configuration parameter
- `required`: integer NULL — Indicates if the configuration parameter is required
- primary key: config_id

### applications  (source backend: postgres)
Source table applications of the __cache_management_systems__ scenario.

- `app_id`: integer NOT NULL — Unique identifier for each application
- `app_name`: text NULL — Name of the application
- `description`: text NULL — Description of the application
- `created_at`: text NULL — Timestamp when the application was created
- `updated_at`: text NULL — Timestamp when the application was last updated
- `active`: integer NULL — Indicates if the application is active
- primary key: app_id

### cache_usage  (source backend: postgres)
Source table cache_usage of the __cache_management_systems__ scenario.

- `usage_id`: integer NOT NULL — Unique identifier for each usage event
- `app_id`: integer NULL — Reference to the application using the cache
- `cache_id`: integer NULL — Reference to the cache type
- `usage_date`: text NULL — Date when the cache was used
- `hits`: integer NULL — Number of cache hits
- `misses`: integer NULL — Number of cache misses
- `evictions`: integer NULL — Number of cache evictions
- `invalidations`: integer NULL — Number of cache invalidations
- `hit_ratio`: float NULL — Hit ratio calculated as hits / (hits + misses)
- primary key: usage_id

### cache_monitoring  (source backend: s3)
Source table cache_monitoring of the __cache_management_systems__ scenario.

- `monitoring_id`: integer NOT NULL — Unique identifier for each monitoring event
- `cache_id`: integer NULL — Reference to the cache type
- `metric`: text NULL — Name of the metric being monitored
- `value`: text NULL — Value of the metric
- `data_type`: text NULL — Data type of the metric
- `unit`: text NULL — Unit of the metric
- `source`: text NULL — Source of the metric
- `timestamp`: text NULL — Timestamp when the metric was recorded
- primary key: monitoring_id

### Relationships

- cache_configurations(cache_id) -> caches(cache_id) [optional (may be NULL/dangling)]
- cache_monitoring(cache_id) -> caches(cache_id) [optional (may be NULL/dangling)]
- cache_usage(app_id) -> applications(app_id) [optional (may be NULL/dangling)]
- cache_usage(cache_id) -> caches(cache_id) [optional (may be NULL/dangling)]

