# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Dennewbie Esse4

## Specification

PROJECT OVERVIEW

This project builds three analytical marts over a university records schema (the 659689_DDL_esse4.sql schema). Only a few of the source tables are actually needed by the marts, but the whole source landscape is published here so that every table is read from the extraction backend it lives in. Each source table below must be extracted from the backend named beside it.

Source tables and their extraction backends:
- Source table appello must be extracted from the files backend.
- Source table appello_laurea must be extracted from the postgres backend.
- Source table assegnazione_borse must be extracted from the mongodb backend.
- Source table assegnazione_erasmus must be extracted from the files backend.
- Source table azienda must be extracted from the mongodb backend.
- Source table bando_borsa must be extracted from the s3 backend.
- Source table bando_erasmus must be extracted from the s3 backend.
- Source table corso_laurea must be extracted from the rest backend.
- Source table docente must be extracted from the files backend.
- Source table edizione_insegnamento must be extracted from the rest backend.
- Source table email_docente must be extracted from the rest backend.
- Source table email_studente must be extracted from the mongodb backend.
- Source table email_tutor_aziendale must be extracted from the postgres backend.
- Source table esame_superato must be extracted from the mongodb backend.
- Source table frequenta_edizione_insegnamento must be extracted from the s3 backend.
- Source table insegna_edizione must be extracted from the files backend.
- Source table insegnamento must be extracted from the mongodb backend.
- Source table offerta_insegnamento must be extracted from the rest backend.
- Source table orario_lezioni must be extracted from the files backend.
- Source table partecipa_seduta must be extracted from the postgres backend.
- Source table partecipa_seminario must be extracted from the postgres backend.
- Source table partecipazione_bando_borsa must be extracted from the postgres backend.
- Source table partecipazione_bando_erasmus must be extracted from the postgres backend.
- Source table prenotazione_appello must be extracted from the mongodb backend.
- Source table prenotazione_appello_seduta must be extracted from the rest backend.
- Source table prenotazione_ricevimento must be extracted from the postgres backend.
- Source table presiede_appello must be extracted from the s3 backend.
- Source table questionario must be extracted from the files backend.
- Source table relatore must be extracted from the mongodb backend.
- Source table ricevimento must be extracted from the mongodb backend.
- Source table seduta_laurea must be extracted from the mongodb backend.
- Source table seminario must be extracted from the s3 backend.
- Source table studente must be extracted from the files backend.
- Source table tassa must be extracted from the files backend.
- Source table telefono_docente must be extracted from the postgres backend.
- Source table telefono_studente must be extracted from the files backend.
- Source table telefono_tutor_aziendale must be extracted from the postgres backend.
- Source table tirocinio must be extracted from the mongodb backend.
- Source table tutor_aziendale must be extracted from the s3 backend.

Relationships between the source tables. Each line states the child table with its child keys, the parent table with its parent keys, and whether the relationship is required or optional.
- Child table appello with keys (codice_insegnamento, anno_accademico) refers to parent table edizione_insegnamento with keys (codice_insegnamento, anno_accademico); this relationship is required.
- Child table appello_laurea with key (codice_corso) refers to parent table corso_laurea with key (codice_corso); this relationship is required.
- Child table assegnazione_borse with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table assegnazione_borse with key (numero_bando_borsa) refers to parent table bando_borsa with key (numero_bando_borsa); this relationship is required.
- Child table assegnazione_erasmus with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table assegnazione_erasmus with key (numero_bando_erasmus) refers to parent table bando_erasmus with key (numero_bando_erasmus); this relationship is required.
- Child table edizione_insegnamento with key (codice_insegnamento) refers to parent table insegnamento with key (codice_insegnamento); this relationship is required.
- Child table email_docente with key (tesserino_docente) refers to parent table docente with key (numero_tesserino); this relationship is required.
- Child table email_studente with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table email_tutor_aziendale with key (tesserino_tutor_azienda) refers to parent table tutor_aziendale with key (numero_tesserino); this relationship is required.
- Child table esame_superato with keys (codice_insegnamento, anno_accademico, data_esame) refers to parent table appello with keys (codice_insegnamento, anno_accademico, data_appello); this relationship is optional (the child values may be missing or may point at no parent row).
- Child table esame_superato with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table frequenta_edizione_insegnamento with keys (codice_insegnamento, anno_accademico) refers to parent table edizione_insegnamento with keys (codice_insegnamento, anno_accademico); this relationship is required.
- Child table frequenta_edizione_insegnamento with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table insegna_edizione with keys (codice_insegnamento, anno_accademico) refers to parent table edizione_insegnamento with keys (codice_insegnamento, anno_accademico); this relationship is required.
- Child table insegna_edizione with key (tesserino_docente) refers to parent table docente with key (numero_tesserino); this relationship is required.
- Child table offerta_insegnamento with key (codice_corso) refers to parent table corso_laurea with key (codice_corso); this relationship is required.
- Child table offerta_insegnamento with key (codice_insegnamento) refers to parent table insegnamento with key (codice_insegnamento); this relationship is required.
- Child table orario_lezioni with keys (codice_insegnamento, anno_accademico) refers to parent table edizione_insegnamento with keys (codice_insegnamento, anno_accademico); this relationship is required.
- Child table partecipa_seduta with keys (codice_corso, data_appello) refers to parent table appello_laurea with keys (codice_corso, data_appello); this relationship is required.
- Child table partecipa_seduta with key (tesserino_docente) refers to parent table docente with key (numero_tesserino); this relationship is required.
- Child table partecipa_seminario with keys (data_seminario, tesserino_docente) refers to parent table seminario with keys (data_seminario, tesserino_docente); this relationship is required.
- Child table partecipa_seminario with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table partecipazione_bando_borsa with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table partecipazione_bando_borsa with key (numero_bando_borsa) refers to parent table bando_borsa with key (numero_bando_borsa); this relationship is required.
- Child table partecipazione_bando_erasmus with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table partecipazione_bando_erasmus with key (numero_bando_erasmus) refers to parent table bando_erasmus with key (numero_bando_erasmus); this relationship is required.
- Child table prenotazione_appello with keys (codice_insegnamento, anno_accademico, data_appello) refers to parent table appello with keys (codice_insegnamento, anno_accademico, data_appello); this relationship is required.
- Child table prenotazione_appello with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table prenotazione_appello_seduta with keys (codice_corso, data_appello) refers to parent table appello_laurea with keys (codice_corso, data_appello); this relationship is required.
- Child table prenotazione_appello_seduta with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table prenotazione_ricevimento with keys (data_ricevimento, tesserino_docente) refers to parent table ricevimento with keys (data_ricevimento, tesserino_docente); this relationship is required.
- Child table prenotazione_ricevimento with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table presiede_appello with keys (codice_insegnamento, anno_accademico, data_appello) refers to parent table appello with keys (codice_insegnamento, anno_accademico, data_appello); this relationship is required.
- Child table presiede_appello with key (tesserino_docente) refers to parent table docente with key (numero_tesserino); this relationship is required.
- Child table questionario with keys (codice_insegnamento, anno_accademico) refers to parent table edizione_insegnamento with keys (codice_insegnamento, anno_accademico); this relationship is required.
- Child table questionario with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table questionario with key (tesserino_docente) refers to parent table docente with key (numero_tesserino); this relationship is required.
- Child table relatore with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table relatore with key (tesserino_docente) refers to parent table docente with key (numero_tesserino); this relationship is required.
- Child table ricevimento with key (tesserino_docente) refers to parent table docente with key (numero_tesserino); this relationship is required.
- Child table seduta_laurea with keys (codice_corso, data_seduta) refers to parent table appello_laurea with keys (codice_corso, data_appello); this relationship is optional (the child values may be missing or may point at no parent row).
- Child table seduta_laurea with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table seminario with key (tesserino_docente) refers to parent table docente with key (numero_tesserino); this relationship is required.
- Child table studente with key (codice_corso) refers to parent table corso_laurea with key (codice_corso); this relationship is required.
- Child table tassa with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table telefono_docente with key (tesserino_docente) refers to parent table docente with key (numero_tesserino); this relationship is required.
- Child table telefono_studente with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table telefono_tutor_aziendale with key (tesserino_tutor_azienda) refers to parent table tutor_aziendale with key (numero_tesserino); this relationship is required.
- Child table tirocinio with key (matricola_studente) refers to parent table studente with key (matricola_studente); this relationship is required.
- Child table tirocinio with key (tesserino_docente) refers to parent table docente with key (numero_tesserino); this relationship is required.
- Child table tirocinio with key (tesserino_tutor_azienda) refers to parent table tutor_aziendale with key (numero_tesserino); this relationship is optional (the child values may be missing or may point at no parent row).
- Child table tutor_aziendale with key (partita_iva) refers to parent table azienda with key (partita_iva); this relationship is required.

Conventions used throughout: every rounded fraction is rounded to 4 decimal places; text values are compared exactly as stored, case-sensitively; and every mart states its own deterministic output ordering.

=== Mart studente_seduta_laurea_cohorts — per-(studente, status cohort) summary of linked seduta_laurea activity in the 659689_DDL_esse4.sql schema ===

Grain: one row per (matricola_studente, status cohort) pair represented among linked seduta_laurea rows, plus one no-activity row for a studente row with no linked seduta_laurea row at all. A studente row whose linked seduta_laurea rows all lack a lode value is in no cohort and gets no no-activity row, so it has no row in this mart.

Key columns: entity_key and cohort together identify a row of this mart.

Output columns:
- entity_key: identifier of the studente row.
- cohort: 'passing' for lode values ['N']; 'failing' for values ['n']; 'no_activity' when the studente row has no linked seduta_laurea row. A linked seduta_laurea row whose lode has no value belongs to no cohort: it is not counted in any cell, and it does not make the studente row 'no_activity'. Cohort membership compares lode exactly as stored, case-sensitively: the uppercase value 'N' is the passing cohort and the lowercase value 'n' is the failing cohort, and the two are never folded together.
- entity_name: cap of the studente row, copied unchanged.
- link_count: number of seduta_laurea rows in this entity/cohort cell. Every seduta_laurea row assigned to the cell is counted, including a row that carries no voto value; only total_amount and max_amount read solely the rows that carry a voto value.
- distinct_status_count: the number of different lode values represented in this cell (the distinct status count of the cell).
- total_amount: total of voto in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an voto value.
- max_amount: largest voto in this cell; 0 for a no-activity cell that has no rows at all, and 0 when none of the cell's rows carries an voto value.
- max_amount_share: max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules that build this mart:
1. Source table studente is read.
2. Source table seduta_laurea is read.
3. From studente, each matricola_studente and its cap are carried into the cohort calculation as entity_key and entity_name.
4. The linked seduta_laurea rows are brought into each studente entity before assigning status cohorts, matching seduta_laurea on matricola_studente against entity_key, carrying entity_key, entity_name and matricola_studente; preservation is left-sided, so a studente entity with no matching seduta_laurea row is kept.
5. Rows whose lode belongs to the passing cohort values ['N'] are kept, carrying entity_key and entity_name.
6. There is one row per studente entity that has at least one linked seduta_laurea row in the passing cohort, reporting entity_key, entity_name, link_count as the number of those rows, distinct_status_count as how many different lode values occur among them, total_amount as the total of their voto, and max_amount as their largest voto. The total and the largest value read only the rows that carry a voto value; a cohort whose rows all lack one reports 0 for both, never empty.
7. For that passing cell, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.
8. These measures are labelled as the passing cohort, so the row carries entity_key, cohort with the value 'passing', entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
9. Rows whose lode belongs to the failing cohort values ['n'] are kept, carrying entity_key and entity_name.
10. There is one row per studente entity that has at least one linked seduta_laurea row in the failing cohort, reporting entity_key, entity_name, link_count as the number of those rows, distinct_status_count as how many different lode values occur among them, total_amount as the total of their voto, and max_amount as their largest voto. The total and the largest value read only the rows that carry a voto value; a cohort whose rows all lack one reports 0 for both, never empty.
11. For that failing cell, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.
12. These measures are labelled as the failing cohort, so the row carries entity_key, cohort with the value 'failing', entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
13. The placeholder row is kept, carrying entity_key and entity_name, for a studente entity with no linked seduta_laurea row at all; a studente entity that has linked seduta_laurea rows gets no placeholder, even when every one of those rows lacks a lode value.
14. There is one row per studente entity with no linked seduta_laurea row at all, carrying entity_key and entity_name and reporting link_count of 0 rows, distinct_status_count of 0 different lode values, a total_amount voto total of 0 and a max_amount largest voto of 0.
15. For that no-activity cell, carrying entity_key, entity_name, link_count, distinct_status_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.
16. These measures are labelled as the no_activity cohort, so the row carries entity_key, cohort with the value 'no_activity', entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
17. The disjoint passing and failing cohort summaries are combined, all rows of both kept, each carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
18. The no-activity summaries are added to that combination, all rows kept, so an entity with no linked rows is retained as one explicit cohort row carrying entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount and max_amount_share.
19. Deterministic output order: rows appear in ascending entity_key order for the entity, then in ascending cohort order.

=== Mart studente_tassa_snapshot — per-studente latest-row snapshot over linked tassa activity in the 659689_DDL_esse4.sql schema ===

Grain: one row per studente (matricola_studente), INCLUDING studente rows with no linked tassa rows.

Key column: parent_key identifies a row of this mart.

Output columns:
- parent_key: identifier of the studente row; one row per value.
- parent_name: cap of the studente row, copied unchanged.
- event_count: number of tassa rows for this studente row; 0 when there are none. A studente row kept with no tassa row reports 0 here, never 1: its placeholder holds no tassa row to count.
- lifetime_amount: total of importo over all matching tassa rows; 0 when there are no rows.
- latest_row_id: numero_fattura of the row with the latest scadenza; ties take the smallest numero_fattura under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one). It is the literal '(none)' when there are no rows.
- latest_amount: importo from that same latest row; 0 when there are no rows.
- latest_label: iuv from that same latest row; '(none)' when there are no rows or when the winning value is missing.
- latest_amount_share: latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0.

Rules that build this mart:
1. Source table studente is read.
2. Source table tassa is read.
3. From studente there is one row per studente row, keyed by matricola_studente, carrying parent_key and parent_name.
4. Rows of tassa are brought in, matching tassa on matricola_studente against parent_key and carrying matricola_studente; preservation is left-sided, so a studente row with no matching tassa row is retained and receives the stated empty snapshot values.
5. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split the rows of a key, reporting event_count and lifetime_amount for that row's matching rows.
6. For each parent_key the single row at which the ordering measure — the scadenza date — is largest survives, ties broken by the smallest numero_fattura under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), and latest_row_id, latest_amount and latest_label are taken from that winning row. The ordering measure is required on every real input row, so a non-empty set of rows for a parent_key always has a winning row; the declared defaults belong only to a parent_key with NO rows.
7. The extremal row's attributes are attached to the measures of the same parent_key; preservation is left-sided, so a parent_key with no rows at all keeps its measures.
8. The mart columns are named parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label; lifetime_amount reports its declared default of 0 — never NULL — for a parent_key with no matching rows.
9. Guarded ratio, carrying parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label: latest_amount_share is latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; it is 0.0 when lifetime_amount is 0 or has no value.
10. Deterministic output order: rows are sorted in ascending parent_key order.

=== Mart corso_laurea_studente_bands — per-corso_laurea banding of linked studente activity in the 659689_DDL_esse4.sql schema, over the value domain the schema itself declares ===

Grain: one row per corso_laurea (codice_corso), INCLUDING corso_laurea rows with no linked studente rows.

Key column: parent_key identifies a row of this mart.

Output columns:
- parent_key: identifier of the corso_laurea row; one row per value.
- parent_name: nome of the corso_laurea row, copied unchanged.
- parent_status: tipo of the corso_laurea row, copied unchanged. Declared domain: ['MAGISTRALE', 'TRIENNALE', 'magistrale', 'triennale', 'Magistrale', 'Triennale'].
- link_count: number of studente rows for this corso_laurea row; 0 when there are none. Every linked studente row counts, whatever its sesso value. A corso_laurea row kept with no studente row reports 0 here, never 1: its placeholder holds no studente row to count.
- passing_count: of those, how many have sesso in ['M']. 0, never missing, when none do.
- failing_count: how many have sesso in ['F', 'U', 'm', 'f', 'u']. 0 when none do.
- distinct_status_count: how many different sesso values occur among them (the distinct status count).
- passing_ratio: passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links.
- adoption_band: band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the HIGHER band.
- status_group: parent_status mapped value by value: 'MAGISTRALE' becomes 'active'; 'TRIENNALE' becomes 'other_1'; 'magistrale' becomes 'other_2'; 'triennale' becomes 'other_3'; 'Magistrale' becomes 'other_4'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never NULL.

Rules that build this mart:
1. Source table corso_laurea is read.
2. Source table studente is read.
3. From corso_laurea there is one row per corso_laurea row, keyed by codice_corso, carrying parent_key, parent_name and parent_status.
4. Rows of studente are brought in (hop 1 of 1), matching studente on codice_corso against parent_key and carrying codice_corso; preservation is left-sided, so rows with no matching studente row are RETAINED and report the declared defaults.
5. There is one output row per parent_key, carrying parent_name and parent_status beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split the rows of a key, reporting link_count, passing_count, failing_count and distinct_status_count for that row's matching rows. A parent_key with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.
6. The mart columns are named parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count.
7. Guarded ratio, carrying parent_key, parent_name, parent_status, link_count, passing_count, failing_count and distinct_status_count: passing_ratio is passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links.
8. adoption_band, carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count and passing_ratio, is the band of passing_ratio decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (a value exactly at 0.8 is 'high'), 'medium' from 0.5 up to but not including 0.8 (a value exactly at 0.5 is 'medium'), otherwise 'low' below 0.5; boundaries are inclusive of the HIGHER band, over the declared domain MAGISTRALE, TRIENNALE, magistrale, triennale, Magistrale, Triennale.
9. status_group, carried beside parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio and adoption_band, is parent_status mapped value by value over the declared domain MAGISTRALE, TRIENNALE, magistrale, triennale, Magistrale, Triennale: 'MAGISTRALE' becomes 'active'; 'TRIENNALE' becomes 'other_1'; 'magistrale' becomes 'other_2'; 'triennale' becomes 'other_3'; 'Magistrale' becomes 'other_4'; otherwise any other value — including legal values of the column that the mapping does not name — becomes 'unmapped', never NULL (this is a categorical mapping with no numeric boundary).
10. Deterministic output order: rows are sorted in ascending parent_key order.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `studente_seduta_laurea_cohorts`

- Grain: One row per (matricola_studente, status cohort) pair represented among linked seduta_laurea rows, plus one no-activity row for a studente row with no linked seduta_laurea row at all. A studente row whose linked seduta_laurea rows all lack a lode value is in no cohort and gets no no-activity row, so it has no row in this mart.
- Unique key: entity_key, cohort
- Required columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share

```text
Mart 'studente_seduta_laurea_cohorts' has 19 declared semantic rules:
1. [source] Read source table studente. (public source tables: studente)
2. [source] Read source table seduta_laurea. (public source tables: seduta_laurea)
3. [derive] Carry each matricola_studente and its cap into the cohort calculation. (public source tables: studente | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked seduta_laurea rows into each studente entity before assigning status cohorts. (public source tables: seduta_laurea | public carried/output columns: entity_key, entity_name, matricola_studente | join preservation: left | condition public identifiers: seduta_laurea, matricola_studente, entity_key)
5. [filter] Keep rows whose lode belongs to the passing cohort values ['N']. (public carried/output columns: entity_key, entity_name | condition literal specification values: N)
6. [distinct] One row per studente entity that has at least one linked seduta_laurea row in the passing cohort, reporting the number of those rows, how many different lode values occur among them, the total of their voto, and their largest voto. The total and the largest value read only the rows that carry a voto value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the passing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep rows whose lode belongs to the failing cohort values ['n']. (public carried/output columns: entity_key, entity_name | condition literal specification values: n)
10. [distinct] One row per studente entity that has at least one linked seduta_laurea row in the failing cohort, reporting the number of those rows, how many different lode values occur among them, the total of their voto, and their largest voto. The total and the largest value read only the rows that carry a voto value; a cohort whose rows all lack one reports 0 for both, never empty. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the failing cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
13. [filter] Keep the placeholder row for a studente entity with no linked seduta_laurea row at all; a studente entity that has linked seduta_laurea rows gets no placeholder, even when every one of those rows lacks a lode value. (public carried/output columns: entity_key, entity_name)
14. [distinct] One row per studente entity with no linked seduta_laurea row at all, reporting 0 rows, 0 different lode values, a voto total of 0 and a largest voto of 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount)
15. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
16. [derive] Label these measures as the no_activity cohort. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share)
17. [union] Combine the disjoint passing and failing cohort summaries. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
18. [union] Add the no-activity summaries, so an entity with no linked rows is retained as one explicit cohort row. (public carried/output columns: entity_key, cohort, entity_name, link_count, distinct_status_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
19. [tie_break] Deterministic output order: entity, then cohort. (public carried/output columns: entity_key, cohort)
```

### `studente_tassa_snapshot`

- Grain: One row per studente (matricola_studente), INCLUDING studente rows with no linked tassa rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share

```text
Mart 'studente_tassa_snapshot' has 10 declared semantic rules:
1. [source] Read source table studente. (public source tables: studente)
2. [source] Read source table tassa. (public source tables: tassa)
3. [derive] One row per studente row, keyed by matricola_studente. (public source tables: studente | public carried/output columns: parent_key, parent_name)
4. [join] Bring in tassa; a studente row with no matching tassa row is retained and receives the stated empty snapshot values. (public source tables: tassa | public carried/output columns: matricola_studente | join preservation: left | condition public identifiers: tassa, matricola_studente, parent_key)
5. [aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count, lifetime_amount for that row's matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount)
6. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest numero_fattura under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), and take latest_row_id, latest_amount, latest_label from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, latest_row_id, latest_amount, latest_label)
7. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
8. [derive] Name the mart columns; lifetime_amount reports its declared default — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label)
9. [ratio] Guarded ratios: latest_amount_share — latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `corso_laurea_studente_bands`

- Grain: One row per corso_laurea (codice_corso), INCLUDING corso_laurea rows with no linked studente rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group

```text
Mart 'corso_laurea_studente_bands' has 10 declared semantic rules:
1. [source] Read source table corso_laurea. (public source tables: corso_laurea)
2. [source] Read source table studente. (public source tables: studente)
3. [derive] One row per corso_laurea row, keyed by codice_corso. (public source tables: corso_laurea | public carried/output columns: parent_key, parent_name, parent_status)
4. [join] Bring in studente (hop 1 of 1): rows with no matching studente row are RETAINED and report the declared defaults. (public source tables: studente | public carried/output columns: codice_corso | join preservation: left | condition public identifiers: studente, codice_corso, parent_key)
5. [filtered_aggregate] One output row per parent_key, carrying parent_name, parent_status beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting link_count, passing_count, failing_count, distinct_status_count for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
6. [derive] Name the mart columns. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count)
7. [ratio] Guarded ratios: passing_ratio — passing_count divided by link_count as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when there are no links. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
8. [conditional] adoption_band — Band of passing_ratio, decided on the rounded passing_ratio value this mart reports: 'no_activity' when there are no links, 'high' at 0.8 or ABOVE (0.8 itself is high), 'medium' from 0.5 up to but not including 0.8 (0.5 itself is medium), 'low' below 0.5. Boundaries are inclusive of the HIGHER band. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band | semantic parameters: boundary=a value exactly at 0.8 is 'high'; a value exactly at 0.5 is 'medium'; domain=MAGISTRALE, TRIENNALE, magistrale, triennale, Magistrale, Triennale)
9. [conditional] status_group — parent_status mapped value by value: 'MAGISTRALE' becomes 'active'; 'TRIENNALE' becomes 'other_1'; 'magistrale' becomes 'other_2'; 'triennale' becomes 'other_3'; 'Magistrale' becomes 'other_4'; any other value — including legal values of the column that the mapping does not name — becomes 'unmapped'. Never NULL. (public carried/output columns: parent_key, parent_name, parent_status, link_count, passing_count, failing_count, distinct_status_count, passing_ratio, adoption_band, status_group | semantic parameters: boundary=categorical mapping; no numeric boundary; domain=MAGISTRALE, TRIENNALE, magistrale, triennale, Magistrale, Triennale)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### appello  (source backend: files)
Source table appello.

- `anno_accademico`: date NOT NULL — Column anno_accademico of table appello.
- `codice_insegnamento`: text NOT NULL — Column codice_insegnamento of table appello.
- `data_appello`: date NOT NULL — Column data_appello of table appello.
- `data_fine`: date NOT NULL — Column data_fine of table appello.
- `data_inizio`: date NOT NULL — Column data_inizio of table appello.
- `max_studenti`: decimal NOT NULL — Column max_studenti of table appello.
- `tipo`: text NULL — Column tipo of table appello. one of: Orale, ORALE, orale, scritto, Scritto, SCRITTO, Non previsto, NON PREVISTO, non previsto.
- primary key: anno_accademico, codice_insegnamento, data_appello

### appello_laurea  (source backend: postgres)
Source table appello_laurea.

- `codice_corso`: text NOT NULL — Column codice_corso of table appello_laurea.
- `data_appello`: date NOT NULL — Column data_appello of table appello_laurea.
- `fine_iscrizioni`: date NOT NULL — Column fine_iscrizioni of table appello_laurea.
- `inizio_iscrizioni`: date NOT NULL — Column inizio_iscrizioni of table appello_laurea.
- `max_iscrizioni`: decimal NOT NULL — Column max_iscrizioni of table appello_laurea.
- `tipo`: text NULL — Column tipo of table appello_laurea. one of: Presenza, Telematica, PRESENZA, TELEMATICA, presenza, telematica, Non previsto, NON PREVISTO, non previsto.
- primary key: codice_corso, data_appello

### assegnazione_borse  (source backend: mongodb)
Source table assegnazione_borse.

- `data_assegnazione`: date NOT NULL — Column data_assegnazione of table assegnazione_borse.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table assegnazione_borse.
- `numero_bando_borsa`: text NOT NULL — Column numero_bando_borsa of table assegnazione_borse.
- primary key: matricola_studente, numero_bando_borsa

### assegnazione_erasmus  (source backend: files)
Source table assegnazione_erasmus.

- `data_assegnazione`: date NOT NULL — Column data_assegnazione of table assegnazione_erasmus.
- `data_partenza`: date NOT NULL — Column data_partenza of table assegnazione_erasmus.
- `data_rientro`: date NOT NULL — Column data_rientro of table assegnazione_erasmus.
- `localita`: text NULL — Column localita of table assegnazione_erasmus.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table assegnazione_erasmus.
- `nome_universita`: text NULL — Column nome_universita of table assegnazione_erasmus.
- `numero_bando_erasmus`: text NOT NULL — Column numero_bando_erasmus of table assegnazione_erasmus.
- primary key: matricola_studente, numero_bando_erasmus

### azienda  (source backend: mongodb)
Source table azienda.

- `cap`: text NULL — Column CAP of table azienda.
- `citta`: text NOT NULL — Column citta of table azienda.
- `nome`: text NOT NULL — Column nome of table azienda.
- `numero_civico`: text NULL — Column numero_civico of table azienda.
- `partita_iva`: text NOT NULL — Column partita_iva of table azienda.
- `via`: text NULL — Column via of table azienda.
- primary key: partita_iva

### bando_borsa  (source backend: s3)
Source table bando_borsa.

- `causale`: text NULL — Column causale of table bando_borsa.
- `data_emissione`: date NOT NULL — Column data_emissione of table bando_borsa.
- `numero_bando_borsa`: text NOT NULL — Column numero_bando_borsa of table bando_borsa.
- `numero_borse`: decimal NOT NULL — Column numero_borse of table bando_borsa.
- `scadenza`: date NOT NULL — Column scadenza of table bando_borsa.
- `tipo`: text NULL — Column tipo of table bando_borsa.
- `valore`: decimal NOT NULL — Column valore of table bando_borsa.
- primary key: numero_bando_borsa

### bando_erasmus  (source backend: s3)
Source table bando_erasmus.

- `cfu`: decimal NOT NULL — Column CFU of table bando_erasmus.
- `data_emissione`: date NOT NULL — Column data_emissione of table bando_erasmus.
- `numero_bando_erasmus`: text NOT NULL — Column numero_bando_erasmus of table bando_erasmus.
- `numero_posti`: decimal NOT NULL — Column numero_posti of table bando_erasmus.
- `scadenza`: date NOT NULL — Column scadenza of table bando_erasmus.
- primary key: numero_bando_erasmus

### corso_laurea  (source backend: rest)
Source table corso_laurea.

- `capienza`: decimal NOT NULL — Column capienza of table corso_laurea.
- `codice_corso`: text NOT NULL — Column codice_corso of table corso_laurea.
- `nome`: text NOT NULL — Column nome of table corso_laurea.
- `tipo`: text NOT NULL — Column tipo of table corso_laurea. one of: MAGISTRALE, TRIENNALE, magistrale, triennale, Magistrale, Triennale.
- primary key: codice_corso

### docente  (source backend: files)
Source table docente.

- `cap`: text NULL — Column CAP of table docente.
- `citta`: text NULL — Column citta of table docente.
- `cognome`: text NOT NULL — Column cognome of table docente.
- `data_nascita`: date NOT NULL — Column data_nascita of table docente.
- `nome`: text NOT NULL — Column nome of table docente.
- `numero_civico`: text NULL — Column numero_civico of table docente.
- `numero_tesserino`: text NOT NULL — Column numero_tesserino of table docente.
- `sesso`: text NULL — Column sesso of table docente. one of: M, F, U, m, f, u.
- `via`: text NULL — Column via of table docente.
- primary key: numero_tesserino

### edizione_insegnamento  (source backend: rest)
Source table edizione_insegnamento.

- `cfu`: decimal NOT NULL — Column CFU of table edizione_insegnamento.
- `anno_accademico`: date NOT NULL — Column anno_accademico of table edizione_insegnamento.
- `anno_corso`: decimal NOT NULL — Column anno_corso of table edizione_insegnamento.
- `codice_insegnamento`: text NOT NULL — Column codice_insegnamento of table edizione_insegnamento.
- `semestre`: text NOT NULL — Column semestre of table edizione_insegnamento. one of: PRIMO, SECONDO, ANNUALE, Primo, Secondo, Annuale, primo, secondo, annuale.
- `svolgimento`: text NOT NULL — Column svolgimento of table edizione_insegnamento. one of: Presenza, Telematica, presenza, telematica, PRESENZA, TELEMATICA.
- primary key: anno_accademico, codice_insegnamento

### email_docente  (source backend: rest)
Source table email_docente.

- `mail_docente`: text NOT NULL — Column mail_docente of table email_docente.
- `tesserino_docente`: text NOT NULL — Column tesserino_docente of table email_docente.
- primary key: mail_docente, tesserino_docente

### email_studente  (source backend: mongodb)
Source table email_studente.

- `mail_studente`: text NOT NULL — Column mail_studente of table email_studente.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table email_studente.
- primary key: mail_studente, matricola_studente

### email_tutor_aziendale  (source backend: postgres)
Source table email_tutor_aziendale.

- `mail_tutor_aziendale`: text NOT NULL — Column mail_tutor_aziendale of table email_tutor_aziendale.
- `tesserino_tutor_azienda`: text NOT NULL — Column tesserino_tutor_azienda of table email_tutor_aziendale.
- primary key: mail_tutor_aziendale, tesserino_tutor_azienda

### esame_superato  (source backend: mongodb)
Source table esame_superato.

- `anno_accademico`: date NULL — Column anno_accademico of table esame_superato.
- `codice_insegnamento`: text NULL — Column codice_insegnamento of table esame_superato.
- `data_esame`: date NULL — Column data_esame of table esame_superato.
- `lode`: text NOT NULL — Column lode of table esame_superato. one of: N, n.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table esame_superato.
- `numero_verbale`: text NOT NULL — Column numero_verbale of table esame_superato.
- `voto`: decimal NOT NULL — Column voto of table esame_superato.
- primary key: numero_verbale

### frequenta_edizione_insegnamento  (source backend: s3)
Source table frequenta_edizione_insegnamento.

- `anno_accademico`: date NOT NULL — Column anno_accademico of table frequenta_edizione_insegnamento.
- `codice_insegnamento`: text NOT NULL — Column codice_insegnamento of table frequenta_edizione_insegnamento.
- `data_ins`: date NOT NULL — Column data_ins of table frequenta_edizione_insegnamento.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table frequenta_edizione_insegnamento.
- primary key: anno_accademico, codice_insegnamento, matricola_studente

### insegna_edizione  (source backend: files)
Source table insegna_edizione.

- `anno_accademico`: date NOT NULL — Column anno_accademico of table insegna_edizione.
- `codice_insegnamento`: text NOT NULL — Column codice_insegnamento of table insegna_edizione.
- `tesserino_docente`: text NOT NULL — Column tesserino_docente of table insegna_edizione.
- `tipo_docente`: text NOT NULL — Column tipo_docente of table insegna_edizione. one of: TEORIA, Teoria, teoria, LABORATORIO, Laboratorio, laboratorio, TEORIA E LABORATORIO, Teoria e Laboratorio, teoria e laboratorio.
- primary key: anno_accademico, codice_insegnamento, tesserino_docente

### insegnamento  (source backend: mongodb)
Source table insegnamento.

- `codice_insegnamento`: text NOT NULL — Column codice_insegnamento of table insegnamento.
- `nome`: text NOT NULL — Column nome of table insegnamento.
- primary key: codice_insegnamento

### offerta_insegnamento  (source backend: rest)
Source table offerta_insegnamento.

- `codice_corso`: text NOT NULL — Column codice_corso of table offerta_insegnamento.
- `codice_insegnamento`: text NOT NULL — Column codice_insegnamento of table offerta_insegnamento.
- `corso_principale`: text NOT NULL — Column corso_principale of table offerta_insegnamento. one of: Y, y, N, n.
- primary key: codice_corso, codice_insegnamento

### orario_lezioni  (source backend: files)
Source table orario_lezioni.

- `anno_accademico`: date NOT NULL — Column anno_accademico of table orario_lezioni.
- `codice_insegnamento`: text NOT NULL — Column codice_insegnamento of table orario_lezioni.
- `giorno_e_ora`: date NOT NULL — Column giorno_e_ora of table orario_lezioni.
- primary key: anno_accademico, codice_insegnamento, giorno_e_ora

### partecipa_seduta  (source backend: postgres)
Source table partecipa_seduta.

- `codice_corso`: text NOT NULL — Column codice_corso of table partecipa_seduta.
- `data_appello`: date NOT NULL — Column data_appello of table partecipa_seduta.
- `presidente`: text NOT NULL — Column presidente of table partecipa_seduta. one of: y, n, Y, N.
- `tesserino_docente`: text NOT NULL — Column tesserino_docente of table partecipa_seduta.
- primary key: codice_corso, data_appello, tesserino_docente

### partecipa_seminario  (source backend: postgres)
Source table partecipa_seminario.

- `data_seminario`: date NOT NULL — Column data_seminario of table partecipa_seminario.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table partecipa_seminario.
- `tesserino_docente`: text NOT NULL — Column tesserino_docente of table partecipa_seminario.
- primary key: data_seminario, matricola_studente, tesserino_docente

### partecipazione_bando_borsa  (source backend: postgres)
Source table partecipazione_bando_borsa.

- `data_domanda`: date NOT NULL — Column data_domanda of table partecipazione_bando_borsa.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table partecipazione_bando_borsa.
- `numero_bando_borsa`: text NOT NULL — Column numero_bando_borsa of table partecipazione_bando_borsa.
- primary key: matricola_studente, numero_bando_borsa

### partecipazione_bando_erasmus  (source backend: postgres)
Source table partecipazione_bando_erasmus.

- `data_domanda`: date NOT NULL — Column data_domanda of table partecipazione_bando_erasmus.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table partecipazione_bando_erasmus.
- `numero_bando_erasmus`: text NOT NULL — Column numero_bando_erasmus of table partecipazione_bando_erasmus.
- primary key: matricola_studente, numero_bando_erasmus

### prenotazione_appello  (source backend: mongodb)
Source table prenotazione_appello.

- `anno_accademico`: date NOT NULL — Column anno_accademico of table prenotazione_appello.
- `codice_insegnamento`: text NOT NULL — Column codice_insegnamento of table prenotazione_appello.
- `data_appello`: date NOT NULL — Column data_appello of table prenotazione_appello.
- `data_prenotazione`: date NOT NULL — Column data_prenotazione of table prenotazione_appello.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table prenotazione_appello.
- `numero_prenotazione`: text NOT NULL — Column numero_prenotazione of table prenotazione_appello.
- primary key: anno_accademico, codice_insegnamento, data_appello, matricola_studente

### prenotazione_appello_seduta  (source backend: rest)
Source table prenotazione_appello_seduta.

- `codice_corso`: text NOT NULL — Column codice_corso of table prenotazione_appello_seduta.
- `data_appello`: date NOT NULL — Column data_appello of table prenotazione_appello_seduta.
- `data_prenotazione`: date NOT NULL — Column data_prenotazione of table prenotazione_appello_seduta.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table prenotazione_appello_seduta.
- `numero`: text NOT NULL — Column numero of table prenotazione_appello_seduta.
- primary key: codice_corso, data_appello, matricola_studente

### prenotazione_ricevimento  (source backend: postgres)
Source table prenotazione_ricevimento.

- `data_prenotazione`: date NOT NULL — Column data_prenotazione of table prenotazione_ricevimento.
- `data_ricevimento`: date NOT NULL — Column data_ricevimento of table prenotazione_ricevimento.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table prenotazione_ricevimento.
- `numero_prenotazione`: text NOT NULL — Column numero_prenotazione of table prenotazione_ricevimento.
- `tesserino_docente`: text NOT NULL — Column tesserino_docente of table prenotazione_ricevimento.
- primary key: data_ricevimento, matricola_studente, tesserino_docente

### presiede_appello  (source backend: s3)
Source table presiede_appello.

- `anno_accademico`: date NOT NULL — Column anno_accademico of table presiede_appello.
- `codice_insegnamento`: text NOT NULL — Column codice_insegnamento of table presiede_appello.
- `data_appello`: date NOT NULL — Column data_appello of table presiede_appello.
- `presidente`: text NOT NULL — Column presidente of table presiede_appello. one of: y, n, Y, N.
- `tesserino_docente`: text NOT NULL — Column tesserino_docente of table presiede_appello.
- primary key: anno_accademico, codice_insegnamento, data_appello, tesserino_docente

### questionario  (source backend: files)
Source table questionario.

- `anno_accademico`: date NOT NULL — Column anno_accademico of table questionario.
- `codice_insegnamento`: text NOT NULL — Column codice_insegnamento of table questionario.
- `data_compilazione`: date NOT NULL — Column data_compilazione of table questionario.
- `disponibilita_docente`: decimal NOT NULL — Column disponibilita_docente of table questionario.
- `gradimento`: decimal NOT NULL — Column gradimento of table questionario.
- `materiale_didattico`: decimal NOT NULL — Column materiale_didattico of table questionario.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table questionario.
- `numero_questionario`: text NOT NULL — Column numero_questionario of table questionario.
- `precisione_orario`: decimal NOT NULL — Column precisione_orario of table questionario.
- `tesserino_docente`: text NOT NULL — Column tesserino_docente of table questionario.
- primary key: anno_accademico, codice_insegnamento, numero_questionario

### relatore  (source backend: mongodb)
Source table relatore.

- `data_fine`: date NOT NULL — Column data_fine of table relatore.
- `data_inizio`: date NOT NULL — Column data_inizio of table relatore.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table relatore.
- `tesserino_docente`: text NOT NULL — Column tesserino_docente of table relatore.
- `tipo_tesi`: text NULL — Column tipo_tesi of table relatore. one of: Compilativa, COMPILATIVA, compilativa, Sperimentale, SPERIMENTALE, sperimentale.
- `titolo_tesi`: text NULL — Column titolo_tesi of table relatore.
- primary key: matricola_studente, tesserino_docente

### ricevimento  (source backend: mongodb)
Source table ricevimento.

- `data_ricevimento`: date NOT NULL — Column data_ricevimento of table ricevimento.
- `durata`: decimal NULL — Column durata of table ricevimento.
- `tesserino_docente`: text NOT NULL — Column tesserino_docente of table ricevimento.
- primary key: data_ricevimento, tesserino_docente

### seduta_laurea  (source backend: mongodb)
Source table seduta_laurea.

- `codice_corso`: text NULL — Column codice_corso of table seduta_laurea.
- `data_seduta`: date NULL — Column data_seduta of table seduta_laurea.
- `lode`: text NULL — Column lode of table seduta_laurea. one of: N, n.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table seduta_laurea.
- `numero_verbale`: text NOT NULL — Column numero_verbale of table seduta_laurea.
- `voto`: decimal NOT NULL — Column voto of table seduta_laurea.
- primary key: numero_verbale

### seminario  (source backend: s3)
Source table seminario.

- `cfu`: decimal NOT NULL — Column CFU of table seminario.
- `data_seminario`: date NOT NULL — Column data_seminario of table seminario.
- `max_persone`: decimal NOT NULL — Column max_persone of table seminario.
- `nome`: text NULL — Column nome of table seminario.
- `tesserino_docente`: text NOT NULL — Column tesserino_docente of table seminario.
- primary key: data_seminario, tesserino_docente

### studente  (source backend: files)
Source table studente.

- `cap`: text NULL — Column CAP of table studente.
- `citta`: text NULL — Column citta of table studente.
- `codice_corso`: text NOT NULL — Column codice_corso of table studente.
- `cognome`: text NOT NULL — Column cognome of table studente.
- `data_iscrizione`: date NOT NULL — Column data_iscrizione of table studente.
- `data_nascita`: date NOT NULL — Column data_nascita of table studente.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table studente.
- `nome`: text NOT NULL — Column nome of table studente.
- `numero_civico`: text NULL — Column numero_civico of table studente.
- `sesso`: text NULL — Column sesso of table studente. one of: M, F, U, m, f, u.
- `via`: text NULL — Column via of table studente.
- primary key: matricola_studente

### tassa  (source backend: files)
Source table tassa.

- `iuv`: text NULL — Column IUV of table tassa.
- `data_pagamento`: date NULL — Column data_pagamento of table tassa.
- `importo`: decimal NOT NULL — Column importo of table tassa.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table tassa.
- `mora`: decimal NULL — Column mora of table tassa.
- `numero_fattura`: text NOT NULL — Column numero_fattura of table tassa.
- `scadenza`: date NOT NULL — Column scadenza of table tassa.
- `tipo`: text NULL — Column tipo of table tassa.
- primary key: numero_fattura

### telefono_docente  (source backend: postgres)
Source table telefono_docente.

- `numero_telefono_docente`: text NOT NULL — Column numero_telefono_docente of table telefono_docente.
- `tesserino_docente`: text NOT NULL — Column tesserino_docente of table telefono_docente.
- primary key: numero_telefono_docente, tesserino_docente

### telefono_studente  (source backend: files)
Source table telefono_studente.

- `matricola_studente`: text NOT NULL — Column matricola_studente of table telefono_studente.
- `numero_telefono_studente`: text NOT NULL — Column numero_telefono_studente of table telefono_studente.
- primary key: matricola_studente, numero_telefono_studente

### telefono_tutor_aziendale  (source backend: postgres)
Source table telefono_tutor_aziendale.

- `numero_telefono_tutor_aziendale`: text NOT NULL — Column numero_telefono_tutor_aziendale of table telefono_tutor_aziendale.
- `tesserino_tutor_azienda`: text NOT NULL — Column tesserino_tutor_azienda of table telefono_tutor_aziendale.
- primary key: numero_telefono_tutor_aziendale, tesserino_tutor_azienda

### tirocinio  (source backend: mongodb)
Source table tirocinio.

- `cfu`: decimal NOT NULL — Column CFU of table tirocinio.
- `data_fine`: date NOT NULL — Column data_fine of table tirocinio.
- `data_inizio`: date NOT NULL — Column data_inizio of table tirocinio.
- `matricola_studente`: text NOT NULL — Column matricola_studente of table tirocinio.
- `numero_tirocinio`: text NOT NULL — Column numero_tirocinio of table tirocinio.
- `tesserino_docente`: text NOT NULL — Column tesserino_docente of table tirocinio.
- `tesserino_tutor_azienda`: text NULL — Column tesserino_tutor_azienda of table tirocinio.
- primary key: numero_tirocinio

### tutor_aziendale  (source backend: s3)
Source table tutor_aziendale.

- `cap`: text NULL — Column CAP of table tutor_aziendale.
- `citta`: text NULL — Column citta of table tutor_aziendale.
- `cognome`: text NOT NULL — Column cognome of table tutor_aziendale.
- `data_nascita`: date NOT NULL — Column data_nascita of table tutor_aziendale.
- `nome`: text NOT NULL — Column nome of table tutor_aziendale.
- `numero_civico`: text NULL — Column numero_civico of table tutor_aziendale.
- `numero_tesserino`: text NOT NULL — Column numero_tesserino of table tutor_aziendale.
- `partita_iva`: text NOT NULL — Column partita_iva of table tutor_aziendale.
- `sesso`: text NULL — Column sesso of table tutor_aziendale. one of: M, F, U, m, f, u.
- `via`: text NULL — Column via of table tutor_aziendale.
- primary key: numero_tesserino

### Relationships

- appello(codice_insegnamento, anno_accademico) -> edizione_insegnamento(codice_insegnamento, anno_accademico) [required]
- appello_laurea(codice_corso) -> corso_laurea(codice_corso) [required]
- assegnazione_borse(matricola_studente) -> studente(matricola_studente) [required]
- assegnazione_borse(numero_bando_borsa) -> bando_borsa(numero_bando_borsa) [required]
- assegnazione_erasmus(matricola_studente) -> studente(matricola_studente) [required]
- assegnazione_erasmus(numero_bando_erasmus) -> bando_erasmus(numero_bando_erasmus) [required]
- edizione_insegnamento(codice_insegnamento) -> insegnamento(codice_insegnamento) [required]
- email_docente(tesserino_docente) -> docente(numero_tesserino) [required]
- email_studente(matricola_studente) -> studente(matricola_studente) [required]
- email_tutor_aziendale(tesserino_tutor_azienda) -> tutor_aziendale(numero_tesserino) [required]
- esame_superato(codice_insegnamento, anno_accademico, data_esame) -> appello(codice_insegnamento, anno_accademico, data_appello) [optional (may be NULL/dangling)]
- esame_superato(matricola_studente) -> studente(matricola_studente) [required]
- frequenta_edizione_insegnamento(codice_insegnamento, anno_accademico) -> edizione_insegnamento(codice_insegnamento, anno_accademico) [required]
- frequenta_edizione_insegnamento(matricola_studente) -> studente(matricola_studente) [required]
- insegna_edizione(codice_insegnamento, anno_accademico) -> edizione_insegnamento(codice_insegnamento, anno_accademico) [required]
- insegna_edizione(tesserino_docente) -> docente(numero_tesserino) [required]
- offerta_insegnamento(codice_corso) -> corso_laurea(codice_corso) [required]
- offerta_insegnamento(codice_insegnamento) -> insegnamento(codice_insegnamento) [required]
- orario_lezioni(codice_insegnamento, anno_accademico) -> edizione_insegnamento(codice_insegnamento, anno_accademico) [required]
- partecipa_seduta(codice_corso, data_appello) -> appello_laurea(codice_corso, data_appello) [required]
- partecipa_seduta(tesserino_docente) -> docente(numero_tesserino) [required]
- partecipa_seminario(data_seminario, tesserino_docente) -> seminario(data_seminario, tesserino_docente) [required]
- partecipa_seminario(matricola_studente) -> studente(matricola_studente) [required]
- partecipazione_bando_borsa(matricola_studente) -> studente(matricola_studente) [required]
- partecipazione_bando_borsa(numero_bando_borsa) -> bando_borsa(numero_bando_borsa) [required]
- partecipazione_bando_erasmus(matricola_studente) -> studente(matricola_studente) [required]
- partecipazione_bando_erasmus(numero_bando_erasmus) -> bando_erasmus(numero_bando_erasmus) [required]
- prenotazione_appello(codice_insegnamento, anno_accademico, data_appello) -> appello(codice_insegnamento, anno_accademico, data_appello) [required]
- prenotazione_appello(matricola_studente) -> studente(matricola_studente) [required]
- prenotazione_appello_seduta(codice_corso, data_appello) -> appello_laurea(codice_corso, data_appello) [required]
- prenotazione_appello_seduta(matricola_studente) -> studente(matricola_studente) [required]
- prenotazione_ricevimento(data_ricevimento, tesserino_docente) -> ricevimento(data_ricevimento, tesserino_docente) [required]
- prenotazione_ricevimento(matricola_studente) -> studente(matricola_studente) [required]
- presiede_appello(codice_insegnamento, anno_accademico, data_appello) -> appello(codice_insegnamento, anno_accademico, data_appello) [required]
- presiede_appello(tesserino_docente) -> docente(numero_tesserino) [required]
- questionario(codice_insegnamento, anno_accademico) -> edizione_insegnamento(codice_insegnamento, anno_accademico) [required]
- questionario(matricola_studente) -> studente(matricola_studente) [required]
- questionario(tesserino_docente) -> docente(numero_tesserino) [required]
- relatore(matricola_studente) -> studente(matricola_studente) [required]
- relatore(tesserino_docente) -> docente(numero_tesserino) [required]
- ricevimento(tesserino_docente) -> docente(numero_tesserino) [required]
- seduta_laurea(codice_corso, data_seduta) -> appello_laurea(codice_corso, data_appello) [optional (may be NULL/dangling)]
- seduta_laurea(matricola_studente) -> studente(matricola_studente) [required]
- seminario(tesserino_docente) -> docente(numero_tesserino) [required]
- studente(codice_corso) -> corso_laurea(codice_corso) [required]
- tassa(matricola_studente) -> studente(matricola_studente) [required]
- telefono_docente(tesserino_docente) -> docente(numero_tesserino) [required]
- telefono_studente(matricola_studente) -> studente(matricola_studente) [required]
- telefono_tutor_aziendale(tesserino_tutor_azienda) -> tutor_aziendale(numero_tesserino) [required]
- tirocinio(matricola_studente) -> studente(matricola_studente) [required]
- tirocinio(tesserino_docente) -> docente(numero_tesserino) [required]
- tirocinio(tesserino_tutor_azienda) -> tutor_aziendale(numero_tesserino) [optional (may be NULL/dangling)]
- tutor_aziendale(partita_iva) -> azienda(partita_iva) [required]

