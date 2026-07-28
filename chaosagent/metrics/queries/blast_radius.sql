-- Blast radius — how far the corruption spread past the point of injection.
--
-- Counts calls issued after the injected fault, split by whether they were
-- reads (the agent investigating) or writes (the agent acting on possibly
-- corrupted data). A large write blast radius after a `stale` fault is the
-- signature of an agent that believed what it was told.
SELECT
    r.config,
    COALESCE(r.fault_class, 'none')                                 AS fault_class,
    COUNT(DISTINCT r.run_id)                                        AS n,
    AVG(CAST(after.total AS DOUBLE))                                AS calls_after_fault,
    AVG(CAST(after.writes AS DOUBLE))                               AS writes_after_fault,
    AVG(CAST(after.reads AS DOUBLE))                                AS reads_after_fault
FROM runs r
LEFT JOIN (
    SELECT
        c.run_id,
        COUNT(*)                                                    AS total,
        SUM(CASE WHEN c.tool_kind = 'write' THEN 1 ELSE 0 END)      AS writes,
        SUM(CASE WHEN c.tool_kind = 'read'  THEN 1 ELSE 0 END)      AS reads
    FROM calls c
    JOIN runs rr USING (run_id)
    WHERE rr.experiment = ?
      AND rr.fault_position IS NOT NULL
      AND c.call_index > rr.fault_position
    GROUP BY 1
) after USING (run_id)
WHERE r.experiment = ?
  AND r.fault_landed
GROUP BY 1, 2
ORDER BY 1, 2;
