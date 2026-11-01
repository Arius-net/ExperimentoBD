SELECT
    data
FROM orders
WHERE data->>'timestamp' >= '2025-11-01T00:00:00.000Z'
  AND data->>'timestamp' <  '2025-12-01T00:00:00.000Z'
  AND data->'payment'->>'status' = 'completed'
ORDER BY (data->'payment'->'totals'->>'final_total')::numeric DESC, id ASC
LIMIT 10 OFFSET 34653;

