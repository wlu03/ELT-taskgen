SELECT
    e.event_id AS event_id,
    e.big_count AS big_count,
    e.label AS label,
    e.occurred_at AS occurred_at,
    e.tz_stamp AS tz_stamp,
    m.value AS metric_value,
    m.big_note AS big_note
FROM events AS e
LEFT JOIN metrics AS m ON m.event_id = e.event_id
ORDER BY e.event_id
