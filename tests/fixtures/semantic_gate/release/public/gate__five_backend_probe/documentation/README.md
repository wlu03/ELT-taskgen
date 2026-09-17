# Five-backend semantic gate probe

## Transformation specification

Build every mart below using the ordered semantic rules. The rules
define required source inputs, matching behavior, filters, aggregates,
null handling, and deterministic tie behavior; they do not prescribe
a particular SQL implementation.

### `customer_rollup`

- Grain: One row per customer, including customers with no orders.
- Unique key: customer_id
- Required columns: customer_id, completed_orders, total_spend

```text
Mart 'customer_rollup' has 1 declared semantic rules:
1. [source] Bring customers into scope. (public source tables: customers)
```

### `event_wide`

- Grain: One row per event.
- Unique key: event_id
- Required columns: event_id, big_count, label, occurred_at, tz_stamp, metric_value, big_note

```text
Mart 'event_wide' has 1 declared semantic rules:
1. [source] Bring events into scope. (public source tables: events)
```

## Source tables

### customers  (source backend: postgres)
One row per customer.

- `customer_id`: integer NOT NULL — Unique customer identifier.
- `customer_name`: text NOT NULL — Display name.
- `segment`: text NULL — Free-text segment; may be NULL.
- primary key: customer_id

### orders  (source backend: mongodb)
One row per order header.

- `order_id`: integer NOT NULL — Unique order identifier.
- `customer_id`: integer NULL — Customer who placed the order; may be NULL.
- `status`: text NOT NULL — Order status. one of: cancelled, completed.
- `note`: text NULL — Free-text note; may be NULL or absent.
- primary key: order_id

### order_items  (source backend: files)
One row per line item; exact duplicates possible.

- `order_id`: integer NOT NULL — Order this line item belongs to.
- `quantity`: integer NOT NULL — Units purchased.
- `unit_price`: decimal NOT NULL — Price per unit.
- `discount`: decimal NULL — Absolute discount; may be NULL.

### events  (source backend: rest)
One row per event.

- `event_id`: integer NOT NULL — Unique event identifier.
- `big_count`: bigint NOT NULL — 64-bit counter; exceeds 2**53.
- `label`: text NOT NULL — Event label; may be empty text.
- `occurred_at`: timestamp NULL — Naive event timestamp; may be NULL.
- `tz_stamp`: text NULL — ISO-8601 text with a UTC offset.
- primary key: event_id

### metrics  (source backend: s3)
At most one measurement per event.

- `metric_id`: integer NOT NULL — Unique metric identifier.
- `event_id`: integer NOT NULL — Event the measurement belongs to.
- `value`: decimal NULL — Measured value; may be NULL.
- `big_note`: text NOT NULL — Free-form note; can be very large.
- primary key: metric_id

### Relationships

- orders(customer_id) -> customers(customer_id) [optional (may be NULL/dangling)]
- order_items(order_id) -> orders(order_id) [required]
- metrics(event_id) -> events(event_id) [required]

