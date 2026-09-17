WITH completed_orders AS (
    SELECT DISTINCT order_id, customer_id
    FROM orders
    WHERE status = 'completed'
),
order_totals AS (
    SELECT order_id, SUM(quantity * unit_price) AS order_total
    FROM order_items
    GROUP BY order_id
)
SELECT
    c.customer_id AS customer_id,
    COUNT(DISTINCT co.order_id) AS completed_orders,
    COALESCE(SUM(ot.order_total), 0) AS total_spend
FROM customers AS c
LEFT JOIN completed_orders AS co ON co.customer_id = c.customer_id
LEFT JOIN order_totals AS ot ON ot.order_id = co.order_id
GROUP BY c.customer_id
ORDER BY c.customer_id
