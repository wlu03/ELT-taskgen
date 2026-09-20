# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Aeganaden E Rev Learning And Information

## Specification

PROJECT OVERVIEW

This project builds three analytical marts from a学 learning-and-information schema. The marts are engineering_year_level_engineering_subject_list_distribution, engineering_topic_engineering_courseware_top and engineering_subject_engineering_topic_distribution.

Source tables and where each one must be extracted from. The table engineering_activity must be extracted from the s3 backend. The table engineering_activity_details must be extracted from the files backend. The table engineering_activity_schedule must be extracted from the rest backend. The table engineering_admin must be extracted from the mongodb backend. The table engineering_announcement must be extracted from the mongodb backend. The table engineering_attendance_in must be extracted from the files backend. The table engineering_attendance_out must be extracted from the postgres backend. The table engineering_choice must be extracted from the s3 backend. The table engineering_comment must be extracted from the files backend. The table engineering_course must be extracted from the files backend. The table engineering_course_modules must be extracted from the mongodb backend. The table engineering_courseware must be extracted from the files backend. The table engineering_courseware_question must be extracted from the postgres backend. The table engineering_courseware_time must be extracted from the files backend. The table engineering_data_scores must be extracted from the files backend. The table engineering_enrollment must be extracted from the postgres backend. The table engineering_fic must be extracted from the s3 backend. The table engineering_grade_assessment must be extracted from the postgres backend. The table engineering_lecturer must be extracted from the mongodb backend. The table engineering_lecturer_attendance must be extracted from the files backend. The table engineering_lecturer_feedback must be extracted from the postgres backend. The table engineering_log must be extracted from the s3 backend. The table engineering_log_content must be extracted from the files backend. The table engineering_login_sessions must be extracted from the postgres backend. The table engineering_offering must be extracted from the mongodb backend. The table engineering_professor must be extracted from the mongodb backend. The table engineering_remedial_coursewares must be extracted from the rest backend. The table engineering_remedial_grade_assessment must be extracted from the mongodb backend. The table engineering_remedial_student_answer must be extracted from the postgres backend. The table engineering_schedule must be extracted from the mongodb backend. The table engineering_student must be extracted from the files backend. The table engineering_student_answer must be extracted from the s3 backend. The table engineering_student_scores must be extracted from the s3 backend. The table engineering_subject must be extracted from the postgres backend. The table engineering_subject_list must be extracted from the mongodb backend. The table engineering_subject_list_has_topic_list must be extracted from the mongodb backend. The table engineering_topic must be extracted from the files backend. The table engineering_total_grade must be extracted from the mongodb backend. The table engineering_year_level must be extracted from the mongodb backend.

Relationships between the source tables, each labelled exactly as the source schema labels it.
Child table engineering_activity with key activity_details_id refers to parent table engineering_activity_details with key activity_details_id; this relationship is required.
Child table engineering_activity with key activity_schedule_id refers to parent table engineering_activity_schedule with key activity_schedule_id; this relationship is required.
Child table engineering_activity with key lecturer_id refers to parent table engineering_lecturer with key lecturer_id; this relationship is optional (may be NULL or dangling).
Child table engineering_activity with key offering_id refers to parent table engineering_offering with key offering_id; this relationship is required.
Child table engineering_attendance_in with key lecturer_attendance_id refers to parent table engineering_lecturer_attendance with key lecturer_attendance_id; this relationship is required.
Child table engineering_attendance_out with key lecturer_attendance_id refers to parent table engineering_lecturer_attendance with key lecturer_attendance_id; this relationship is required.
Child table engineering_choice with key courseware_question_id refers to parent table engineering_courseware_question with key courseware_question_id; this relationship is required.
Child table engineering_comment with key courseware_question_id refers to parent table engineering_courseware_question with key courseware_question_id; this relationship is required.
Child table engineering_course with key enrollment_id refers to parent table engineering_enrollment with key enrollment_id; this relationship is required.
Child table engineering_course with key professor_id refers to parent table engineering_professor with key professor_id; this relationship is required.
Child table engineering_course with key year_level_id refers to parent table engineering_year_level with key year_level_id; this relationship is required.
Child table engineering_course_modules with key topic_id refers to parent table engineering_topic with key topic_id; this relationship is required.
Child table engineering_courseware with key topic_id refers to parent table engineering_topic with key topic_id; this relationship is required.
Child table engineering_courseware_question with key courseware_id refers to parent table engineering_courseware with key courseware_id; this relationship is required.
Child table engineering_courseware_time with key grade_assessment_id refers to parent table engineering_grade_assessment with key grade_assessment_id; this relationship is required.
Child table engineering_grade_assessment with key courseware_id refers to parent table engineering_courseware with key courseware_id; this relationship is required.
Child table engineering_grade_assessment with key student_id refers to parent table engineering_student with key student_id; this relationship is required.
Child table engineering_lecturer_attendance with key lecturer_id refers to parent table engineering_lecturer with key lecturer_id; this relationship is required.
Child table engineering_lecturer_attendance with key offering_id refers to parent table engineering_offering with key offering_id; this relationship is required.
Child table engineering_lecturer_attendance with key schedule_id refers to parent table engineering_schedule with key schedule_id; this relationship is required.
Child table engineering_lecturer_feedback with key enrollment_id refers to parent table engineering_enrollment with key enrollment_id; this relationship is required.
Child table engineering_lecturer_feedback with key lecturer_id refers to parent table engineering_lecturer with key lecturer_id; this relationship is required.
Child table engineering_lecturer_feedback with key offering_id refers to parent table engineering_offering with key offering_id; this relationship is required.
Child table engineering_lecturer_feedback with key student_id refers to parent table engineering_student with key student_id; this relationship is required.
Child table engineering_log with key log_content_id refers to parent table engineering_log_content with key log_content_id; this relationship is required.
Child table engineering_offering with key course_id refers to parent table engineering_course with key course_id; this relationship is required.
Child table engineering_offering with key fic_id refers to parent table engineering_fic with key fic_id; this relationship is required.
Child table engineering_remedial_coursewares with key courseware_id refers to parent table engineering_courseware with key courseware_id; this relationship is required.
Child table engineering_remedial_coursewares with key student_scores_id refers to parent table engineering_student_scores with key student_scores_id; this relationship is required.
Child table engineering_remedial_grade_assessment with key courseware_id refers to parent table engineering_courseware with key courseware_id; this relationship is required.
Child table engineering_remedial_grade_assessment with key remedial_coursewares_id refers to parent table engineering_remedial_coursewares with key remedial_coursewares_id; this relationship is required.
Child table engineering_remedial_grade_assessment with key student_id refers to parent table engineering_student with key student_id; this relationship is required.
Child table engineering_remedial_student_answer with key choice_id refers to parent table engineering_choice with key choice_id; this relationship is required.
Child table engineering_remedial_student_answer with key courseware_id refers to parent table engineering_courseware with key courseware_id; this relationship is required.
Child table engineering_remedial_student_answer with key courseware_question_id refers to parent table engineering_courseware_question with key courseware_question_id; this relationship is required.
Child table engineering_remedial_student_answer with key student_id refers to parent table engineering_student with key student_id; this relationship is required.
Child table engineering_schedule with key lecturer_id refers to parent table engineering_lecturer with key lecturer_id; this relationship is optional (may be NULL or dangling).
Child table engineering_schedule with key offering_id refers to parent table engineering_offering with key offering_id; this relationship is required.
Child table engineering_student with key offering_id refers to parent table engineering_offering with key offering_id; this relationship is required.
Child table engineering_student_answer with key choice_id refers to parent table engineering_choice with key choice_id; this relationship is required.
Child table engineering_student_answer with key courseware_id refers to parent table engineering_courseware with key courseware_id; this relationship is required.
Child table engineering_student_answer with key courseware_question_id refers to parent table engineering_courseware_question with key courseware_question_id; this relationship is required.
Child table engineering_student_answer with key student_id refers to parent table engineering_student with key student_id; this relationship is required.
Child table engineering_student_scores with key course_id refers to parent table engineering_course with key course_id; this relationship is required.
Child table engineering_student_scores with key data_scores_id refers to parent table engineering_data_scores with key data_scores_id; this relationship is required.
Child table engineering_subject with key course_id refers to parent table engineering_course with key course_id; this relationship is required.
Child table engineering_subject with key lecturer_id refers to parent table engineering_lecturer with key lecturer_id; this relationship is optional (may be NULL or dangling).
Child table engineering_subject with key subject_list_id refers to parent table engineering_subject_list with key subject_list_id; this relationship is optional (may be NULL or dangling).
Child table engineering_subject_list with key year_level_id refers to parent table engineering_year_level with key year_level_id; this relationship is required.
Child table engineering_subject_list_has_topic_list with key subject_list_id refers to parent table engineering_subject_list with key subject_list_id; this relationship is required.
Child table engineering_topic with key subject_id refers to parent table engineering_subject with key subject_id; this relationship is required.
Child table engineering_total_grade with key student_id refers to parent table engineering_student with key student_id; this relationship is required.
Child table engineering_total_grade with key subject_id refers to parent table engineering_subject with key subject_id; this relationship is required.


=== Mart engineering_year_level_engineering_subject_list_distribution: Per-(engineering_year_level, measure state) distribution of linked engineering_subject_list activity in the 605358_dbase_structure.sql schema. ===

Grain: one row per (year_level_id, measure state) pair represented by linked engineering_subject_list rows, plus one absent no-activity row for a engineering_year_level row with no links. Because subject_list_is_active is required, no linked engineering_subject_list row belongs to the absent state.

The key columns of this mart are entity_key and measure_state; together they identify one output row.

Rule 1: the source table engineering_year_level is read in full.

Rule 2: the source table engineering_subject_list is read in full.

Rule 3: from engineering_year_level, each year_level_id is carried into the measure-state calculation as entity_key and its year_level_name is carried as entity_name.

Rule 4: the linked engineering_subject_list rows are brought into each engineering_year_level entity, matching an engineering_subject_list row to the entity whose entity_key equals its year_level_id, carrying entity_key, entity_name and year_level_id; preservation is left-sided on the entity side, so an entity with no linked row is retained and its absent state is visible.

Rule 5: the present measure-state rows are the rows that have a real engineering_subject_list row behind them, carrying entity_key and entity_name; subject_list_is_active is required on every such row.

Rule 6: there is one row per engineering_year_level entity that has at least one row in the present measure state, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different subject_list_is_active values occur (each different value counted once, however many rows repeat it), total_amount as the total subject_list_is_active, and max_amount as the largest subject_list_is_active.

Rule 7: for these present-state measures, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and is 0.0 when total_amount is 0.

Rule 8: these measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state as the present measure state, that is, the text value 'present'.

Rule 9: the absent measure-state rows are the retained placeholders for an engineering_year_level row with no engineering_subject_list rows, carrying entity_key and entity_name; no real row can enter this state because subject_list_is_active is required.

Rule 10: there is one row per engineering_year_level entity with no linked engineering_subject_list row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked engineering_subject_list row, carrying entity_key and entity_name and reporting a row_count of 0, 0 different subject_list_is_active values in distinct_amount_count, a total subject_list_is_active of 0 in total_amount and a largest subject_list_is_active of 0 in max_amount.

Rule 11: for these absent-state measures, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and is 0.0 when total_amount is 0.

Rule 12: these measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state as the absent measure state, that is, the text value 'absent'.

Rule 13: the present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share, keeping all rows: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14: deterministic output order — rows appear in ascending entity_key order, and within one entity in ascending measure_state order.

Output columns of engineering_year_level_engineering_subject_list_distribution:
- entity_key (integer): identifier of the engineering_year_level row.
- measure_state (text): 'present' for a linked engineering_subject_list row; 'absent' only for a engineering_year_level row with no linked engineering_subject_list row. subject_list_is_active is required on every real engineering_subject_list row.
- entity_name (text): year_level_name of the engineering_year_level row, copied unchanged.
- row_count (bigint): number of linked engineering_subject_list rows in this entity/state cell; 0 for a no-activity absent cell.
- distinct_amount_count (bigint): number of unique subject_list_is_active values in this cell; each unique value is counted once, however many rows repeat it; 0 for a no-activity absent cell.
- total_amount (integer): sum of subject_list_is_active in this cell; 0 for a no-activity absent cell.
- max_amount (integer): largest subject_list_is_active in this cell; 0 for a no-activity absent cell.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.


=== Mart engineering_topic_engineering_courseware_top: Per-engineering_topic extremes over linked engineering_courseware rows in the 605358_dbase_structure.sql schema: WHICH row is largest, not how large it is. ===

Grain: one row per engineering_topic (topic_id), INCLUDING engineering_topic rows with no linked engineering_courseware rows.

The key column of this mart is parent_key; it identifies one output row.

Rule 1: the source table engineering_topic is read in full.

Rule 2: the source table engineering_courseware is read in full.

Rule 3: from engineering_topic there is one row per engineering_topic row, keyed by topic_id, carrying parent_key and parent_name.

Rule 4: engineering_courseware rows are brought in on topic_id, matching an engineering_courseware row to the parent whose parent_key equals its topic_id and carrying topic_id; preservation is left-sided on the engineering_topic side, so a engineering_topic row with no engineering_courseware rows still appears, with the declared defaults.

Rule 5: the matched rows are ranked within each parent_key under an explicit total order — the measure first, then the declared tie-break — so the extremal row is a function of the input and not of row order.

Rule 6: there is one output row per parent_key, carrying parent_name beside the keys — a key value identifies one source row for the carried columns, so they take one value per key and never split a group — and reporting top_measure, tied_count, child_count and total_measure for that row's matching rows; a group with no qualifying rows still appears, reporting 0, and a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7: for each parent_key the single surviving row is the one at which the ordering measure is largest, with ties broken by the smallest courseware_description under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), a row with no courseware_description value sorting after every row that has one, then by the smallest courseware_id; top_label and top_row_id are taken from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row, and the declared defaults belong only to a group with NO rows.

Rule 8: the extremal row's attributes are attached to the grouped measures on parent_key; preservation is left-sided on the measures side, so a group with no rows at all keeps its measures.

Rule 9: the mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults — never NULL — for a group with no matching rows.

Rule 10: guarded ratios, carrying parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id — top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when total_measure is 0 or NULL.

Rule 11: tie_state, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, is 'empty' when no row holds a maximum at all — the parent has no engineering_courseware rows — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; this is a categorical mapping with no numeric boundary, and equivalently tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more.

Rule 12: deterministic output order — rows appear in ascending parent_key order.

Output columns of engineering_topic_engineering_courseware_top:
- parent_key (integer): identifier of the engineering_topic row. One row per value.
- parent_name (text): topic_description of the engineering_topic row, copied unchanged.
- top_measure (integer): the largest courseware_date_added itself; 0 when the parent has no engineering_courseware rows.
- tied_count (bigint): how many engineering_courseware rows are tied at that largest courseware_date_added; 1 when exactly one row carries that largest courseware_date_added; 0 when there are no rows.
- child_count (bigint): number of engineering_courseware rows for this engineering_topic row; 0 when there are none. An engineering_topic row kept with no engineering_courseware row reports 0 here, never 1: its placeholder holds no engineering_courseware row to count.
- total_measure (integer): sum of courseware_date_added over all of them; 0 when the parent has no engineering_courseware rows.
- top_label (text): the courseware_description of the engineering_courseware row with the LARGEST courseware_date_added for this engineering_topic row. Ties in courseware_date_added are broken by taking the SMALLEST courseware_description under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no courseware_description value sorts after every labelled row; rows tied on both are resolved by the smallest courseware_id. The literal '(none)' when the parent has no engineering_courseware rows at all, and '(none)' when the winning row has no courseware_description value.
- top_row_id (integer): the courseware_id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real engineering_courseware row whenever the parent has any. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no engineering_courseware rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more.


=== Mart engineering_subject_engineering_topic_distribution: Per-(engineering_subject, measure state) distribution of linked engineering_topic activity in the 605358_dbase_structure.sql schema. ===

Grain: one row per (subject_id, measure state) pair represented by linked engineering_topic rows, plus one absent no-activity row for a engineering_subject row with no links. Because topic_done is required, no linked engineering_topic row belongs to the absent state.

The key columns of this mart are entity_key and measure_state; together they identify one output row.

Rule 1: the source table engineering_subject is read in full.

Rule 2: the source table engineering_topic is read in full.

Rule 3: from engineering_subject, each subject_id is carried into the measure-state calculation as entity_key and its subject_description is carried as entity_name.

Rule 4: the linked engineering_topic rows are brought into each engineering_subject entity, matching an engineering_topic row to the entity whose entity_key equals its subject_id, carrying entity_key, entity_name and subject_id; preservation is left-sided on the entity side, so an entity with no linked row is retained and its absent state is visible.

Rule 5: the present measure-state rows are the rows that have a real engineering_topic row behind them, carrying entity_key and entity_name; topic_done is required on every such row.

Rule 6: there is one row per engineering_subject entity that has at least one row in the present measure state, and no row here for an entity with none, carrying entity_key and entity_name and reporting row_count as the row count, distinct_amount_count as how many different topic_done values occur (each different value counted once, however many rows repeat it), total_amount as the total topic_done, and max_amount as the largest topic_done.

Rule 7: for these present-state measures, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and is 0.0 when total_amount is 0.

Rule 8: these measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state as the present measure state, that is, the text value 'present'.

Rule 9: the absent measure-state rows are the retained placeholders for an engineering_subject row with no engineering_topic rows, carrying entity_key and entity_name; no real row can enter this state because topic_done is required.

Rule 10: there is one row per engineering_subject entity with no linked engineering_topic row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked engineering_topic row, carrying entity_key and entity_name and reporting a row_count of 0, 0 different topic_done values in distinct_amount_count, a total topic_done of 0 in total_amount and a largest topic_done of 0 in max_amount.

Rule 11: for these absent-state measures, carrying entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places, and is 0.0 when total_amount is 0.

Rule 12: these measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled with measure_state as the absent measure state, that is, the text value 'absent'.

Rule 13: the present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share, keeping all rows: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14: deterministic output order — rows appear in ascending entity_key order, and within one entity in ascending measure_state order.

Output columns of engineering_subject_engineering_topic_distribution:
- entity_key (integer): identifier of the engineering_subject row.
- measure_state (text): 'present' for a linked engineering_topic row; 'absent' only for a engineering_subject row with no linked engineering_topic row. topic_done is required on every real engineering_topic row.
- entity_name (text): subject_description of the engineering_subject row, copied unchanged.
- row_count (bigint): number of linked engineering_topic rows in this entity/state cell; 0 for a no-activity absent cell.
- distinct_amount_count (bigint): number of unique topic_done values in this cell; each unique value is counted once, however many rows repeat it; 0 for a no-activity absent cell.
- total_amount (integer): sum of topic_done in this cell; 0 for a no-activity absent cell.
- max_amount (integer): largest topic_done in this cell; 0 for a no-activity absent cell.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `engineering_year_level_engineering_subject_list_distribution`

- Grain: One row per (year_level_id, measure state) pair represented by linked engineering_subject_list rows, plus one absent no-activity row for a engineering_year_level row with no links. Because subject_list_is_active is required, no linked engineering_subject_list row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'engineering_year_level_engineering_subject_list_distribution' has 14 declared semantic rules:
1. [source] Read source table engineering_year_level. (public source tables: engineering_year_level)
2. [source] Read source table engineering_subject_list. (public source tables: engineering_subject_list)
3. [derive] Carry each year_level_id and its year_level_name into the measure-state calculation. (public source tables: engineering_year_level | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked engineering_subject_list rows into each engineering_year_level entity; retain an entity with no linked row so its absent state is visible. (public source tables: engineering_subject_list | public carried/output columns: entity_key, entity_name, year_level_id | join preservation: left | condition public identifiers: engineering_subject_list, year_level_id, entity_key)
5. [filter] Keep the present measure-state rows: a real engineering_subject_list row; subject_list_is_active is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per engineering_year_level entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different subject_list_is_active values occur (each different value counted once, however many rows repeat it), total subject_list_is_active, and largest subject_list_is_active. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a engineering_year_level row with no engineering_subject_list rows; no real row can enter this state because subject_list_is_active is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per engineering_year_level entity with no linked engineering_subject_list row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked engineering_subject_list row, reporting a row count of 0, 0 different subject_list_is_active values, a total subject_list_is_active of 0 and a largest subject_list_is_active of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `engineering_topic_engineering_courseware_top`

- Grain: One row per engineering_topic (topic_id), INCLUDING engineering_topic rows with no linked engineering_courseware rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'engineering_topic_engineering_courseware_top' has 12 declared semantic rules:
1. [source] Read source table engineering_topic. (public source tables: engineering_topic)
2. [source] Read source table engineering_courseware. (public source tables: engineering_courseware)
3. [derive] One row per engineering_topic row, keyed by topic_id. (public source tables: engineering_topic | public carried/output columns: parent_key, parent_name)
4. [join] Bring in engineering_courseware: a engineering_topic row with no engineering_courseware rows still appears, with the declared defaults. (public source tables: engineering_courseware | public carried/output columns: topic_id | join preservation: left | condition public identifiers: engineering_courseware, topic_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest courseware_description under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no courseware_description value sorts after every row that has one), then the smallest courseware_id, and take top_label, top_row_id from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no engineering_courseware rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `engineering_subject_engineering_topic_distribution`

- Grain: One row per (subject_id, measure state) pair represented by linked engineering_topic rows, plus one absent no-activity row for a engineering_subject row with no links. Because topic_done is required, no linked engineering_topic row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'engineering_subject_engineering_topic_distribution' has 14 declared semantic rules:
1. [source] Read source table engineering_subject. (public source tables: engineering_subject)
2. [source] Read source table engineering_topic. (public source tables: engineering_topic)
3. [derive] Carry each subject_id and its subject_description into the measure-state calculation. (public source tables: engineering_subject | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked engineering_topic rows into each engineering_subject entity; retain an entity with no linked row so its absent state is visible. (public source tables: engineering_topic | public carried/output columns: entity_key, entity_name, subject_id | join preservation: left | condition public identifiers: engineering_topic, subject_id, entity_key)
5. [filter] Keep the present measure-state rows: a real engineering_topic row; topic_done is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per engineering_subject entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different topic_done values occur (each different value counted once, however many rows repeat it), total topic_done, and largest topic_done. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a engineering_subject row with no engineering_topic rows; no real row can enter this state because topic_done is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per engineering_subject entity with no linked engineering_topic row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked engineering_topic row, reporting a row count of 0, 0 different topic_done values, a total topic_done of 0 and a largest topic_done of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

## Source tables

### engineering_activity  (source backend: s3)
Source table engineering.activity.

- `activity_description`: text NOT NULL — Column activity_description of table engineering.activity.
- `activity_details_id`: integer NOT NULL — Column activity_details_id of table engineering.activity.
- `activity_id`: integer NOT NULL — Column activity_id of table engineering.activity.
- `activity_schedule_id`: integer NOT NULL — Column activity_schedule_id of table engineering.activity.
- `activity_status`: integer NOT NULL — Column activity_status of table engineering.activity.
- `activity_venue`: text NOT NULL — Column activity_venue of table engineering.activity.
- `lecturer_id`: integer NULL — Column lecturer_id of table engineering.activity.
- `offering_id`: integer NOT NULL — Column offering_id of table engineering.activity.
- primary key: activity_id

### engineering_activity_details  (source backend: files)
Source table engineering.activity_details.

- `activity_details_id`: integer NOT NULL — Column activity_details_id of table engineering.activity_details.
- `activity_details_name`: text NOT NULL — Column activity_details_name of table engineering.activity_details.
- primary key: activity_details_id

### engineering_activity_schedule  (source backend: rest)
Source table engineering.activity_schedule.

- `activity_schedule_date`: integer NOT NULL — Column activity_schedule_date of table engineering.activity_schedule.
- `activity_schedule_end_time`: integer NOT NULL — Column activity_schedule_end_time of table engineering.activity_schedule.
- `activity_schedule_id`: integer NOT NULL — Column activity_schedule_id of table engineering.activity_schedule.
- `activity_schedule_start_time`: integer NOT NULL — Column activity_schedule_start_time of table engineering.activity_schedule.
- primary key: activity_schedule_id

### engineering_admin  (source backend: mongodb)
Source table engineering.admin.

- `admin_id`: integer NOT NULL — Column admin_id of table engineering.admin.
- `firstname`: text NOT NULL — Column firstname of table engineering.admin.
- `image_path`: text NOT NULL — Column image_path of table engineering.admin.
- `lastname`: text NOT NULL — Column lastname of table engineering.admin.
- `midname`: text NOT NULL — Column midname of table engineering.admin.
- `password`: text NOT NULL — Column password of table engineering.admin.
- `username`: text NOT NULL — Column username of table engineering.admin.
- primary key: admin_id

### engineering_announcement  (source backend: mongodb)
Source table engineering.announcement.

- `announcement_announcer`: text NOT NULL — Column announcement_announcer of table engineering.announcement.
- `announcement_audience`: text NOT NULL — Column announcement_audience of table engineering.announcement.
- `announcement_content`: text NOT NULL — Column announcement_content of table engineering.announcement.
- `announcement_created_at`: integer NOT NULL — Column announcement_created_at of table engineering.announcement.
- `announcement_edited_at`: integer NOT NULL — Column announcement_edited_at of table engineering.announcement.
- `announcement_end_datetime`: integer NOT NULL — Column announcement_end_datetime of table engineering.announcement.
- `announcement_id`: integer NOT NULL — Column announcement_id of table engineering.announcement.
- `announcement_is_active`: integer NOT NULL — Column announcement_is_active of table engineering.announcement.
- `announcement_start_datetime`: integer NOT NULL — Column announcement_start_datetime of table engineering.announcement.
- `announcement_title`: text NOT NULL — Column announcement_title of table engineering.announcement.
- primary key: announcement_id

### engineering_attendance_in  (source backend: files)
Source table engineering.attendance_in.

- `attendance_in_id`: integer NOT NULL — Column attendance_in_id of table engineering.attendance_in.
- `attendance_in_time`: integer NOT NULL — Column attendance_in_time of table engineering.attendance_in.
- `lecturer_attendance_id`: integer NOT NULL — Column lecturer_attendance_id of table engineering.attendance_in.
- primary key: attendance_in_id

### engineering_attendance_out  (source backend: postgres)
Source table engineering.attendance_out.

- `attendance_out_id`: integer NOT NULL — Column attendance_out_id of table engineering.attendance_out.
- `attendance_out_time`: integer NOT NULL — Column attendance_out_time of table engineering.attendance_out.
- `lecturer_attendance_id`: integer NOT NULL — Column lecturer_attendance_id of table engineering.attendance_out.
- primary key: attendance_out_id

### engineering_choice  (source backend: s3)
Source table engineering.choice.

- `choice_choice`: text NOT NULL — Column choice_choice of table engineering.choice.
- `choice_id`: integer NOT NULL — Column choice_id of table engineering.choice.
- `choice_is_answer`: integer NOT NULL — Column choice_is_answer of table engineering.choice.
- `courseware_question_id`: integer NOT NULL — Column courseware_question_id of table engineering.choice.
- primary key: choice_id

### engineering_comment  (source backend: files)
Source table engineering.comment.

- `comment_content`: text NOT NULL — Column comment_content of table engineering.comment.
- `comment_id`: integer NOT NULL — Column comment_id of table engineering.comment.
- `comment_user_id`: integer NOT NULL — Column comment_user_id of table engineering.comment.
- `courseware_question_id`: integer NOT NULL — Column courseware_question_id of table engineering.comment.
- primary key: comment_id

### engineering_course  (source backend: files)
Source table engineering.course.

- `course_course_code`: text NOT NULL — Column course_course_code of table engineering.course.
- `course_course_title`: text NOT NULL — Column course_course_title of table engineering.course.
- `course_department`: text NOT NULL — Column course_department of table engineering.course.
- `course_id`: integer NOT NULL — Column course_id of table engineering.course.
- `course_is_active`: integer NOT NULL — Column course_is_active of table engineering.course.
- `enrollment_id`: integer NOT NULL — Column enrollment_id of table engineering.course.
- `professor_id`: integer NOT NULL — Column professor_id of table engineering.course.
- `year_level_id`: integer NOT NULL — Column year_level_id of table engineering.course.
- primary key: course_id

### engineering_course_modules  (source backend: mongodb)
Source table engineering.course_modules.

- `course_modules_id`: integer NOT NULL — Column course_modules_id of table engineering.course_modules.
- `course_modules_name`: text NOT NULL — Column course_modules_name of table engineering.course_modules.
- `course_modules_path`: text NOT NULL — Column course_modules_path of table engineering.course_modules.
- `course_modules_status`: integer NOT NULL — Column course_modules_status of table engineering.course_modules.
- `topic_id`: integer NOT NULL — Column topic_id of table engineering.course_modules.
- primary key: course_modules_id

### engineering_courseware  (source backend: files)
Source table engineering.courseware.

- `courseware_date_added`: integer NOT NULL — Column courseware_date_added of table engineering.courseware.
- `courseware_date_edited`: integer NOT NULL — Column courseware_date_edited of table engineering.courseware.
- `courseware_description`: text NULL — Column courseware_description of table engineering.courseware.
- `courseware_id`: integer NOT NULL — Column courseware_id of table engineering.courseware.
- `courseware_name`: text NOT NULL — Column courseware_name of table engineering.courseware.
- `courseware_status`: integer NULL — Column courseware_status of table engineering.courseware.
- `topic_id`: integer NOT NULL — Column topic_id of table engineering.courseware.
- primary key: courseware_id

### engineering_courseware_question  (source backend: postgres)
Source table engineering.courseware_question.

- `courseware_id`: integer NOT NULL — Column courseware_id of table engineering.courseware_question.
- `courseware_question_id`: integer NOT NULL — Column courseware_question_id of table engineering.courseware_question.
- `courseware_question_question`: text NOT NULL — Column courseware_question_question of table engineering.courseware_question.
- `courseware_question_status`: integer NOT NULL — Column courseware_question_status of table engineering.courseware_question.
- primary key: courseware_question_id

### engineering_courseware_time  (source backend: files)
Source table engineering.courseware_time.

- `courseware_time_id`: integer NOT NULL — Column courseware_time_id of table engineering.courseware_time.
- `courseware_time_time`: text NOT NULL — Column courseware_time_time of table engineering.courseware_time.
- `grade_assessment_id`: integer NOT NULL — Column grade_assessment_id of table engineering.courseware_time.
- primary key: courseware_time_id

### engineering_data_scores  (source backend: files)
Source table engineering.data_scores.

- `data_scores_id`: integer NOT NULL — Column data_scores_id of table engineering.data_scores.
- `data_scores_passing`: integer NOT NULL — Column data_scores_passing of table engineering.data_scores.
- `data_scores_score`: integer NOT NULL — Column data_scores_score of table engineering.data_scores.
- `data_scores_type`: integer NOT NULL — Column data_scores_type of table engineering.data_scores.
- primary key: data_scores_id

### engineering_enrollment  (source backend: postgres)
Source table engineering.enrollment.

- `enrollment_id`: integer NOT NULL — Column enrollment_id of table engineering.enrollment.
- `enrollment_is_active`: integer NOT NULL — Column enrollment_is_active of table engineering.enrollment.
- `enrollment_sy`: text NOT NULL — Column enrollment_sy of table engineering.enrollment.
- `enrollment_term`: integer NOT NULL — Column enrollment_term of table engineering.enrollment.
- `passingpercentage`: integer NOT NULL — Column passingPercentage of table engineering.enrollment.
- primary key: enrollment_id

### engineering_fic  (source backend: s3)
Source table engineering.fic.

- `email`: text NOT NULL — Column email of table engineering.fic.
- `fic_department`: text NOT NULL — Column fic_department of table engineering.fic.
- `fic_id`: integer NOT NULL — Column fic_id of table engineering.fic.
- `fic_status`: integer NOT NULL — Column fic_status of table engineering.fic.
- `firstname`: text NOT NULL — Column firstname of table engineering.fic.
- `image_path`: text NOT NULL — Column image_path of table engineering.fic.
- `lastname`: text NOT NULL — Column lastname of table engineering.fic.
- `midname`: text NOT NULL — Column midname of table engineering.fic.
- `password`: text NOT NULL — Column password of table engineering.fic.
- `username`: text NOT NULL — Column username of table engineering.fic.
- primary key: fic_id

### engineering_grade_assessment  (source backend: postgres)
Source table engineering.grade_assessment.

- `courseware_id`: integer NOT NULL — Column courseware_id of table engineering.grade_assessment.
- `grade_assessment_id`: integer NOT NULL — Column grade_assessment_id of table engineering.grade_assessment.
- `grade_assessment_score`: integer NOT NULL — Column grade_assessment_score of table engineering.grade_assessment.
- `grade_assessment_total`: integer NOT NULL — Column grade_assessment_total of table engineering.grade_assessment.
- `student_id`: integer NOT NULL — Column student_id of table engineering.grade_assessment.
- primary key: grade_assessment_id

### engineering_lecturer  (source backend: mongodb)
Source table engineering.lecturer.

- `email`: text NOT NULL — Column email of table engineering.lecturer.
- `firstname`: text NOT NULL — Column firstname of table engineering.lecturer.
- `id_number`: integer NULL — Column id_number of table engineering.lecturer.
- `image_path`: text NOT NULL — Column image_path of table engineering.lecturer.
- `lastname`: text NOT NULL — Column lastname of table engineering.lecturer.
- `lecturer_expertise`: text NOT NULL — Column lecturer_expertise of table engineering.lecturer.
- `lecturer_id`: integer NOT NULL — Column lecturer_id of table engineering.lecturer.
- `lecturer_is_confirm`: integer NOT NULL — Column lecturer_is_confirm of table engineering.lecturer.
- `lecturer_status`: integer NOT NULL — Column lecturer_status of table engineering.lecturer.
- `midname`: text NOT NULL — Column midname of table engineering.lecturer.
- primary key: lecturer_id

### engineering_lecturer_attendance  (source backend: files)
Source table engineering.lecturer_attendance.

- `lecturer_attendance_date`: integer NOT NULL — Column lecturer_attendance_date of table engineering.lecturer_attendance.
- `lecturer_attendance_id`: integer NOT NULL — Column lecturer_attendance_id of table engineering.lecturer_attendance.
- `lecturer_id`: integer NOT NULL — Column lecturer_id of table engineering.lecturer_attendance.
- `offering_id`: integer NOT NULL — Column offering_id of table engineering.lecturer_attendance.
- `schedule_id`: integer NOT NULL — Column schedule_id of table engineering.lecturer_attendance.
- primary key: lecturer_attendance_id

### engineering_lecturer_feedback  (source backend: postgres)
Source table engineering.lecturer_feedback.

- `enrollment_id`: integer NOT NULL — Column enrollment_id of table engineering.lecturer_feedback.
- `lecturer_feedback_comment`: text NOT NULL — Column lecturer_feedback_comment of table engineering.lecturer_feedback.
- `lecturer_feedback_department`: text NOT NULL — Column lecturer_feedback_department of table engineering.lecturer_feedback.
- `lecturer_feedback_id`: integer NOT NULL — Column lecturer_feedback_id of table engineering.lecturer_feedback.
- `lecturer_feedback_timedate`: integer NOT NULL — Column lecturer_feedback_timedate of table engineering.lecturer_feedback.
- `lecturer_id`: integer NOT NULL — Column lecturer_id of table engineering.lecturer_feedback.
- `offering_id`: integer NOT NULL — Column offering_id of table engineering.lecturer_feedback.
- `student_id`: integer NOT NULL — Column student_id of table engineering.lecturer_feedback.
- primary key: lecturer_feedback_id

### engineering_log  (source backend: s3)
Source table engineering.log.

- `log_content_id`: integer NOT NULL — Column log_content_id of table engineering.log.
- `log_id`: integer NOT NULL — Column log_id of table engineering.log.
- `log_platform`: integer NOT NULL — Column log_platform of table engineering.log.
- `log_timedate`: integer NOT NULL — Column log_timedate of table engineering.log.
- `log_user_id`: integer NOT NULL — Column log_user_id of table engineering.log.
- primary key: log_id

### engineering_log_content  (source backend: files)
Source table engineering.log_content.

- `log_content_id`: integer NOT NULL — Column log_content_id of table engineering.log_content.
- `log_content_name`: text NOT NULL — Column log_content_name of table engineering.log_content.
- primary key: log_content_id

### engineering_login_sessions  (source backend: postgres)
Source table engineering.login_sessions.

- `login_sessions_id`: integer NOT NULL — Column login_sessions_id of table engineering.login_sessions.
- `login_sessions_identifier`: integer NOT NULL — Column login_sessions_identifier of table engineering.login_sessions.
- `login_sessions_status`: integer NOT NULL — Column login_sessions_status of table engineering.login_sessions.
- primary key: login_sessions_id

### engineering_offering  (source backend: mongodb)
Source table engineering.offering.

- `course_id`: integer NOT NULL — Column course_id of table engineering.offering.
- `fic_id`: integer NOT NULL — Column fic_id of table engineering.offering.
- `offering_department`: text NOT NULL — Column offering_department of table engineering.offering.
- `offering_id`: integer NOT NULL — Column offering_id of table engineering.offering.
- `offering_name`: text NOT NULL — Column offering_name of table engineering.offering.
- primary key: offering_id

### engineering_professor  (source backend: mongodb)
Source table engineering.professor.

- `email`: text NOT NULL — Column email of table engineering.professor.
- `firstname`: text NOT NULL — Column firstname of table engineering.professor.
- `image_path`: text NOT NULL — Column image_path of table engineering.professor.
- `lastname`: text NOT NULL — Column lastname of table engineering.professor.
- `midname`: text NOT NULL — Column midname of table engineering.professor.
- `password`: text NOT NULL — Column password of table engineering.professor.
- `professor_department`: text NOT NULL — Column professor_department of table engineering.professor.
- `professor_feedback_active`: integer NOT NULL — Column professor_feedback_active of table engineering.professor.
- `professor_id`: integer NOT NULL — Column professor_id of table engineering.professor.
- `professor_status`: integer NOT NULL — Column professor_status of table engineering.professor.
- `username`: text NOT NULL — Column username of table engineering.professor.
- primary key: professor_id

### engineering_remedial_coursewares  (source backend: rest)
Source table engineering.remedial_coursewares.

- `courseware_id`: integer NOT NULL — Column courseware_id of table engineering.remedial_coursewares.
- `is_done`: integer NOT NULL — Column is_done of table engineering.remedial_coursewares.
- `remedial_coursewares_id`: integer NOT NULL — Column remedial_coursewares_id of table engineering.remedial_coursewares.
- `student_scores_id`: integer NOT NULL — Column student_scores_id of table engineering.remedial_coursewares.
- primary key: remedial_coursewares_id

### engineering_remedial_grade_assessment  (source backend: mongodb)
Source table engineering.remedial_grade_assessment.

- `courseware_id`: integer NOT NULL — Column courseware_id of table engineering.remedial_grade_assessment.
- `remedial_coursewares_id`: integer NOT NULL — Column remedial_coursewares_id of table engineering.remedial_grade_assessment.
- `remedial_grade_assessment_id`: integer NOT NULL — Column remedial_grade_assessment_id of table engineering.remedial_grade_assessment.
- `remedial_grade_assessment_score`: integer NOT NULL — Column remedial_grade_assessment_score of table engineering.remedial_grade_assessment.
- `remedial_grade_assessment_time`: text NOT NULL — Column remedial_grade_assessment_time of table engineering.remedial_grade_assessment.
- `remedial_grade_assessment_total`: integer NOT NULL — Column remedial_grade_assessment_total of table engineering.remedial_grade_assessment.
- `student_id`: integer NOT NULL — Column student_id of table engineering.remedial_grade_assessment.
- primary key: remedial_grade_assessment_id

### engineering_remedial_student_answer  (source backend: postgres)
Source table engineering.remedial_student_answer.

- `choice_id`: integer NOT NULL — Column choice_id of table engineering.remedial_student_answer.
- `choice_is_correct`: integer NOT NULL — Column choice_is_correct of table engineering.remedial_student_answer.
- `courseware_id`: integer NOT NULL — Column courseware_id of table engineering.remedial_student_answer.
- `courseware_question_id`: integer NOT NULL — Column courseware_question_id of table engineering.remedial_student_answer.
- `remedial_student_answer_id`: integer NOT NULL — Column remedial_student_answer_id of table engineering.remedial_student_answer.
- `student_id`: integer NOT NULL — Column student_id of table engineering.remedial_student_answer.
- primary key: remedial_student_answer_id

### engineering_schedule  (source backend: mongodb)
Source table engineering.schedule.

- `lecturer_id`: integer NULL — Column lecturer_id of table engineering.schedule.
- `offering_id`: integer NOT NULL — Column offering_id of table engineering.schedule.
- `schedule_end_time`: integer NOT NULL — Column schedule_end_time of table engineering.schedule.
- `schedule_id`: integer NOT NULL — Column schedule_id of table engineering.schedule.
- `schedule_start_time`: integer NOT NULL — Column schedule_start_time of table engineering.schedule.
- `schedule_venue`: text NOT NULL — Column schedule_venue of table engineering.schedule.
- primary key: schedule_id

### engineering_student  (source backend: files)
Source table engineering.student.

- `email`: text NOT NULL — Column email of table engineering.student.
- `firstname`: text NOT NULL — Column firstname of table engineering.student.
- `image_path`: text NOT NULL — Column image_path of table engineering.student.
- `lastname`: text NOT NULL — Column lastname of table engineering.student.
- `midname`: text NOT NULL — Column midname of table engineering.student.
- `offering_id`: integer NOT NULL — Column offering_id of table engineering.student.
- `password`: text NOT NULL — Column password of table engineering.student.
- `student_department`: text NOT NULL — Column student_department of table engineering.student.
- `student_id`: integer NOT NULL — Column student_id of table engineering.student.
- `student_is_blocked`: integer NOT NULL — Column student_is_blocked of table engineering.student.
- `student_num`: integer NOT NULL — Column student_num of table engineering.student.
- `token`: text NULL — Column token of table engineering.student.
- `username`: text NOT NULL — Column username of table engineering.student.
- primary key: student_id

### engineering_student_answer  (source backend: s3)
Source table engineering.student_answer.

- `choice_id`: integer NOT NULL — Column choice_id of table engineering.student_answer.
- `choice_is_correct`: integer NOT NULL — Column choice_is_correct of table engineering.student_answer.
- `courseware_id`: integer NOT NULL — Column courseware_id of table engineering.student_answer.
- `courseware_question_id`: integer NOT NULL — Column courseware_question_id of table engineering.student_answer.
- `student_answer_id`: integer NOT NULL — Column student_answer_id of table engineering.student_answer.
- `student_id`: integer NOT NULL — Column student_id of table engineering.student_answer.
- primary key: student_answer_id

### engineering_student_scores  (source backend: s3)
Source table engineering.student_scores.

- `course_id`: integer NOT NULL — Column course_id of table engineering.student_scores.
- `data_scores_id`: integer NOT NULL — Column data_scores_id of table engineering.student_scores.
- `student_scores_id`: integer NOT NULL — Column student_scores_id of table engineering.student_scores.
- `student_scores_is_failed`: integer NOT NULL — Column student_scores_is_failed of table engineering.student_scores.
- `student_scores_score`: integer NOT NULL — Column student_scores_score of table engineering.student_scores.
- `student_scores_stud_num`: integer NOT NULL — Column student_scores_stud_num of table engineering.student_scores.
- `student_scores_topic_id`: text NOT NULL — Column student_scores_topic_id of table engineering.student_scores.
- primary key: student_scores_id

### engineering_subject  (source backend: postgres)
Source table engineering.subject.

- `course_id`: integer NOT NULL — Column course_id of table engineering.subject.
- `lecturer_id`: integer NULL — Column lecturer_id of table engineering.subject.
- `subject_description`: text NULL — Column subject_description of table engineering.subject.
- `subject_id`: integer NOT NULL — Column subject_id of table engineering.subject.
- `subject_list_id`: integer NULL — Column subject_list_id of table engineering.subject.
- `subject_name`: text NOT NULL — Column subject_name of table engineering.subject.
- primary key: subject_id

### engineering_subject_list  (source backend: mongodb)
Source table engineering.subject_list.

- `subject_list_department`: text NOT NULL — Column subject_list_department of table engineering.subject_list.
- `subject_list_description`: text NULL — Column subject_list_description of table engineering.subject_list.
- `subject_list_id`: integer NOT NULL — Column subject_list_id of table engineering.subject_list.
- `subject_list_is_active`: integer NOT NULL — Column subject_list_is_active of table engineering.subject_list.
- `subject_list_name`: text NOT NULL — Column subject_list_name of table engineering.subject_list.
- `year_level_id`: integer NOT NULL — Column year_level_id of table engineering.subject_list.
- primary key: subject_list_id

### engineering_subject_list_has_topic_list  (source backend: mongodb)
Source table engineering.subject_list_has_topic_list.

- `subject_list_id`: integer NOT NULL — Column subject_list_id of table engineering.subject_list_has_topic_list.
- `topic_list_id`: integer NOT NULL — Column topic_list_id of table engineering.subject_list_has_topic_list.
- primary key: subject_list_id, topic_list_id

### engineering_topic  (source backend: files)
Source table engineering.topic.

- `subject_id`: integer NOT NULL — Column subject_id of table engineering.topic.
- `topic_description`: text NULL — Column topic_description of table engineering.topic.
- `topic_done`: integer NOT NULL — Column topic_done of table engineering.topic.
- `topic_id`: integer NOT NULL — Column topic_id of table engineering.topic.
- `topic_list_id`: integer NULL — Column topic_list_id of table engineering.topic.
- `topic_name`: text NOT NULL — Column topic_name of table engineering.topic.
- primary key: topic_id

### engineering_total_grade  (source backend: mongodb)
Source table engineering.total_grade.

- `student_id`: integer NOT NULL — Column student_id of table engineering.total_grade.
- `subject_id`: integer NOT NULL — Column subject_id of table engineering.total_grade.
- `total_grade_id`: integer NOT NULL — Column total_grade_id of table engineering.total_grade.
- `total_grade_total`: text NOT NULL — Column total_grade_total of table engineering.total_grade.
- primary key: total_grade_id

### engineering_year_level  (source backend: mongodb)
Source table engineering.year_level.

- `year_level_id`: integer NOT NULL — Column year_level_id of table engineering.year_level.
- `year_level_name`: text NOT NULL — Column year_level_name of table engineering.year_level.
- primary key: year_level_id

### Relationships

- engineering_activity(activity_details_id) -> engineering_activity_details(activity_details_id) [required]
- engineering_activity(activity_schedule_id) -> engineering_activity_schedule(activity_schedule_id) [required]
- engineering_activity(lecturer_id) -> engineering_lecturer(lecturer_id) [optional (may be NULL/dangling)]
- engineering_activity(offering_id) -> engineering_offering(offering_id) [required]
- engineering_attendance_in(lecturer_attendance_id) -> engineering_lecturer_attendance(lecturer_attendance_id) [required]
- engineering_attendance_out(lecturer_attendance_id) -> engineering_lecturer_attendance(lecturer_attendance_id) [required]
- engineering_choice(courseware_question_id) -> engineering_courseware_question(courseware_question_id) [required]
- engineering_comment(courseware_question_id) -> engineering_courseware_question(courseware_question_id) [required]
- engineering_course(enrollment_id) -> engineering_enrollment(enrollment_id) [required]
- engineering_course(professor_id) -> engineering_professor(professor_id) [required]
- engineering_course(year_level_id) -> engineering_year_level(year_level_id) [required]
- engineering_course_modules(topic_id) -> engineering_topic(topic_id) [required]
- engineering_courseware(topic_id) -> engineering_topic(topic_id) [required]
- engineering_courseware_question(courseware_id) -> engineering_courseware(courseware_id) [required]
- engineering_courseware_time(grade_assessment_id) -> engineering_grade_assessment(grade_assessment_id) [required]
- engineering_grade_assessment(courseware_id) -> engineering_courseware(courseware_id) [required]
- engineering_grade_assessment(student_id) -> engineering_student(student_id) [required]
- engineering_lecturer_attendance(lecturer_id) -> engineering_lecturer(lecturer_id) [required]
- engineering_lecturer_attendance(offering_id) -> engineering_offering(offering_id) [required]
- engineering_lecturer_attendance(schedule_id) -> engineering_schedule(schedule_id) [required]
- engineering_lecturer_feedback(enrollment_id) -> engineering_enrollment(enrollment_id) [required]
- engineering_lecturer_feedback(lecturer_id) -> engineering_lecturer(lecturer_id) [required]
- engineering_lecturer_feedback(offering_id) -> engineering_offering(offering_id) [required]
- engineering_lecturer_feedback(student_id) -> engineering_student(student_id) [required]
- engineering_log(log_content_id) -> engineering_log_content(log_content_id) [required]
- engineering_offering(course_id) -> engineering_course(course_id) [required]
- engineering_offering(fic_id) -> engineering_fic(fic_id) [required]
- engineering_remedial_coursewares(courseware_id) -> engineering_courseware(courseware_id) [required]
- engineering_remedial_coursewares(student_scores_id) -> engineering_student_scores(student_scores_id) [required]
- engineering_remedial_grade_assessment(courseware_id) -> engineering_courseware(courseware_id) [required]
- engineering_remedial_grade_assessment(remedial_coursewares_id) -> engineering_remedial_coursewares(remedial_coursewares_id) [required]
- engineering_remedial_grade_assessment(student_id) -> engineering_student(student_id) [required]
- engineering_remedial_student_answer(choice_id) -> engineering_choice(choice_id) [required]
- engineering_remedial_student_answer(courseware_id) -> engineering_courseware(courseware_id) [required]
- engineering_remedial_student_answer(courseware_question_id) -> engineering_courseware_question(courseware_question_id) [required]
- engineering_remedial_student_answer(student_id) -> engineering_student(student_id) [required]
- engineering_schedule(lecturer_id) -> engineering_lecturer(lecturer_id) [optional (may be NULL/dangling)]
- engineering_schedule(offering_id) -> engineering_offering(offering_id) [required]
- engineering_student(offering_id) -> engineering_offering(offering_id) [required]
- engineering_student_answer(choice_id) -> engineering_choice(choice_id) [required]
- engineering_student_answer(courseware_id) -> engineering_courseware(courseware_id) [required]
- engineering_student_answer(courseware_question_id) -> engineering_courseware_question(courseware_question_id) [required]
- engineering_student_answer(student_id) -> engineering_student(student_id) [required]
- engineering_student_scores(course_id) -> engineering_course(course_id) [required]
- engineering_student_scores(data_scores_id) -> engineering_data_scores(data_scores_id) [required]
- engineering_subject(course_id) -> engineering_course(course_id) [required]
- engineering_subject(lecturer_id) -> engineering_lecturer(lecturer_id) [optional (may be NULL/dangling)]
- engineering_subject(subject_list_id) -> engineering_subject_list(subject_list_id) [optional (may be NULL/dangling)]
- engineering_subject_list(year_level_id) -> engineering_year_level(year_level_id) [required]
- engineering_subject_list_has_topic_list(subject_list_id) -> engineering_subject_list(subject_list_id) [required]
- engineering_topic(subject_id) -> engineering_subject(subject_id) [required]
- engineering_total_grade(student_id) -> engineering_student(student_id) [required]
- engineering_total_grade(subject_id) -> engineering_subject(subject_id) [required]

