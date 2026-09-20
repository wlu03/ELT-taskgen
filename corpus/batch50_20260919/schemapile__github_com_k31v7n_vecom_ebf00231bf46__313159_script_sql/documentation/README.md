# Generated runtime references

The files in this directory mirror the shared Terraform and Airbyte references
in the original ELT-Bench solver inputs. The task-specific specification is
included below.

# SchemaPile schema: Github Com K31v7n Vecom

## Specification

PROJECT OVERVIEW

This project builds three analytical marts over the cventa operational schema (the 313159_script.sql schema). Each source table must be extracted from the backend named beside it, and nothing else may be read.

Source tables and their extraction backends:
- cventa_bitacora_password must be extracted from the files backend.
- cventa_bodega must be extracted from the postgres backend.
- cventa_bodega_movimiento must be extracted from the mongodb backend.
- cventa_bodega_movimiento_tipo must be extracted from the files backend.
- cventa_bodega_producto must be extracted from the rest backend.
- cventa_bodega_tipo must be extracted from the s3 backend.
- cventa_cliente must be extracted from the postgres backend.
- cventa_cliente_tipo must be extracted from the s3 backend.
- cventa_compra must be extracted from the mongodb backend.
- cventa_compra_detalle must be extracted from the s3 backend.
- cventa_compra_estatus must be extracted from the mongodb backend.
- cventa_empresa must be extracted from the files backend.
- cventa_factura must be extracted from the postgres backend.
- cventa_factura_estatus must be extracted from the postgres backend.
- cventa_factura_serie must be extracted from the postgres backend.
- cventa_factura_venta must be extracted from the postgres backend.
- cventa_menu must be extracted from the files backend.
- cventa_modulo must be extracted from the s3 backend.
- cventa_moneda must be extracted from the postgres backend.
- cventa_pais_empresa must be extracted from the files backend.
- cventa_producto must be extracted from the files backend.
- cventa_producto_tipo must be extracted from the s3 backend.
- cventa_proveedor must be extracted from the mongodb backend.
- cventa_proveedor_clasificacion must be extracted from the rest backend.
- cventa_proveedor_tipo must be extracted from the mongodb backend.
- cventa_rol must be extracted from the rest backend.
- cventa_submenu must be extracted from the files backend.
- cventa_tipo_pago must be extracted from the rest backend.
- cventa_unidad_medida must be extracted from the rest backend.
- cventa_usuario must be extracted from the postgres backend.
- cventa_usuario_empresa must be extracted from the postgres backend.
- cventa_usuario_genero must be extracted from the mongodb backend.
- cventa_usuario_menu must be extracted from the rest backend.
- cventa_vendedor must be extracted from the rest backend.
- cventa_vendedor_cliente must be extracted from the files backend.
- cventa_venta must be extracted from the mongodb backend.
- cventa_venta_detalle must be extracted from the postgres backend.
- cventa_venta_estatus must be extracted from the files backend.

Relationships in the source schema. Each line names the child table with its keys and the parent table with its keys, and is labelled exactly as the source schema labels it:
- child cventa_bitacora_password(usuario) refers to parent cventa_usuario(usuario): required.
- child cventa_bodega(bodega_tipo) refers to parent cventa_bodega_tipo(bodega_tipo): required.
- child cventa_bodega(empresa) refers to parent cventa_empresa(empresa): required.
- child cventa_bodega_movimiento(bodega) refers to parent cventa_bodega(bodega): required.
- child cventa_bodega_movimiento(bodega_movimiento_tipo) refers to parent cventa_bodega_movimiento_tipo(bodega_movimiento_tipo): required.
- child cventa_bodega_movimiento(producto) refers to parent cventa_producto(producto): required.
- child cventa_bodega_movimiento(usuario) refers to parent cventa_usuario(usuario): required.
- child cventa_bodega_producto(bodega) refers to parent cventa_bodega(bodega): required.
- child cventa_bodega_producto(producto) refers to parent cventa_producto(producto): required.
- child cventa_bodega_producto(usuario) refers to parent cventa_usuario(usuario): required.
- child cventa_cliente(cliente_tipo) refers to parent cventa_cliente_tipo(cliente_tipo): required.
- child cventa_cliente(empresa) refers to parent cventa_empresa(empresa): required.
- child cventa_cliente(usuario) refers to parent cventa_usuario(usuario): required.
- child cventa_compra(compra_estatus) refers to parent cventa_compra_estatus(compra_estatus): required.
- child cventa_compra(moneda) refers to parent cventa_moneda(moneda): required.
- child cventa_compra(proveedor) refers to parent cventa_proveedor(proveedor): required.
- child cventa_compra(tipo_pago) refers to parent cventa_tipo_pago(tipo_pago): required.
- child cventa_compra(usuario) refers to parent cventa_usuario(usuario): required.
- child cventa_compra_detalle(compra) refers to parent cventa_compra(compra): required.
- child cventa_compra_detalle(producto) refers to parent cventa_producto(producto): required.
- child cventa_empresa(moneda) refers to parent cventa_moneda(moneda): required.
- child cventa_empresa(pais_empresa) refers to parent cventa_pais_empresa(pais_empresa): required.
- child cventa_factura(factura_estatus) refers to parent cventa_factura_estatus(factura_estatus): required.
- child cventa_factura(factura_serie) refers to parent cventa_factura_serie(factura_serie): required.
- child cventa_factura(usuario) refers to parent cventa_usuario(usuario): required.
- child cventa_factura_serie(empresa) refers to parent cventa_empresa(empresa): required.
- child cventa_factura_venta(factura) refers to parent cventa_factura(factura): required.
- child cventa_factura_venta(venta) refers to parent cventa_venta(venta): required.
- child cventa_menu(modulo) refers to parent cventa_modulo(modulo): required.
- child cventa_menu(submenu) refers to parent cventa_submenu(submenu): required.
- child cventa_producto(producto_tipo) refers to parent cventa_producto_tipo(producto_tipo): required.
- child cventa_producto(proveedor) refers to parent cventa_proveedor(proveedor): required.
- child cventa_producto(unidad_medida) refers to parent cventa_unidad_medida(unidad_medida): required.
- child cventa_producto(usuario) refers to parent cventa_usuario(usuario): required.
- child cventa_proveedor(empresa) refers to parent cventa_empresa(empresa): required.
- child cventa_proveedor(proveedor_clasificacion) refers to parent cventa_proveedor_clasificacion(proveedor_clasificacion): required.
- child cventa_proveedor(proveedor_tipo) refers to parent cventa_proveedor_tipo(proveedor_tipo): required.
- child cventa_proveedor(usuario) refers to parent cventa_usuario(usuario): required.
- child cventa_usuario(empresa) refers to parent cventa_empresa(empresa): required.
- child cventa_usuario(rol) refers to parent cventa_rol(rol): required.
- child cventa_usuario(usuario_genero) refers to parent cventa_usuario_genero(usuario_genero): required.
- child cventa_usuario_empresa(empresa) refers to parent cventa_empresa(empresa): required.
- child cventa_usuario_empresa(usuario) refers to parent cventa_usuario(usuario): required.
- child cventa_usuario_menu(menu) refers to parent cventa_menu(menu): required.
- child cventa_usuario_menu(usuario) refers to parent cventa_usuario(usuario): required.
- child cventa_vendedor(usuario) refers to parent cventa_usuario(usuario): required.
- child cventa_vendedor_cliente(cliente) refers to parent cventa_cliente(cliente): required.
- child cventa_vendedor_cliente(vendedor) refers to parent cventa_vendedor(vendedor): required.
- child cventa_venta(cliente) refers to parent cventa_cliente(cliente): required.
- child cventa_venta(moneda) refers to parent cventa_moneda(moneda): required.
- child cventa_venta(tipo_pago) refers to parent cventa_tipo_pago(tipo_pago): required.
- child cventa_venta(usuario) refers to parent cventa_usuario(usuario): required.
- child cventa_venta(venta_estatus) refers to parent cventa_venta_estatus(venta_estatus): required.
- child cventa_venta_detalle(producto) refers to parent cventa_producto(producto): required.
- child cventa_venta_detalle(venta) refers to parent cventa_venta(venta): required.

Throughout, every ratio is a fraction rounded to 4 decimal places, and text labels are written exactly as quoted.

=== Mart cventa_venta_estatus_cventa_venta_snapshot: Per-cventa_venta_estatus latest-row snapshot over linked cventa_venta activity in the 313159_script.sql schema ===

Grain: one row per cventa_venta_estatus (venta_estatus), INCLUDING cventa_venta_estatus rows with no linked cventa_venta rows.

Key column: parent_key.

Output columns.
- parent_key (integer): the identifier of the cventa_venta_estatus row; one row per value.
- parent_name (text): the nombre of the cventa_venta_estatus row, copied unchanged.
- event_count (bigint): the number of cventa_venta rows for this cventa_venta_estatus row; 0 when there are none. A cventa_venta_estatus row kept with no cventa_venta row reports 0 here, never 1: its placeholder holds no cventa_venta row to count.
- lifetime_amount (decimal): the total of iva over all matching cventa_venta rows; 0 when there are no rows.
- latest_row_id (integer): the venta of the row with the latest fecha_sis; ties take the smallest venta. It is 0 when there are no rows.
- latest_amount (decimal): the iva from that same latest row; 0 when there are no rows.
- latest_label (text): the concepto from that same latest row; '(none)' when there are no rows.
- latest_amount_share (float): latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0.

Rules.
1. The source table cventa_venta_estatus is read in full.
2. The source table cventa_venta is read in full.
3. From cventa_venta_estatus there is one row per cventa_venta_estatus row, keyed by venta_estatus, carrying parent_key and parent_name.
4. The cventa_venta rows are brought in by matching venta_estatus of cventa_venta to parent_key, carrying venta_estatus; preservation is left-sided on the cventa_venta_estatus side, so a cventa_venta_estatus row with no matching cventa_venta row is retained and receives the stated empty snapshot values.
5. There is one output row per parent_key, carrying parent_name beside the keys: a parent_key value identifies one source row for the carried columns, so parent_name takes one value per key and never splits a group, and each such row reports event_count and lifetime_amount for that row's matching rows.
6. For each parent_key the single row at which the ordering measure fecha_sis is largest survives, ties broken by the smallest venta, and latest_row_id, latest_amount and latest_label are taken from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows.
7. The extremal row's attributes are attached to the grouped measures by matching parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.
8. The mart columns are named parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label; lifetime_amount reports its declared default of 0 — never NULL — for a group with no matching rows.
9. Guarded ratio, carried beside parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount and latest_label: latest_amount_share is latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places, and is 0.0 when the denominator lifetime_amount is 0 or has no value.
10. Deterministic output order: rows appear sorted by parent_key ascending.

=== Mart cventa_usuario_genero_cventa_usuario_distribution: Per-(cventa_usuario_genero, measure state) distribution of linked cventa_usuario activity in the 313159_script.sql schema ===

Grain: one row per (usuario_genero, measure state) pair represented by linked cventa_usuario rows, plus one absent no-activity row for a cventa_usuario_genero row with no links. Because activo is required, no linked cventa_usuario row belongs to the absent state.

Key columns: entity_key and measure_state.

Output columns.
- entity_key (integer): the identifier of the cventa_usuario_genero row.
- measure_state (text): 'present' for a linked cventa_usuario row; 'absent' only for a cventa_usuario_genero row with no linked cventa_usuario row. activo is required on every real cventa_usuario row.
- entity_name (text): the codigo of the cventa_usuario_genero row, copied unchanged.
- row_count (bigint): the number of linked cventa_usuario rows in this entity/state cell; 0 for a no-activity absent cell.
- distinct_amount_count (bigint): the number of unique activo values in this cell; each unique value is counted once, however many rows repeat it; 0 for a no-activity absent cell.
- total_amount (integer): the total of activo in this cell; 0 for a no-activity absent cell.
- max_amount (integer): the largest activo in this cell; 0 for a no-activity absent cell.
- max_amount_share (float): max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.

Rules.
1. The source table cventa_usuario_genero is read in full.
2. The source table cventa_usuario is read in full.
3. From cventa_usuario_genero, each usuario_genero and its codigo are carried into the measure-state calculation as entity_key and entity_name.
4. The linked cventa_usuario rows are brought into each cventa_usuario_genero entity by matching usuario_genero of cventa_usuario to entity_key, carrying entity_key, entity_name and usuario_genero; preservation is left-sided, so an entity with no linked row is retained and its absent state is visible.
5. The present measure-state rows are kept, carrying entity_key and entity_name: a row is in the present state when it is a real cventa_usuario row; activo is required on every such row.
6. In the present measure state there is one row per cventa_usuario_genero entity that has at least one row in that state, and no row here for an entity with none, reporting row_count as its row count, distinct_amount_count as how many different activo values occur (each different value counted once, however many rows repeat it), total_amount as the total activo, and max_amount as the largest activo, carried beside entity_key and entity_name.
7. For those present-state rows, carried beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount: max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.
8. These measures are labelled as the present measure state, so measure_state reads 'present' beside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.
9. The absent measure-state rows are kept, carrying entity_key and entity_name: the retained placeholder for a cventa_usuario_genero row with no cventa_usuario rows; no real row can enter this state because activo is required.
10. In the absent measure state there is one row per cventa_usuario_genero entity with no linked cventa_usuario row at all, whose retained placeholder is its one row in that state, and no row here for an entity that has a linked cventa_usuario row, reporting a row_count of 0, 0 different activo values in distinct_amount_count, a total activo of 0 in total_amount and a largest activo of 0 in max_amount, carried beside entity_key and entity_name.
11. For those absent-state rows, carried beside entity_key, entity_name, row_count, distinct_amount_count, total_amount and max_amount: max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0.
12. These measures are labelled as the absent measure state, so measure_state reads 'absent' beside entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share.
13. The present-state summary and the absent-state summary are stacked into one output list carrying entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount and max_amount_share: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other.
14. Deterministic output order: rows appear sorted by entity_key ascending, then by measure_state ascending.

=== Mart cventa_usuario_cventa_venta_top: Per-cventa_usuario extremes over linked cventa_venta rows in the 313159_script.sql schema — WHICH row is largest, not how large it is ===

Grain: one row per cventa_usuario (usuario), INCLUDING cventa_usuario rows with no linked cventa_venta rows.

Key column: parent_key.

Output columns.
- parent_key (integer): the identifier of the cventa_usuario row; one row per value.
- parent_name (text): the alias of the cventa_usuario row, copied unchanged.
- top_measure (decimal): the largest iva itself; 0 when the parent has no cventa_venta rows.
- tied_count (bigint): how many cventa_venta rows are tied at that largest iva; 1 when exactly one row carries that largest iva; 0 when there are no rows.
- child_count (bigint): the number of cventa_venta rows for this cventa_usuario row; 0 when there are none. A cventa_usuario row kept with no cventa_venta row reports 0 here, never 1: its placeholder holds no cventa_venta row to count.
- total_measure (decimal): the total of iva over all of them; 0 when the parent has no cventa_venta rows.
- top_label (text): the concepto of the cventa_venta row with the LARGEST iva for this cventa_usuario row. Ties in iva are broken by taking the SMALLEST concepto under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one); rows tied on both are resolved by the smallest venta. It is the literal '(none)' when the parent has no cventa_venta rows at all.
- top_row_id (integer): the venta of that same extremal row — the winner under the SAME total order, so it is the identifier of a real cventa_venta row whenever the parent has any. It is 0 when there are no rows, and only then. It identifies WHICH row won, so a tie resolved the wrong way is visible even when two rows share a label.
- top_measure_share (float): top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0.
- tie_state (text): 'empty' when no row holds a maximum at all — the parent has no cventa_venta rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do.

Rules.
1. The source table cventa_usuario is read in full.
2. The source table cventa_venta is read in full.
3. From cventa_usuario there is one row per cventa_usuario row, keyed by usuario, carrying parent_key and parent_name.
4. The cventa_venta rows are brought in by matching usuario of cventa_venta to parent_key, carrying usuario; preservation is left-sided, so a cventa_usuario row with no cventa_venta rows still appears, with the declared defaults.
5. Within each parent_key group the matched rows are ranked under an explicit total order — the measure iva first, then the declared tie-break — so the extremal row is a function of the input and not of row order.
6. There is one output row per parent_key, carrying parent_name beside the keys: a parent_key value identifies one source row for the carried columns, so parent_name takes one value per key and never splits a group, and each such row reports top_measure, tied_count, child_count and total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1.
7. For each parent_key the single row at which the ordering measure iva is largest survives, ties broken by the smallest concepto under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then the smallest venta, and top_label and top_row_id are taken from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows.
8. The extremal row's attributes are attached to the grouped measures by matching parent_key; preservation is left-sided, so a group with no rows at all keeps its measures.
9. The mart columns are named parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id; top_measure and total_measure report their declared defaults of 0 — never NULL — for a group with no matching rows.
10. Guarded ratio, carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label and top_row_id: top_measure_share is top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, and 0.0 when the denominator total_measure is 0 or has no value.
11. Carried beside parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id and top_measure_share, tie_state is 'empty' when no row holds a maximum at all — the parent has no cventa_venta rows — 'unique' when exactly one row holds the maximum, and 'tied' when two or more do; equivalently tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, otherwise 'tied' when it is 2 or more. This is a categorical mapping with no numeric boundary, and tie_state is never null or blank.
12. Deterministic output order: rows appear sorted by parent_key ascending.

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `cventa_venta_estatus_cventa_venta_snapshot`

- Grain: One row per cventa_venta_estatus (venta_estatus), INCLUDING cventa_venta_estatus rows with no linked cventa_venta rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share

```text
Mart 'cventa_venta_estatus_cventa_venta_snapshot' has 10 declared semantic rules:
1. [source] Read source table cventa_venta_estatus. (public source tables: cventa_venta_estatus)
2. [source] Read source table cventa_venta. (public source tables: cventa_venta)
3. [derive] One row per cventa_venta_estatus row, keyed by venta_estatus. (public source tables: cventa_venta_estatus | public carried/output columns: parent_key, parent_name)
4. [join] Bring in cventa_venta; a cventa_venta_estatus row with no matching cventa_venta row is retained and receives the stated empty snapshot values. (public source tables: cventa_venta | public carried/output columns: venta_estatus | join preservation: left | condition public identifiers: cventa_venta, venta_estatus, parent_key)
5. [aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting event_count, lifetime_amount for that row's matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount)
6. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest venta, and take latest_row_id, latest_amount, latest_label from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, latest_row_id, latest_amount, latest_label)
7. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
8. [derive] Name the mart columns; lifetime_amount reports its declared default — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label)
9. [ratio] Guarded ratios: latest_amount_share — latest_amount divided by lifetime_amount as a fraction, rounded to 4 decimal places; 0.0 when lifetime_amount is 0. (public carried/output columns: parent_key, parent_name, event_count, lifetime_amount, latest_row_id, latest_amount, latest_label, latest_amount_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
10. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

### `cventa_usuario_genero_cventa_usuario_distribution`

- Grain: One row per (usuario_genero, measure state) pair represented by linked cventa_usuario rows, plus one absent no-activity row for a cventa_usuario_genero row with no links. Because activo is required, no linked cventa_usuario row belongs to the absent state.
- Unique key: entity_key, measure_state
- Required columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share

```text
Mart 'cventa_usuario_genero_cventa_usuario_distribution' has 14 declared semantic rules:
1. [source] Read source table cventa_usuario_genero. (public source tables: cventa_usuario_genero)
2. [source] Read source table cventa_usuario. (public source tables: cventa_usuario)
3. [derive] Carry each usuario_genero and its codigo into the measure-state calculation. (public source tables: cventa_usuario_genero | public carried/output columns: entity_key, entity_name)
4. [join] Bring the linked cventa_usuario rows into each cventa_usuario_genero entity; retain an entity with no linked row so its absent state is visible. (public source tables: cventa_usuario | public carried/output columns: entity_key, entity_name, usuario_genero | join preservation: left | condition public identifiers: cventa_usuario, usuario_genero, entity_key)
5. [filter] Keep the present measure-state rows: a real cventa_usuario row; activo is required on every such row. (public carried/output columns: entity_key, entity_name)
6. [distinct] One row per cventa_usuario_genero entity that has at least one row in the present measure state, and no row here for an entity with none, reporting row count, how many different activo values occur (each different value counted once, however many rows repeat it), total activo, and largest activo. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
7. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
8. [derive] Label these measures as the present measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
9. [filter] Keep the absent measure-state rows: the retained placeholder for a cventa_usuario_genero row with no cventa_usuario rows; no real row can enter this state because activo is required. (public carried/output columns: entity_key, entity_name)
10. [distinct] One row per cventa_usuario_genero entity with no linked cventa_usuario row at all, whose retained placeholder is its one row in the absent measure state, and no row here for an entity that has a linked cventa_usuario row, reporting a row count of 0, 0 different activo values, a total activo of 0 and a largest activo of 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount)
11. [ratio] max_amount_share is max_amount divided by total_amount as a fraction, rounded to 4 decimal places; 0.0 when total_amount is 0. (public carried/output columns: entity_key, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: null_result=0.0 when total_amount is 0; rounding=ROUND to 4 decimal places; units=fraction)
12. [derive] Label these measures as the absent measure state. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share)
13. [union] Stack the present-state summary and the absent-state summary into one output list: every present-state row and every absent-state row is its own output row, an entity with rows in both states appears twice, once per state, and the two summaries are never matched to each other. (public carried/output columns: entity_key, measure_state, entity_name, row_count, distinct_amount_count, total_amount, max_amount, max_amount_share | semantic parameters: mode=all)
14. [tie_break] Deterministic output order: entity, then measure state. (public carried/output columns: entity_key, measure_state)
```

### `cventa_usuario_cventa_venta_top`

- Grain: One row per cventa_usuario (usuario), INCLUDING cventa_usuario rows with no linked cventa_venta rows.
- Unique key: parent_key
- Required columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state

```text
Mart 'cventa_usuario_cventa_venta_top' has 12 declared semantic rules:
1. [source] Read source table cventa_usuario. (public source tables: cventa_usuario)
2. [source] Read source table cventa_venta. (public source tables: cventa_venta)
3. [derive] One row per cventa_usuario row, keyed by usuario. (public source tables: cventa_usuario | public carried/output columns: parent_key, parent_name)
4. [join] Bring in cventa_venta: a cventa_usuario row with no cventa_venta rows still appears, with the declared defaults. (public source tables: cventa_venta | public carried/output columns: usuario | join preservation: left | condition public identifiers: cventa_venta, usuario, parent_key)
5. [window] Rank the joined rows within each group under an explicit total order (measure first, then the declared tie-break), so the extremal row is a function of the input and not of row order. (public carried/output columns: parent_key)
6. [filtered_aggregate] One output row per parent_key, carrying parent_name beside the keys: a key value identifies one source row for the carried columns, so they take one value per key and never split a group, reporting top_measure, tied_count, child_count, total_measure for that row's matching rows. A group with no qualifying rows still appears, reporting 0; a retained row with no matching rows has nothing to count, so its counts are 0, never 1. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure)
7. [extrema] Keep the single row per parent_key at which the ordering measure is largest, ties broken by the smallest concepto under a plain case-sensitive comparison of the stored text (the warehouse default order, in which every uppercase letter sorts before every lowercase one), then the smallest venta, and take top_label, top_row_id from that winning row. The ordering measure is required on every real input row, so a non-empty group always has a winning row; the declared defaults belong only to a group with NO rows. (public carried/output columns: parent_key, top_label, top_row_id)
8. [join] Attach the extremal row's attributes to the grouped measures. LEFT, so a group with no rows at all keeps its measures. (public carried/output columns: parent_key | join preservation: left | condition public identifiers: parent_key)
9. [derive] Name the mart columns; top_measure, total_measure report their declared defaults — never NULL — for a group with no matching rows. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id)
10. [ratio] Guarded ratios: top_measure_share — top_measure divided by total_measure as a FRACTION between 0 and 1 (not a percentage), rounded to 4 decimals, 0.0 when total_measure is 0. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share | semantic parameters: null_result=0.0 when the denominator is 0 or NULL; rounding=ROUND to 4 decimal places; units=fraction)
11. [conditional] tie_state — 'empty' when no row holds a maximum at all — the parent has no cventa_venta rows — 'unique' when exactly one row holds the maximum, 'tied' when two or more do. Equivalently, tie_state follows tied_count: 'empty' when tied_count is 0, 'unique' when it is 1, 'tied' when it is 2 or more. (public carried/output columns: parent_key, parent_name, top_measure, tied_count, child_count, total_measure, top_label, top_row_id, top_measure_share, tie_state | semantic parameters: boundary=categorical mapping; no numeric boundary)
12. [tie_break] Deterministic output order: sort by parent_key. (public carried/output columns: parent_key)
```

## Source tables

### cventa_bitacora_password  (source backend: files)
Source table cventa.bitacora_password.

- `bitacora_password`: integer NOT NULL — Column bitacora_password of table cventa.bitacora_password.
- `fecha_cambio`: timestamp NOT NULL — Column fecha_cambio of table cventa.bitacora_password.
- `ip`: text NULL — Column ip of table cventa.bitacora_password.
- `usuario`: integer NOT NULL — Column usuario of table cventa.bitacora_password.
- primary key: bitacora_password

### cventa_bodega  (source backend: postgres)
Source table cventa.bodega.

- `bodega`: integer NOT NULL — Column bodega of table cventa.bodega.
- `bodega_tipo`: integer NOT NULL — Column bodega_tipo of table cventa.bodega.
- `codigo`: text NULL — Column codigo of table cventa.bodega.
- `empresa`: integer NOT NULL — Column empresa of table cventa.bodega.
- `nombre`: text NOT NULL — Column nombre of table cventa.bodega.
- `ubicacion`: text NULL — Column ubicacion of table cventa.bodega.
- primary key: bodega

### cventa_bodega_movimiento  (source backend: mongodb)
Source table cventa.bodega_movimiento.

- `bodega`: integer NOT NULL — Column bodega of table cventa.bodega_movimiento.
- `bodega_movimiento`: integer NOT NULL — Column bodega_movimiento of table cventa.bodega_movimiento.
- `bodega_movimiento_tipo`: integer NOT NULL — Column bodega_movimiento_tipo of table cventa.bodega_movimiento.
- `cantidad`: integer NOT NULL — Column cantidad of table cventa.bodega_movimiento.
- `fecha_accion`: timestamp NOT NULL — Column fecha_accion of table cventa.bodega_movimiento.
- `producto`: integer NOT NULL — Column producto of table cventa.bodega_movimiento.
- `usuario`: integer NOT NULL — Column usuario of table cventa.bodega_movimiento.
- primary key: bodega_movimiento

### cventa_bodega_movimiento_tipo  (source backend: files)
Source table cventa.bodega_movimiento_tipo.

- `bodega_movimiento_tipo`: integer NOT NULL — Column bodega_movimiento_tipo of table cventa.bodega_movimiento_tipo.
- `nombre`: text NOT NULL — Column nombre of table cventa.bodega_movimiento_tipo.
- primary key: bodega_movimiento_tipo

### cventa_bodega_producto  (source backend: rest)
Source table cventa.bodega_producto.

- `activo`: integer NOT NULL — Column activo of table cventa.bodega_producto.
- `bodega`: integer NOT NULL — Column bodega of table cventa.bodega_producto.
- `bodega_producto`: integer NOT NULL — Column bodega_producto of table cventa.bodega_producto.
- `fecha_ingreso`: timestamp NOT NULL — Column fecha_ingreso of table cventa.bodega_producto.
- `producto`: integer NOT NULL — Column producto of table cventa.bodega_producto.
- `usuario`: integer NOT NULL — Column usuario of table cventa.bodega_producto.
- primary key: bodega_producto

### cventa_bodega_tipo  (source backend: s3)
Source table cventa.bodega_tipo.

- `bodega_tipo`: integer NOT NULL — Column bodega_tipo of table cventa.bodega_tipo.
- `nombre`: text NOT NULL — Column nombre of table cventa.bodega_tipo.
- primary key: bodega_tipo

### cventa_cliente  (source backend: postgres)
Source table cventa.cliente.

- `activo`: integer NOT NULL — Column activo of table cventa.cliente.
- `aplica_descuento`: integer NOT NULL — Column aplica_descuento of table cventa.cliente.
- `aplica_iva`: integer NOT NULL — Column aplica_iva of table cventa.cliente.
- `cliente`: integer NOT NULL — Column cliente of table cventa.cliente.
- `cliente_tipo`: integer NOT NULL — Column cliente_tipo of table cventa.cliente.
- `correo`: text NULL — Column correo of table cventa.cliente.
- `direccion`: text NULL — Column direccion of table cventa.cliente.
- `empresa`: integer NOT NULL — Column empresa of table cventa.cliente.
- `fecha_sis`: timestamp NOT NULL — Column fecha_sis of table cventa.cliente.
- `monto_descuento`: decimal NOT NULL — Column monto_descuento of table cventa.cliente.
- `nit`: text NULL — Column nit of table cventa.cliente.
- `nombre`: text NOT NULL — Column nombre of table cventa.cliente.
- `telefono`: integer NULL — Column telefono of table cventa.cliente.
- `usuario`: integer NOT NULL — Column usuario of table cventa.cliente.
- primary key: cliente

### cventa_cliente_tipo  (source backend: s3)
Source table cventa.cliente_tipo.

- `cliente_tipo`: integer NOT NULL — Column cliente_tipo of table cventa.cliente_tipo.
- `nombre`: text NULL — Column nombre of table cventa.cliente_tipo.
- primary key: cliente_tipo

### cventa_compra  (source backend: mongodb)
Source table cventa.compra.

- `compra`: integer NOT NULL — Column compra of table cventa.compra.
- `compra_estatus`: integer NOT NULL — Column compra_estatus of table cventa.compra.
- `concepto`: text NULL — Column concepto of table cventa.compra.
- `factura_numero`: text NOT NULL — Column factura_numero of table cventa.compra.
- `fecha_compra`: timestamp NOT NULL — Column fecha_compra of table cventa.compra.
- `fecha_factura`: date NOT NULL — Column fecha_factura of table cventa.compra.
- `fecha_pago`: date NOT NULL — Column fecha_pago of table cventa.compra.
- `moneda`: integer NOT NULL — Column moneda of table cventa.compra.
- `monto`: decimal NULL — Column monto of table cventa.compra.
- `proveedor`: integer NOT NULL — Column proveedor of table cventa.compra.
- `serie`: text NOT NULL — Column serie of table cventa.compra.
- `tipo_pago`: integer NOT NULL — Column tipo_pago of table cventa.compra.
- `usuario`: integer NOT NULL — Column usuario of table cventa.compra.
- `valor_base`: decimal NOT NULL — Column valor_base of table cventa.compra.
- `valor_iva`: decimal NOT NULL — Column valor_iva of table cventa.compra.
- primary key: compra

### cventa_compra_detalle  (source backend: s3)
Source table cventa.compra_detalle.

- `anulado`: integer NOT NULL — Column anulado of table cventa.compra_detalle.
- `cantidad`: integer NOT NULL — Column cantidad of table cventa.compra_detalle.
- `compra`: integer NOT NULL — Column compra of table cventa.compra_detalle.
- `compra_detalle`: integer NOT NULL — Column compra_detalle of table cventa.compra_detalle.
- `precio`: decimal NOT NULL — Column precio of table cventa.compra_detalle.
- `producto`: integer NOT NULL — Column producto of table cventa.compra_detalle.
- `total`: decimal NOT NULL — Column total of table cventa.compra_detalle.
- primary key: compra_detalle

### cventa_compra_estatus  (source backend: mongodb)
Source table cventa.compra_estatus.

- `compra_estatus`: integer NOT NULL — Column compra_estatus of table cventa.compra_estatus.
- `nombre`: text NOT NULL — Column nombre of table cventa.compra_estatus.
- primary key: compra_estatus

### cventa_empresa  (source backend: files)
Source table cventa.empresa.

- `abreviatura`: text NOT NULL — Column abreviatura of table cventa.empresa.
- `activo`: integer NOT NULL — Column activo of table cventa.empresa.
- `aplica_iva`: integer NOT NULL — Monto de iva que aplica la empresa
- `direccion`: text NOT NULL — Column direccion of table cventa.empresa.
- `empresa`: integer NOT NULL — Column empresa of table cventa.empresa.
- `fecha_sis`: timestamp NOT NULL — Column fecha_sis of table cventa.empresa.
- `logo`: text NULL — Column logo of table cventa.empresa.
- `moneda`: integer NOT NULL — Column moneda of table cventa.empresa.
- `nit`: text NULL — Column nit of table cventa.empresa.
- `nombre`: text NOT NULL — Column nombre of table cventa.empresa.
- `pais_empresa`: integer NOT NULL — Column pais_empresa of table cventa.empresa.
- `representante`: text NULL — Encargado de la empresa
- `telefono`: text NOT NULL — Column telefono of table cventa.empresa.
- primary key: empresa

### cventa_factura  (source backend: postgres)
Source table cventa.factura.

- `factura`: integer NOT NULL — Column factura of table cventa.factura.
- `factura_estatus`: integer NOT NULL — Column factura_estatus of table cventa.factura.
- `factura_serie`: integer NOT NULL — Column factura_serie of table cventa.factura.
- `fecha`: date NULL — Column fecha of table cventa.factura.
- `monto`: decimal NOT NULL — Column monto of table cventa.factura.
- `numero`: text NULL — Column numero of table cventa.factura.
- `usuario`: integer NOT NULL — Column usuario of table cventa.factura.
- primary key: factura

### cventa_factura_estatus  (source backend: postgres)
Source table cventa.factura_estatus.

- `factura_estatus`: integer NOT NULL — Column factura_estatus of table cventa.factura_estatus.
- `nombre`: text NOT NULL — Column nombre of table cventa.factura_estatus.
- primary key: factura_estatus

### cventa_factura_serie  (source backend: postgres)
Source table cventa.factura_serie.

- `activo`: integer NOT NULL — Column activo of table cventa.factura_serie.
- `codigo`: text NULL — Column codigo of table cventa.factura_serie.
- `correlativo`: integer NOT NULL — Column correlativo of table cventa.factura_serie.
- `descripcion`: text NULL — Column descripcion of table cventa.factura_serie.
- `empresa`: integer NOT NULL — Column empresa of table cventa.factura_serie.
- `factura_serie`: integer NOT NULL — Column factura_serie of table cventa.factura_serie.
- `fin`: integer NOT NULL — Column fin of table cventa.factura_serie.
- `inicio`: integer NOT NULL — Column inicio of table cventa.factura_serie.
- `nombre`: text NOT NULL — Column nombre of table cventa.factura_serie.
- primary key: factura_serie

### cventa_factura_venta  (source backend: postgres)
Source table cventa.factura_venta.

- `anulado`: integer NOT NULL — Column anulado of table cventa.factura_venta.
- `factura`: integer NOT NULL — Column factura of table cventa.factura_venta.
- `factura_venta`: integer NOT NULL — Column factura_venta of table cventa.factura_venta.
- `monto`: decimal NOT NULL — Column monto of table cventa.factura_venta.
- `venta`: integer NOT NULL — Column venta of table cventa.factura_venta.
- primary key: factura_venta

### cventa_menu  (source backend: files)
Source table cventa.menu.

- `activo`: integer NOT NULL — Column activo of table cventa.menu.
- `icono`: text NOT NULL — Column icono of table cventa.menu.
- `menu`: integer NOT NULL — Column menu of table cventa.menu.
- `modulo`: integer NOT NULL — Column modulo of table cventa.menu.
- `nombre`: text NOT NULL — Column nombre of table cventa.menu.
- `orden`: integer NOT NULL — Column orden of table cventa.menu.
- `submenu`: integer NOT NULL — Column submenu of table cventa.menu.
- `url`: text NOT NULL — Column url of table cventa.menu.
- primary key: menu

### cventa_modulo  (source backend: s3)
Source table cventa.modulo.

- `activo`: integer NOT NULL — Column activo of table cventa.modulo.
- `fecha_sis`: timestamp NOT NULL — Column fecha_sis of table cventa.modulo.
- `icono`: text NOT NULL — Column icono of table cventa.modulo.
- `modulo`: integer NOT NULL — Column modulo of table cventa.modulo.
- `nombre`: text NOT NULL — Column nombre of table cventa.modulo.
- `orden`: integer NOT NULL — Column orden of table cventa.modulo.
- primary key: modulo

### cventa_moneda  (source backend: postgres)
Source table cventa.moneda.

- `codigo`: text NOT NULL — Column codigo of table cventa.moneda.
- `moneda`: integer NOT NULL — Column moneda of table cventa.moneda.
- `nombre`: text NOT NULL — Column nombre of table cventa.moneda.
- primary key: moneda

### cventa_pais_empresa  (source backend: files)
Source table cventa.pais_empresa.

- `activo`: integer NOT NULL — Column activo of table cventa.pais_empresa.
- `codigo`: text NOT NULL — Column codigo of table cventa.pais_empresa.
- `codigo_postal`: text NULL — Column codigo_postal of table cventa.pais_empresa.
- `iva`: decimal NOT NULL — Column iva of table cventa.pais_empresa.
- `nombre`: text NOT NULL — Column nombre of table cventa.pais_empresa.
- `pais_empresa`: integer NOT NULL — Column pais_empresa of table cventa.pais_empresa.
- primary key: pais_empresa

### cventa_producto  (source backend: files)
Source table cventa.producto.

- `activo`: integer NOT NULL — Column activo of table cventa.producto.
- `cantidad`: decimal NOT NULL — Column cantidad of table cventa.producto.
- `codigo`: text NOT NULL — Column codigo of table cventa.producto.
- `fecha_ingreso`: date NOT NULL — Column fecha_ingreso of table cventa.producto.
- `fecha_sis`: timestamp NOT NULL — Column fecha_sis of table cventa.producto.
- `fecha_vencimiento`: date NULL — Column fecha_vencimiento of table cventa.producto.
- `incluye_iva`: integer NOT NULL — Column incluye_iva of table cventa.producto.
- `nombre`: text NOT NULL — Column nombre of table cventa.producto.
- `precio_compra`: decimal NOT NULL — Column precio_compra of table cventa.producto.
- `precio_venta`: decimal NOT NULL — Column precio_venta of table cventa.producto.
- `producto`: integer NOT NULL — Column producto of table cventa.producto.
- `producto_tipo`: integer NOT NULL — Column producto_tipo of table cventa.producto.
- `proveedor`: integer NOT NULL — Column proveedor of table cventa.producto.
- `unidad_medida`: integer NOT NULL — Column unidad_medida of table cventa.producto.
- `usuario`: integer NOT NULL — Column usuario of table cventa.producto.
- `valor_iva`: decimal NOT NULL — Column valor_iva of table cventa.producto.
- primary key: producto

### cventa_producto_tipo  (source backend: s3)
Source table cventa.producto_tipo.

- `nombre`: text NOT NULL — Column nombre of table cventa.producto_tipo.
- `producto_tipo`: integer NOT NULL — Column producto_tipo of table cventa.producto_tipo.
- primary key: producto_tipo

### cventa_proveedor  (source backend: mongodb)
Source table cventa.proveedor.

- `activo`: integer NOT NULL — Column activo of table cventa.proveedor.
- `contacto`: text NULL — Column contacto of table cventa.proveedor.
- `credito_contado`: integer NOT NULL — Column credito_contado of table cventa.proveedor.
- `dias_credito`: integer NOT NULL — Column dias_credito of table cventa.proveedor.
- `direccion`: text NOT NULL — Column direccion of table cventa.proveedor.
- `empresa`: integer NOT NULL — Column empresa of table cventa.proveedor.
- `fecha_sis`: timestamp NOT NULL — Column fecha_sis of table cventa.proveedor.
- `nit`: text NOT NULL — Column nit of table cventa.proveedor.
- `nombre`: text NOT NULL — Column nombre of table cventa.proveedor.
- `proveedor`: integer NOT NULL — Column proveedor of table cventa.proveedor.
- `proveedor_clasificacion`: integer NOT NULL — Column proveedor_clasificacion of table cventa.proveedor.
- `proveedor_tipo`: integer NOT NULL — Column proveedor_tipo of table cventa.proveedor.
- `razon_social`: text NOT NULL — Column razon_social of table cventa.proveedor.
- `telefono`: integer NOT NULL — Column telefono of table cventa.proveedor.
- `usuario`: integer NOT NULL — Column usuario of table cventa.proveedor.
- primary key: proveedor

### cventa_proveedor_clasificacion  (source backend: rest)
Source table cventa.proveedor_clasificacion.

- `nombre`: text NOT NULL — Column nombre of table cventa.proveedor_clasificacion.
- `proveedor_clasificacion`: integer NOT NULL — Column proveedor_clasificacion of table cventa.proveedor_clasificacion.
- primary key: proveedor_clasificacion

### cventa_proveedor_tipo  (source backend: mongodb)
Source table cventa.proveedor_tipo.

- `nombre`: text NULL — Column nombre of table cventa.proveedor_tipo.
- `proveedor_tipo`: integer NOT NULL — Column proveedor_tipo of table cventa.proveedor_tipo.
- primary key: proveedor_tipo

### cventa_rol  (source backend: rest)
Source table cventa.rol.

- `nombre`: text NULL — Column nombre of table cventa.rol.
- `rol`: integer NOT NULL — Column rol of table cventa.rol.
- primary key: rol

### cventa_submenu  (source backend: files)
Source table cventa.submenu.

- `activo`: integer NOT NULL — Column activo of table cventa.submenu.
- `fecha_sis`: timestamp NOT NULL — Column fecha_sis of table cventa.submenu.
- `nombre`: text NOT NULL — Column nombre of table cventa.submenu.
- `submenu`: integer NOT NULL — Column submenu of table cventa.submenu.
- primary key: submenu

### cventa_tipo_pago  (source backend: rest)
Source table cventa.tipo_pago.

- `nombre`: text NOT NULL — Column nombre of table cventa.tipo_pago.
- `tipo_pago`: integer NOT NULL — Column tipo_pago of table cventa.tipo_pago.
- primary key: tipo_pago

### cventa_unidad_medida  (source backend: rest)
Source table cventa.unidad_medida.

- `codigo`: text NOT NULL — Column codigo of table cventa.unidad_medida.
- `nombre`: text NOT NULL — Column nombre of table cventa.unidad_medida.
- `unidad_medida`: integer NOT NULL — Column unidad_medida of table cventa.unidad_medida.
- primary key: unidad_medida

### cventa_usuario  (source backend: postgres)
Source table cventa.usuario.

- `activo`: integer NOT NULL — Column activo of table cventa.usuario.
- `alias`: text NOT NULL — Column alias of table cventa.usuario.
- `correo`: text NOT NULL — Column correo of table cventa.usuario.
- `direccion`: text NOT NULL — Column direccion of table cventa.usuario.
- `empresa`: integer NOT NULL — Column empresa of table cventa.usuario.
- `fecha_modificacion`: timestamp NULL — Column fecha_modificacion of table cventa.usuario.
- `fecha_sis`: timestamp NOT NULL — Column fecha_sis of table cventa.usuario.
- `firma`: text NULL — Column firma of table cventa.usuario.
- `foto`: text NULL — Column foto of table cventa.usuario.
- `identificacion`: text NOT NULL — Número de DPI
- `jefe`: integer NOT NULL — Column jefe of table cventa.usuario.
- `nombre`: text NOT NULL — Column nombre of table cventa.usuario.
- `password`: text NOT NULL — Column password of table cventa.usuario.
- `rol`: integer NOT NULL — Column rol of table cventa.usuario.
- `root`: integer NOT NULL — Column root of table cventa.usuario.
- `subjefe`: integer NOT NULL — Column subjefe of table cventa.usuario.
- `telefono`: integer NOT NULL — Column telefono of table cventa.usuario.
- `usuario`: integer NOT NULL — Column usuario of table cventa.usuario.
- `usuario_genero`: integer NOT NULL — Column usuario_genero of table cventa.usuario.
- primary key: usuario

### cventa_usuario_empresa  (source backend: postgres)
Source table cventa.usuario_empresa.

- `activo`: integer NOT NULL — Column activo of table cventa.usuario_empresa.
- `empresa`: integer NOT NULL — Column empresa of table cventa.usuario_empresa.
- `fecha_sis`: timestamp NOT NULL — Column fecha_sis of table cventa.usuario_empresa.
- `usuario`: integer NOT NULL — Column usuario of table cventa.usuario_empresa.
- `usuario_empresa`: integer NOT NULL — Column usuario_empresa of table cventa.usuario_empresa.
- primary key: usuario_empresa

### cventa_usuario_genero  (source backend: mongodb)
Source table cventa.usuario_genero.

- `codigo`: text NULL — Column codigo of table cventa.usuario_genero.
- `nombre`: text NOT NULL — Column nombre of table cventa.usuario_genero.
- `usuario_genero`: integer NOT NULL — Column usuario_genero of table cventa.usuario_genero.
- primary key: usuario_genero

### cventa_usuario_menu  (source backend: rest)
Source table cventa.usuario_menu.

- `activo`: integer NOT NULL — Column activo of table cventa.usuario_menu.
- `fecha_sis`: timestamp NOT NULL — Column fecha_sis of table cventa.usuario_menu.
- `menu`: integer NOT NULL — Column menu of table cventa.usuario_menu.
- `usuario`: integer NOT NULL — Column usuario of table cventa.usuario_menu.
- `usuario_menu`: integer NOT NULL — Column usuario_menu of table cventa.usuario_menu.
- primary key: usuario_menu

### cventa_vendedor  (source backend: rest)
Source table cventa.vendedor.

- `activo`: integer NOT NULL — Column activo of table cventa.vendedor.
- `fecha_sis`: timestamp NOT NULL — Column fecha_sis of table cventa.vendedor.
- `usuario`: integer NOT NULL — Column usuario of table cventa.vendedor.
- `vendedor`: integer NOT NULL — Column vendedor of table cventa.vendedor.
- primary key: vendedor

### cventa_vendedor_cliente  (source backend: files)
Source table cventa.vendedor_cliente.

- `activo`: integer NOT NULL — Column activo of table cventa.vendedor_cliente.
- `cliente`: integer NOT NULL — Column cliente of table cventa.vendedor_cliente.
- `fecha_sis`: timestamp NOT NULL — Column fecha_sis of table cventa.vendedor_cliente.
- `vendedor`: integer NOT NULL — Column vendedor of table cventa.vendedor_cliente.
- `vendedor_cliente`: integer NOT NULL — Column vendedor_cliente of table cventa.vendedor_cliente.
- primary key: vendedor_cliente

### cventa_venta  (source backend: mongodb)
Source table cventa.venta.

- `cliente`: integer NOT NULL — Column cliente of table cventa.venta.
- `concepto`: text NOT NULL — Column concepto of table cventa.venta.
- `fecha_sis`: timestamp NOT NULL — Column fecha_sis of table cventa.venta.
- `iva`: decimal NOT NULL — Column iva of table cventa.venta.
- `moneda`: integer NOT NULL — Column moneda of table cventa.venta.
- `monto`: decimal NOT NULL — Column monto of table cventa.venta.
- `saldo`: decimal NOT NULL — Column saldo of table cventa.venta.
- `tipo_pago`: integer NOT NULL — Column tipo_pago of table cventa.venta.
- `usuario`: integer NOT NULL — Column usuario of table cventa.venta.
- `valor_base`: decimal NOT NULL — Column valor_base of table cventa.venta.
- `venta`: integer NOT NULL — Column venta of table cventa.venta.
- `venta_estatus`: integer NOT NULL — Column venta_estatus of table cventa.venta.
- primary key: venta

### cventa_venta_detalle  (source backend: postgres)
Source table cventa.venta_detalle.

- `anulado`: integer NOT NULL — Column anulado of table cventa.venta_detalle.
- `cantidad`: integer NOT NULL — Column cantidad of table cventa.venta_detalle.
- `precio`: decimal NOT NULL — Column precio of table cventa.venta_detalle.
- `producto`: integer NOT NULL — Column producto of table cventa.venta_detalle.
- `total`: decimal NOT NULL — Column total of table cventa.venta_detalle.
- `venta`: integer NOT NULL — Column venta of table cventa.venta_detalle.
- `venta_detalle`: integer NOT NULL — Column venta_detalle of table cventa.venta_detalle.
- primary key: venta_detalle

### cventa_venta_estatus  (source backend: files)
Source table cventa.venta_estatus.

- `nombre`: text NOT NULL — Column nombre of table cventa.venta_estatus.
- `venta_estatus`: integer NOT NULL — Column venta_estatus of table cventa.venta_estatus.
- primary key: venta_estatus

### Relationships

- cventa_bitacora_password(usuario) -> cventa_usuario(usuario) [required]
- cventa_bodega(bodega_tipo) -> cventa_bodega_tipo(bodega_tipo) [required]
- cventa_bodega(empresa) -> cventa_empresa(empresa) [required]
- cventa_bodega_movimiento(bodega) -> cventa_bodega(bodega) [required]
- cventa_bodega_movimiento(bodega_movimiento_tipo) -> cventa_bodega_movimiento_tipo(bodega_movimiento_tipo) [required]
- cventa_bodega_movimiento(producto) -> cventa_producto(producto) [required]
- cventa_bodega_movimiento(usuario) -> cventa_usuario(usuario) [required]
- cventa_bodega_producto(bodega) -> cventa_bodega(bodega) [required]
- cventa_bodega_producto(producto) -> cventa_producto(producto) [required]
- cventa_bodega_producto(usuario) -> cventa_usuario(usuario) [required]
- cventa_cliente(cliente_tipo) -> cventa_cliente_tipo(cliente_tipo) [required]
- cventa_cliente(empresa) -> cventa_empresa(empresa) [required]
- cventa_cliente(usuario) -> cventa_usuario(usuario) [required]
- cventa_compra(compra_estatus) -> cventa_compra_estatus(compra_estatus) [required]
- cventa_compra(moneda) -> cventa_moneda(moneda) [required]
- cventa_compra(proveedor) -> cventa_proveedor(proveedor) [required]
- cventa_compra(tipo_pago) -> cventa_tipo_pago(tipo_pago) [required]
- cventa_compra(usuario) -> cventa_usuario(usuario) [required]
- cventa_compra_detalle(compra) -> cventa_compra(compra) [required]
- cventa_compra_detalle(producto) -> cventa_producto(producto) [required]
- cventa_empresa(moneda) -> cventa_moneda(moneda) [required]
- cventa_empresa(pais_empresa) -> cventa_pais_empresa(pais_empresa) [required]
- cventa_factura(factura_estatus) -> cventa_factura_estatus(factura_estatus) [required]
- cventa_factura(factura_serie) -> cventa_factura_serie(factura_serie) [required]
- cventa_factura(usuario) -> cventa_usuario(usuario) [required]
- cventa_factura_serie(empresa) -> cventa_empresa(empresa) [required]
- cventa_factura_venta(factura) -> cventa_factura(factura) [required]
- cventa_factura_venta(venta) -> cventa_venta(venta) [required]
- cventa_menu(modulo) -> cventa_modulo(modulo) [required]
- cventa_menu(submenu) -> cventa_submenu(submenu) [required]
- cventa_producto(producto_tipo) -> cventa_producto_tipo(producto_tipo) [required]
- cventa_producto(proveedor) -> cventa_proveedor(proveedor) [required]
- cventa_producto(unidad_medida) -> cventa_unidad_medida(unidad_medida) [required]
- cventa_producto(usuario) -> cventa_usuario(usuario) [required]
- cventa_proveedor(empresa) -> cventa_empresa(empresa) [required]
- cventa_proveedor(proveedor_clasificacion) -> cventa_proveedor_clasificacion(proveedor_clasificacion) [required]
- cventa_proveedor(proveedor_tipo) -> cventa_proveedor_tipo(proveedor_tipo) [required]
- cventa_proveedor(usuario) -> cventa_usuario(usuario) [required]
- cventa_usuario(empresa) -> cventa_empresa(empresa) [required]
- cventa_usuario(rol) -> cventa_rol(rol) [required]
- cventa_usuario(usuario_genero) -> cventa_usuario_genero(usuario_genero) [required]
- cventa_usuario_empresa(empresa) -> cventa_empresa(empresa) [required]
- cventa_usuario_empresa(usuario) -> cventa_usuario(usuario) [required]
- cventa_usuario_menu(menu) -> cventa_menu(menu) [required]
- cventa_usuario_menu(usuario) -> cventa_usuario(usuario) [required]
- cventa_vendedor(usuario) -> cventa_usuario(usuario) [required]
- cventa_vendedor_cliente(cliente) -> cventa_cliente(cliente) [required]
- cventa_vendedor_cliente(vendedor) -> cventa_vendedor(vendedor) [required]
- cventa_venta(cliente) -> cventa_cliente(cliente) [required]
- cventa_venta(moneda) -> cventa_moneda(moneda) [required]
- cventa_venta(tipo_pago) -> cventa_tipo_pago(tipo_pago) [required]
- cventa_venta(usuario) -> cventa_usuario(usuario) [required]
- cventa_venta(venta_estatus) -> cventa_venta_estatus(venta_estatus) [required]
- cventa_venta_detalle(producto) -> cventa_producto(producto) [required]
- cventa_venta_detalle(venta) -> cventa_venta(venta) [required]

