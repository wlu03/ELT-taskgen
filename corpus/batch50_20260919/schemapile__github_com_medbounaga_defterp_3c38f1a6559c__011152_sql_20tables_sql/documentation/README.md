# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com Medbounaga Defterp

## Specification

PROJECT OVERVIEW

This project builds three analytical marts over an extract of an ERP schema. The sources are delivered from several different systems, and each source table must be read from its own extraction backend, as follows.

Source tables and their extraction backends:
- erp_account (source table erp.account) is extracted from the s3 backend.
- erp_delivery_order (source table erp.delivery_order) is extracted from the mongodb backend.
- erp_delivery_order_line (source table erp.delivery_order_line) is extracted from the rest backend.
- erp_inventory (source table erp.inventory) is extracted from the files backend.
- erp_invoice (source table erp.invoice) is extracted from the files backend.
- erp_invoice_line (source table erp.invoice_line) is extracted from the postgres backend.
- erp_invoice_payment (source table erp.invoice_payment) is extracted from the rest backend.
- erp_invoice_tax (source table erp.invoice_tax) is extracted from the s3 backend.
- erp_journal (source table erp.journal) is extracted from the rest backend.
- erp_journal_entry (source table erp.journal_entry) is extracted from the s3 backend.
- erp_journal_item (source table erp.journal_item) is extracted from the rest backend.
- erp_partner (source table erp.partner) is extracted from the files backend.
- erp_payment (source table erp.payment) is extracted from the s3 backend.
- erp_product (source table erp.product) is extracted from the rest backend.
- erp_product_category (source table erp.product_category) is extracted from the rest backend.
- erp_product_uom (source table erp.product_uom) is extracted from the files backend.
- erp_product_uom_category (source table erp.product_uom_category) is extracted from the files backend.
- erp_purchase_order (source table erp.purchase_order) is extracted from the s3 backend.
- erp_purchase_order_line (source table erp.purchase_order_line) is extracted from the files backend.
- erp_sale_order (source table erp.sale_order) is extracted from the mongodb backend.
- erp_sale_order_line (source table erp.sale_order_line) is extracted from the postgres backend.
- erp_tax (source table erp.tax) is extracted from the rest backend.
- erp_user (source table erp.user) is extracted from the mongodb backend.

Every table listed above has id as its primary key.

RELATIONSHIPS

Each relationship below names the child table with its key column, the parent table with its key column, and whether the relationship is required or optional exactly as the source schema declares it.

- Child erp_delivery_order(partner_id) refers to parent erp_partner(id): required.
- Child erp_delivery_order(purchase_id) refers to parent erp_purchase_order(id): optional (may be NULL or dangling).
- Child erp_delivery_order(sale_id) refers to parent erp_sale_order(id): optional (may be NULL or dangling).
- Child erp_delivery_order_line(delivery_id) refers to parent erp_delivery_order(id): required.
- Child erp_delivery_order_line(partner_id) refers to parent erp_partner(id): optional (may be NULL or dangling).
- Child erp_delivery_order_line(product_id) refers to parent erp_product(id): required.
- Child erp_inventory(product_id) refers to parent erp_product(id): required.
- Child erp_invoice(account_id) refers to parent erp_account(id): optional (may be NULL or dangling).
- Child erp_invoice(entry_id) refers to parent erp_journal_entry(id): optional (may be NULL or dangling).
- Child erp_invoice(journal_id) refers to parent erp_journal(id): optional (may be NULL or dangling).
- Child erp_invoice(partner_id) refers to parent erp_partner(id): required.
- Child erp_invoice(purchase_id) refers to parent erp_purchase_order(id): optional (may be NULL or dangling).
- Child erp_invoice(sale_id) refers to parent erp_sale_order(id): optional (may be NULL or dangling).
- Child erp_invoice_line(account_id) refers to parent erp_account(id): optional (may be NULL or dangling).
- Child erp_invoice_line(invoice_id) refers to parent erp_invoice(id): required.
- Child erp_invoice_line(partner_id) refers to parent erp_partner(id): required.
- Child erp_invoice_line(product_id) refers to parent erp_product(id): optional (may be NULL or dangling).
- Child erp_invoice_line(tax_id) refers to parent erp_tax(id): optional (may be NULL or dangling).
- Child erp_invoice_payment(invoice_id) refers to parent erp_invoice(id): required.
- Child erp_invoice_payment(journal_entry_id) refers to parent erp_journal_entry(id): required.
- Child erp_invoice_tax(account_id) refers to parent erp_account(id): optional (may be NULL or dangling).
- Child erp_invoice_tax(invoice_id) refers to parent erp_invoice(id): optional (may be NULL or dangling).
- Child erp_invoice_tax(tax_id) refers to parent erp_tax(id): optional (may be NULL or dangling).
- Child erp_journal_entry(journal_id) refers to parent erp_journal(id): optional (may be NULL or dangling).
- Child erp_journal_entry(partner_id) refers to parent erp_partner(id): optional (may be NULL or dangling).
- Child erp_journal_item(account_id) refers to parent erp_account(id): optional (may be NULL or dangling).
- Child erp_journal_item(entry_id) refers to parent erp_journal_entry(id): optional (may be NULL or dangling).
- Child erp_journal_item(journal_id) refers to parent erp_journal(id): optional (may be NULL or dangling).
- Child erp_journal_item(partner_id) refers to parent erp_partner(id): optional (may be NULL or dangling).
- Child erp_journal_item(product_id) refers to parent erp_product(id): optional (may be NULL or dangling).
- Child erp_journal_item(tax_id) refers to parent erp_tax(id): optional (may be NULL or dangling).
- Child erp_journal_item(uom_id) refers to parent erp_product_uom(id): optional (may be NULL or dangling).
- Child erp_partner(accountpayable_id) refers to parent erp_account(id): required.
- Child erp_partner(accountreceivable_id) refers to parent erp_account(id): required.
- Child erp_payment(account_id) refers to parent erp_account(id): optional (may be NULL or dangling).
- Child erp_payment(entry_id) refers to parent erp_journal_entry(id): optional (may be NULL or dangling).
- Child erp_payment(invoice_id) refers to parent erp_invoice(id): optional (may be NULL or dangling).
- Child erp_payment(journal_id) refers to parent erp_journal(id): optional (may be NULL or dangling).
- Child erp_payment(partner_id) refers to parent erp_partner(id): optional (may be NULL or dangling).
- Child erp_product(categ_id) refers to parent erp_product_category(id): required.
- Child erp_product(uom_id) refers to parent erp_product_uom(id): required.
- Child erp_product_uom(category_id) refers to parent erp_product_uom_category(id): required.
- Child erp_purchase_order(partner_id) refers to parent erp_partner(id): required.
- Child erp_purchase_order_line(order_id) refers to parent erp_purchase_order(id): required.
- Child erp_purchase_order_line(product_id) refers to parent erp_product(id): required.
- Child erp_purchase_order_line(tax_id) refers to parent erp_tax(id): optional (may be NULL or dangling).
- Child erp_sale_order(partner_id) refers to parent erp_partner(id): required.
- Child erp_sale_order_line(order_id) refers to parent erp_sale_order(id): required.
- Child erp_sale_order_line(product_id) refers to parent erp_product(id): required.
- Child erp_sale_order_line(tax_id) refers to parent erp_tax(id): optional (may be NULL or dangling).

Only some of these tables feed the three marts below; each mart section states exactly which source tables it reads.

============================================================
Mart erp_tax_erp_sale_order_line_snapshot — a per-erp_tax latest-row snapshot over linked erp_sale_order_line activity in the 011152_SQL%20Tables.sql schema.

Grain: one row per erp_tax (id), INCLUDING erp_tax rows with no linked erp_sale_order_line rows. The key column is parent_key.

Rule 1. This mart reads source table erp_tax.

Rule 2. This mart reads source table erp_sale_order_line.

Rule 3. From source table erp_tax there is one row per erp_tax row, keyed by id, carrying parent_key and parent_name.

Rule 4. The erp_sale_order_line rows are brought in by matching erp_sale_order_line tax_id against parent_key, carrying tax_id and id; preservation is left-sided, so an erp_tax row with no matching erp_sale_order_line row is retained and receives the stated empty snapshot values.

Rule 5. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group, reporting event_count and lifetime_amount for that row's matching rows.

Rule 6. For each parent_key the single row at which the ordering measure — date — is largest survives, ties broken by the smallest id, and latest_row_id, latest_amount and latest_label are taken from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows.

Rule 7. The extremal row's attributes are attributed to the grouped measures on matching parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

Rule 8. The mart columns are named parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label; lifetime_amount reports its declared default of 0 — never NULL — for a group with no matching rows.

Rule 9. Guarded ratio: alongside parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label, the column latest_amount_share is latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; it is 0.0 when the denominator lifetime_amount is 0 or has no value.

Rule 10. Deterministic output order: rows appear sorted by parent_key, ascending.

Output columns:
- parent_key (integer): identifier of the erp_tax row. One row per value.
- parent_name (text): name of the erp_tax row, copied unchanged.
- event_count (bigint): number of erp_sale_order_line rows for this erp_tax row; 0 when there are none. An erp_tax row kept with no erp_sale_order_line row reports 0 here, never 1: its placeholder holds no erp_sale_order_line row to count.
- lifetime_amount (integer): total of active over all matching erp_sale_order_line rows; 0 when there are no rows.
- latest_row_id (integer): id of the row with the latest date; ties take the smallest id. It is 0 when there are no rows.
- latest_amount (integer): active from that same latest row; 0 when there are no rows.
- latest_label (text): name from that same latest row; the literal '(none)' when there are no rows or when the winning value is missing.
- latest_amount_share (float): latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0.

============================================================
Mart erp_sale_order_erp_sale_order_line_distribution — a per-(erp_sale_order, measure state) distribution of linked erp_sale_order_line activity in the 011152_SQL%20Tables.sql schema.

Grain: one row per (id, measure state) pair represented by linked erp_sale_order_line rows, plus one absent no-activity row for an erp_sale_order row with no links. Because active is required, no linked erp_sale_order_line row belongs to the absent state. The key columns are entity_key and measure_state.

Rule 1. This mart reads source table erp_sale_order.

Rule 2. This mart reads source table erp_sale_order_line.

Rule 3. From source table erp_sale_order, each id and its invoice_method are carried into the measure-state calculation as entity_key and entity_name.

Rule 4. The linked erp_sale_order_line rows are brought into each erp_sale_order entity by matching erp_sale_order_line order_id against entity_key, carrying entity_key, entity_name, id and order_id; preservation is left-sided, so an entity with no linked row is retained and its absent state is visible.

Rule 5. The present measure-state rows, carrying entity_key and entity_name, are the rows that survive: a real erp_sale_order_line row; active is required on every such row.

Rule 6. There is one row per erp_sale_order entity that has at least one row in the present measure state, and no row here for an entity with none, reporting entity_key, entity_name, row_count as the row count, distinct_amount_count as how many different active values occur (each different value counted once, however many rows repeat it), total_amount as the total active, and max_amount as the largest active.

Rule 7. For these present-state rows, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 8. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled in measure_state as the present measure state, with the literal value 'present'.

Rule 9. The absent measure-state rows, carrying entity_key and entity_name, are the rows that survive as the retained placeholder for an erp_sale_order row with no erp_sale_order_line rows; no real row can enter this state because active is required.

Rule 10. There is one row per erp_sale_order entity with no linked erp_sale_order_line row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked erp_sale_order_line row, reporting entity_key, entity_name, a row_count of 0, 0 different active values in distinct_amount_count, a total active of 0 in total_amount and a largest active of 0 in max_amount.

Rule 11. For these absent-state rows, alongside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount, max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; it is 0.0 when total_amount is 0.

Rule 12. These measures — entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share — are labelled in measure_state as the absent measure state, with the literal value 'absent'.

Rule 13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, all rows kept, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.

Rule 14. Deterministic output order: rows appear sorted ascending by entity_key, the entity, and then by measure_state, the measure state.

Output columns:
- entity_key (integer): identifier of the erp_sale_order row.
- measure_state (text): 'present' for a linked erp_sale_order_line row; 'absent' only for an erp_sale_order row with no linked erp_sale_order_line row. active is required on every real erp_sale_order_line row.
- entity_name (text): invoice_method of the erp_sale_order row, copied unchanged.
- row_count (bigint): number of linked erp_sale_order_line rows in this entity/state cell; 0 for a no-activity absent cell.
- distinct_amount_count (bigint): number of unique active values in this cell; each unique value is counted once, however many rows repeat it; 0 for a no-activity absent cell.
- total_amount (integer): total of active in this cell; 0 for a no-activity absent cell.
- max_amount (integer): largest active in this cell; 0 for a no-activity absent cell.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

============================================================
Mart erp_purchase_order_erp_purchase_order_line_top — per-erp_purchase_order extremes over linked erp_purchase_order_line rows in the 011152_SQL%20Tables.sql schema: WHICH row is largest, not how large it is.

Grain: one row per erp_purchase_order (id), INCLUDING erp_purchase_order rows with no linked erp_purchase_order_line rows. The key column is parent_key.

Rule 1. This mart reads source table erp_purchase_order.

Rule 2. This mart reads source table erp_purchase_order_line.

Rule 3. From source table erp_purchase_order there is one row per erp_purchase_order row, keyed by id, carrying parent_key and parent_name.

Rule 4. The erp_purchase_order_line rows are brought in by matching erp_purchase_order_line order_id against parent_key, carrying order_id and id; preservation is left-sided, so an erp_purchase_order row with no erp_purchase_order_line rows still appears, with the declared defaults.

Rule 5. Within each parent_key the matched rows are ranked under an explicit total order — the measure active first, then the declared tie-break — so the extremal row is a function of the input and not of row order.

Rule 6. There is one output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so parent_key and parent_name take one value per key and never split a group, reporting top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.

Rule 7. For each parent_key the single row at which the ordering measure active is largest survives, ties broken by the smallest name under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no name value sorts after every row that has one — then the smallest id, and top_label and top_row_id are taken from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows.

Rule 8. The extremal row's attributes are attributed to the grouped measures on matching parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.

Rule 9. The mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults of 0 — never NULL — for a group with no matching rows.

Rule 10. Guarded ratio: alongside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id, the column top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when the denominator total_measure is 0 or has no value.

Rule 11. Alongside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, tie_state is 'empty' when no row holds a maximum at all — the parent has no erp_purchase_order_line rows — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; this is a categorical mapping with no numeric boundary. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, otherwise 'tied' when it is 2 or more; tie_state is always one of these three literals and never null or blank.

Rule 12. Deterministic output order: rows appear sorted by parent_key, ascending.

Output columns:
- parent_key (integer): identifier of the erp_purchase_order row. One row per value.
- parent_name (text): invoice_method of the erp_purchase_order row, copied unchanged.
- top_measure (integer): the largest active itself; 0 when the parent has no erp_purchase_order_line rows.
- tied_count (bigint): how many erp_purchase_order_line rows are tied at that largest active. It is 1 when exactly one row carries that largest active; 0 when there are no rows.
- child_count (bigint): number of erp_purchase_order_line rows for this erp_purchase_order row; 0 when there are none. An erp_purchase_order row kept with no erp_purchase_order_line row reports 0 here, never 1: its placeholder holds no erp_purchase_order_line row to count.
- total_measure (integer): total of active over all of them; 0 when the parent has no erp_purchase_order_line rows.
- top_label (text): the name of the erp_purchase_order_line row with the LARGEST active for this erp_purchase_order row. Ties in active are broken by taking the SMALLEST name under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) — a row with no name value sorts after every labelled row; rows tied on both are resolved by the smallest id. It is the literal '(none)' when the parent has no erp_purchase_order_line rows at all, and '(none)' when the winning row has no name value.
- top_row_id (integer): the id of that same extremal row — the winner under the SAME total order, so it is the identifier of a real erp_purchase_order_line row whenever the parent has any. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no erp_purchase_order_line rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `erp_tax_erp_sale_order_line_snapshot`

- Grain: One row per erp_tax (id), INCLUDING erp_tax rows with no linked erp_sale_order_line rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share

```text
Mart 'erp_tax_erp_sale_order_line_snapshot' has 10 declared semantic rules:
1. [source] Read source table erp_tax. (public source tables: erp_tax)
2. [source] Read source table erp_sale_order_line. (public source tables: erp_sale_order_line)
3. [derive] One row per erp_tax row, keyed by id. (public source tables: erp_tax | public carried/output columns: parent_key, parent_name)
4. [join] Bring in erp_sale_order_line; a erp_tax row with no matching erp_sale_order_line row is retained and receives the stated empty snapshot values. (public source tables: erp_sale_order_line | public carried/output columns: tax_id, id | join preservation: left | condition public identifiers: erp_sale_order_line, tax_id, parent_key)
5. [aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count, lifetime_amount for that row's matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount)
6. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest id, and take latest_row_id, latest_amount, latest_label from that winning row. EVERY row of the group ranks, including a row whose ordering measure has no value — such a row sorts after every row that has one — so a group with at least one row always has a winning row, and the declared defaults belong to a group with NO rows. (public carried/output columns: parent_key, latest_row_id, latest_amount, latest_label)
7. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
8. [derive] Name the mart columns; lifetime_amount reports its declared default — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label)
9. [ratio] Guarded ratios: latest_amount_share — latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `erp_sale_order_erp_sale_order_line_distribution`

- Grain: One row per (id, measure state) pair represented by linked erp_sale_order_line rows, plus one absent no-activity row for a erp_sale_order row with no links. Because active is required, no linked erp_sale_order_line row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'erp_sale_order_erp_sale_order_line_distribution' has 14 declared semantic rules:
1. [source] Read source table erp_sale_order. (public source tables: erp_sale_order)
2. [source] Read source table erp_sale_order_line. (public source tables: erp_sale_order_line)
3. [derive] Carry each id and its invoice_method into the measure-state calculation. (public source tables: erp_sale_order | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked erp_sale_order_line rows into each erp_sale_order entity; retain an entity with no linked row so its absent state is visible. (public source tables: erp_sale_order_line | public carried/output columns: entity_key, entity_name, id, order_id | join preservation: left | condition public identifiers: erp_sale_order_line, order_id, entity_key)
5. [filter] Keep the present measure-state rows: a real erp_sale_order_line row; active is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per erp_sale_order entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different active values occur (each different value counted once, however many rows repeat it), total active, and largest active. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a erp_sale_order row with no erp_sale_order_line rows; no real row can enter this state because active is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per erp_sale_order entity with no linked erp_sale_order_line row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked erp_sale_order_line row, reporting a row count of 0, 0 different active values, a total active of 0 and a largest active of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `erp_purchase_order_erp_purchase_order_line_top`

- Grain: One row per erp_purchase_order (id), INCLUDING erp_purchase_order rows with no linked erp_purchase_order_line rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'erp_purchase_order_erp_purchase_order_line_top' has 12 declared semantic rules:
1. [source] Read source table erp_purchase_order. (public source tables: erp_purchase_order)
2. [source] Read source table erp_purchase_order_line. (public source tables: erp_purchase_order_line)
3. [derive] One row per erp_purchase_order row, keyed by id. (public source tables: erp_purchase_order | public carried/output columns: parent_key, parent_name)
4. [join] Bring in erp_purchase_order_line: a erp_purchase_order row with no erp_purchase_order_line rows still appears, with the declared defaults. (public source tables: erp_purchase_order_line | public carried/output columns: order_id, id | join preservation: left | condition public identifiers: erp_purchase_order_line, order_id, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest name under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one) (a row with no name value sorts after every row that has one), then the smallest id, and take top_label, top_row_id from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no erp_purchase_order_line rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### erp_account  (source backend: s3)
Source table erp.account.

- `active`: integer NOT NULL — Column active of table erp.account.
- `code`: text NOT NULL — Column code of table erp.account.
- `id`: integer NOT NULL — Column id of table erp.account.
- `name`: text NOT NULL — Column name of table erp.account.
- `title`: text NOT NULL — Column title of table erp.account.
- `type`: text NOT NULL — Column type of table erp.account.
- primary key: id

### erp_delivery_order  (source backend: mongodb)
Source table erp.delivery_order.

- `active`: integer NOT NULL — Column active of table erp.delivery_order.
- `back_order_id`: integer NULL — Column back_order_id of table erp.delivery_order.
- `date`: timestamp NOT NULL — Column date of table erp.delivery_order.
- `delivery_method`: text NULL — Column delivery_method of table erp.delivery_order.
- `id`: integer NOT NULL — Column id of table erp.delivery_order.
- `name`: text NULL — Column name of table erp.delivery_order.
- `origin`: text NULL — Column origin of table erp.delivery_order.
- `partner_id`: integer NOT NULL — Column partner_id of table erp.delivery_order.
- `purchase_id`: integer NULL — Column purchase_id of table erp.delivery_order.
- `sale_id`: integer NULL — Column sale_id of table erp.delivery_order.
- `state`: text NULL — Column state of table erp.delivery_order.
- `type`: text NULL — Column type of table erp.delivery_order.
- primary key: id

### erp_delivery_order_line  (source backend: rest)
Source table erp.delivery_order_line.

- `active`: integer NOT NULL — Column active of table erp.delivery_order_line.
- `delivery_id`: integer NOT NULL — Column delivery_id of table erp.delivery_order_line.
- `id`: integer NOT NULL — Column id of table erp.delivery_order_line.
- `partner_id`: integer NULL — Column partner_id of table erp.delivery_order_line.
- `price`: float NOT NULL — Column price of table erp.delivery_order_line.
- `product_id`: integer NOT NULL — Column product_id of table erp.delivery_order_line.
- `quantity`: float NOT NULL — Column quantity of table erp.delivery_order_line.
- `reserved`: float NOT NULL — Column reserved of table erp.delivery_order_line.
- `state`: text NOT NULL — Column state of table erp.delivery_order_line.
- `type`: text NULL — Column type of table erp.delivery_order_line.
- `uom`: text NOT NULL — Column uom of table erp.delivery_order_line.
- primary key: id

### erp_inventory  (source backend: files)
Source table erp.inventory.

- `active`: integer NOT NULL — Column active of table erp.inventory.
- `id`: integer NOT NULL — Column id of table erp.inventory.
- `incoming`: float NOT NULL — Column incoming of table erp.inventory.
- `max_qty`: float NULL — Column max_qty of table erp.inventory.
- `min_qty`: float NULL — Column min_qty of table erp.inventory.
- `product_id`: integer NOT NULL — Column product_id of table erp.inventory.
- `quantity`: float NOT NULL — Column quantity of table erp.inventory.
- `reserved`: float NOT NULL — Column reserved of table erp.inventory.
- `total_cost`: float NOT NULL — Column total_cost of table erp.inventory.
- `unit_cost`: float NOT NULL — Column unit_cost of table erp.inventory.
- primary key: id

### erp_invoice  (source backend: files)
Source table erp.invoice.

- `account_id`: integer NULL — Column account_id of table erp.invoice.
- `active`: integer NOT NULL — Column active of table erp.invoice.
- `amount_tax`: float NULL — Column amount_tax of table erp.invoice.
- `amount_total`: float NULL — Column amount_total of table erp.invoice.
- `amount_untaxed`: float NULL — Column amount_untaxed of table erp.invoice.
- `comment`: text NULL — Column comment of table erp.invoice.
- `date`: timestamp NOT NULL — Column date of table erp.invoice.
- `entry_id`: integer NULL — Column entry_id of table erp.invoice.
- `id`: integer NOT NULL — Column id of table erp.invoice.
- `journal_id`: integer NULL — Column journal_id of table erp.invoice.
- `name`: text NULL — Column name of table erp.invoice.
- `number`: text NULL — Column number of table erp.invoice.
- `origin`: text NULL — Column origin of table erp.invoice.
- `partner_id`: integer NOT NULL — Column partner_id of table erp.invoice.
- `purchase_id`: integer NULL — Column purchase_id of table erp.invoice.
- `reference`: text NULL — Column reference of table erp.invoice.
- `residual`: float NULL — Column residual of table erp.invoice.
- `sale_id`: integer NULL — Column sale_id of table erp.invoice.
- `state`: text NULL — Column state of table erp.invoice.
- `supplier_invoice_number`: text NULL — Column supplier_invoice_number of table erp.invoice.
- `type`: text NULL — Column type of table erp.invoice.
- primary key: id

### erp_invoice_line  (source backend: postgres)
Source table erp.invoice_line.

- `account_id`: integer NULL — Column account_id of table erp.invoice_line.
- `active`: integer NOT NULL — Column active of table erp.invoice_line.
- `date`: timestamp NULL — Column date of table erp.invoice_line.
- `discount`: float NOT NULL — Column discount of table erp.invoice_line.
- `id`: integer NOT NULL — Column id of table erp.invoice_line.
- `invoice_id`: integer NOT NULL — Column invoice_id of table erp.invoice_line.
- `name`: text NULL — Column name of table erp.invoice_line.
- `partner_id`: integer NOT NULL — Column partner_id of table erp.invoice_line.
- `price`: float NOT NULL — Column price of table erp.invoice_line.
- `price_subtotal`: float NOT NULL — Column price_subtotal of table erp.invoice_line.
- `product_id`: integer NULL — Column product_id of table erp.invoice_line.
- `quantity`: float NOT NULL — Column quantity of table erp.invoice_line.
- `tax_id`: integer NULL — Column tax_id of table erp.invoice_line.
- `uom`: text NOT NULL — Column uom of table erp.invoice_line.
- primary key: id

### erp_invoice_payment  (source backend: rest)
Source table erp.invoice_payment.

- `date`: timestamp NOT NULL — Column date of table erp.invoice_payment.
- `id`: integer NOT NULL — Column id of table erp.invoice_payment.
- `invoice_id`: integer NOT NULL — Column invoice_id of table erp.invoice_payment.
- `journal_entry_id`: integer NOT NULL — Column journal_entry_id of table erp.invoice_payment.
- `name`: text NULL — Column name of table erp.invoice_payment.
- `paid_amount`: float NOT NULL — Column paid_amount of table erp.invoice_payment.
- primary key: id

### erp_invoice_tax  (source backend: s3)
Source table erp.invoice_tax.

- `account_id`: integer NULL — Column account_id of table erp.invoice_tax.
- `active`: integer NOT NULL — Column active of table erp.invoice_tax.
- `base_amount`: float NULL — Column base_amount of table erp.invoice_tax.
- `date`: timestamp NULL — Column date of table erp.invoice_tax.
- `id`: integer NOT NULL — Column id of table erp.invoice_tax.
- `invoice_id`: integer NULL — Column invoice_id of table erp.invoice_tax.
- `name`: text NULL — Column name of table erp.invoice_tax.
- `tax_amount`: float NULL — Column tax_amount of table erp.invoice_tax.
- `tax_id`: integer NULL — Column tax_id of table erp.invoice_tax.
- primary key: id

### erp_journal  (source backend: rest)
Source table erp.journal.

- `active`: integer NOT NULL — Column active of table erp.journal.
- `code`: text NOT NULL — Column code of table erp.journal.
- `id`: integer NOT NULL — Column id of table erp.journal.
- `name`: text NOT NULL — Column name of table erp.journal.
- `type`: text NOT NULL — Column type of table erp.journal.
- primary key: id

### erp_journal_entry  (source backend: s3)
Source table erp.journal_entry.

- `active`: integer NOT NULL — Column active of table erp.journal_entry.
- `amount`: float NOT NULL — Column amount of table erp.journal_entry.
- `date`: timestamp NULL — Column date of table erp.journal_entry.
- `id`: integer NOT NULL — Column id of table erp.journal_entry.
- `journal_id`: integer NULL — Column journal_id of table erp.journal_entry.
- `name`: text NULL — Column name of table erp.journal_entry.
- `partner_id`: integer NULL — Column partner_id of table erp.journal_entry.
- `ref`: text NULL — Column ref of table erp.journal_entry.
- `state`: text NULL — Column state of table erp.journal_entry.
- primary key: id

### erp_journal_item  (source backend: rest)
Source table erp.journal_item.

- `account_id`: integer NULL — Column account_id of table erp.journal_item.
- `active`: integer NOT NULL — Column active of table erp.journal_item.
- `cost_of_goods_sold`: float NOT NULL — Column cost_of_goods_sold of table erp.journal_item.
- `credit`: float NOT NULL — Column credit of table erp.journal_item.
- `date`: timestamp NOT NULL — Column date of table erp.journal_item.
- `debit`: float NOT NULL — Column debit of table erp.journal_item.
- `entry_id`: integer NULL — Column entry_id of table erp.journal_item.
- `id`: integer NOT NULL — Column id of table erp.journal_item.
- `journal_id`: integer NULL — Column journal_id of table erp.journal_item.
- `name`: text NULL — Column name of table erp.journal_item.
- `partner_id`: integer NULL — Column partner_id of table erp.journal_item.
- `product_id`: integer NULL — Column product_id of table erp.journal_item.
- `quantity`: float NULL — Column quantity of table erp.journal_item.
- `ref`: text NULL — Column ref of table erp.journal_item.
- `residual_amount`: float NULL — Column residual_amount of table erp.journal_item.
- `tax_amount`: float NULL — Column tax_amount of table erp.journal_item.
- `tax_id`: integer NULL — Column tax_id of table erp.journal_item.
- `uom_id`: integer NULL — Column uom_id of table erp.journal_item.
- primary key: id

### erp_partner  (source backend: files)
Source table erp.partner.

- `accountpayable_id`: integer NOT NULL — Column accountPayable_id of table erp.partner.
- `accountreceivable_id`: integer NOT NULL — Column accountReceivable_id of table erp.partner.
- `active`: integer NOT NULL — Column active of table erp.partner.
- `city`: text NULL — Column city of table erp.partner.
- `country`: text NULL — Column country of table erp.partner.
- `create_date`: timestamp NOT NULL — Column create_date of table erp.partner.
- `credit`: float NULL — Column credit of table erp.partner.
- `customer`: integer NULL — Column customer of table erp.partner.
- `debit`: float NULL — Column debit of table erp.partner.
- `email`: text NULL — Column email of table erp.partner.
- `fax`: text NULL — Column fax of table erp.partner.
- `id`: integer NOT NULL — Column id of table erp.partner.
- `image`: text NULL — Column image of table erp.partner.
- `image_medium`: text NULL — Column image_medium of table erp.partner.
- `is_company`: integer NULL — Column is_company of table erp.partner.
- `mobile`: text NULL — Column mobile of table erp.partner.
- `name`: text NOT NULL — Column name of table erp.partner.
- `phone`: text NULL — Column phone of table erp.partner.
- `purchase_deals`: integer NULL — Column purchase_deals of table erp.partner.
- `sale_deals`: integer NULL — Column sale_deals of table erp.partner.
- `street`: text NULL — Column street of table erp.partner.
- `supplier`: integer NULL — Column supplier of table erp.partner.
- `website`: text NULL — Column website of table erp.partner.
- primary key: id

### erp_payment  (source backend: s3)
Source table erp.payment.

- `account_id`: integer NULL — Column account_id of table erp.payment.
- `active`: integer NOT NULL — Column active of table erp.payment.
- `amount`: float NOT NULL — Column amount of table erp.payment.
- `date`: timestamp NOT NULL — Column date of table erp.payment.
- `entry_id`: integer NULL — Column entry_id of table erp.payment.
- `id`: integer NOT NULL — Column id of table erp.payment.
- `invoice_id`: integer NULL — Column invoice_id of table erp.payment.
- `journal_id`: integer NULL — Column journal_id of table erp.payment.
- `name`: text NULL — Column name of table erp.payment.
- `overpayment`: float NOT NULL — Column overpayment of table erp.payment.
- `partner_id`: integer NULL — Column partner_id of table erp.payment.
- `partner_type`: text NOT NULL — Column partner_type of table erp.payment.
- `reference`: text NULL — Column reference of table erp.payment.
- `state`: text NULL — Column state of table erp.payment.
- `type`: text NULL — Column type of table erp.payment.
- primary key: id

### erp_product  (source backend: rest)
Source table erp.product.

- `lenght`: float NULL — Column Lenght of table erp.product.
- `active`: integer NOT NULL — Column active of table erp.product.
- `categ_id`: integer NOT NULL — Column categ_id of table erp.product.
- `default_code`: text NULL — Column default_code of table erp.product.
- `description`: text NULL — Column description of table erp.product.
- `id`: integer NOT NULL — Column id of table erp.product.
- `image`: text NULL — Column image of table erp.product.
- `image_medium`: text NULL — Column image_medium of table erp.product.
- `name`: text NOT NULL — Column name of table erp.product.
- `purchase_ok`: integer NULL — Column purchase_ok of table erp.product.
- `purchase_price`: float NULL — Column purchase_price of table erp.product.
- `sale_ok`: integer NULL — Column sale_ok of table erp.product.
- `sale_price`: float NULL — Column sale_price of table erp.product.
- `uom_id`: integer NOT NULL — Column uom_id of table erp.product.
- `volume`: float NULL — Column volume of table erp.product.
- `weight`: float NULL — Column weight of table erp.product.
- primary key: id

### erp_product_category  (source backend: rest)
Source table erp.product_category.

- `active`: integer NOT NULL — Column active of table erp.product_category.
- `id`: integer NOT NULL — Column id of table erp.product_category.
- `name`: text NOT NULL — Column name of table erp.product_category.
- primary key: id

### erp_product_uom  (source backend: files)
Source table erp.product_uom.

- `active`: integer NOT NULL — Column active of table erp.product_uom.
- `category_id`: integer NOT NULL — Column category_id of table erp.product_uom.
- `decimals`: integer NOT NULL — Column decimals of table erp.product_uom.
- `id`: integer NOT NULL — Column id of table erp.product_uom.
- `name`: text NOT NULL — Column name of table erp.product_uom.
- primary key: id

### erp_product_uom_category  (source backend: files)
Source table erp.product_uom_category.

- `id`: integer NOT NULL — Column id of table erp.product_uom_category.
- `name`: text NOT NULL — Column name of table erp.product_uom_category.
- primary key: id

### erp_purchase_order  (source backend: s3)
Source table erp.purchase_order.

- `active`: integer NOT NULL — Column active of table erp.purchase_order.
- `amount_tax`: float NULL — Column amount_tax of table erp.purchase_order.
- `amount_total`: float NULL — Column amount_total of table erp.purchase_order.
- `amount_untaxed`: float NULL — Column amount_untaxed of table erp.purchase_order.
- `date`: timestamp NOT NULL — Column date of table erp.purchase_order.
- `delivery_created`: integer NULL — Column delivery_created of table erp.purchase_order.
- `discount`: integer NULL — Column discount of table erp.purchase_order.
- `id`: integer NOT NULL — Column id of table erp.purchase_order.
- `invoice_method`: text NULL — Column invoice_method of table erp.purchase_order.
- `name`: text NULL — Column name of table erp.purchase_order.
- `notes`: text NULL — Column notes of table erp.purchase_order.
- `paid`: integer NULL — Column paid of table erp.purchase_order.
- `partner_id`: integer NOT NULL — Column partner_id of table erp.purchase_order.
- `reference`: text NULL — Column reference of table erp.purchase_order.
- `shipped`: integer NULL — Column shipped of table erp.purchase_order.
- `state`: text NULL — Column state of table erp.purchase_order.
- `unpaid`: float NULL — Column unpaid of table erp.purchase_order.
- primary key: id

### erp_purchase_order_line  (source backend: files)
Source table erp.purchase_order_line.

- `active`: integer NOT NULL — Column active of table erp.purchase_order_line.
- `date`: timestamp NULL — Column date of table erp.purchase_order_line.
- `id`: integer NOT NULL — Column id of table erp.purchase_order_line.
- `invoiced`: integer NOT NULL — Column invoiced of table erp.purchase_order_line.
- `name`: text NULL — Column name of table erp.purchase_order_line.
- `order_id`: integer NOT NULL — Column order_id of table erp.purchase_order_line.
- `price`: float NOT NULL — Column price of table erp.purchase_order_line.
- `product_id`: integer NOT NULL — Column product_id of table erp.purchase_order_line.
- `quantity`: float NOT NULL — Column quantity of table erp.purchase_order_line.
- `state`: text NULL — Column state of table erp.purchase_order_line.
- `sub_total`: float NOT NULL — Column sub_total of table erp.purchase_order_line.
- `tax_id`: integer NULL — Column tax_id of table erp.purchase_order_line.
- `uom`: text NOT NULL — Column uom of table erp.purchase_order_line.
- primary key: id

### erp_sale_order  (source backend: mongodb)
Source table erp.sale_order.

- `active`: integer NOT NULL — Column active of table erp.sale_order.
- `amount_tax`: float NULL — Column amount_tax of table erp.sale_order.
- `amount_total`: float NULL — Column amount_total of table erp.sale_order.
- `amount_untaxed`: float NULL — Column amount_untaxed of table erp.sale_order.
- `date`: timestamp NOT NULL — Column date of table erp.sale_order.
- `delivery_created`: integer NULL — Column delivery_created of table erp.sale_order.
- `discount`: integer NULL — Column discount of table erp.sale_order.
- `id`: integer NOT NULL — Column id of table erp.sale_order.
- `invoice_method`: text NULL — Column invoice_method of table erp.sale_order.
- `name`: text NULL — Column name of table erp.sale_order.
- `notes`: text NULL — Column notes of table erp.sale_order.
- `paid`: integer NULL — Column paid of table erp.sale_order.
- `partner_id`: integer NOT NULL — Column partner_id of table erp.sale_order.
- `shipped`: integer NULL — Column shipped of table erp.sale_order.
- `state`: text NULL — Column state of table erp.sale_order.
- `unpaid`: float NULL — Column unpaid of table erp.sale_order.
- primary key: id

### erp_sale_order_line  (source backend: postgres)
Source table erp.sale_order_line.

- `active`: integer NOT NULL — Column active of table erp.sale_order_line.
- `date`: timestamp NULL — Column date of table erp.sale_order_line.
- `discount`: float NOT NULL — Column discount of table erp.sale_order_line.
- `id`: integer NOT NULL — Column id of table erp.sale_order_line.
- `invoiced`: integer NOT NULL — Column invoiced of table erp.sale_order_line.
- `name`: text NULL — Column name of table erp.sale_order_line.
- `order_id`: integer NOT NULL — Column order_id of table erp.sale_order_line.
- `price`: float NOT NULL — Column price of table erp.sale_order_line.
- `product_id`: integer NOT NULL — Column product_id of table erp.sale_order_line.
- `quantity`: float NOT NULL — Column quantity of table erp.sale_order_line.
- `sub_total`: float NOT NULL — Column sub_total of table erp.sale_order_line.
- `tax_id`: integer NULL — Column tax_id of table erp.sale_order_line.
- `uom`: text NOT NULL — Column uom of table erp.sale_order_line.
- primary key: id

### erp_tax  (source backend: rest)
Source table erp.tax.

- `active`: integer NOT NULL — Column active of table erp.tax.
- `amount`: float NOT NULL — Column amount of table erp.tax.
- `id`: integer NOT NULL — Column id of table erp.tax.
- `name`: text NOT NULL — Column name of table erp.tax.
- `percent`: float NOT NULL — Column percent of table erp.tax.
- `type_tax_use`: text NOT NULL — Column type_tax_use of table erp.tax.
- primary key: id

### erp_user  (source backend: mongodb)
Source table erp.user.

- `active`: integer NOT NULL — Column active of table erp.user.
- `id`: integer NOT NULL — Column id of table erp.user.
- `image`: text NULL — Column image of table erp.user.
- `login`: text NOT NULL — Column login of table erp.user.
- `name`: text NOT NULL — Column name of table erp.user.
- `password`: text NOT NULL — Column password of table erp.user.
- `user_type`: text NULL — Column user_type of table erp.user.
- primary key: id

### Relationships

- erp_delivery_order(partner_id) -> erp_partner(id) [required]
- erp_delivery_order(purchase_id) -> erp_purchase_order(id) [optional (may be NULL/dangling)]
- erp_delivery_order(sale_id) -> erp_sale_order(id) [optional (may be NULL/dangling)]
- erp_delivery_order_line(delivery_id) -> erp_delivery_order(id) [required]
- erp_delivery_order_line(partner_id) -> erp_partner(id) [optional (may be NULL/dangling)]
- erp_delivery_order_line(product_id) -> erp_product(id) [required]
- erp_inventory(product_id) -> erp_product(id) [required]
- erp_invoice(account_id) -> erp_account(id) [optional (may be NULL/dangling)]
- erp_invoice(entry_id) -> erp_journal_entry(id) [optional (may be NULL/dangling)]
- erp_invoice(journal_id) -> erp_journal(id) [optional (may be NULL/dangling)]
- erp_invoice(partner_id) -> erp_partner(id) [required]
- erp_invoice(purchase_id) -> erp_purchase_order(id) [optional (may be NULL/dangling)]
- erp_invoice(sale_id) -> erp_sale_order(id) [optional (may be NULL/dangling)]
- erp_invoice_line(account_id) -> erp_account(id) [optional (may be NULL/dangling)]
- erp_invoice_line(invoice_id) -> erp_invoice(id) [required]
- erp_invoice_line(partner_id) -> erp_partner(id) [required]
- erp_invoice_line(product_id) -> erp_product(id) [optional (may be NULL/dangling)]
- erp_invoice_line(tax_id) -> erp_tax(id) [optional (may be NULL/dangling)]
- erp_invoice_payment(invoice_id) -> erp_invoice(id) [required]
- erp_invoice_payment(journal_entry_id) -> erp_journal_entry(id) [required]
- erp_invoice_tax(account_id) -> erp_account(id) [optional (may be NULL/dangling)]
- erp_invoice_tax(invoice_id) -> erp_invoice(id) [optional (may be NULL/dangling)]
- erp_invoice_tax(tax_id) -> erp_tax(id) [optional (may be NULL/dangling)]
- erp_journal_entry(journal_id) -> erp_journal(id) [optional (may be NULL/dangling)]
- erp_journal_entry(partner_id) -> erp_partner(id) [optional (may be NULL/dangling)]
- erp_journal_item(account_id) -> erp_account(id) [optional (may be NULL/dangling)]
- erp_journal_item(entry_id) -> erp_journal_entry(id) [optional (may be NULL/dangling)]
- erp_journal_item(journal_id) -> erp_journal(id) [optional (may be NULL/dangling)]
- erp_journal_item(partner_id) -> erp_partner(id) [optional (may be NULL/dangling)]
- erp_journal_item(product_id) -> erp_product(id) [optional (may be NULL/dangling)]
- erp_journal_item(tax_id) -> erp_tax(id) [optional (may be NULL/dangling)]
- erp_journal_item(uom_id) -> erp_product_uom(id) [optional (may be NULL/dangling)]
- erp_partner(accountpayable_id) -> erp_account(id) [required]
- erp_partner(accountreceivable_id) -> erp_account(id) [required]
- erp_payment(account_id) -> erp_account(id) [optional (may be NULL/dangling)]
- erp_payment(entry_id) -> erp_journal_entry(id) [optional (may be NULL/dangling)]
- erp_payment(invoice_id) -> erp_invoice(id) [optional (may be NULL/dangling)]
- erp_payment(journal_id) -> erp_journal(id) [optional (may be NULL/dangling)]
- erp_payment(partner_id) -> erp_partner(id) [optional (may be NULL/dangling)]
- erp_product(categ_id) -> erp_product_category(id) [required]
- erp_product(uom_id) -> erp_product_uom(id) [required]
- erp_product_uom(category_id) -> erp_product_uom_category(id) [required]
- erp_purchase_order(partner_id) -> erp_partner(id) [required]
- erp_purchase_order_line(order_id) -> erp_purchase_order(id) [required]
- erp_purchase_order_line(product_id) -> erp_product(id) [required]
- erp_purchase_order_line(tax_id) -> erp_tax(id) [optional (may be NULL/dangling)]
- erp_sale_order(partner_id) -> erp_partner(id) [required]
- erp_sale_order_line(order_id) -> erp_sale_order(id) [required]
- erp_sale_order_line(product_id) -> erp_product(id) [required]
- erp_sale_order_line(tax_id) -> erp_tax(id) [optional (may be NULL/dangling)]

