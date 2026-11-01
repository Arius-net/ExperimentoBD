SELECT
    item->>'category' AS category,
    SUM((item->>'qty')::bigint) AS total_qty,
    ROUND(SUM(((item->>'qty')::numeric) * ((item->>'price')::numeric)), 2) AS total_revenue
FROM orders
CROSS JOIN LATERAL jsonb_array_elements(data->'items') AS item
WHERE data->'payment'->>'status' = 'completed'
GROUP BY item->>'category'
ORDER BY category ASC;

