# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Inria Muse Hostview Processing

## Specification

PROJECT OVERVIEW

This project builds three analytic marts over the 162575_init-hostview.sql HostView measurement schema. Every source table must be extracted from the backend named for it below; the backend is part of the contract and a table may not be read from anywhere else.

Source tables and their extraction backends:
- Table activities is extracted from the s3 backend.
- Table activity_io is extracted from the rest backend.
- Table browser_activity is extracted from the postgres backend.
- Table connections is extracted from the postgres backend.
- Table device_info is extracted from the rest backend.
- Table devices is extracted from the rest backend.
- Table dns_logs is extracted from the s3 backend.
- Table files is extracted from the postgres backend.
- Table http_logs is extracted from the rest backend.
- Table io is extracted from the s3 backend.
- Table locations is extracted from the files backend.
- Table netlabels is extracted from the mongodb backend.
- Table pcap is extracted from the mongodb backend.
- Table pcap_events is extracted from the mongodb backend.
- Table pcap_file is extracted from the postgres backend.
- Table pcap_flow is extracted from the s3 backend.
- Table pcap_rtt is extracted from the s3 backend.
- Table pcap_throughput is extracted from the mongodb backend.
- Table ports is extracted from the s3 backend.
- Table power_states is extracted from the postgres backend.
- Table processes is extracted from the s3 backend.
- Table processes_running is extracted from the rest backend.
- Table sessions is extracted from the rest backend.
- Table survey_activity_importance is extracted from the postgres backend.
- Table survey_activity_qoe is extracted from the files backend.
- Table survey_activity_tags is extracted from the postgres backend.
- Table survey_problem_tags is extracted from the s3 backend.
- Table surveys is extracted from the postgres backend.
- Table users is extracted from the mongodb backend.
- Table video_buffered_play_time_sample is extracted from the postgres backend; it holds a sample of the current video playtime and the current amount of video in the buffer.
- Table video_buffering_event is extracted from the postgres backend; it models periods of video playback freezing, for buffering.
- Table video_off_screen_event is extracted from the mongodb backend; it models periods during which the video playback is going off-screen (i.e., the user activates a different browser tab or minimizes the browser window).
- Table video_pause_event is extracted from the postgres backend; it models periods during which the video playback is being paused by the user.
- Table video_playback_quality_sample is extracted from the s3 backend; it holds a sample of the playback quality (in terms of video frames/sec, dropped, corrupted frames, etc).
- Table video_player_size is extracted from the s3 backend; it models player size change events during a video session.
- Table video_resolution is extracted from the rest backend; it models resolution change events during a video session.
- Table video_seek_event is extracted from the rest backend; it models events of user navigating to a specific timepoint of playback through the seekbar.
- Table video_session is extracted from the postgres backend; it models a streaming video (e.g., YouTube) playback session.
- Table wifi_stats is extracted from the rest backend.

Relationships between the source tables. Each line states the child table with its key columns, the parent table with its key columns, and whether the relationship is required or optional.
- Child activities(session_id) refers to parent sessions(id): required.
- Child activity_io(id) refers to parent activities(id): required.
- Child activity_io(session_id) refers to parent sessions(id): required.
- Child browser_activity(session_id) refers to parent sessions(id): required.
- Child connections(location_id) refers to parent locations(id): optional (may be NULL or dangling).
- Child connections(session_id) refers to parent sessions(id): required.
- Child device_info(session_id) refers to parent sessions(id): required.
- Child devices(user_id) refers to parent users(id): optional (may be NULL or dangling).
- Child dns_logs(connection_id) refers to parent connections(id): required.
- Child files(device_id) refers to parent devices(id): optional (may be NULL or dangling).
- Child http_logs(connection_id) refers to parent connections(id): required.
- Child io(session_id) refers to parent sessions(id): required.
- Child netlabels(session_id) refers to parent sessions(id): required.
- Child pcap(connection_id) refers to parent connections(id): required.
- Child pcap_events(flow_id) refers to parent pcap_flow(id): optional (may be NULL or dangling).
- Child pcap_events(pcap_id) refers to parent pcap(id): required.
- Child pcap_file(file_id) refers to parent files(id): required.
- Child pcap_file(pcap_id) refers to parent pcap(id): required.
- Child pcap_flow(pcap_id) refers to parent pcap(id): required.
- Child pcap_rtt(flow_id) refers to parent pcap_flow(id): optional (may be NULL or dangling).
- Child pcap_rtt(pcap_id) refers to parent pcap(id): required.
- Child pcap_throughput(flow_id) refers to parent pcap_flow(id): optional (may be NULL or dangling).
- Child pcap_throughput(pcap_id) refers to parent pcap(id): required.
- Child ports(session_id) refers to parent sessions(id): required.
- Child power_states(session_id) refers to parent sessions(id): required.
- Child processes(session_id) refers to parent sessions(id): required.
- Child processes_running(device_id) refers to parent devices(id): required.
- Child processes_running(session_id) refers to parent sessions(id): required.
- Child sessions(device_id) refers to parent devices(id): optional (may be NULL or dangling).
- Child sessions(file_id) refers to parent files(id): optional (may be NULL or dangling).
- Child survey_activity_importance(survey_id) refers to parent surveys(id): required.
- Child survey_activity_qoe(survey_id) refers to parent surveys(id): required.
- Child survey_activity_tags(survey_id) refers to parent surveys(id): required.
- Child survey_problem_tags(survey_id) refers to parent surveys(id): required.
- Child surveys(session_id) refers to parent sessions(id): required.
- Child video_buffered_play_time_sample(video_session_id) refers to parent video_session(id): required.
- Child video_buffering_event(video_session_id) refers to parent video_session(id): required.
- Child video_off_screen_event(video_session_id) refers to parent video_session(id): required.
- Child video_pause_event(video_session_id) refers to parent video_session(id): required.
- Child video_playback_quality_sample(video_session_id) refers to parent video_session(id): required.
- Child video_player_size(video_session_id) refers to parent video_session(id): required.
- Child video_resolution(video_session_id) refers to parent video_session(id): required.
- Child video_seek_event(buffering_event_id) refers to parent video_buffering_event(id): optional (may be NULL or dangling).
- Child video_seek_event(pause_event_id) refers to parent video_pause_event(id): optional (may be NULL or dangling).
- Child video_seek_event(video_session_id) refers to parent video_session(id): required.
- Child video_session(file_id) refers to parent files(id): optional (may be NULL or dangling).
- Child video_session(session_id) refers to parent sessions(id): required.
- Child wifi_stats(session_id) refers to parent sessions(id): required.

Conventions used throughout: text comparisons are plain case-sensitive comparisons of the stored text, the warehouse default order, in which every uppercase letter sorts before every lowercase one. All ratios are fractions between 0 and 1, rounded to 4 decimal places.


=== Mart sessions_wifi_stats_snapshot: Per-sessions latest-row snapshot over linked wifi_stats activity in the 162575_init-hostview.sql schema ===

Grain: one row per sessions (id), INCLUDING sessions rows with no linked wifi_stats rows.

Key column: parent_key.

Rule 1 — the source table sessions is read; all sessions rows are available to this mart.

Rule 2 — the source table wifi_stats is read; all wifi_stats rows are available to this mart.

Rule 3 — from source table sessions there is one row per sessions row, keyed by id, carrying parent_key and parent_name.

Rule 4 — the wifi_stats rows are brought in, a wifi_stats row matching a sessions row when the wifi_stats session_id equals that sessions row's parent_key, carrying wifi_stats session_id and id; preservation is left-sided on the sessions side, so a sessions row with no matching wifi_stats row is retained and receives the stated empty snapshot values.

Rule 5 — there is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group, and each such group reports event_count and lifetime_amount over that row's matching rows.

Rule 6 — for each parent_key the single row at which the ordering measure logged_at is largest survives, ties broken by the smallest id, and latest_row_id, latest_amount and latest_label are taken from that winning row; EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 7 — the extremal row's attributes are attached to the grouped measures by matching on parent_key, with left-sided preservation of the grouped measures, so a group with no rows at all keeps its measures.

Rule 8 — the mart columns are named parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label; lifetime_amount reports its declared default of 0 — never NULL — for a group with no matching rows, and for lifetime_amount that default also applies to a group none of whose real rows carries an input value.

Rule 9 — guarded ratio, carried beside parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label: latest_amount_share is latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places, and is 0.0 when the denominator lifetime_amount is 0 or NULL; the division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose r_speed is missing, so such a row gives 0.0.

Rule 10 — deterministic output order: rows appear sorted by parent_key, ascending.

Output columns:
- parent_key (bigint): identifier of the sessions row; one row per value.
- parent_name (text): start_event of the sessions row, copied unchanged.
- event_count (bigint): number of wifi_stats rows for this sessions row; 0 when there are none. Every linked wifi_stats row counts, whether or not it carries a r_speed value. A sessions row kept with no wifi_stats row reports 0 here, never 1: its placeholder holds no wifi_stats row to count.
- lifetime_amount (bigint): total of r_speed over all matching wifi_stats rows; 0 when there are no rows and when none of those rows carries an r_speed value; a row with no r_speed value adds nothing, so a group with some values totals the values it has.
- latest_row_id (bigint): id of the row with the latest logged_at; ties take the smallest id. It is 0 when there are no rows. Every wifi_stats row of the sessions row ranks, whether or not it carries a r_speed value: the latest logged_at wins even when that row's r_speed is missing.
- latest_amount (bigint): r_speed from that same latest row; 0 when there are no rows or when the winning value is missing.
- latest_label (text): guid from that same latest row; '(none)' when there are no rows or when the winning value is missing.
- latest_amount_share (float): latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose r_speed is missing, so such a row gives 0.0.


=== Mart pcap_flow_pcap_throughput_distribution: Per-(pcap_flow, measure state) distribution of linked pcap_throughput activity in the 162575_init-hostview.sql schema ===

Grain: one row per (id, measure state) pair represented among linked pcap_throughput rows; the absent state includes missing value values and a no-activity row for a pcap_flow row with no links. A linked pcap_throughput row whose value has a value belongs only to the present state and never to the absent state.

Key columns: entity_key and measure_state.

Rule 1 — the source table pcap_flow is read; all pcap_flow rows are available to this mart.

Rule 2 — the source table pcap_throughput is read; all pcap_throughput rows are available to this mart.

Rule 3 — from source table pcap_flow, each id is carried as entity_key and its destination_ip as entity_name into the measure-state calculation.

Rule 4 — the linked pcap_throughput rows are brought into each pcap_flow entity, a pcap_throughput row being linked when its flow_id equals the entity_key, carrying entity_key, entity_name, the pcap_throughput id and flow_id; preservation is left-sided on the pcap_flow side, so an entity with no linked row is retained and its absent state stays visible.

Rule 5 — the present measure-state rows are kept, carrying entity_key and entity_name: a real pcap_throughput row whose value has a value.

Rule 6 — in the present measure state there is one row per pcap_flow entity that has at least one row in that state, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different non-missing value values occur (each different value counted once, however many rows repeat it), total_amount as the total value, and max_amount as the largest value.

Rule 7 — for those present-state measures, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it is carried beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount.

Rule 8 — these measures are labelled with measure_state 'present', the present measure state, carried beside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 9 — the absent measure-state rows are kept, carrying entity_key and entity_name: value is missing, including the retained placeholder for a pcap_flow row with no pcap_throughput rows. A real pcap_throughput row whose value has a value belongs only to the present state and never to this absent state.

Rule 10 — in the absent measure state there is one row per pcap_flow entity that has at least one row in that state, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different non-missing value values occur (each different value counted once, however many rows repeat it), total_amount as the total value, and max_amount as the largest value.

Rule 11 — for those absent-state measures, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and 0.0 when total_amount is 0; it is carried beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount.

Rule 12 — these measures are labelled with measure_state 'absent', the absent measure state, carried beside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.

Rule 13 — the present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, all rows are kept, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14 — deterministic output order: rows appear sorted ascending by entity_key, then by measure_state.

Output columns:
- entity_key (bigint): identifier of the pcap_flow row.
- measure_state (text): 'present' for a linked pcap_throughput row whose value has a value; 'absent' when value is missing, including a pcap_flow row with no linked pcap_throughput row. A linked pcap_throughput row whose value has a value belongs only to the present state and never to the absent state.
- entity_name (text): destination_ip of the pcap_flow row, copied unchanged.
- row_count (bigint): number of linked pcap_throughput rows in this entity/state cell; an absent cell holding real pcap_throughput rows whose value is missing COUNTS those rows, and only the placeholder cell of a pcap_flow row with no linked pcap_throughput row at all reports 0.
- distinct_amount_count (bigint): number of unique non-missing value values in this cell; each unique non-missing value is counted once, however many rows repeat it; 0 whenever the cell holds no value value at all — both for a pcap_flow row with no linked pcap_throughput row and for an absent cell whose rows all have a missing value.
- total_amount (float): total of value in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an value value.
- max_amount (float): largest value in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an value value.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.


=== Mart pcap_pcap_throughput_top: Per-pcap extremes over linked pcap_throughput rows in the 162575_init-hostview.sql schema: WHICH row is largest, not how large it is ===

Grain: one row per pcap (id), INCLUDING pcap rows with no linked pcap_throughput rows.

Key column: parent_key.

Rule 1 — the source table pcap is read; all pcap rows are available to this mart.

Rule 2 — the source table pcap_throughput is read; all pcap_throughput rows are available to this mart.

Rule 3 — from source table pcap there is one row per pcap row, keyed by id, carrying parent_key and parent_name.

Rule 4 — the pcap_throughput rows are brought in, a pcap_throughput row matching when its pcap_id equals the parent_key, carrying pcap_throughput pcap_id and id; preservation is left-sided on the pcap side, so a pcap row with no pcap_throughput rows still appears, with the declared defaults.

Rule 5 — within each parent_key the matched rows are ranked under an explicit total order (the measure value first, then the declared tie-break), so the extremal row is a function of the input and not of row order.

Rule 6 — there is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group, and each group reports top_measure, tied_count, child_count and total_measure over that row's matching rows; a group with no qualifying rows still appears, reporting 0, and a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7 — for each parent_key the single row at which the ordering measure value is largest survives, ties broken by the smallest direction under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no direction value sorts after every row that has one), then the smallest id, and top_label and top_row_id are taken from that winning row; EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 8 — the extremal row's attributes are attached to the grouped measures by matching on parent_key, with left-sided preservation of the grouped measures, so a group with no rows at all keeps its measures.

Rule 9 — the mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults of 0 — never NULL — for a group with no matching rows, and for top_measure and total_measure the default also applies to a group none of whose real rows carries an input value.

Rule 10 — guarded ratio, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when the denominator total_measure is 0 or NULL.

Rule 11 — tie_state, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, is a categorical mapping with no numeric boundary: 'empty' when no row holds a maximum at all — the parent has no pcap_throughput rows, or none of its rows carries a value value — 'unique' when exactly one row holds the maximum, and otherwise 'tied' when two or more do; equivalently tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more, never null or blank. A row holds the maximum only when it carries a value value equal to the largest value value among the parent's rows; a row with no value value never holds the maximum, so a parent whose pcap_throughput rows all lack a value value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

Rule 12 — deterministic output order: rows appear sorted by parent_key, ascending.

Output columns:
- parent_key (bigint): identifier of the pcap row; one row per value.
- parent_name (text): basename of the pcap row, copied unchanged.
- top_measure (float): the largest value itself; 0 when the parent has no pcap_throughput rows, and 0 when none of its rows carries a value value.
- tied_count (bigint): how many pcap_throughput rows are tied at that largest value. 1 when exactly one row carries that largest value; 0 when there are no rows or when none of the rows carries a value value; a row with no value value never ties: only a row whose value value equals the largest value among the parent's rows holds the maximum, so the winning row of a parent whose rows all lack a value — the row the tie-break alone selects — is not counted here.
- child_count (bigint): number of pcap_throughput rows for this pcap row; 0 when there are none. Every linked pcap_throughput row counts, whether or not it carries a value value. A pcap row kept with no pcap_throughput row reports 0 here, never 1: its placeholder holds no pcap_throughput row to count.
- total_measure (float): total of value over all of them; 0 when the parent has no pcap_throughput rows, and 0 when none of its rows carries a value value (rows with no value value add nothing).
- top_label (text): the direction of the pcap_throughput row with the LARGEST value for this pcap row. Ties in value are broken by taking the SMALLEST direction under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no direction value sorts after every labelled row; rows tied on both are resolved by the smallest id. A row with no value value still ranks, after every row that has one, so a parent holding at least one pcap_throughput row always has a winning row — when NONE of its rows carries a value value the winner is the one the tie-break alone selects, not the no-rows default. The literal '(none)' when the parent has no pcap_throughput rows at all, and '(none)' when the winning row has no direction value.
- top_row_id (bigint): the id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real pcap_throughput row whenever the parent has any. This includes when none of them carries a value value. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no pcap_throughput rows, or none of its rows carries a value value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a value value equal to the largest value value among the parent's rows; a row with no value value never holds the maximum. So a parent whose pcap_throughput rows all lack a value value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `sessions_wifi_stats_snapshot`

- Grain: One row per sessions (id), INCLUDING sessions rows with no linked wifi_stats rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share

```text
Mart 'sessions_wifi_stats_snapshot' has 10 declared semantic rules:
1. [source] Read source table sessions. (public source tables: sessions)
2. [source] Read source table wifi_stats. (public source tables: wifi_stats)
3. [derive] One row per sessions row, keyed by id. (public source tables: sessions | public carried/output columns: parent_key, parent_name)
4. [join] Bring in wifi_stats; a sessions row with no matching wifi_stats row is retained and receives the stated empty snapshot values. (public source tables: wifi_stats | public carried/output columns: session_id, id | join preservation: left | condition public identifiers: wifi_stats, session_id, parent_key)
5. [aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count, lifetime_amount for that row's matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount)
6. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest id, and take latest_row_id, latest_amount, latest_label from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, latest_row_id, latest_amount, latest_label)
7. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
8. [derive] Name the mart columns; lifetime_amount reports its declared default — never NULL — for a group with no matching rows. For lifetime_amount, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label)
9. [ratio] Guarded ratios: latest_amount_share — latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. The division uses latest_amount as this mart reports it, after its default of 0 for a winning row whose r_speed is missing, so such a row gives 0.0. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `pcap_flow_pcap_throughput_distribution`

- Grain: One row per (id, measure state) pair represented among linked pcap_throughput rows; the absent state includes missing value values and a no-activity row for a pcap_flow row with no links. A linked pcap_throughput row whose value has a value belongs only to the present state and never to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'pcap_flow_pcap_throughput_distribution' has 14 declared semantic rules:
1. [source] Read source table pcap_flow. (public source tables: pcap_flow)
2. [source] Read source table pcap_throughput. (public source tables: pcap_throughput)
3. [derive] Carry each id and its destination_ip into the measure-state calculation. (public source tables: pcap_flow | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked pcap_throughput rows into each pcap_flow entity; retain an entity with no linked row so its absent state is visible. (public source tables: pcap_throughput | public carried/output columns: entity_key, entity_name, id, flow_id | join preservation: left | condition public identifiers: pcap_throughput, flow_id, entity_key)
5. [filter] Keep the present measure-state rows: a real pcap_throughput row whose value has a value. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per pcap_flow entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different non-missing value values occur (each different value counted once, however many rows repeat it), total value, and largest value. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: value is missing, including the retained placeholder for a pcap_flow row with no pcap_throughput rows. A real pcap_throughput row whose value has a value belongs only to the present state and never to this absent state. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per pcap_flow entity that has at least one row in the absent measure state, and no row here for an entity with none, reporting row count, how many different non-missing value values occur (each different value counted once, however many rows repeat it), total value, and largest value. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `pcap_pcap_throughput_top`

- Grain: One row per pcap (id), INCLUDING pcap rows with no linked pcap_throughput rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'pcap_pcap_throughput_top' has 12 declared semantic rules:
1. [source] Read source table pcap. (public source tables: pcap)
2. [source] Read source table pcap_throughput. (public source tables: pcap_throughput)
3. [derive] One row per pcap row, keyed by id. (public source tables: pcap | public carried/output columns: parent_key, parent_name)
4. [join] Bring in pcap_throughput: a pcap row with no pcap_throughput rows still appears, with the declared defaults. (public source tables: pcap_throughput | public carried/output columns: pcap_id, id | join preservation: left | condition public identifiers: pcap_throughput, pcap_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest direction under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no direction value sorts after every row that has one), then the smallest id, and take top_label, top_row_id from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. For top_measure, total_measure, the default also applies to a group none of whose real rows carries an input value. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no pcap_throughput rows, or none of its rows carries a value value — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. A row holds the maximum only when it carries a value value equal to the largest value value among the parent's rows; a row with no value value never holds the maximum. So a parent whose pcap_throughput rows all lack a value value has no row holding the maximum and is 'empty', with tied_count 0, even though the ranking still selects a winning row for it by the tie-break alone, which top_label and top_row_id name. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### activities  (source backend: s3)
Source table activities.

- `description`: text NULL — Column description of table activities.
- `finished_at`: timestamp NULL — Column finished_at of table activities.
- `fullscreen`: integer NULL — Column fullscreen of table activities.
- `id`: bigint NOT NULL — Column id of table activities.
- `idle`: integer NULL — Column idle of table activities.
- `logged_at`: timestamp NULL — Column logged_at of table activities.
- `name`: text NULL — Column name of table activities.
- `pid`: integer NULL — Column pid of table activities.
- `session_id`: bigint NOT NULL — Column session_id of table activities.
- `user_name`: text NULL — Column user_name of table activities.
- primary key: id

### activity_io  (source backend: rest)
Source table activity_io.

- `description`: text NULL — Column description of table activity_io.
- `finished_at`: timestamp NULL — Column finished_at of table activity_io.
- `id`: bigint NOT NULL — Column id of table activity_io.
- `idle`: integer NULL — Column idle of table activity_io.
- `io_activity`: integer NULL — Column io_activity of table activity_io.
- `logged_at`: timestamp NULL — Column logged_at of table activity_io.
- `name`: text NULL — Column name of table activity_io.
- `pid`: integer NULL — Column pid of table activity_io.
- `session_id`: bigint NOT NULL — Column session_id of table activity_io.

### browser_activity  (source backend: postgres)
Source table browser_activity.

- `browser`: text NULL — Column browser of table browser_activity.
- `id`: bigint NOT NULL — Column id of table browser_activity.
- `location`: text NULL — Column location of table browser_activity.
- `logged_at`: timestamp NULL — Column logged_at of table browser_activity.
- `session_id`: bigint NOT NULL — Column session_id of table browser_activity.
- primary key: id

### connections  (source backend: postgres)
Source table connections.

- `bssid`: text NULL — Column bssid of table connections.
- `bssid_type`: text NULL — Column bssid_type of table connections.
- `channel`: integer NULL — Column channel of table connections.
- `description`: text NULL — Column description of table connections.
- `dns_suffix`: text NULL — Column dns_suffix of table connections.
- `dnses`: text NULL — Column dnses of table connections.
- `ended_at`: timestamp NULL — Column ended_at of table connections.
- `friendly_name`: text NULL — Column friendly_name of table connections.
- `gateways`: text NULL — Column gateways of table connections.
- `guid`: text NULL — Column guid of table connections.
- `id`: bigint NOT NULL — Column id of table connections.
- `ips`: text NULL — Column ips of table connections.
- `location_id`: bigint NULL — Column location_id of table connections.
- `mac`: text NULL — Column mac of table connections.
- `phy_index`: integer NULL — Column phy_index of table connections.
- `phy_type`: text NULL — Column phy_type of table connections.
- `profile`: text NULL — Column profile of table connections.
- `r_speed`: bigint NULL — Column r_speed of table connections.
- `session_id`: bigint NOT NULL — Column session_id of table connections.
- `ssid`: text NULL — Column ssid of table connections.
- `started_at`: timestamp NULL — Column started_at of table connections.
- `t_speed`: bigint NULL — Column t_speed of table connections.
- `wireless`: integer NULL — Column wireless of table connections.
- primary key: id

### device_info  (source backend: rest)
Source table device_info.

- `cpu`: text NULL — Column cpu of table device_info.
- `hdd_capacity`: bigint NULL — Column hdd_capacity of table device_info.
- `hostview_version`: text NULL — Column hostview_version of table device_info.
- `id`: bigint NOT NULL — Column id of table device_info.
- `logged_at`: timestamp NULL — Column logged_at of table device_info.
- `manufacturer`: text NULL — Column manufacturer of table device_info.
- `memory_installed`: bigint NULL — Column memory_installed of table device_info.
- `operating_system`: text NULL — Column operating_system of table device_info.
- `product`: text NULL — Column product of table device_info.
- `serial_number`: text NULL — Column serial_number of table device_info.
- `session_id`: bigint NOT NULL — Column session_id of table device_info.
- `settings_version`: text NULL — Column settings_version of table device_info.
- `timezone`: text NULL — Column timezone of table device_info.
- `timezone_offset`: bigint NULL — Column timezone_offset of table device_info.
- primary key: id

### devices  (source backend: rest)
Source table devices.

- `created_at`: timestamp NULL — Column created_at of table devices.
- `device_id`: text NULL — Column device_id of table devices.
- `id`: bigint NOT NULL — Column id of table devices.
- `secret_token`: text NULL — Column secret_token of table devices.
- `user_id`: bigint NULL — Column user_id of table devices.
- primary key: id

### dns_logs  (source backend: s3)
Source table dns_logs.

- `connection_id`: bigint NOT NULL — Column connection_id of table dns_logs.
- `destination_ip`: text NULL — Column destination_ip of table dns_logs.
- `destination_port`: integer NULL — Column destination_port of table dns_logs.
- `host`: text NULL — Column host of table dns_logs.
- `id`: bigint NOT NULL — Column id of table dns_logs.
- `ip`: text NULL — Column ip of table dns_logs.
- `logged_at`: timestamp NULL — Column logged_at of table dns_logs.
- `protocol`: integer NULL — Column protocol of table dns_logs.
- `source_ip`: text NULL — Column source_ip of table dns_logs.
- `source_port`: integer NULL — Column source_port of table dns_logs.
- `type`: integer NULL — Column type of table dns_logs.
- primary key: id

### files  (source backend: postgres)
Source table files.

- `basename`: text NULL — Column basename of table files.
- `created_at`: timestamp NULL — Column created_at of table files.
- `device_id`: bigint NULL — Column device_id of table files.
- `error_info`: text NULL — Column error_info of table files.
- `folder`: text NULL — Column folder of table files.
- `hostview_version`: text NULL — Column hostview_version of table files.
- `id`: bigint NOT NULL — Column id of table files.
- `status`: text NULL — Column status of table files.
- `updated_at`: timestamp NULL — Column updated_at of table files.
- primary key: id

### http_logs  (source backend: rest)
Source table http_logs.

- `connection_id`: bigint NOT NULL — Column connection_id of table http_logs.
- `content_length`: text NULL — Column content_length of table http_logs.
- `content_type`: text NULL — Column content_type of table http_logs.
- `destination_ip`: text NULL — Column destination_ip of table http_logs.
- `destination_port`: integer NULL — Column destination_port of table http_logs.
- `http_host`: text NULL — Column http_host of table http_logs.
- `http_status_code`: text NULL — Column http_status_code of table http_logs.
- `http_verb`: text NULL — Column http_verb of table http_logs.
- `http_verb_param`: text NULL — Column http_verb_param of table http_logs.
- `id`: bigint NOT NULL — Column id of table http_logs.
- `logged_at`: timestamp NULL — Column logged_at of table http_logs.
- `protocol`: integer NULL — Column protocol of table http_logs.
- `referer`: text NULL — Column referer of table http_logs.
- `source_ip`: text NULL — Column source_ip of table http_logs.
- `source_port`: integer NULL — Column source_port of table http_logs.
- primary key: id

### io  (source backend: s3)
Source table io.

- `device`: integer NULL — Column device of table io.
- `id`: bigint NOT NULL — Column id of table io.
- `logged_at`: timestamp NULL — Column logged_at of table io.
- `name`: text NULL — Column name of table io.
- `pid`: integer NULL — Column pid of table io.
- `session_id`: bigint NOT NULL — Column session_id of table io.
- primary key: id

### locations  (source backend: files)
Source table locations.

- `asn_name`: text NULL — Column asn_name of table locations.
- `asn_number`: text NULL — Column asn_number of table locations.
- `city`: text NULL — Column city of table locations.
- `country_code`: text NULL — Column country_code of table locations.
- `id`: bigint NOT NULL — Column id of table locations.
- `latitude`: text NULL — Column latitude of table locations.
- `longitude`: text NULL — Column longitude of table locations.
- `public_ip`: text NULL — Column public_ip of table locations.
- `reverse_dns`: text NULL — Column reverse_dns of table locations.
- primary key: id

### netlabels  (source backend: mongodb)
Source table netlabels.

- `gateway`: text NULL — Column gateway of table netlabels.
- `guid`: text NULL — Column guid of table netlabels.
- `id`: bigint NOT NULL — Column id of table netlabels.
- `label`: text NULL — Column label of table netlabels.
- `logged_at`: timestamp NULL — Column logged_at of table netlabels.
- `session_id`: bigint NOT NULL — Column session_id of table netlabels.
- primary key: id

### pcap  (source backend: mongodb)
Source table pcap.

- `basename`: text NULL — Column basename of table pcap.
- `connection_id`: bigint NOT NULL — Column connection_id of table pcap.
- `created_at`: timestamp NULL — Column created_at of table pcap.
- `error_info`: text NULL — Column error_info of table pcap.
- `folder`: text NULL — Column folder of table pcap.
- `id`: bigint NOT NULL — Column id of table pcap.
- `status`: text NULL — Column status of table pcap.
- `updated_at`: timestamp NULL — Column updated_at of table pcap.
- primary key: id

### pcap_events  (source backend: mongodb)
Source table pcap_events.

- `direction`: text NULL — Column direction of table pcap_events.
- `flow_id`: bigint NULL — Column flow_id of table pcap_events.
- `id`: bigint NOT NULL — Column id of table pcap_events.
- `pcap_id`: bigint NOT NULL — Column pcap_id of table pcap_events.
- `seq`: bigint NULL — Column seq of table pcap_events.
- `time`: timestamp NULL — Column time of table pcap_events.
- `type`: text NULL — Column type of table pcap_events.
- primary key: id

### pcap_file  (source backend: postgres)
Source table pcap_file.

- `file_id`: bigint NOT NULL — Column file_id of table pcap_file.
- `file_order`: integer NOT NULL — Column file_order of table pcap_file.
- `pcap_id`: bigint NOT NULL — Column pcap_id of table pcap_file.
- primary key: file_id, pcap_id

### pcap_flow  (source backend: s3)
Source table pcap_flow.

- `bytes_ab`: bigint NULL — Column bytes_ab of table pcap_flow.
- `bytes_ba`: bigint NULL — Column bytes_ba of table pcap_flow.
- `destination_ip`: text NULL — Column destination_ip of table pcap_flow.
- `destination_port`: integer NULL — Column destination_port of table pcap_flow.
- `first_packet`: timestamp NULL — Column first_packet of table pcap_flow.
- `flow_code`: text NULL — Column flow_code of table pcap_flow.
- `id`: bigint NOT NULL — Column id of table pcap_flow.
- `idle_time_ab`: float NULL — Column idle_time_ab of table pcap_flow.
- `idle_time_ba`: float NULL — Column idle_time_ba of table pcap_flow.
- `last_packet`: timestamp NULL — Column last_packet of table pcap_flow.
- `pcap_id`: bigint NOT NULL — Column pcap_id of table pcap_flow.
- `protocol`: text NULL — Column protocol of table pcap_flow.
- `source_ip`: text NULL — Column source_ip of table pcap_flow.
- `source_port`: integer NULL — Column source_port of table pcap_flow.
- `status`: text NULL — Column status of table pcap_flow.
- `total_packets`: integer NULL — Column total_packets of table pcap_flow.
- `total_time`: float NULL — Column total_time of table pcap_flow.
- primary key: id

### pcap_rtt  (source backend: s3)
Source table pcap_rtt.

- `direction`: text NULL — Column direction of table pcap_rtt.
- `flow_id`: bigint NULL — Column flow_id of table pcap_rtt.
- `id`: bigint NOT NULL — Column id of table pcap_rtt.
- `pcap_id`: bigint NOT NULL — Column pcap_id of table pcap_rtt.
- `rtt`: float NULL — Column rtt of table pcap_rtt.
- `seq`: bigint NULL — Column seq of table pcap_rtt.
- `time`: timestamp NULL — Column time of table pcap_rtt.
- primary key: id

### pcap_throughput  (source backend: mongodb)
Source table pcap_throughput.

- `direction`: text NULL — Column direction of table pcap_throughput.
- `flow_id`: bigint NULL — Column flow_id of table pcap_throughput.
- `id`: bigint NOT NULL — Column id of table pcap_throughput.
- `pcap_id`: bigint NOT NULL — Column pcap_id of table pcap_throughput.
- `time`: timestamp NULL — Column time of table pcap_throughput.
- `value`: float NULL — Column value of table pcap_throughput.
- primary key: id

### ports  (source backend: s3)
Source table ports.

- `destination_ip`: text NULL — Column destination_ip of table ports.
- `destination_port`: integer NULL — Column destination_port of table ports.
- `id`: bigint NOT NULL — Column id of table ports.
- `logged_at`: timestamp NULL — Column logged_at of table ports.
- `name`: text NULL — Column name of table ports.
- `pid`: integer NULL — Column pid of table ports.
- `protocol`: integer NULL — Column protocol of table ports.
- `session_id`: bigint NOT NULL — Column session_id of table ports.
- `source_ip`: text NULL — Column source_ip of table ports.
- `source_port`: integer NULL — Column source_port of table ports.
- `state`: integer NULL — Column state of table ports.
- primary key: id

### power_states  (source backend: postgres)
Source table power_states.

- `event`: text NULL — Column event of table power_states.
- `id`: bigint NOT NULL — Column id of table power_states.
- `logged_at`: timestamp NULL — Column logged_at of table power_states.
- `session_id`: bigint NOT NULL — Column session_id of table power_states.
- `value`: integer NULL — Column value of table power_states.
- primary key: id

### processes  (source backend: s3)
Source table processes.

- `cpu`: float NULL — Column cpu of table processes.
- `id`: bigint NOT NULL — Column id of table processes.
- `logged_at`: timestamp NULL — Column logged_at of table processes.
- `memory`: integer NULL — Column memory of table processes.
- `name`: text NULL — Column name of table processes.
- `pid`: integer NULL — Column pid of table processes.
- `session_id`: bigint NOT NULL — Column session_id of table processes.
- primary key: id

### processes_running  (source backend: rest)
Source table processes_running.

- `device_id`: bigint NOT NULL — Column device_id of table processes_running.
- `ended_at`: timestamp NULL — Column ended_at of table processes_running.
- `process_name`: text NULL — Column process_name of table processes_running.
- `process_running`: integer NULL — Column process_running of table processes_running.
- `session_id`: bigint NOT NULL — Column session_id of table processes_running.
- `session_running`: integer NULL — Column session_running of table processes_running.
- `started_at`: timestamp NULL — Column started_at of table processes_running.

### sessions  (source backend: rest)
Source table sessions.

- `device_id`: bigint NULL — Column device_id of table sessions.
- `ended_at`: timestamp NULL — Column ended_at of table sessions.
- `file_id`: bigint NULL — Column file_id of table sessions.
- `id`: bigint NOT NULL — Column id of table sessions.
- `start_event`: text NULL — Column start_event of table sessions.
- `started_at`: timestamp NULL — Column started_at of table sessions.
- `stop_event`: text NULL — Column stop_event of table sessions.
- primary key: id

### survey_activity_importance  (source backend: postgres)
Source table survey_activity_importance.

- `id`: bigint NOT NULL — Column id of table survey_activity_importance.
- `importance`: text NULL — Column importance of table survey_activity_importance.
- `process_desc`: text NULL — Column process_desc of table survey_activity_importance.
- `process_name`: text NULL — Column process_name of table survey_activity_importance.
- `survey_id`: bigint NOT NULL — Column survey_id of table survey_activity_importance.
- primary key: id

### survey_activity_qoe  (source backend: files)
Source table survey_activity_qoe.

- `id`: bigint NOT NULL — Column id of table survey_activity_qoe.
- `process_desc`: text NULL — Column process_desc of table survey_activity_qoe.
- `process_name`: text NULL — Column process_name of table survey_activity_qoe.
- `qoe`: text NULL — Column qoe of table survey_activity_qoe.
- `survey_id`: bigint NOT NULL — Column survey_id of table survey_activity_qoe.
- primary key: id

### survey_activity_tags  (source backend: postgres)
Source table survey_activity_tags.

- `id`: bigint NOT NULL — Column id of table survey_activity_tags.
- `process_desc`: text NULL — Column process_desc of table survey_activity_tags.
- `process_name`: text NULL — Column process_name of table survey_activity_tags.
- `survey_id`: bigint NOT NULL — Column survey_id of table survey_activity_tags.
- `tags`: text NULL — Column tags of table survey_activity_tags.
- primary key: id

### survey_problem_tags  (source backend: s3)
Source table survey_problem_tags.

- `id`: bigint NOT NULL — Column id of table survey_problem_tags.
- `process_desc`: text NULL — Column process_desc of table survey_problem_tags.
- `process_name`: text NULL — Column process_name of table survey_problem_tags.
- `survey_id`: bigint NOT NULL — Column survey_id of table survey_problem_tags.
- `tags`: text NULL — Column tags of table survey_problem_tags.
- primary key: id

### surveys  (source backend: postgres)
Source table surveys.

- `duration`: integer NULL — Column duration of table surveys.
- `ended_at`: timestamp NULL — Column ended_at of table surveys.
- `id`: bigint NOT NULL — Column id of table surveys.
- `ondemand`: integer NULL — Column ondemand of table surveys.
- `qoe_score`: integer NULL — Column qoe_score of table surveys.
- `session_id`: bigint NOT NULL — Column session_id of table surveys.
- `started_at`: timestamp NULL — Column started_at of table surveys.
- primary key: id

### users  (source backend: mongodb)
Source table users.

- `created_at`: timestamp NULL — Column created_at of table users.
- `id`: bigint NOT NULL — Column id of table users.
- `password`: text NOT NULL — Column password of table users.
- `updated_at`: timestamp NULL — Column updated_at of table users.
- `username`: text NOT NULL — Column username of table users.
- primary key: id

### video_buffered_play_time_sample  (source backend: postgres)
A sample of the current video playtime and the current amount of video in the buffer.

- `buffered_minus_current`: float NULL — Column buffered_minus_current of table video_buffered_play_time_sample.
- `current_video_playtime`: float NULL — Column current_video_playtime of table video_buffered_play_time_sample.
- `id`: bigint NOT NULL — Column id of table video_buffered_play_time_sample.
- `logged_at`: timestamp NULL — Column logged_at of table video_buffered_play_time_sample.
- `video_session_id`: bigint NOT NULL — Column video_session_id of table video_buffered_play_time_sample.
- primary key: id

### video_buffering_event  (source backend: postgres)
Models periods of video playback freezing, for buffering.

- `ended_at`: timestamp NULL — Column ended_at of table video_buffering_event.
- `ended_by_abort`: integer NULL — Column ended_by_abort of table video_buffering_event.
- `id`: bigint NOT NULL — Column id of table video_buffering_event.
- `started_at`: timestamp NULL — Column started_at of table video_buffering_event.
- `type`: integer NULL — Column type of table video_buffering_event.
- `video_session_id`: bigint NOT NULL — Column video_session_id of table video_buffering_event.
- primary key: id

### video_off_screen_event  (source backend: mongodb)
Models periods during which the video playback is going off-screen (i.e., the user activates a different browser tab or minimizes the browser window.

- `ended_at`: timestamp NULL — Column ended_at of table video_off_screen_event.
- `id`: bigint NOT NULL — Column id of table video_off_screen_event.
- `started_at`: timestamp NULL — Column started_at of table video_off_screen_event.
- `video_session_id`: bigint NOT NULL — Column video_session_id of table video_off_screen_event.
- primary key: id

### video_pause_event  (source backend: postgres)
Models periods during which the video playback is being paused by the user.

- `ended_at`: timestamp NULL — Column ended_at of table video_pause_event.
- `id`: bigint NOT NULL — Column id of table video_pause_event.
- `started_at`: timestamp NULL — Column started_at of table video_pause_event.
- `type`: integer NULL — Column type of table video_pause_event.
- `video_session_id`: bigint NOT NULL — Column video_session_id of table video_pause_event.
- primary key: id

### video_playback_quality_sample  (source backend: s3)
A sample of the playback quality (in terms of video frames/sec, dropped, corrupted frames, etc). Rows of this table contain samples of HTML5 video (see https://developer.mozilla.org/en/docs/Web/API/HTMLVideoElement) and VideoPlaybackQuality (See https://developer.mozilla.org/en-US/docs/Web/API/VideoPlaybackQuality) properties.

- `corruptedvideoframes`: bigint NULL — Column corruptedvideoframes of table video_playback_quality_sample.
- `droppedvideoframes`: bigint NULL — Column droppedvideoframes of table video_playback_quality_sample.
- `id`: bigint NOT NULL — Column id of table video_playback_quality_sample.
- `logged_at`: timestamp NULL — Column logged_at of table video_playback_quality_sample.
- `mozdecodedframes`: bigint NULL — Column mozdecodedframes of table video_playback_quality_sample.
- `mozframedelay`: float NULL — Column mozframedelay of table video_playback_quality_sample.
- `mozpaintedframes`: bigint NULL — Column mozpaintedframes of table video_playback_quality_sample.
- `mozparsedframes`: bigint NULL — Column mozparsedframes of table video_playback_quality_sample.
- `mozpresentedframes`: bigint NULL — Column mozpresentedframes of table video_playback_quality_sample.
- `totalframedelay`: bigint NULL — Column totalframedelay of table video_playback_quality_sample.
- `totalvideoframes`: bigint NULL — Column totalvideoframes of table video_playback_quality_sample.
- `video_session_id`: bigint NOT NULL — Column video_session_id of table video_playback_quality_sample.
- primary key: id

### video_player_size  (source backend: s3)
Models player size change events during a video session.

- `ended_at`: timestamp NULL — Column ended_at of table video_player_size.
- `height`: integer NULL — Column height of table video_player_size.
- `id`: bigint NOT NULL — Column id of table video_player_size.
- `is_full_screen`: integer NULL — Column is_full_screen of table video_player_size.
- `started_at`: timestamp NULL — Column started_at of table video_player_size.
- `video_session_id`: bigint NOT NULL — Column video_session_id of table video_player_size.
- `width`: integer NULL — Column width of table video_player_size.
- primary key: id

### video_resolution  (source backend: rest)
Models resolution change events during a video session.

- `ended_at`: timestamp NULL — Column ended_at of table video_resolution.
- `id`: bigint NOT NULL — Column id of table video_resolution.
- `res_x`: integer NULL — Column res_x of table video_resolution.
- `res_y`: integer NULL — Column res_y of table video_resolution.
- `started_at`: timestamp NULL — Column started_at of table video_resolution.
- `video_session_id`: bigint NOT NULL — Column video_session_id of table video_resolution.
- primary key: id

### video_seek_event  (source backend: rest)
Models events of user navigating to a specific timepoint of playback through the seekbar.

- `buffering_event_id`: bigint NULL — Column buffering_event_id of table video_seek_event.
- `id`: bigint NOT NULL — Column id of table video_seek_event.
- `logged_at`: timestamp NULL — Column logged_at of table video_seek_event.
- `pause_event_id`: bigint NULL — Column pause_event_id of table video_seek_event.
- `to_video_time`: float NULL — Column to_video_time of table video_seek_event.
- `video_session_id`: bigint NOT NULL — Column video_session_id of table video_seek_event.
- primary key: id

### video_session  (source backend: postgres)
Models a streaming video (e.g., YouTube) playback session.

- `current_src`: text NULL — Column current_src of table video_session.
- `duration`: float NULL — Column duration of table video_session.
- `end_reason`: integer NULL — Column end_reason of table video_session.
- `ended_at`: timestamp NULL — Column ended_at of table video_session.
- `file_id`: bigint NULL — Column file_id of table video_session.
- `id`: bigint NOT NULL — Column id of table video_session.
- `qoe_score`: integer NULL — Column qoe_score of table video_session.
- `service_type`: integer NULL — Column service_type of table video_session.
- `session_id`: bigint NOT NULL — Column session_id of table video_session.
- `started_at`: timestamp NULL — Column started_at of table video_session.
- `title`: text NULL — Column title of table video_session.
- `window_location`: text NULL — Column window_location of table video_session.
- primary key: id

### wifi_stats  (source backend: rest)
Source table wifi_stats.

- `guid`: text NULL — Column guid of table wifi_stats.
- `id`: bigint NOT NULL — Column id of table wifi_stats.
- `logged_at`: timestamp NULL — Column logged_at of table wifi_stats.
- `r_speed`: bigint NULL — Column r_speed of table wifi_stats.
- `rssi`: integer NULL — Column rssi of table wifi_stats.
- `session_id`: bigint NOT NULL — Column session_id of table wifi_stats.
- `signal`: integer NULL — Column signal of table wifi_stats.
- `state`: integer NULL — Column state of table wifi_stats.
- `t_speed`: bigint NULL — Column t_speed of table wifi_stats.
- primary key: id

### Relationships

- activities(session_id) -> sessions(id) [required]
- activity_io(id) -> activities(id) [required]
- activity_io(session_id) -> sessions(id) [required]
- browser_activity(session_id) -> sessions(id) [required]
- connections(location_id) -> locations(id) [optional (may be NULL/dangling)]
- connections(session_id) -> sessions(id) [required]
- device_info(session_id) -> sessions(id) [required]
- devices(user_id) -> users(id) [optional (may be NULL/dangling)]
- dns_logs(connection_id) -> connections(id) [required]
- files(device_id) -> devices(id) [optional (may be NULL/dangling)]
- http_logs(connection_id) -> connections(id) [required]
- io(session_id) -> sessions(id) [required]
- netlabels(session_id) -> sessions(id) [required]
- pcap(connection_id) -> connections(id) [required]
- pcap_events(flow_id) -> pcap_flow(id) [optional (may be NULL/dangling)]
- pcap_events(pcap_id) -> pcap(id) [required]
- pcap_file(file_id) -> files(id) [required]
- pcap_file(pcap_id) -> pcap(id) [required]
- pcap_flow(pcap_id) -> pcap(id) [required]
- pcap_rtt(flow_id) -> pcap_flow(id) [optional (may be NULL/dangling)]
- pcap_rtt(pcap_id) -> pcap(id) [required]
- pcap_throughput(flow_id) -> pcap_flow(id) [optional (may be NULL/dangling)]
- pcap_throughput(pcap_id) -> pcap(id) [required]
- ports(session_id) -> sessions(id) [required]
- power_states(session_id) -> sessions(id) [required]
- processes(session_id) -> sessions(id) [required]
- processes_running(device_id) -> devices(id) [required]
- processes_running(session_id) -> sessions(id) [required]
- sessions(device_id) -> devices(id) [optional (may be NULL/dangling)]
- sessions(file_id) -> files(id) [optional (may be NULL/dangling)]
- survey_activity_importance(survey_id) -> surveys(id) [required]
- survey_activity_qoe(survey_id) -> surveys(id) [required]
- survey_activity_tags(survey_id) -> surveys(id) [required]
- survey_problem_tags(survey_id) -> surveys(id) [required]
- surveys(session_id) -> sessions(id) [required]
- video_buffered_play_time_sample(video_session_id) -> video_session(id) [required]
- video_buffering_event(video_session_id) -> video_session(id) [required]
- video_off_screen_event(video_session_id) -> video_session(id) [required]
- video_pause_event(video_session_id) -> video_session(id) [required]
- video_playback_quality_sample(video_session_id) -> video_session(id) [required]
- video_player_size(video_session_id) -> video_session(id) [required]
- video_resolution(video_session_id) -> video_session(id) [required]
- video_seek_event(buffering_event_id) -> video_buffering_event(id) [optional (may be NULL/dangling)]
- video_seek_event(pause_event_id) -> video_pause_event(id) [optional (may be NULL/dangling)]
- video_seek_event(video_session_id) -> video_session(id) [required]
- video_session(file_id) -> files(id) [optional (may be NULL/dangling)]
- video_session(session_id) -> sessions(id) [required]
- wifi_stats(session_id) -> sessions(id) [required]

